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
			if gotWarn := strings.Contains(buf.String(), "label="+tc.profile); gotWarn != tc.wantWarn {
				t.Errorf("resolve(%q) logged WARN with the raw label = %v, want %v (logs: %q)", tc.profile, gotWarn, tc.wantWarn, buf.String())
			}
		})
	}
}

func TestProfileChartsComponentChart(t *testing.T) {
	charts := testProfileCharts("ingestion-gateway-test")
	tests := []struct {
		name      string
		profile   string
		component string
		wantChart string // empty means no embedded chart
	}{
		{name: "default profile embeds ingestion-gateway", profile: DefaultProfile, component: "ingestion-gateway", wantChart: "ingestion-gateway"},
		{name: "registered profile embeds its own chart", profile: "stable-test", component: "ingestion-gateway", wantChart: "ingestion-gateway-test"},
		{name: "component without embedded chart", profile: DefaultProfile, component: "ray-operator"},
		{name: "profile without entry", profile: "stable-2099z", component: "ingestion-gateway"},
		{name: "path traversal component", profile: DefaultProfile, component: "../ingestion-gateway"},
		{name: "empty component", profile: DefaultProfile, component: ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			ch, err := charts.componentChart(tc.profile, tc.component)
			if err != nil {
				t.Fatalf("componentChart(%q, %q) failed: %v", tc.profile, tc.component, err)
			}
			got := ""
			if ch != nil {
				got = ch.Name()
			}
			if got != tc.wantChart {
				t.Errorf("componentChart(%q, %q) chart = %q, want %q", tc.profile, tc.component, got, tc.wantChart)
			}
		})
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
