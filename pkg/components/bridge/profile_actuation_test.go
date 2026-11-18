// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package bridge

import (
	"context"
	"sync"
	"testing"
	"testing/fstest"

	"github.com/google/go-cmp/cmp"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/client-go/kubernetes/fake"

	"helm.sh/helm/v3/pkg/chart"
	"helm.sh/helm/v3/pkg/release"

	"xmanager_cloud/pkg/components/bridge/helm"
)

// chartRecordingRunner is a helm.HelmActionRunner that records the chart of every upgrade and reports every release
// as deployed with all of its resources present.
type chartRecordingRunner struct {
	mu     sync.Mutex
	charts []string
}

var _ helm.HelmActionRunner = (*chartRecordingRunner)(nil)

func (r *chartRecordingRunner) Upgrade(_ context.Context, releaseName, namespace string, chartObj *chart.Chart, _ map[string]any) (*release.Release, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	name := ""
	if chartObj != nil {
		name = chartObj.Name()
	}
	r.charts = append(r.charts, name)
	return &release.Release{Name: releaseName, Namespace: namespace}, nil
}

func (r *chartRecordingRunner) Uninstall(_ context.Context, releaseName, namespace string) (*release.UninstallReleaseResponse, error) {
	return &release.UninstallReleaseResponse{Release: &release.Release{Name: releaseName, Namespace: namespace}}, nil
}

func (r *chartRecordingRunner) Status(_ context.Context, releaseName, namespace string) (*release.Release, error) {
	return &release.Release{Name: releaseName, Namespace: namespace, Info: &release.Info{Status: release.StatusDeployed}}, nil
}

func (r *chartRecordingRunner) MissingResources(context.Context, *release.Release) ([]string, error) {
	return nil, nil
}

// upgradedCharts returns the charts upgraded so far and forgets them.
func (r *chartRecordingRunner) upgradedCharts() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	got := r.charts
	r.charts = nil
	return got
}

// profileTestCharts embeds an ingestion-gateway chart named "ig-a" for stable-2026a and "ig-b" for stable-2026b.
func profileTestCharts() helm.ProfileCharts {
	chartFS := func(name string) fstest.MapFS {
		return fstest.MapFS{"components/ingestion-gateway/Chart.yaml": {Data: []byte("apiVersion: v2\nname: " + name + "\nversion: 0.1.0\n")}}
	}
	return helm.ProfileCharts{"stable-2026a": chartFS("ig-a"), "stable-2026b": chartFS("ig-b")}
}

// profiledComponent returns the ingestion-gateway Component CR at generation 1 labelled with profile, reporting Ready
// at that generation when ready is true.
func profiledComponent(profile string, ready bool) *unstructured.Unstructured {
	u := &unstructured.Unstructured{Object: map[string]any{
		"metadata": map[string]any{"name": "ingestion-gateway", "namespace": "xmc", "generation": int64(1), "labels": map[string]any{helm.ProfileLabel: profile}},
		"spec":     map[string]any{"enabled": true, "componentType": "COMPONENT_TYPE_CUSTOM_1P_COMPONENT"},
	}}
	if ready {
		u.Object["status"] = map[string]any{"observedGeneration": int64(1), "conditions": []any{
			map[string]any{"type": ConditionTypeReady, "status": ConditionStatusTrue, "observedGeneration": int64(1), "reason": ReasonComponentHealthy},
		}}
	}
	return u
}

// TestReconcile_ProfileLabelFlipReinstallsOnce checks the D3.17 contract end to end through the reconciler's
// actuated-generation gate: a label-only profile flip (generation unchanged) re-installs the 1P release exactly once
// with the new profile's chart, and a restarted bridge that finds the profile stored with the values does not
// upgrade again.
func TestReconcile_ProfileLabelFlipReinstallsOnce(t *testing.T) {
	ctx := context.Background()
	client := fake.NewClientset()
	newReconciler := func(runner *chartRecordingRunner) (*Reconciler, *helm.K8sHelmManager) {
		mgr := helm.NewK8sHelmManager(client, "xmc", "xmanager-cloud", helm.WithActionRunner(runner), helm.WithSyncExecution(true), helm.WithProfileCharts(profileTestCharts()))
		t.Cleanup(func() { mgr.Close() })
		r, err := NewReconciler(&mockCMS{}, nil, mgr)
		if err != nil {
			t.Fatalf("NewReconciler failed: %v", err)
		}
		return r, mgr
	}
	runner := &chartRecordingRunner{}
	r, _ := newReconciler(runner)
	steps := []struct {
		name    string
		profile string
		want    []string
	}{
		{name: "first actuation", profile: "stable-2026a", want: []string{"ig-a"}},
		{name: "same generation and label", profile: "stable-2026a", want: nil},
		{name: "label flip a to b", profile: "stable-2026b", want: []string{"ig-b"}},
		{name: "after the flip", profile: "stable-2026b", want: nil},
		{name: "label flip b to a", profile: "stable-2026a", want: []string{"ig-a"}},
		{name: "after the flip back", profile: "stable-2026a", want: nil},
		{name: "flip to b again", profile: "stable-2026b", want: []string{"ig-b"}},
	}
	for _, step := range steps {
		if _, err := r.Reconcile(ctx, profiledComponent(step.profile, false)); err != nil {
			t.Fatalf("%s: Reconcile failed: %v", step.name, err)
		}
		if diff := cmp.Diff(step.want, runner.upgradedCharts()); diff != "" {
			t.Errorf("%s: upgraded charts mismatch (-want +got):\n%s", step.name, diff)
		}
	}

	restartedRunner := &chartRecordingRunner{}
	restarted, _ := newReconciler(restartedRunner)
	if _, err := restarted.Reconcile(ctx, profiledComponent("stable-2026b", true)); err != nil {
		t.Fatalf("Reconcile after restart failed: %v", err)
	}
	if got := restartedRunner.upgradedCharts(); len(got) != 0 {
		t.Errorf("upgrades after a restart with the profile stored = %v, want none", got)
	}
}

// TestReconcile_PassesProfileLabelToHelm checks that the Component's raw xmc.dev/profile label reaches the Helm
// manager, which resolves missing and unknown labels.
func TestReconcile_PassesProfileLabelToHelm(t *testing.T) {
	tests := []struct {
		name   string
		labels map[string]any
		want   string
	}{
		{name: "profile label", labels: map[string]any{helm.ProfileLabel: "stable-2026b"}, want: "stable-2026b"},
		{name: "missing label", labels: nil, want: ""},
		{name: "unknown profile label", labels: map[string]any{helm.ProfileLabel: "stable-2099z"}, want: "stable-2099z"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			helmMgr := &mockHelm{}
			r, err := NewReconciler(&mockCMS{}, nil, helmMgr)
			if err != nil {
				t.Fatalf("NewReconciler failed: %v", err)
			}
			metadata := map[string]any{"name": "ingestion-gateway", "namespace": "xmc", "generation": int64(1)}
			if tc.labels != nil {
				metadata["labels"] = tc.labels
			}
			u := &unstructured.Unstructured{Object: map[string]any{
				"metadata": metadata,
				"spec":     map[string]any{"enabled": true, "componentType": "COMPONENT_TYPE_CUSTOM_1P_COMPONENT"},
			}}
			if _, err := r.Reconcile(context.Background(), u); err != nil {
				t.Fatalf("Reconcile failed: %v", err)
			}
			if got := helmMgr.lastOpts.Profile; got != tc.want {
				t.Errorf("HelmComponentOptions.Profile = %q, want %q", got, tc.want)
			}
		})
	}
}
