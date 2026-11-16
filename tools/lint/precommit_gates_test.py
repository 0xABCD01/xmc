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

    def test_warn_missing_fails_only_in_check_mode(self):
        fix_ctx = gates.RunContext(mode="fix")
        check_ctx = gates.RunContext(mode="check")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as buf:
            fix_ctx.warn_missing("tool missing", "install hint")
            check_ctx.warn_missing("tool missing")
        self.assertFalse(fix_ctx.failed)
        self.assertTrue(check_ctx.failed)
        self.assertIn("install hint", buf.getvalue())


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

    def test_walk_repo_files_collect_target_files_and_should_run_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "main.tf").write_text("", encoding="utf-8")
            (root / "sub").mkdir()
            (root / "sub" / "vars.tf").write_text("", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "skip.tf").write_text("", encoding="utf-8")
            found = gates.walk_repo_files([".", "missing"], suffixes=(".tf",), base_dir=root)
        self.assertEqual(found, ["main.tf", "sub/vars.tf"])

        with (
            mock.patch.object(gates, "walk_repo_files", return_value=["deploy/a.tf"]),
            mock.patch.object(gates, "git_staged_paths", return_value=["deploy/a.tf", "b.py"]),
            mock.patch.object(gates, "staged_paths_match", return_value=False),
        ):
            self.assertEqual(
                gates.collect_target_files("check", staged_regex=r"\.tf$", suffixes=(".tf",)),
                ["deploy/a.tf"],
            )
            self.assertEqual(
                gates.collect_target_files("fix", staged_regex=r"\.tf$", suffixes=(".tf",)),
                ["deploy/a.tf"],
            )
            self.assertTrue(
                gates.should_run_scope(
                    "check", roots=("deploy",), suffixes=(".tf",), fix_pattern=r"\.tf$"
                )
            )
            self.assertFalse(
                gates.should_run_scope(
                    "fix", roots=("deploy",), suffixes=(".tf",), fix_pattern=r"\.tf$"
                )
            )


class ToolResolutionTest(unittest.TestCase):
    def test_docker_fallback_and_entrypoint(self):
        ctx = gates.RunContext(mode="check")
        with mock.patch.object(
            gates.shutil,
            "which",
            side_effect=lambda name: "/usr/local/bin/terraform" if name == "terraform" else None,
        ):
            local_res = gates.resolve_local_or_docker(
                ctx, "terraform", docker_image="tf:latest", mount="/workspace", missing_label="skip"
            )
        self.assertEqual(local_res, (["/usr/local/bin/terraform"], False))

        with mock.patch.object(
            gates.shutil,
            "which",
            side_effect=lambda name: "/usr/bin/docker" if name == "docker" else None,
        ):
            res = gates.resolve_local_or_docker(
                ctx,
                "tflint",
                docker_image="tflint:latest",
                mount="/data",
                missing_label="skip",
                entrypoint="sh",
            )
            assert res is not None
            self.assertTrue(res[1])
            self.assertIn(f"{gates.REPO_ROOT}:/data", res[0])
            self.assertIn("--entrypoint", res[0])

        with (
            mock.patch.object(gates.shutil, "which", return_value=None),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertIsNone(
                gates.resolve_local_or_docker(
                    ctx, "trivy", docker_image="img", mount="/src", missing_label="skipped."
                )
            )
        self.assertTrue(ctx.failed)

    def test_run_mode_command_respects_ignore_fix_error(self):
        ctx_fix = gates.RunContext(mode="fix")
        ctx_check = gates.RunContext(mode="check")
        with mock.patch.object(
            gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 1, "", "")
        ):
            gates.run_mode_command(
                ctx_fix,
                ["tf"],
                check_args=["fmt", "-check"],
                fix_args=["fmt"],
                failure_msg="bad",
                ignore_fix_error=True,
            )
            self.assertFalse(ctx_fix.failed)
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                gates.run_mode_command(
                    ctx_check,
                    ["tf"],
                    check_args=["fmt", "-check"],
                    fix_args=["fmt"],
                    failure_msg="bad",
                    ignore_fix_error=True,
                )
            self.assertTrue(ctx_check.failed)


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
