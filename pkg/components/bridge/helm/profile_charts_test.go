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

package helm

import (
	"bytes"
	"context"
	"io/fs"
	"log/slog"
	"strings"
	"testing"
	"testing/fstest"

	"xmanager_cloud/internal/logging"
)

// testProfileCharts returns the embedded default profile plus a "stable-test" profile whose ingestion-gateway chart is
// named chartName.
func testProfileCharts(chartName string) ProfileCharts {
	return ProfileCharts{
		DefaultProfile: defaultProfileCharts[DefaultProfile],
		"stable-test": fstest.MapFS{
			"components/ingestion-gateway/Chart.yaml": {Data: []byte("apiVersion: v2\nname: " + chartName + "\nversion: 0.1.0\n")},
		},
	}
}

func TestProfileChartsResolve(t *testing.T) {
	charts := testProfileCharts("ingestion-gateway-test")
	tests := []struct {
		name     string
		profile  string
		want     string
		wantWarn bool
	}{
		{name: "missing label", profile: "", want: DefaultProfile},
		{name: "default profile", profile: DefaultProfile, want: DefaultProfile},
		{name: "registered profile", profile: "stable-test", want: "stable-test"},
		{name: "unknown profile", profile: "stable-2099z", want: DefaultProfile, wantWarn: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			var buf bytes.Buffer
			ctx := logging.WithLogger(context.Background(), slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelWarn})))
			if got := charts.resolve(ctx, tc.profile); got != tc.want {
				t.Errorf("resolve(%q) = %q, want %q", tc.profile, got, tc.want)
			}
			if got := charts.lookup(tc.profile); got != tc.want {
				t.Errorf("lookup(%q) = %q, want %q", tc.profile, got, tc.want)
			}
			if gotWarn := strings.Contains(buf.String(), "label="+tc.profile); gotWarn != tc.wantWarn {
				t.Errorf("resolve(%q) logged WARN with the raw label = %v, want %v (logs: %q)", tc.profile, gotWarn, tc.wantWarn, buf.String())
			}
		})
	}
}

func TestProfileChartsComponentChart(t *testing.T) {
	charts := testProfileCharts("ingestion-gateway-test")
	charts["stable-metadata-only"] = fstest.MapFS{}
	tests := []struct {
		name         string
		profile      string
		component    string
		chartVersion string
		wantChart    string // empty means no embedded chart
		wantErr      bool
	}{
		{name: "default profile embeds ingestion-gateway", profile: DefaultProfile, component: "ingestion-gateway", wantChart: "ingestion-gateway"},
		{name: "registered profile embeds its own chart", profile: "stable-test", component: "ingestion-gateway", wantChart: "ingestion-gateway-test"},
		{name: "metadata-only entry falls back to the default profile", profile: "stable-metadata-only", component: "ingestion-gateway", wantChart: "ingestion-gateway"},
		{name: "fallback with matching chartVersion", profile: "stable-metadata-only", component: "ingestion-gateway", chartVersion: "0.1.0", wantChart: "ingestion-gateway"},
		{name: "fallback with mismatched chartVersion fails", profile: "stable-metadata-only", component: "ingestion-gateway", chartVersion: "0.2.0", wantErr: true},
		{name: "own chart ignores chartVersion", profile: "stable-test", component: "ingestion-gateway", chartVersion: "9.9.9", wantChart: "ingestion-gateway-test"},
		{name: "default profile ignores chartVersion", profile: DefaultProfile, component: "ingestion-gateway", chartVersion: "9.9.9", wantChart: "ingestion-gateway"},
		{name: "component without embedded chart", profile: DefaultProfile, component: "ray-operator"},
		{name: "component without chart in either profile", profile: "stable-metadata-only", component: "ray-operator"},
		{name: "path traversal component", profile: DefaultProfile, component: "../ingestion-gateway"},
		{name: "empty component", profile: DefaultProfile, component: ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			ch, err := charts.componentChart(tc.profile, tc.component, tc.chartVersion)
			if (err != nil) != tc.wantErr {
				t.Fatalf("componentChart(%q, %q, %q) error = %v, wantErr %v", tc.profile, tc.component, tc.chartVersion, err, tc.wantErr)
			}
			got := ""
			if ch != nil {
				got = ch.Name()
			}
			if got != tc.wantChart {
				t.Errorf("componentChart(%q, %q, %q) chart = %q, want %q", tc.profile, tc.component, tc.chartVersion, got, tc.wantChart)
			}
		})
	}
}

// TestDefaultProfileCharts checks the embedded registry: stable-2026b is known and its metadata-only ingestion-gateway
// entry resolves to the stable-2026a chart.
func TestDefaultProfileCharts(t *testing.T) {
	if got := defaultProfileCharts.resolve(context.Background(), "stable-2026b"); got != "stable-2026b" {
		t.Errorf("resolve(stable-2026b) = %q, want stable-2026b", got)
	}
	ch, err := defaultProfileCharts.componentChart("stable-2026b", "ingestion-gateway", "0.1.0")
	if err != nil || ch == nil || ch.Name() != "ingestion-gateway" {
		t.Errorf("componentChart(stable-2026b, ingestion-gateway) = %v, %v; want the stable-2026a ingestion-gateway chart", ch, err)
	}
}

func TestLoadEmbeddedChart(t *testing.T) {
	tests := []struct {
		name      string
		fsys      fs.FS
		wantChart string
		wantErr   bool
	}{
		{name: "nil filesystem", fsys: nil},
		{name: "missing root", fsys: fstest.MapFS{}},
		{
			name: "chart with templates",
			fsys: fstest.MapFS{
				"components/demo/Chart.yaml":            {Data: []byte("apiVersion: v2\nname: demo\nversion: 1.0.0\n")},
				"components/demo/templates/config.yaml": {Data: []byte("kind: ConfigMap\n")},
			},
			wantChart: "demo",
		},
		{
			name:    "malformed Chart.yaml",
			fsys:    fstest.MapFS{"components/demo/Chart.yaml": {Data: []byte("name: [unterminated\n")}},
			wantErr: true,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			ch, err := loadEmbeddedChart(tc.fsys, "components/demo")
			if (err != nil) != tc.wantErr {
				t.Fatalf("loadEmbeddedChart() error = %v, wantErr %v", err, tc.wantErr)
			}
			got := ""
			if ch != nil {
				got = ch.Name()
			}
			if got != tc.wantChart {
				t.Errorf("loadEmbeddedChart() chart = %q, want %q", got, tc.wantChart)
			}
		})
	}
}
