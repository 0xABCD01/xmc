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

"""Unit tests for precommit_deploy_checks."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.lint import (  # noqa: E402
    precommit_deploy_checks as deploy_checks,
    precommit_gates as gates,
)


class DeployChecksTest(unittest.TestCase):
    def test_check_terraform_runs_fmt_and_skips_when_empty_or_missing(self):
        ctx_empty = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "collect_target_files", return_value=[]),
            mock.patch.object(gates, "run_mode_command") as run_empty,
        ):
            deploy_checks.check_terraform(ctx_empty)
        run_empty.assert_not_called()

        ctx_missing = gates.RunContext(mode="fix")
        with (
            mock.patch.object(gates, "collect_target_files", return_value=["deploy/main.tf"]),
            mock.patch.object(gates, "resolve_local_or_docker", return_value=None),
            mock.patch.object(gates, "run_mode_command") as run_missing,
        ):
            deploy_checks.check_terraform(ctx_missing)
        run_missing.assert_not_called()

        ctx = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "collect_target_files", return_value=["deploy/main.tf"]),
            mock.patch.object(
                gates, "resolve_local_or_docker", return_value=(["terraform"], False)
            ),
            mock.patch.object(gates, "run_mode_command") as run_mode,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            deploy_checks.check_terraform(ctx)
        run_mode.assert_called_once()
        self.assertTrue(run_mode.call_args.kwargs["ignore_fix_error"])
        self.assertEqual(run_mode.call_args.kwargs["cwd"], gates.REPO_ROOT)

    def test_check_tflint_local_and_docker(self):
        ctx_local = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "should_run_scope", return_value=True),
            mock.patch.object(gates, "resolve_local_or_docker", return_value=(["tflint"], False)),
            mock.patch.object(
                gates,
                "exec_cmd",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),  # --init
                    subprocess.CompletedProcess([], 0, "", ""),  # --recursive
                ],
            ) as exec_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            deploy_checks.check_tflint(ctx_local)
        self.assertFalse(ctx_local.failed)
        self.assertEqual(exec_mock.call_count, 2)
        self.assertTrue(
            all(call.kwargs["cwd"] == gates.REPO_ROOT for call in exec_mock.call_args_list)
        )

        ctx_docker = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "should_run_scope", return_value=True),
            mock.patch.object(
                gates, "resolve_local_or_docker", return_value=(["docker", "run", "tflint"], True)
            ),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 1, "", "")
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            deploy_checks.check_tflint(ctx_docker)
        self.assertTrue(ctx_docker.failed)

    def test_check_helm_lint_pythonpath_and_failures(self):
        ctx_helm = gates.RunContext(mode="fix")
        with (
            mock.patch.object(gates, "should_run_scope", return_value=True),
            mock.patch.object(gates, "staged_paths_match", return_value=False),
            mock.patch.dict(os.environ, {"PYTHONPATH": "existing/path"}, clear=False),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 1, "", "")
            ) as exec_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            deploy_checks.check_helm(ctx_helm)
        self.assertTrue(ctx_helm.failed)
        exec_mock.assert_called_once()
        self.assertEqual(exec_mock.call_args.kwargs["cwd"], gates.REPO_ROOT)
        self.assertEqual(
            exec_mock.call_args.kwargs["env"]["PYTHONPATH"],
            f"test/helm{os.pathsep}existing/path",
        )

        ctx_check_lint_fail = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "should_run_scope", return_value=True),
            mock.patch.object(deploy_checks.shutil, "which", return_value="/usr/bin/helm"),
            mock.patch.object(
                gates,
                "exec_cmd",
                side_effect=[
                    subprocess.CompletedProcess([], 1, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                ],
            ) as check_exec,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            deploy_checks.check_helm(ctx_check_lint_fail)
        self.assertTrue(ctx_check_lint_fail.failed)
        self.assertEqual(check_exec.call_count, 2)

        ctx_no_helm = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "should_run_scope", return_value=True),
            mock.patch.object(deploy_checks.shutil, "which", return_value=None),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 0, "", "")
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            deploy_checks.check_helm(ctx_no_helm)
        self.assertTrue(ctx_no_helm.failed)

    def test_check_security_pass_fail_and_skip(self):
        ctx_skip = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "should_run_scope", return_value=False),
            mock.patch.object(gates, "exec_cmd") as exec_skip,
        ):
            deploy_checks.check_security(ctx_skip)
        exec_skip.assert_not_called()

        ctx_sec = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "should_run_scope", return_value=True),
            mock.patch.object(gates, "resolve_local_or_docker", return_value=(["trivy"], False)),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 0, "", "")
            ) as ok_exec,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            deploy_checks.check_security(ctx_sec)
        self.assertFalse(ctx_sec.failed)
        self.assertEqual(ok_exec.call_args.kwargs["cwd"], gates.REPO_ROOT)

        ctx_fail = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "should_run_scope", return_value=True),
            mock.patch.object(gates, "resolve_local_or_docker", return_value=(["trivy"], False)),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 1, "", "")
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            deploy_checks.check_security(ctx_fail)
        self.assertTrue(ctx_fail.failed)

    def test_main_runs_check_deploy_and_propagates_failure(self):
        with mock.patch.object(deploy_checks, "check_deploy") as check_mock:
            self.assertEqual(deploy_checks.main(["--mode", "check"]), 0)
        check_mock.assert_called_once()

        def _fail(ctx: gates.RunContext) -> None:
            ctx.fail("deploy check failed")

        with (
            mock.patch.object(deploy_checks, "check_deploy", side_effect=_fail),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(deploy_checks.main(["--mode", "fix"]), 1)


if __name__ == "__main__":
    unittest.main()
