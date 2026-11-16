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

"""Unit tests for precommit_runner."""

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
    precommit_runner as runner,
)


class LanguageChecksTest(unittest.TestCase):
    def test_check_bazel_invokes_buildifier(self):
        ctx = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "resolve_go_tool", return_value="/bin/buildifier"),
            mock.patch.object(gates, "walk_repo_files", return_value=["BUILD.bazel"]),
            mock.patch.object(gates, "run_mode_command") as run_mode,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_bazel(ctx)
        run_mode.assert_called_once()
        self.assertEqual(run_mode.call_args.args[1], ["/bin/buildifier"])
        self.assertIn("BUILD.bazel", run_mode.call_args.kwargs["check_args"])

    def test_check_go_runs_all_subchecks_in_check_mode(self):
        ctx = gates.RunContext(mode="check")
        with (
            mock.patch.object(runner.shutil, "which", return_value="/usr/bin/go"),
            mock.patch.object(gates, "resolve_go_tool", return_value="/bin/golangci-lint"),
            mock.patch.object(runner.os.path, "isdir", return_value=True),
            mock.patch.object(gates, "run_mode_command"),
            mock.patch.object(
                gates,
                "exec_cmd",
                side_effect=[
                    subprocess.CompletedProcess([], 0, stdout="pkg/a.go\n", stderr=""),  # gofmt
                    subprocess.CompletedProcess([], 1, stdout="", stderr=""),  # determinism
                    subprocess.CompletedProcess([], 1, stdout="", stderr=""),  # go mod tidy -diff
                ],
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_go(ctx)
        self.assertTrue(ctx.failed)

    def test_check_ui_and_license(self):
        ctx = gates.RunContext(mode="fix")
        with (
            mock.patch.object(gates, "collect_target_files", return_value=["cmd/ui/app.ts"]),
            mock.patch.object(gates, "resolve_ui_binary", side_effect=["/p/prettier", "/p/eslint"]),
            mock.patch.object(gates, "run_mode_command") as run_mode,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_ui(ctx)
        self.assertEqual(run_mode.call_count, 2)

        ctx_check = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "resolve_go_tool", return_value="/bin/addlicense"),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 1, "", "")
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_license(ctx_check)
        self.assertTrue(ctx_check.failed)

    def test_check_python_uses_docker_fallback_with_no_cache(self):
        ctx = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "collect_target_files", return_value=["a.py"]),
            mock.patch.object(
                gates,
                "resolve_local_or_docker",
                return_value=(["docker", "run", "ruff"], True),
            ),
            mock.patch.object(
                gates,
                "exec_cmd",
                side_effect=[
                    subprocess.CompletedProcess([], 1, "", ""),  # check
                    subprocess.CompletedProcess([], 0, "", ""),  # format --check
                ],
            ) as exec_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_python(ctx)
        self.assertTrue(ctx.failed)
        self.assertIn("--no-cache", exec_mock.call_args_list[0].args[0])

    def test_check_mypy_skips_unrelated_fix_and_fails_when_missing(self):
        ctx_fix = gates.RunContext(mode="fix")
        with (
            mock.patch.object(gates, "staged_paths_match", return_value=False),
            mock.patch.object(runner, "_resolve_mypy_cmd") as resolve_mypy,
        ):
            runner.check_mypy(ctx_fix)
        resolve_mypy.assert_not_called()

        ctx_check = gates.RunContext(mode="check")
        with (
            mock.patch.object(runner, "_resolve_mypy_cmd", return_value=[]),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_mypy(ctx_check)
        self.assertTrue(ctx_check.failed)


class OrchestrationAndMainTest(unittest.TestCase):
    def test_linter_phase_blocks_when_autostage_restage_fails(self):
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(runner, "check_bazel"),
            mock.patch.object(runner, "check_go"),
            mock.patch.object(gates, "run_python_gate"),
            mock.patch.object(runner, "check_ui"),
            mock.patch.object(runner, "check_license"),
            mock.patch.object(runner, "check_python"),
            mock.patch.object(runner, "check_mypy"),
            mock.patch.object(deploy_checks, "check_deploy"),
            mock.patch.object(runner, "_autostage", side_effect=[True, False]) as auto_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO) as buf,
        ):
            ok = runner._run_linter_phase("fix")
        self.assertFalse(ok)
        self.assertEqual(auto_mock.call_count, 2)
        self.assertIn("not fully staged", buf.getvalue())

    def test_test_phase_skips_docs_only_commits_and_restores_lockfile(self):
        with (
            mock.patch.object(gates, "git_staged_paths", return_value=["README.md", "docs/a.md"]),
            mock.patch.object(runner.shutil, "which") as which_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertTrue(runner._run_test_phase("fix"))
        which_mock.assert_not_called()

        with (
            mock.patch.object(runner.shutil, "which", return_value="/usr/bin/bazel"),
            mock.patch.object(
                gates,
                "exec_cmd",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),  # lockfile clean
                    subprocess.CompletedProcess([], 0, "", ""),  # run_affected_tests
                    subprocess.CompletedProcess([], 0, "", ""),  # checkout lockfile
                ],
            ) as exec_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertTrue(runner._run_test_phase("check"))
        self.assertEqual(exec_mock.call_count, 3)

    def test_main_check_flag_and_exit_codes(self):
        with (
            mock.patch.object(runner, "_run_linter_phase", return_value=True) as linter,
            mock.patch.object(runner, "_run_test_phase", return_value=True) as tests,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(runner.main(["--check"]), 0)
        linter.assert_called_once_with("check")
        tests.assert_called_once_with("check")

        with (
            mock.patch.object(runner, "_run_linter_phase", return_value=False),
            mock.patch.object(runner, "_run_test_phase") as tests_skipped,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(runner.main([]), 1)
        tests_skipped.assert_not_called()


if __name__ == "__main__":
    unittest.main()
