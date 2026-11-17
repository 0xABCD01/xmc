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

mock_provider "google" {}

run "empty_image_registry_is_noop" {
  command = plan

  variables {
    image_registry             = ""
    enable_node_sa_binding     = true
    node_service_account_email = "xmc-np-sa@host-proj.iam.gserviceaccount.com"
    reader_service_accounts    = []
  }

  assert {
    condition     = length(google_artifact_registry_repository_iam_member.node_pool_reader) == 0
    error_message = "Expected no node_pool_reader binding when image_registry is empty."
  }

  assert {
    condition     = length(google_artifact_registry_repository_iam_member.additional_readers) == 0
    error_message = "Expected no additional_readers bindings when image_registry is empty."
  }
}

run "valid_artifact_registry_binds_node_and_additional_readers" {
  command = plan

  variables {
    image_registry             = "us-west1-docker.pkg.dev/reg-proj/xmc/subpath"
    enable_node_sa_binding     = true
    node_service_account_email = "xmc-np-sa@host-proj.iam.gserviceaccount.com"
    reader_service_accounts = [
      "worker-np-sa@worker-a.iam.gserviceaccount.com",
      "worker-np-sa@worker-b.iam.gserviceaccount.com",
      "worker-np-sa@worker-a.iam.gserviceaccount.com",
    ]
  }

  assert {
    condition     = google_artifact_registry_repository_iam_member.node_pool_reader[0].project == "reg-proj"
    error_message = "Expected repository project to be parsed as reg-proj."
  }

  assert {
    condition     = google_artifact_registry_repository_iam_member.node_pool_reader[0].location == "us-west1"
    error_message = "Expected repository location to be parsed as us-west1."
  }

  assert {
    condition     = google_artifact_registry_repository_iam_member.node_pool_reader[0].repository == "xmc"
    error_message = "Expected repository id to be parsed as xmc."
  }

  assert {
    condition     = google_artifact_registry_repository_iam_member.node_pool_reader[0].member == "serviceAccount:xmc-np-sa@host-proj.iam.gserviceaccount.com"
    error_message = "Expected node_pool_reader member to bind xmc-np-sa."
  }

  assert {
    condition     = length(google_artifact_registry_repository_iam_member.additional_readers) == 2
    error_message = "Expected duplicate reader_service_accounts entries to be deduplicated."
  }
}

run "rejects_default_compute_service_account" {
  command = plan

  variables {
    image_registry = "us-west1-docker.pkg.dev/reg-proj/xmc"
    reader_service_accounts = [
      "123456789012-compute@developer.gserviceaccount.com",
    ]
  }

  expect_failures = [
    var.reader_service_accounts,
  ]
}

run "rejects_service_account_prefix" {
  command = plan

  variables {
    image_registry = "us-west1-docker.pkg.dev/reg-proj/xmc"
    reader_service_accounts = [
      "serviceAccount:worker-np-sa@worker-a.iam.gserviceaccount.com",
    ]
  }

  expect_failures = [
    var.reader_service_accounts,
  ]
}
