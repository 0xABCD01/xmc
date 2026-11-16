# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Hermetic unit tests for Artifact Registry IAM bindings in the CTK v2 blueprint."""

import re
import unittest

from chart_test_lib import (
    BLUEPRINT_PATH,
    extract_module_block,
    find_repo_file,
)


def _read_repo_file(rel_path: str) -> str:
    with open(find_repo_file(rel_path), encoding="utf-8") as f:
        return f.read()


class ArtifactRegistryIamTest(unittest.TestCase):
    """Verifies Artifact Registry reader IAM setup for default Compute SAs."""

    def setUp(self) -> None:
        super().setUp()
        self.blueprint = _read_repo_file(BLUEPRINT_PATH)
        self.template = _read_repo_file("deploy/ctk_v2/xmc-deployment-template.yaml")

    def test_cloud_api_enables_artifact_registry(self) -> None:
        cloud_api_block = extract_module_block(self.blueprint, "cloud_api")
        self.assertIn('"artifactregistry.googleapis.com"', cloud_api_block)

    def test_blueprint_and_template_declare_worker_project_ids(self) -> None:
        self.assertRegex(
            self.blueprint,
            re.compile(r"^  worker_project_ids:\s*\[\]", re.MULTILINE),
        )
        self.assertRegex(
            self.template,
            re.compile(r"^  worker_project_ids:\s*\[\]", re.MULTILINE),
        )

    def test_default_compute_sa_artifact_registry_reader_module_wired(self) -> None:
        module_block = extract_module_block(
            self.blueprint, "default_compute_sa_artifact_registry_reader"
        )
        self.assertIn(
            "source: ./deploy/ctk_v2/modules/artifact-registry-iam",
            module_block,
        )
        self.assertIn("project_id: $(vars.project_id)", module_block)
        self.assertIn("worker_project_ids: $(vars.worker_project_ids)", module_block)

    def test_terraform_module_grants_artifact_registry_reader_to_default_sa(
        self,
    ) -> None:
        main_tf = _read_repo_file("deploy/ctk_v2/modules/artifact-registry-iam/main.tf")
        variables_tf = _read_repo_file("deploy/ctk_v2/modules/artifact-registry-iam/variables.tf")

        self.assertIn('data "google_project" "projects"', main_tf)
        self.assertIn(
            'resource "google_project_iam_member" "default_compute_sa_artifact_registry_reader"',
            main_tf,
        )
        self.assertRegex(main_tf, r'role\s*=\s*"roles/artifactregistry\.reader"')
        self.assertIn("-compute@developer.gserviceaccount.com", main_tf)
        self.assertIn('variable "project_id"', variables_tf)
        self.assertIn('variable "worker_project_ids"', variables_tf)


if __name__ == "__main__":
    unittest.main()
