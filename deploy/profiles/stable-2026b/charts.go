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

// Package stable2026b provides embedded 1P component Helm charts for the stable-2026b profile.
package stable2026b

import "embed"

// EmbeddedCharts embeds the 1P component Helm charts under components/ that stable-2026b does not share with
// stable-2026a: hpo. The redpanda, clickhouse and ingestion-gateway directories are metadata-only, and xmc-bridge
// installs the 1P ingestion-gateway workload from the stable-2026a chart of the same name and version.
//
//go:embed components/hpo/Chart.yaml components/hpo/values.yaml components/hpo/templates/*
var EmbeddedCharts embed.FS
