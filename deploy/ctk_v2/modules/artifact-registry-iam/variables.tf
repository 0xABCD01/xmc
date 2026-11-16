# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

variable "project_id" {
  description = "The host GCP project ID."
  type        = string
}

variable "worker_project_ids" {
  description = "Additional GCP project IDs hosting MultiKueue worker clusters whose Compute Engine default service accounts require Artifact Registry reader access."
  type        = list(string)
  default     = []
}
