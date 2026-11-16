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

locals {
  all_project_ids = toset(compact(concat([var.project_id], var.worker_project_ids)))

  iam_bindings = {
    for pair in concat(
      [
        for sa_project_id in local.all_project_ids : {
          target_project_id = var.project_id
          sa_project_id     = sa_project_id
        }
      ],
      [
        for worker_project_id in compact(var.worker_project_ids) : {
          target_project_id = worker_project_id
          sa_project_id     = worker_project_id
        } if worker_project_id != var.project_id
      ],
    ) : "${pair.target_project_id}:${pair.sa_project_id}" => pair
  }
}

data "google_project" "projects" {
  for_each   = local.all_project_ids
  project_id = each.value
}

# trivy:ignore:AVD-GCP-0005
resource "google_project_iam_member" "default_compute_sa_artifact_registry_reader" {
  for_each = local.iam_bindings
  project  = each.value.target_project_id
  role     = "roles/artifactregistry.reader"
  member   = "serviceAccount:${data.google_project.projects[each.value.sa_project_id].number}-compute@developer.gserviceaccount.com"
}
