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

// Package stable2026b_test tests the deploy/profiles/stable-2026b meta-chart. The generic profile
// contract comes from profilecontract; this file holds only the stable-2026b expectations: its
// catalog, the profile label of its Component CRs, the component-specific values it accepts and
// rejects, and the metadata-only 1P catalog entries that reuse stable-2026a workload charts.
package stable2026b_test

import (
	"io/fs"
	"os"
	"path/filepath"
	"testing"

	"github.com/google/go-cmp/cmp"
	"gopkg.in/yaml.v3"

	stable2026bcharts "xmanager_cloud/deploy/profiles/stable-2026b"
	"xmanager_cloud/test/helm/profiles/profilecontract"
)

// stable2026b describes the stable-2026b profile catalog, rendered in dependency order.
func stable2026b() profilecontract.Spec {
	return profilecontract.Spec{
		ChartRelPath: "deploy/profiles/stable-2026b",
		ProfileName:  "stable-2026b",
		Components:   []string{"redpanda", "clickhouse", "ingestion-gateway"},
	}
}

func TestStable2026b_ProfileContract(t *testing.T) {
	t.Parallel()
	profilecontract.Run(t, stable2026b())
}

// TestStable2026b_IsPreview checks that stable-2026b stays a preview profile until its release,
// so dev_env requires --allow-preview-profile to install it.
func TestStable2026b_IsPreview(t *testing.T) {
	t.Parallel()
	if got := profilecontract.ReleaseStatus(t, stable2026b()); got != profilecontract.StatusPreview {
		t.Errorf("profile.yaml status = %q, want %q", got, profilecontract.StatusPreview)
	}
}

func TestStable2026b_RendersCatalogWithImageTag(t *testing.T) {
	t.Parallel()
	profilecontract.AssertRendersComponents(t, stable2026b(), map[string]any{"components": map[string]any{"redpanda": map[string]any{"enabled": true},
		"clickhouse": map[string]any{"enabled": true}, "ingestion-gateway": map[string]any{"enabled": true, "imageTag": "v1.2.3"}}})
}

// TestStable2026b_LabelsComponentsWithProfile checks that every Component CR carries the profile
// label that makes xmc-bridge select the stable-2026b embedded charts.
func TestStable2026b_LabelsComponentsWithProfile(t *testing.T) {
	t.Parallel()
	s := stable2026b()
	docs := profilecontract.RenderDocs(t, profilecontract.LoadProfile(t, s), nil)
	for _, name := range s.Components {
		if got := profilecontract.Lookup(profilecontract.DocByName(t, docs, name), "metadata", "labels", "xmc.dev/profile"); got != "stable-2026b" {
			t.Errorf("%s label xmc.dev/profile = %v, want stable-2026b", name, got)
		}
	}
}

func TestStable2026b_OverridesMatchStandalone(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name      string
		component string
		overrides map[string]any
	}{
		{name: "redpanda overrides", component: "redpanda", overrides: map[string]any{"enabled": true, "namespace": "data",
			"deletionProtection": false, "deletionPolicy": "Orphan", "parameters": map[string]any{"kafkaPort": 19092}, "dependencies": []any{"clickhouse"}}},
		{name: "clickhouse overrides", component: "clickhouse", overrides: map[string]any{"enabled": true,
			"parameters": map[string]any{"host": "ch.example.internal", "httpPort": 18123}}},
		{name: "ingestion-gateway overrides", component: "ingestion-gateway", overrides: map[string]any{"enabled": true, "imageTag": "v9",
			"dependencies": []any{"redpanda"}, "parameters": map[string]any{"topicName": "custom", "extraKey": "extra"}}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			profilecontract.AssertMatchesStandalone(t, stable2026b(), tt.component, tt.overrides)
		})
	}
}

func TestStable2026b_RejectsInvalidValues(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name      string
		overrides map[string]any
		wantErr   string
	}{
		{name: "workload key override", overrides: profilecontract.ComponentOverrides("ingestion-gateway", map[string]any{"deployWorkload": true}), wantErr: "deployWorkload"},
		{name: "unknown 3P parameter on redpanda", overrides: profilecontract.ComponentOverrides("redpanda", map[string]any{"parameters": map[string]any{"bogus": "1"}}), wantErr: "bogus"},
		{name: "stable-2026a platformProfile", overrides: map[string]any{"global": map[string]any{"platformProfile": "stable-2026a"}}, wantErr: "platformProfile"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			profilecontract.AssertRenderFails(t, stable2026b(), profilecontract.DefaultRelease, tt.overrides, tt.wantErr)
		})
	}
}

// TestStable2026b_MetadataOnlyComponentsMatchStable2026a guards the bridge fallback: a 1P catalog
// component whose chart stable2026b.EmbeddedCharts does not embed gets its workload from the
// stable-2026a chart of the same name, so its Chart.yaml name and version must match that chart.
// Ship and embed a full chart before bumping either.
func TestStable2026b_MetadataOnlyComponentsMatchStable2026a(t *testing.T) {
	t.Parallel()
	s := stable2026b()
	dirB := profilecontract.ChartDir(t, s)
	dirA := profilecontract.ChartDir(t, profilecontract.Spec{ChartRelPath: "deploy/profiles/stable-2026a"})
	metadataOnly := metadataOnly1PComponents(t, s)
	if len(metadataOnly) == 0 {
		t.Fatal("stable-2026b has no metadata-only 1P components; expected at least ingestion-gateway")
	}
	for _, name := range metadataOnly {
		want := readChartIdentity(t, filepath.Join(dirA, "components", name))
		if diff := cmp.Diff(want, readChartIdentity(t, filepath.Join(dirB, "components", name))); diff != "" {
			t.Errorf("components/%s Chart.yaml differs from stable-2026a (-stable-2026a +stable-2026b):\n%s", name, diff)
		}
	}
}

// metadataOnly1PComponents returns the catalog components that render as 1P Components and whose
// chart stable2026b.EmbeddedCharts does not embed.
func metadataOnly1PComponents(t *testing.T, s profilecontract.Spec) []string {
	t.Helper()
	docs := profilecontract.RenderDocs(t, profilecontract.LoadProfile(t, s), nil)
	var names []string
	for _, name := range s.Components {
		if profilecontract.Lookup(profilecontract.DocByName(t, docs, name), "spec", "componentType") != "COMPONENT_TYPE_CUSTOM_1P_COMPONENT" {
			continue
		}
		if _, err := fs.Stat(stable2026bcharts.EmbeddedCharts, "components/"+name+"/Chart.yaml"); err == nil {
			continue
		}
		names = append(names, name)
	}
	return names
}

// chartIdentity is the part of a Chart.yaml that identifies the chart a release installs.
type chartIdentity struct {
	Name    string `yaml:"name"`
	Version string `yaml:"version"`
}

func readChartIdentity(t *testing.T, chartDir string) chartIdentity {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(chartDir, "Chart.yaml"))
	if err != nil {
		t.Fatalf("read Chart.yaml: %v", err)
	}
	var id chartIdentity
	if err := yaml.Unmarshal(raw, &id); err != nil {
		t.Fatalf("parse %s/Chart.yaml: %v", chartDir, err)
	}
	return id
}
