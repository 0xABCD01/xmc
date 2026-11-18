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

"""Unit tests for precommit_gates."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.lint import precommit_gates as gates  # noqa: E402


class RunContextTest(unittest.TestCase):
    def test_fail_sets_failed_in_both_modes(self):
        ctx = gates.RunContext(mode="fix")
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            ctx.fail("broken")
        self.assertTrue(ctx.failed)

    def test_exec_cmd_handles_real_subprocess_and_missing_binary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            res = gates.exec_cmd(
                [sys.executable, "-c", "import os; print(os.path.basename(os.getcwd()))"],
                cwd=tmpdir,
                capture=True,
            )
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout.strip(), Path(tmpdir).name)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err_buf:
            missing = gates.exec_cmd(["nonexistent_binary_xmc_test"])
        self.assertEqual(missing.returncode, 127)
        self.assertIn("Failed to run", err_buf.getvalue())


class GitDiscoveryTest(unittest.TestCase):
    def test_git_staged_paths_exits_on_git_error(self):
        err_proc = subprocess.CompletedProcess([], 128, stdout="", stderr="bad git")
        with (
            mock.patch.object(gates, "exec_cmd", return_value=err_proc),
            mock.patch("sys.stderr", new_callable=io.StringIO) as err_mock,
            self.assertRaises(SystemExit) as cm,
        ):
            gates.git_staged_paths("AM", no_renames=True)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("bad git", err_mock.getvalue())

    def test_staged_paths_match_uses_no_renames_and_regex(self):
        ok_proc = subprocess.CompletedProcess(
            [], 0, stdout="README.md\n.agents/foo.md\n", stderr=""
        )
        with mock.patch.object(gates, "exec_cmd", return_value=ok_proc) as run_mock:
            self.assertTrue(gates.staged_paths_match("AM", r"^\.agents/.*\.md$"))
            self.assertFalse(gates.staged_paths_match("D", r"^cmd/"))
        self.assertIn("--no-renames", run_mock.call_args.args[0])
        self.assertEqual(run_mock.call_args.kwargs["cwd"], gates.REPO_ROOT)


class PythonGateTest(unittest.TestCase):
    def test_skips_when_fix_mode_pattern_does_not_match(self):
        ctx = gates.RunContext(mode="fix")
        gate = gates.PythonGate(
            banner="--> Docs",
            script="tools/lint/check_agent_doc_paths.py",
            failure_msg="fail",
            staged_am_pattern=r"^\.agents/",
            check_deletes=True,
        )
        with (
            mock.patch.object(gates, "staged_paths_match", return_value=False),
            mock.patch.object(gates, "exec_cmd") as exec_mock,
        ):
            gates.run_python_gate(ctx, gate)
        exec_mock.assert_not_called()

        with (
            mock.patch.object(gates, "staged_paths_match", side_effect=[False, True]),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 0, "", "")
            ) as del_exec,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            gates.run_python_gate(ctx, gate)
        del_exec.assert_called_once()

        ctx_check = gates.RunContext(mode="check")
        with (
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 0, "", "")
            ) as chk_exec,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            gates.run_python_gate(ctx_check, gate)
        chk_exec.assert_called_once()

    def test_baseline_error_paths_and_unchanged(self):
        gate = gates.PythonGate(
            banner="--> Ratchet",
            script="tools/lint/check_legacy_imports.py",
            failure_msg="ratchet failed",
            baseline_path="tools/lint/legacy_imports_baseline.json",
        )
        # Unchanged baseline
        ctx_same = gates.RunContext(mode="fix")
        with (
            mock.patch.object(
                gates,
                "exec_cmd",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                ],
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            gates.run_python_gate(ctx_same, gate)
        self.assertFalse(ctx_same.failed)

        # Failed git add / update-baseline / git diff error
        for seq in (
            [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 1, "", ""),
            ],
            [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 2, "", ""),
            ],
            [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 1, "", ""),
                subprocess.CompletedProcess([], 1, "", ""),
            ],
        ):
            ctx_err = gates.RunContext(mode="fix")
            with (
                mock.patch.object(gates, "exec_cmd", side_effect=seq),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                gates.run_python_gate(ctx_err, gate)
            self.assertTrue(ctx_err.failed)

    def test_main_sets_bypass_venv_and_runs_all_gates(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(gates, "run_python_gate") as run_mock,
        ):
            self.assertEqual(gates.main(["--mode", "check"]), 0)
            self.assertEqual(os.environ.get("XMC_BYPASS_VENV"), "1")
        self.assertEqual(run_mock.call_count, len(gates.PYTHON_GATES))

    def test_runs_and_updates_baseline_when_changed_in_fix_mode(self):
        ctx = gates.RunContext(mode="fix")
        gate = gates.PythonGate(
            banner="--> Ratchet",
            script="tools/lint/check_legacy_imports.py",
            failure_msg="ratchet failed",
            baseline_path="tools/lint/legacy_imports_baseline.json",
        )
        side_effects = [
            subprocess.CompletedProcess([], 0, "", ""),  # check
            subprocess.CompletedProcess([], 0, "", ""),  # --update-baseline
            subprocess.CompletedProcess([], 1, "", ""),  # git diff --quiet (changed)
            subprocess.CompletedProcess([], 0, "", ""),  # git add
        ]
        with (
            mock.patch.object(gates, "exec_cmd", side_effect=side_effects) as exec_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO) as buf,
        ):
            gates.run_python_gate(ctx, gate)
        self.assertFalse(ctx.failed)
        self.assertEqual(exec_mock.call_count, 4)
        self.assertTrue(
            all(call.kwargs["cwd"] == gates.REPO_ROOT for call in exec_mock.call_args_list)
        )
        self.assertIn("Lowered and staged", buf.getvalue())

    def test_records_failure_on_nonzero_script_exit(self):
        ctx = gates.RunContext(mode="check")
        gate = gates.PythonGate(
            banner="--> Naming",
            script="tools/lint/check_file_naming.py",
            failure_msg="naming failed",
        )
        with (
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 1, "", "")
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            gates.run_python_gate(ctx, gate)
        self.assertTrue(ctx.failed)

    def test_main_runs_all_gates_and_propagates_failure(self):
        with mock.patch.object(gates, "run_python_gate") as run_mock:
            self.assertEqual(gates.main(["--mode", "check"]), 0)
        self.assertEqual(run_mock.call_count, len(gates.PYTHON_GATES))

        def _fail_first(ctx: gates.RunContext, _gate: gates.PythonGate) -> None:
            ctx.fail("bad")

        with (
            mock.patch.object(gates, "run_python_gate", side_effect=_fail_first),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(gates.main(["--mode", "fix"]), 1)


if __name__ == "__main__":
    unittest.main()
