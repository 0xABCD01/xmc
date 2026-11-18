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
	"context"
	"errors"
	"fmt"
	"io/fs"
	"strings"

	"helm.sh/helm/v3/pkg/chart"
	"helm.sh/helm/v3/pkg/chart/loader"

	stable2026a "xmanager_cloud/deploy/profiles/stable-2026a"
	stable2026b "xmanager_cloud/deploy/profiles/stable-2026b"
	"xmanager_cloud/internal/logging"
)

const (
	// ProfileLabel is the Component CR label naming the profile whose meta-chart rendered the Component.
	ProfileLabel = "xmc.dev/profile"
	// DefaultProfile serves the embedded charts of Components whose ProfileLabel is missing or names a profile the
	// bridge does not embed.
	DefaultProfile = "stable-2026a"
)

// ProfileCharts maps each profile name to the 1P component charts embedded for it, rooted at components/<name>/. A
// profile embeds only the charts it changes; see componentChart.
type ProfileCharts map[string]fs.FS

// defaultProfileCharts are the profiles embedded in the bridge binary. It is never modified; see WithProfileCharts.
var defaultProfileCharts = ProfileCharts{
	DefaultProfile: stable2026a.EmbeddedCharts,
	"stable-2026b": stable2026b.EmbeddedCharts,
}

// resolve returns lookup(profile) and logs a non-empty profile without an entry (an unknown or skewed xmc.dev/profile
// label) at WARN with the raw value. Only actuation calls it: drift checks run on every reconcile pass, so they use
// lookup to keep an idle component from logging the same warning on every pass.
func (charts ProfileCharts) resolve(ctx context.Context, profile string) string {
	resolved := charts.lookup(profile)
	if profile != "" && resolved != profile {
		logging.Warn(ctx, "Unknown component profile label; using the default profile's embedded charts", "label", profile, "defaultProfile", DefaultProfile)
	}
	return resolved
}

// lookup returns profile when charts has an entry for it, and DefaultProfile otherwise.
func (charts ProfileCharts) lookup(profile string) string {
	if _, ok := charts[profile]; ok {
		return profile
	}
	return DefaultProfile
}

// componentChart loads the chart that the resolved profile embeds for componentName. A profile that does not embed the
// component (its catalog entry is metadata-only) falls back to the DefaultProfile chart of the same name, which must
// have the requested chartVersion (the Component's spec.chartVersion; empty skips the check). It returns a nil chart
// when neither profile embeds a chart for the component.
func (charts ProfileCharts) componentChart(profile, componentName, chartVersion string) (*chart.Chart, error) {
	if !isSafeComponentSegment(componentName) {
		return nil, nil
	}
	root := "components/" + componentName
	ch, err := loadEmbeddedChart(charts[profile], root)
	if ch != nil || err != nil || profile == DefaultProfile {
		return ch, err
	}
	if ch, err = loadEmbeddedChart(charts[DefaultProfile], root); ch == nil || err != nil {
		return ch, err
	}
	if chartVersion != "" && ch.Metadata.Version != chartVersion {
		return nil, fmt.Errorf("profile %q embeds no %s chart, and the %s fallback chart has version %s instead of the requested chartVersion %s", profile, componentName, DefaultProfile, ch.Metadata.Version, chartVersion)
	}
	return ch, nil
}

// loadEmbeddedChart loads the chart rooted at root in fsys, or returns a nil chart when fsys is nil or root does not
// exist.
func loadEmbeddedChart(fsys fs.FS, root string) (*chart.Chart, error) {
	if fsys == nil {
		return nil, nil
	}
	var files []*loader.BufferedFile
	err := fs.WalkDir(fsys, root, func(p string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil || d.IsDir() {
			return walkErr
		}
		data, err := fs.ReadFile(fsys, p)
		if err != nil {
			return err
		}
		files = append(files, &loader.BufferedFile{Name: strings.TrimPrefix(p, root+"/"), Data: data})
		return nil
	})
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("failed to read embedded chart %q: %w", root, err)
	}
	ch, err := loader.LoadFiles(files)
	if err != nil {
		return nil, fmt.Errorf("failed to load embedded chart %q: %w", root, err)
	}
	return ch, nil
}
