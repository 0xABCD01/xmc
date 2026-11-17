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
    """Verifies repository-scoped Artifact Registry reader IAM setup for custom node SAs."""

    def setUp(self) -> None:
        super().setUp()
        self.blueprint = _read_repo_file(BLUEPRINT_PATH)
        self.template = _read_repo_file("deploy/ctk_v2/xmc-deployment-template.yaml")

    def test_cloud_api_enables_artifact_registry(self) -> None:
        """Verifies the cloud_api module enables artifactregistry.googleapis.com."""
        cloud_api_block = extract_module_block(self.blueprint, "cloud_api")
        self.assertIn('"artifactregistry.googleapis.com"', cloud_api_block)

    def test_blueprint_and_template_declare_reader_service_accounts(self) -> None:
        """Verifies blueprint and template expose reader_service_accounts and omit worker_project_ids."""
        self.assertRegex(
            self.blueprint,
            re.compile(r"^  reader_service_accounts:\s*\[\]", re.MULTILINE),
        )
        self.assertRegex(
            self.template,
            re.compile(r"^  reader_service_accounts:\s*\[\]", re.MULTILINE),
        )
        self.assertNotIn("worker_project_ids", self.blueprint)
        self.assertNotIn("worker_project_ids", self.template)

    def test_artifact_registry_reader_iam_and_compute_cluster_wired(self) -> None:
        """Verifies artifact_registry_reader_iam and compute_cluster use node_pool_service_account."""
        module_block = extract_module_block(self.blueprint, "artifact_registry_reader_iam")
        self.assertIn(
            "source: ./deploy/ctk_v2/modules/artifact-registry-iam",
            module_block,
        )
        self.assertIn("use: [node_pool_service_account]", module_block)
        self.assertIn("image_registry: $(vars.image_registry)", module_block)
        self.assertIn("enable_node_sa_binding: true", module_block)
        self.assertIn(
            "node_service_account_email: $(node_pool_service_account.service_account_email)",
            module_block,
        )
        self.assertIn(
            "reader_service_accounts: $(vars.reader_service_accounts)",
            module_block,
        )

        cluster_block = extract_module_block(self.blueprint, "compute_cluster")
        self.assertIn("use: [network, node_pool_service_account]", cluster_block)

        workload_sa_block = extract_module_block(self.blueprint, "workload_service_account")
        self.assertNotIn("artifactregistry.reader", workload_sa_block)

        node_sa_block = extract_module_block(self.blueprint, "node_pool_service_account")
        self.assertNotIn("artifactregistry.reader", node_sa_block)

    def test_terraform_module_grants_repository_scoped_reader_to_custom_sas(
        self,
    ) -> None:
        """Verifies repository-scoped IAM bindings and rejects default Compute Engine SAs."""
        main_tf = _read_repo_file("deploy/ctk_v2/modules/artifact-registry-iam/main.tf")
        variables_tf = _read_repo_file("deploy/ctk_v2/modules/artifact-registry-iam/variables.tf")

        self.assertIn(
            'resource "google_artifact_registry_repository_iam_member" "node_pool_reader"',
            main_tf,
        )
        self.assertIn(
            'resource "google_artifact_registry_repository_iam_member" "additional_readers"',
            main_tf,
        )
        self.assertRegex(main_tf, r'role\s*=\s*"roles/artifactregistry\.reader"')
        self.assertIn('variable "image_registry"', variables_tf)
        self.assertIn('variable "enable_node_sa_binding"', variables_tf)
        self.assertIn('variable "node_service_account_email"', variables_tf)
        self.assertIn('variable "reader_service_accounts"', variables_tf)
        self.assertIn(
            '!endswith(trimspace(sa), "-compute@developer.gserviceaccount.com")',
            variables_tf,
        )

        # Negative security assertions: no project-wide IAM, no default Compute SAs, no Trivy ignores.
        self.assertNotIn("google_project_iam_member", main_tf)
        self.assertNotIn('data "google_project"', main_tf)
        self.assertNotIn("-compute@developer.gserviceaccount.com", main_tf)
        self.assertNotIn("AVD-GCP-0005", main_tf)


if __name__ == "__main__":
    unittest.main()
