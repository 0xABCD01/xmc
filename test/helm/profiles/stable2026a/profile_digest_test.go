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

package stable2026a_test

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"xmanager_cloud/test/helm/profiles/profilecontract"
)

// stable2026aChartDigest pins the content of the stable-2026a profile chart (the
// //deploy/profiles/stable-2026a:chart filegroup, including the embedded ingestion-gateway workload chart).
const stable2026aChartDigest = "59c128fd1e389c1db43096ad2c9816fcee133c319ef43fbe724c400ff462581d"

// TestStable2026a_ChartIsImmutable fails on any change to the stable-2026a chart files. stable-2026a shipped with
// XMC 1.0.0 and is GA-locked: clusters that installed it must keep getting exactly the same components, so changes
// go into a new profile under deploy/profiles instead.
func TestStable2026a_ChartIsImmutable(t *testing.T) {
	t.Parallel()
	got, err := chartDigest(profilecontract.ChartDir(t, stable2026a()))
	if err != nil {
		t.Fatalf("chartDigest() failed: %v", err)
	}
	if got != stable2026aChartDigest {
		t.Errorf("stable-2026a chart digest = %s, want %s.\n"+
			"deploy/profiles/stable-2026a is GA-locked for XMC 1.0.0 and must not change. Revert the edit and make the "+
			"change in a new profile under deploy/profiles instead.", got, stable2026aChartDigest)
	}
}

// chartDigest returns a SHA-256 over the sorted relative paths and contents of the chart files under dir. Build and
// documentation files outside the Bazel chart filegroup are skipped, so the digest is the same under plain go test.
//
// The digest assumes that the //deploy/profiles/stable-2026a:chart filegroup (Bazel runfiles) and the source directory
// (plain go test) hold the same files apart from those isChartFile skips. If the filegroup ever excludes another file,
// add it to isChartFile, or the digest would depend on how the test is run.
func chartDigest(dir string) (string, error) {
	files, err := chartFiles(dir)
	if err != nil {
		return "", err
	}
	h := sha256.New()
	for _, rel := range files {
		data, err := os.ReadFile(filepath.Join(dir, rel))
		if err != nil {
			return "", fmt.Errorf("failed to read chart file %s: %w", rel, err)
		}
		contentSum := sha256.Sum256(data)
		fmt.Fprintf(h, "%s\x00%s\n", filepath.ToSlash(rel), hex.EncodeToString(contentSum[:]))
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// chartFiles returns the sorted paths, relative to dir, of the chart files under dir.
func chartFiles(dir string) ([]string, error) {
	var files []string
	err := filepath.WalkDir(dir, func(p string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil || d.IsDir() || !isChartFile(d.Name()) {
			return walkErr
		}
		rel, err := filepath.Rel(dir, p)
		if err != nil {
			return err
		}
		files = append(files, rel)
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("failed to list chart files under %s: %w", dir, err)
	}
	slices.Sort(files)
	return files, nil
}

// isChartFile reports whether a file named name belongs to the chart rather than to its build or documentation.
func isChartFile(name string) bool {
	return name != "BUILD.bazel" && name != "README.md" && !strings.HasSuffix(name, ".go")
}
