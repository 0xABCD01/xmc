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

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	msgpb "xmanager_cloud/api/protos/experiment"
	"xmanager_cloud/internal/grpcerr"
	"xmanager_cloud/internal/logging"
	client "xmanager_cloud/internal/task_queue"
	"xmanager_cloud/internal/validator"
	"xmanager_cloud/internal/workunit"
)

// BatchCreateWorkUnits creates the trials of a STUDY experiment for the study controller: it
// persists all of them in one transaction, records one creation operation per work unit, and
// enqueues their creation tasks.
func (h *WorkUnitHandler) BatchCreateWorkUnits(ctx context.Context, req *msgpb.BatchCreateWorkUnitsRequest) (*msgpb.BatchCreateWorkUnitsResponse, error) {
	ctx = logging.InjectOperation(ctx, "")
	logging.Info(ctx, "Batch create work units", "parent", req.GetParent(), "count", len(req.GetRequests()))

	// 1-3. Authenticate the caller, validate the parent, and require the study controller.
	xid, email, err := h.authorizeStudyController(ctx, req.GetParent())
	if err != nil {
		return nil, err
	}
	ctx = logging.WithExperimentName(ctx, req.GetParent())

	// 4. Only STUDY experiments are supported.
	exp, err := h.fetchBatchCreateParent(ctx, xid)
	if err != nil {
		return nil, err
	}

	// 5. Validate the shape of the request.
	if _, err := validator.ValidateBatchCreateWorkUnitsRequest(req); err != nil {
		logging.Warn(ctx, "BatchCreateWorkUnits validation failed", "error", err)
		return nil, fmt.Errorf("BatchCreateWorkUnits validation failed: %w", err)
	}

	// 6. Copy each work unit with its default values and initial execution.
	wus := newStudyTrials(xid, req.GetRequests())

	// 7. Persist all work units in one transaction.
	created, err := workunit.BatchCreateWorkUnits(ctx, h.storage, xid, email, wus)
	if err != nil {
		return nil, batchCreateError(err)
	}

	// 8. Record an operation per work unit and enqueue their creation tasks.
	if err := h.actuateCreatedWorkUnits(ctx, xid, exp.GetPriority(), created, email); err != nil {
		return nil, err
	}
	logging.Info(ctx, "Batch created work units", "count", len(created))
	return &msgpb.BatchCreateWorkUnitsResponse{WorkUnits: created}, nil
}

// authorizeStudyController authenticates the caller, validates the parent experiment name, and
// requires the caller to be the study controller. It runs before the experiment is read or the
// payload is validated, so that other callers learn nothing about either; BatchCreateWorkUnits
// repeats the authorization inside the creation transaction.
func (h *WorkUnitHandler) authorizeStudyController(ctx context.Context, parent string) (int64, string, error) {
	email, _, err := h.auth.AuthenticateUserFromContext(ctx)
	if err != nil {
		return 0, "", err
	}
	xid, err := validator.ValidateExperimentName(parent)
	if err != nil {
		logging.Warn(ctx, "Failed to validate experiment name", "error", err)
		return 0, "", fmt.Errorf("parent must be in the format experiments/{xid}: %v: %w", err, grpcerr.ErrInvalidArgument)
	}
	if err := workunit.AuthorizeStudyController(ctx, h.storage.Components(), email); err != nil {
		logging.Warn(ctx, "BatchCreateWorkUnits authorization failed", "user", email, "error", err)
		return 0, "", batchCreateError(err)
	}
	return xid, email, nil
}

// fetchBatchCreateParent reads experiment xid and requires it to be a STUDY.
func (h *WorkUnitHandler) fetchBatchCreateParent(ctx context.Context, xid int64) (*msgpb.Experiment, error) {
	exp, err := h.storage.FetchExperiment(ctx, xid)
	if status.Code(err) == codes.NotFound || (err == nil && exp == nil) {
		return nil, fmt.Errorf("experiment %d not found: %w", xid, grpcerr.ErrNotFound)
	}
	if err != nil {
		logging.Error(ctx, "Failed to fetch experiment", "error", err)
		return nil, fmt.Errorf("failed to fetch experiment: %v: %w", err, grpcerr.ErrInternal)
	}
	if exp.GetExperimentType() != msgpb.Experiment_STUDY {
		return nil, fmt.Errorf("experiment %d is %v: %w", xid, exp.GetExperimentType(), workunit.ErrBatchCreateRequiresStudy)
	}
	return exp, nil
}

// newStudyTrials returns copies of the requested work units with default values and an initial
// execution, for creation in experiment xid, which names them.
func newStudyTrials(xid int64, reqs []*msgpb.CreateWorkUnitRequest) []*msgpb.WorkUnit {
	wus := make([]*msgpb.WorkUnit, len(reqs))
	for i, req := range reqs {
		wu := validator.FillWorkUnitNullValues(proto.Clone(req.GetWorkUnit()).(*msgpb.WorkUnit))
		wu.CurrentWorkUnitExecution = newWorkUnitExecution(xid, 0)
		wus[i] = wu
	}
	return wus
}

// batchCreateError reports err to the client: an authorization failure without the configuration
// details it was logged with, an unsupported experiment type as is, and every other failure as
// Internal.
func batchCreateError(err error) error {
	if errors.Is(err, workunit.ErrNotStudyController) {
		return workunit.ErrNotStudyController
	}
	if errors.Is(err, workunit.ErrBatchCreateRequiresStudy) {
		return fmt.Errorf("BatchCreateWorkUnits rejected: %w", err)
	}
	return fmt.Errorf("failed to batch create work units: %v: %w", err, grpcerr.ErrInternal)
}

// actuateCreatedWorkUnits records a creation operation for each created work unit and enqueues its
// creation task, in order. It stops at the first failure and returns it as Internal; the work units
// are already stored.
func (h *WorkUnitHandler) actuateCreatedWorkUnits(ctx context.Context, xid int64, priority msgpb.Priority, created []*msgpb.WorkUnit, email string) error {
	for _, wu := range created {
		if err := h.actuateCreatedWorkUnit(ctx, xid, priority, wu, email); err != nil {
			logging.Error(ctx, "Failed to schedule created work unit", "name", wu.GetName(), "error", err)
			return fmt.Errorf("work unit %s was created but could not be scheduled: %v: %w", wu.GetName(), err, grpcerr.ErrInternal)
		}
	}
	return nil
}

// actuateCreatedWorkUnit records the creation operation of wu and enqueues its creation task.
func (h *WorkUnitHandler) actuateCreatedWorkUnit(ctx context.Context, xid int64, priority msgpb.Priority, wu *msgpb.WorkUnit, email string) error {
	_, wid, err := validator.ValidateWorkUnitName(wu.GetName())
	if err != nil {
		return fmt.Errorf("failed to validate work unit name: %w", err)
	}
	operation, err := h.storage.AddWorkUnitOperation(ctx, xid, wid, &msgpb.OperationMetadata{
		Target:            wu.GetName(),
		ActionType:        msgpb.OperationMetadata_CREATE,
		OriginalRequester: email,
	})
	if err != nil {
		return fmt.Errorf("failed to add work unit operation: %w", err)
	}
	return h.actuator.EnqueueWorkUnitTask(ctx, wu.GetCurrentWorkUnitExecution().GetName(), wu, client.CreateAction(priority, email), operation.GetName())
}
