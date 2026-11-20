// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//	http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package server

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	lropb "cloud.google.com/go/longrunning/autogen/longrunningpb"
	"github.com/google/go-cmp/cmp"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/testing/protocmp"
	msgpb "xmanager_cloud/api/protos/experiment"
	tpb "xmanager_cloud/api/protos/task_queue"
	"xmanager_cloud/cmd/experiment_state_server/config"
	"xmanager_cloud/internal/experiment/experimenttest"
	"xmanager_cloud/internal/grpcerr"
	"xmanager_cloud/internal/prototest"
	"xmanager_cloud/internal/storage"
	tq "xmanager_cloud/internal/task_queue"
	"xmanager_cloud/internal/validator"
	"xmanager_cloud/internal/workunit"
)

const batchParent = "experiments/123"

// batchStorage counts transactions, records one operation per AddWorkUnitOperation call, and keeps
// the inserted work units so that status updates of created work units land. Like the real
// backends, its transactions and operation updates fail once their context is done.
type batchStorage struct {
	*storage.MockStorage
	txCalls    int
	operations []*msgpb.OperationMetadata
	// operationErrs maps a work unit name to the error of its AddWorkUnitOperation call.
	operationErrs map[string]error
	// workUnits holds the inserted work units by ID.
	workUnits map[int64]*msgpb.WorkUnit
}

func (s *batchStorage) RunInTransaction(ctx context.Context, fn func(ctx context.Context, tx *storage.Transaction) error) error {
	s.txCalls++
	if err := ctx.Err(); err != nil {
		return err
	}
	return s.MockStorage.RunInTransaction(ctx, func(ctx context.Context, tx *storage.Transaction) error {
		tx.WorkUnits = s
		return fn(ctx, tx)
	})
}

func (s *batchStorage) InsertWorkUnit(ctx context.Context, wu *msgpb.WorkUnit) error {
	if err := s.MockStorage.InsertWorkUnit(ctx, wu); err != nil {
		return err
	}
	_, wid, err := validator.ValidateWorkUnitName(wu.GetName())
	if err != nil {
		return nil // Work units with invalid names cannot be updated later.
	}
	if s.workUnits == nil {
		s.workUnits = make(map[int64]*msgpb.WorkUnit)
	}
	s.workUnits[wid] = proto.Clone(wu).(*msgpb.WorkUnit)
	return nil
}

func (s *batchStorage) FetchUpdatingWorkUnits(_ context.Context, _ int64, wids []int64) (map[int64]*msgpb.WorkUnit, error) {
	res := make(map[int64]*msgpb.WorkUnit, len(wids))
	for _, wid := range wids {
		if wu, ok := s.workUnits[wid]; ok {
			res[wid] = proto.Clone(wu).(*msgpb.WorkUnit)
		}
	}
	return res, nil
}

func (s *batchStorage) UpdateWorkUnitOperation(ctx context.Context, update *storage.OperationUpdate) (*lropb.Operation, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return s.MockStorage.UpdateWorkUnitOperation(ctx, update)
}

func (s *batchStorage) AddWorkUnitOperation(ctx context.Context, xid, wid int64, metadata *msgpb.OperationMetadata) (*lropb.Operation, error) {
	s.operations = append(s.operations, metadata)
	if err := s.operationErrs[metadata.GetTarget()]; err != nil {
		return nil, err
	}
	return &lropb.Operation{Name: validator.ConstructOperationName(xid, wid, fmt.Sprintf("%08d-0000-0000-0000-000000000000", wid))}, nil
}

// newBatchStorage returns storage holding the STUDY experiment 123, whose next work unit ID is 4,
// and an hpo component whose studyControllerServiceAccount is serviceAccount.
func newBatchStorage(serviceAccount string) *batchStorage {
	return &batchStorage{MockStorage: &storage.MockStorage{
		Experiment: &msgpb.Experiment{
			Name: batchParent, ExperimentType: msgpb.Experiment_STUDY, Priority: msgpb.Priority_HIGH,
			Status:     &msgpb.ExperimentStatus{},
			TypeConfig: &msgpb.Experiment_StudyConfig{StudyConfig: experimenttest.StudyConfig()},
		},
		NextWorkUnitID:            4,
		ComponentRepository:       &storage.MockComponentRepository{Records: experimenttest.HPOCatalog(map[string]string{"studyControllerServiceAccount": serviceAccount})},
		StatusHistoryTxRepository: &storage.MockStatusHistoryTxRepository{},
	}}
}

// batchRequest returns a request for n valid trials, mutated by fn.
func batchRequest(n int, fn func(req *msgpb.BatchCreateWorkUnitsRequest)) *msgpb.BatchCreateWorkUnitsRequest {
	req := &msgpb.BatchCreateWorkUnitsRequest{Parent: batchParent}
	for i := range n {
		req.Requests = append(req.Requests, &msgpb.CreateWorkUnitRequest{WorkUnit: &msgpb.WorkUnit{
			WorkUnitType: msgpb.WorkUnit_STANDARD,
			Executable:   testExecutable,
			Notes:        fmt.Sprintf("trial-%d", i+1),
		}})
	}
	return prototest.Modify(req, fn)
}

func newBatchServer(t *testing.T, st storage.Storage, actuator *tq.FakeClient, opts ...testServerOpt) *Server {
	t.Helper()
	return newTestServer(t, append([]testServerOpt{
		withConfig(&config.Config{DefaultExecutionEnvironment: "gke"}),
		withStorage(st),
		withWorkUnitActuator(actuator),
	}, opts...)...)
}

func TestBatchCreateWorkUnits_CreatesTrialsInOneTransaction(t *testing.T) {
	// The configured principal differs in case from the authenticated test@example.com.
	st := newBatchStorage("TEST@example.com")
	actuator := &tq.FakeClient{}
	server := newBatchServer(t, st, actuator)

	resp, err := server.BatchCreateWorkUnits(context.Background(), batchRequest(3, nil))
	if err != nil {
		t.Fatalf("BatchCreateWorkUnits() failed: %v", err)
	}

	var gotNames, gotTrials []string
	for _, wu := range resp.GetWorkUnits() {
		gotNames = append(gotNames, wu.GetName())
		gotTrials = append(gotTrials, wu.GetNotes())
	}
	wantNames := []string{"experiments/123/workUnits/4", "experiments/123/workUnits/5", "experiments/123/workUnits/6"}
	if diff := cmp.Diff(wantNames, gotNames); diff != "" {
		t.Errorf("created work unit names mismatch (-want +got):\n%s", diff)
	}
	if diff := cmp.Diff([]string{"trial-1", "trial-2", "trial-3"}, gotTrials); diff != "" {
		t.Errorf("created work unit notes mismatch (-want +got):\n%s", diff)
	}
	if st.txCalls != 1 || st.UpdatedExperiment == nil || st.InsertedWorkUnit.GetName() != wantNames[2] {
		t.Errorf("transactions = %d, experiment updated = %v, last inserted = %q; want 1, true, %q", st.txCalls, st.UpdatedExperiment != nil, st.InsertedWorkUnit.GetName(), wantNames[2])
	}
	var gotOps []string
	for _, op := range st.operations {
		gotOps = append(gotOps, fmt.Sprintf("%s %v %s", op.GetTarget(), op.GetActionType(), op.GetOriginalRequester()))
	}
	wantOps := []string{
		"experiments/123/workUnits/4 CREATE test@example.com",
		"experiments/123/workUnits/5 CREATE test@example.com",
		"experiments/123/workUnits/6 CREATE test@example.com",
	}
	if diff := cmp.Diff(wantOps, gotOps); diff != "" {
		t.Errorf("operations mismatch (-want +got):\n%s", diff)
	}
	if len(actuator.EnqueuedTasks) != 3 {
		t.Fatalf("enqueued tasks = %d, want 3", len(actuator.EnqueuedTasks))
	}
	for i, task := range actuator.EnqueuedTasks {
		wid := int64(4 + i)
		wantTarget := validator.ConstructWorkUnitExecutionName(123, wid, 1)
		if task.Target != wantTarget || !strings.Contains(task.OperationName, fmt.Sprintf("workUnits/%d/operations/", wid)) ||
			task.Action.GetCreate().GetExperimentPriority() != msgpb.Priority_HIGH || task.Action.GetCreate().GetAuthor() != "test@example.com" {
			t.Errorf("task %d = %+v, want target %s with its operation and a HIGH priority create action", i, task, wantTarget)
		}
	}
}

func TestBatchCreateWorkUnits_Rejections(t *testing.T) {
	tests := []struct {
		name       string
		storage    func() *batchStorage
		req        *msgpb.BatchCreateWorkUnitsRequest
		opts       []testServerOpt
		wantCode   codes.Code
		wantErrSub string
	}{
		{name: "unauthenticated", opts: []testServerOpt{withAuthorizer(&mockAuthenticator{err: grpcerr.ErrUnauthenticated})}, wantCode: codes.Unauthenticated},
		{name: "invalid parent", req: batchRequest(1, func(r *msgpb.BatchCreateWorkUnitsRequest) { r.Parent = "experiments/x" }), wantCode: codes.InvalidArgument},
		{name: "no requests", req: batchRequest(0, nil), wantCode: codes.InvalidArgument},
		{name: "experiment not found", storage: func() *batchStorage {
			st := newBatchStorage("test@example.com")
			st.FetchExperimentErr = status.Error(codes.NotFound, "missing")
			return st
		}, wantCode: codes.NotFound},
		{name: "experiment absent", storage: func() *batchStorage {
			st := newBatchStorage("test@example.com")
			st.Experiment = nil
			return st
		}, wantCode: codes.NotFound},
		{name: "experiment read failure", storage: func() *batchStorage {
			st := newBatchStorage("test@example.com")
			st.FetchExperimentErr = errors.New("spanner down")
			return st
		}, wantCode: codes.Internal},
		{name: "standard experiment", storage: func() *batchStorage {
			st := newBatchStorage("test@example.com")
			st.Experiment.ExperimentType = msgpb.Experiment_STANDARD
			return st
		}, wantCode: codes.Unimplemented},
		{name: "caller is not the study controller", storage: func() *batchStorage { return newBatchStorage("controller@p.iam.gserviceaccount.com") }, wantCode: codes.PermissionDenied},
		{name: "other caller with an invalid payload", storage: func() *batchStorage { return newBatchStorage("controller@p.iam.gserviceaccount.com") }, req: batchRequest(0, nil), wantCode: codes.PermissionDenied},
		{name: "other caller with a missing experiment", storage: func() *batchStorage {
			st := newBatchStorage("controller@p.iam.gserviceaccount.com")
			st.Experiment = nil
			return st
		}, wantCode: codes.PermissionDenied},
		{name: "other caller with a standard experiment", storage: func() *batchStorage {
			st := newBatchStorage("controller@p.iam.gserviceaccount.com")
			st.Experiment.ExperimentType = msgpb.Experiment_STANDARD
			return st
		}, wantCode: codes.PermissionDenied},
		{name: "no study controller configured", storage: func() *batchStorage { return newBatchStorage("") }, wantCode: codes.PermissionDenied},
		{name: "hpo component absent", storage: func() *batchStorage {
			st := newBatchStorage("test@example.com")
			st.ComponentRepository = &storage.MockComponentRepository{}
			return st
		}, wantCode: codes.PermissionDenied},
		{name: "component catalog failure", storage: func() *batchStorage {
			st := newBatchStorage("test@example.com")
			st.ComponentRepository = &storage.MockComponentRepository{GetActiveComponentErr: errors.New("catalog down")}
			return st
		}, wantCode: codes.Internal},
		{name: "unknown execution environment", opts: []testServerOpt{withConfig(&config.Config{DefaultExecutionEnvironment: "unknown"})}, wantCode: codes.Internal, wantErrSub: "requests[0]"},
		{name: "storage failure", storage: func() *batchStorage {
			st := newBatchStorage("test@example.com")
			st.InsertWorkUnitErr = errors.New("insert failed")
			return st
		}, wantCode: codes.Internal},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			st := newBatchStorage("test@example.com")
			if tt.storage != nil {
				st = tt.storage()
			}
			actuator := &tq.FakeClient{}
			server := newBatchServer(t, st, actuator, tt.opts...)
			req := tt.req
			if req == nil {
				req = batchRequest(1, nil)
			}

			resp, err := server.BatchCreateWorkUnits(context.Background(), req)

			if status.Code(err) != tt.wantCode || resp != nil {
				t.Fatalf("BatchCreateWorkUnits() = (%v, %v), want code %v", resp, err, tt.wantCode)
			}
			if !strings.Contains(err.Error(), tt.wantErrSub) {
				t.Errorf("BatchCreateWorkUnits() = %v, want it to contain %q", err, tt.wantErrSub)
			}
			if st.InsertedWorkUnit != nil || len(actuator.EnqueuedTasks) != 0 {
				t.Errorf("rejected request inserted %v and enqueued %d tasks, want none", st.InsertedWorkUnit, len(actuator.EnqueuedTasks))
			}
		})
	}
}

func TestBatchCreateWorkUnits_ErrorsHideAuthorizationDetails(t *testing.T) {
	detailed := fmt.Errorf("component \"hpo\" has no valid studyControllerServiceAccount parameter: %w", workunit.ErrNotStudyController)
	if err := batchCreateError(detailed); err != workunit.ErrNotStudyController {
		t.Errorf("batchCreateError(%v) = %v, want exactly ErrNotStudyController", detailed, err)
	}
	unsupported := fmt.Errorf("experiment 123: %w", workunit.ErrBatchCreateRequiresStudy)
	if err := batchCreateError(unsupported); status.Code(err) != codes.Unimplemented {
		t.Errorf("batchCreateError(%v) code = %v, want Unimplemented", unsupported, status.Code(err))
	}
}

// unschedulableMessage is the FATAL system message of a created work unit that could not be
// scheduled, without its creation time.
var unschedulableMessage = &msgpb.StructuredMessage{
	UserFacingText: "Work unit was created but could not be scheduled for execution. Please restart the work unit.",
	Severity:       msgpb.StructuredMessage_FATAL,
	Source:         msgpb.StructuredMessage_XMANAGER_BACKEND,
}

// recordedSystemMessages returns the system messages of the work unit executions updated in st, by
// work unit ID.
func recordedSystemMessages(st *batchStorage) map[int64]*msgpb.StructuredMessage {
	got := make(map[int64]*msgpb.StructuredMessage, len(st.UpdatedWorkUnitExecutions))
	for wid, exec := range st.UpdatedWorkUnitExecutions {
		got[wid] = exec.GetStatus().GetSystemMessage()
	}
	return got
}

// checkCompensation checks that exactly the work units wantFATAL got the unschedulable message and
// that the last stopped operation, if wantStoppedWID is not 0, belongs to that work unit, is done
// and carries an Internal error.
func checkCompensation(t *testing.T, st *batchStorage, wantFATAL []int64, wantStopped int, wantStoppedWID int64) {
	t.Helper()
	want := make(map[int64]*msgpb.StructuredMessage, len(wantFATAL))
	for _, wid := range wantFATAL {
		want[wid] = proto.Clone(unschedulableMessage).(*msgpb.StructuredMessage)
		want[wid].Parent = validator.ConstructWorkUnitName(123, wid)
	}
	ignoreTime := protocmp.IgnoreFields(&msgpb.StructuredMessage{}, "create_time")
	if diff := cmp.Diff(want, recordedSystemMessages(st), protocmp.Transform(), ignoreTime); diff != "" {
		t.Errorf("recorded system messages mismatch (-want +got):\n%s", diff)
	}
	if st.UpdateWorkUnitOperationCalls != wantStopped {
		t.Errorf("stopped operations = %d, want %d", st.UpdateWorkUnitOperationCalls, wantStopped)
	}
	if wantStoppedWID == 0 {
		return
	}
	got := st.GotOperationUpdate
	if got == nil || got.WID != wantStoppedWID || !got.Operation.GetDone() || got.Operation.GetError().GetCode() != int32(codes.Internal) {
		t.Errorf("last operation update = %+v, want WID %d done with an Internal error", got, wantStoppedWID)
	}
}

func TestBatchCreateWorkUnits_PartialSchedulingFailure(t *testing.T) {
	tests := []struct {
		name           string
		mutate         func(st *batchStorage, actuator *tq.FakeClient)
		wantTasks      int
		wantFailed     string
		wantFATAL      []int64
		wantStopped    int
		wantStoppedWID int64
	}{
		{
			name: "enqueue fails for one work unit",
			mutate: func(_ *batchStorage, actuator *tq.FakeClient) {
				actuator.EnqueueFunc = func(_ context.Context, target string, _ *msgpb.WorkUnit, _ *tpb.Action, _ string) error {
					if target == validator.ConstructWorkUnitExecutionName(123, 5, 1) {
						return errors.New("redis down")
					}
					return nil
				}
			},
			wantTasks:  3,
			wantFailed: "1 of 3 work units were created but could not be scheduled, restart them: experiments/123/workUnits/5:",
			wantFATAL:  []int64{5}, wantStopped: 1, wantStoppedWID: 5,
		},
		{
			name: "operation fails for one work unit",
			mutate: func(st *batchStorage, _ *tq.FakeClient) {
				st.operationErrs = map[string]error{"experiments/123/workUnits/6": errors.New("operation write failed")}
			},
			wantTasks:  2,
			wantFailed: "1 of 3 work units were created but could not be scheduled, restart them: experiments/123/workUnits/6:",
			wantFATAL:  []int64{6}, wantStopped: 0,
		},
		{
			name: "created work unit name cannot be parsed",
			mutate: func(st *batchStorage, _ *tq.FakeClient) {
				st.NextWorkUnitID = -5
			},
			wantTasks:  0,
			wantFailed: "3 of 3 work units were created but could not be scheduled, restart them: experiments/123/workUnits/-5, experiments/123/workUnits/-4, experiments/123/workUnits/-3:",
			wantFATAL:  nil, wantStopped: 0,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			st := newBatchStorage("test@example.com")
			actuator := &tq.FakeClient{}
			tt.mutate(st, actuator)
			server := newBatchServer(t, st, actuator)

			_, err := server.BatchCreateWorkUnits(context.Background(), batchRequest(3, nil))

			if !errors.Is(err, grpcerr.ErrInternal) || !strings.Contains(err.Error(), tt.wantFailed) {
				t.Errorf("BatchCreateWorkUnits() = %v, want Internal containing %q", err, tt.wantFailed)
			}
			if len(actuator.EnqueuedTasks) != tt.wantTasks {
				t.Errorf("enqueued tasks = %d, want %d", len(actuator.EnqueuedTasks), tt.wantTasks)
			}
			checkCompensation(t, st, tt.wantFATAL, tt.wantStopped, tt.wantStoppedWID)
		})
	}
}

func TestFailCreatedWorkUnit_UnknownIDSkipsStatusUpdate(t *testing.T) {
	tests := []struct {
		name           string
		operation      *lropb.Operation
		wantStopped    int
		wantStoppedWID int64
	}{
		{name: "without operation", wantStopped: 0},
		{name: "with operation", operation: &lropb.Operation{Name: validator.ConstructOperationName(123, 4, "00000004-0000-0000-0000-000000000000")}, wantStopped: 1, wantStoppedWID: 4},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			st := newBatchStorage("test@example.com")
			server := newBatchServer(t, st, &tq.FakeClient{})
			// The execution name parses, so only the unknown ID (0) prevents a status update.
			cwu := createdWorkUnit{
				wu: &msgpb.WorkUnit{
					Name:                     "experiments/123/workUnits/invalid",
					CurrentWorkUnitExecution: &msgpb.WorkUnitExecution{Name: validator.ConstructWorkUnitExecutionName(123, 4, 1)},
				},
				operation: tt.operation,
			}

			got := server.WorkUnitHandler.failCreatedWorkUnit(context.Background(), 123, cwu, errors.New("name does not parse"))

			if got != "experiments/123/workUnits/invalid" {
				t.Errorf("failCreatedWorkUnit() = %q, want the work unit name", got)
			}
			if st.txCalls != 0 {
				t.Errorf("status update transactions = %d, want 0 for a work unit without an ID", st.txCalls)
			}
			checkCompensation(t, st, nil, tt.wantStopped, tt.wantStoppedWID)
		})
	}
}
