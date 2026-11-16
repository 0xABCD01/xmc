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

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
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
        self.assertEqual(run_mode.call_args.kwargs["files"], ["BUILD.bazel"])

    def test_check_go_runs_all_subchecks_in_check_mode(self):
        ctx = gates.RunContext(mode="check")
        with (
            mock.patch.object(runner.shutil, "which", return_value="/usr/bin/go"),
            mock.patch.object(gates, "resolve_go_tool", return_value="/bin/golangci-lint"),
            mock.patch.object(runner.Path, "is_dir", return_value=True),
            mock.patch.object(gates, "run_mode_command") as run_mode,
            mock.patch.object(
                gates,
                "exec_cmd",
                side_effect=[
                    subprocess.CompletedProcess([], 0, stdout="pkg/a.go\n", stderr=""),  # gofmt
                    subprocess.CompletedProcess([], 1, stdout="", stderr=""),  # determinism
                ],
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_go(ctx)
        self.assertTrue(ctx.failed)
        self.assertEqual(run_mode.call_count, 2)

        ctx_win = gates.RunContext(mode="check")
        with (
            mock.patch.object(runner.shutil, "which", return_value=None),
            mock.patch.object(gates, "resolve_go_tool", return_value=None),
            mock.patch.object(runner.os, "sep", "\\"),
            mock.patch.object(
                gates,
                "exec_cmd",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout="pkg\\vendor\\a.go\n", stderr=""
                ),
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_go(ctx_win)
        self.assertFalse(ctx_win.failed)

    def test_check_go_skips_determinism_without_go_and_chunks_gofmt(self):
        ctx_nogo = gates.RunContext(mode="check")
        with (
            mock.patch.object(runner.shutil, "which", return_value=None),
            mock.patch.object(gates, "resolve_go_tool", return_value=None),
            mock.patch.object(runner.Path, "is_dir", return_value=True),
            mock.patch.object(gates, "exec_cmd") as exec_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_go(ctx_nogo)
        self.assertFalse(ctx_nogo.failed)
        exec_mock.assert_not_called()

        ctx_fix = gates.RunContext(mode="fix")
        many_go = [f"pkg/f{i}.go" for i in range(501)]
        with (
            mock.patch.object(
                runner.shutil,
                "which",
                side_effect=lambda name: "/usr/bin/gofmt" if name == "gofmt" else None,
            ),
            mock.patch.object(gates, "resolve_go_tool", return_value=None),
            mock.patch.object(gates, "collect_target_files", return_value=many_go) as collect_mock,
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 0, "", "")
            ) as exec_fix,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_go(ctx_fix)
        # 2 gofmt -w chunks (500 + 1)
        collect_mock.assert_called_once()
        self.assertEqual(exec_fix.call_count, 2)
        self.assertEqual(len(exec_fix.call_args_list[0].args[0]), 503)
        self.assertEqual(len(exec_fix.call_args_list[1].args[0]), 4)

    def test_check_go_fails_on_gofmt_and_go_mod_tidy_errors_in_fix_mode(self):
        ctx = gates.RunContext(mode="fix")
        with (
            mock.patch.object(
                runner.shutil,
                "which",
                side_effect=lambda name: name if name in ("gofmt", "go") else None,
            ),
            mock.patch.object(gates, "resolve_go_tool", return_value=None),
            mock.patch.object(gates, "collect_target_files", return_value=["a.go"]),
            mock.patch.object(
                gates,
                "exec_cmd",
                side_effect=[
                    subprocess.CompletedProcess([], 1, stdout="", stderr=""),  # gofmt -w
                    subprocess.CompletedProcess([], 1, stdout="", stderr=""),  # determinism
                    subprocess.CompletedProcess([], 1, stdout="", stderr=""),  # go mod tidy
                ],
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_go(ctx)
        self.assertTrue(ctx.failed)

    def test_check_ui_chunks_and_license(self):
        ctx = gates.RunContext(mode="fix")
        many_ui = [f"cmd/ui/src/f{i}.ts" for i in range(201)]
        with (
            mock.patch.object(gates, "collect_target_files", return_value=many_ui),
            mock.patch.object(gates, "resolve_ui_binary", side_effect=["/p/prettier", "/p/eslint"]),
            mock.patch.object(gates, "run_mode_command") as run_mode,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_ui(ctx)
        self.assertEqual(run_mode.call_count, 2)
        self.assertEqual(run_mode.call_args_list[0].kwargs["chunk_size"], 200)
        self.assertEqual(run_mode.call_args_list[0].kwargs["files"], many_ui)

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

        ctx_fix_lic = gates.RunContext(mode="fix")
        with (
            mock.patch.object(gates, "resolve_go_tool", return_value="/bin/addlicense"),
            mock.patch.object(gates, "git_staged_paths", return_value=["tools/a.py"]),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 1, "", "")
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_license(ctx_fix_lic)
        self.assertTrue(ctx_fix_lic.failed)

    def test_check_python_uses_repo_dot_in_check_and_fails_closed_in_fix(self):
        ctx = gates.RunContext(mode="check")
        with (
            mock.patch.object(
                gates,
                "resolve_local_or_docker",
                return_value=(["docker", "run", "ruff"], True),
            ),
            mock.patch.object(gates, "run_mode_command") as run_mode,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_python(ctx)
        self.assertEqual(run_mode.call_count, 2)
        self.assertIn("--no-cache", run_mode.call_args_list[0].args[1])
        self.assertEqual(run_mode.call_args_list[0].kwargs["files"], ["."])

        ctx_fix = gates.RunContext(mode="fix")
        with (
            mock.patch.object(gates, "collect_target_files", return_value=["a.py"]),
            mock.patch.object(gates, "resolve_local_or_docker", return_value=(["ruff"], False)),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 1, "", "")
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_python(ctx_fix)
        self.assertTrue(ctx_fix.failed)

    def test_resolve_mypy_cmd_and_check_mypy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            venv_mypy = repo / "tools/common/venv/bin/mypy"
            venv_mypy.parent.mkdir(parents=True)
            venv_mypy.write_text("#!/bin/sh\n", encoding="utf-8")
            with mock.patch.object(gates, "REPO_ROOT", repo):
                self.assertEqual(runner._resolve_mypy_cmd(), [str(venv_mypy)])

            venv_mypy.unlink()
            with (
                mock.patch.object(gates, "REPO_ROOT", repo),
                mock.patch.object(gates, "resolve_binary", return_value="/usr/local/bin/mypy"),
            ):
                self.assertEqual(runner._resolve_mypy_cmd(), ["/usr/local/bin/mypy"])

            with (
                mock.patch.object(gates, "REPO_ROOT", repo),
                mock.patch.object(gates, "resolve_binary", return_value=None),
                mock.patch.object(
                    gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 1, "", "")
                ),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(runner._resolve_mypy_cmd(), [])

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

    def test_check_workflows_and_shell(self):
        ctx_wf = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "git_ls_files", return_value=[".github/workflows/lint.yml"]),
            mock.patch.object(gates, "resolve_go_tool", return_value="/bin/actionlint"),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 1, "", "")
            ) as wf_exec,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_workflows(ctx_wf)
        self.assertTrue(ctx_wf.failed)
        self.assertEqual(wf_exec.call_args.kwargs["cwd"], gates.REPO_ROOT)

        ctx_wf_skip = gates.RunContext(mode="fix")
        with (
            mock.patch.object(gates, "staged_paths_match", return_value=False),
            mock.patch.object(gates, "resolve_go_tool") as wf_resolve,
        ):
            runner.check_workflows(ctx_wf_skip)
        wf_resolve.assert_not_called()

        ctx_wf_empty = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "git_ls_files", return_value=[]),
            mock.patch.object(gates, "resolve_go_tool") as wf_empty_resolve,
        ):
            runner.check_workflows(ctx_wf_empty)
        wf_empty_resolve.assert_not_called()

        ctx_wf_missing = gates.RunContext(mode="fix")
        with (
            mock.patch.object(gates, "staged_paths_match", return_value=True),
            mock.patch.object(gates, "resolve_go_tool", return_value=None),
            mock.patch.object(gates, "exec_cmd") as wf_missing_exec,
        ):
            runner.check_workflows(ctx_wf_missing)
        wf_missing_exec.assert_not_called()

        ctx_sh = gates.RunContext(mode="fix")
        with (
            mock.patch.object(gates, "git_staged_paths", return_value=["a.sh", "b.py"]),
            mock.patch.object(gates, "resolve_binary", return_value="/bin/shellcheck"),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 0, "", "")
            ) as exec_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_shell(ctx_sh)
        self.assertFalse(ctx_sh.failed)
        self.assertEqual(exec_mock.call_args.kwargs["cwd"], gates.REPO_ROOT)
        self.assertEqual(
            exec_mock.call_args.args[0], ["/bin/shellcheck", "-S", "style", "-e", "SC1091", "a.sh"]
        )

        many_sh = [f"scripts/s{i}.sh" for i in range(501)]
        ctx_sh_fail = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "git_ls_files", return_value=many_sh),
            mock.patch.object(gates, "resolve_binary", return_value="/bin/shellcheck"),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 1, "", "")
            ) as sh_fail_exec,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_shell(ctx_sh_fail)
        self.assertTrue(ctx_sh_fail.failed)
        self.assertEqual(sh_fail_exec.call_count, 2)

        ctx_sh_missing = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "git_ls_files", return_value=["a.sh"]),
            mock.patch.object(gates, "resolve_binary", return_value=None),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_shell(ctx_sh_missing)
        self.assertTrue(ctx_sh_missing.failed)


class OrchestrationAndMainTest(unittest.TestCase):
    @contextlib.contextmanager
    def _patch_linter_phase_subchecks(self, *, fail_ctx: bool = False) -> Iterator[None]:
        def _maybe_fail(ctx: gates.RunContext) -> None:
            if fail_ctx:
                ctx.fail("lint")

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(runner, "check_bazel", side_effect=_maybe_fail))
            for target, attr in (
                (runner, "check_go"),
                (runner, "check_ui"),
                (runner, "check_license"),
                (runner, "check_python"),
                (runner, "check_mypy"),
                (runner, "check_workflows"),
                (runner, "check_shell"),
                (deploy_checks, "check_deploy"),
            ):
                stack.enter_context(mock.patch.object(target, attr))
            stack.enter_context(mock.patch.object(gates, "run_python_gate"))
            yield

    def test_linter_phase_blocks_on_snapshot_fail_no_autostage_and_restage_fail(self):
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(runner, "_autostage", return_value=False),
            mock.patch("sys.stdout", new_callable=io.StringIO) as snap_buf,
        ):
            self.assertFalse(runner._run_linter_phase("fix"))
        self.assertIn("Unable to snapshot", snap_buf.getvalue())

        with (
            mock.patch.dict(os.environ, {"XMC_PRECOMMIT_NO_AUTOSTAGE": "1"}, clear=False),
            self._patch_linter_phase_subchecks(),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 1, "", "")
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as dirty_buf,
        ):
            self.assertFalse(runner._run_linter_phase("fix"))
        self.assertIn("Linters fixed formatting", dirty_buf.getvalue())

        with (
            mock.patch.dict(os.environ, {}, clear=False),
            self._patch_linter_phase_subchecks(),
            mock.patch.object(runner, "_autostage", side_effect=[True, False]) as auto_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO) as buf,
        ):
            ok = runner._run_linter_phase("fix")
        self.assertFalse(ok)
        self.assertEqual(auto_mock.call_count, 2)
        self.assertIn("unstaged files", buf.getvalue())

    def test_linter_phase_run_modes_and_git_diff_error(self):
        with (
            mock.patch.dict(os.environ, {"XMC_PRECOMMIT_NO_AUTOSTAGE": "0"}, clear=False),
            self._patch_linter_phase_subchecks(),
            mock.patch.object(runner, "_autostage", return_value=True) as auto_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertTrue(runner._run_linter_phase("fix"))
        self.assertEqual([c.args[0] for c in auto_mock.call_args_list], ["snapshot", "restage"])

        with (
            self._patch_linter_phase_subchecks(),
            mock.patch.object(runner, "_autostage") as check_auto_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertTrue(runner._run_linter_phase("check"))
        check_auto_mock.assert_not_called()

        with (
            self._patch_linter_phase_subchecks(fail_ctx=True),
            mock.patch.object(runner, "_autostage") as fail_auto_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO) as fail_buf,
        ):
            self.assertFalse(runner._run_linter_phase("check"))
        fail_auto_mock.assert_not_called()
        self.assertIn("Linter checks failed!", fail_buf.getvalue())

        with (
            mock.patch.dict(os.environ, {"XMC_PRECOMMIT_NO_AUTOSTAGE": "1"}, clear=False),
            self._patch_linter_phase_subchecks(),
            mock.patch.object(
                gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 2, "", "")
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as err_buf,
        ):
            self.assertFalse(runner._run_linter_phase("fix"))
        self.assertIn("git diff failed.", err_buf.getvalue())

    def test_test_phase_missing_bazel_docs_skip_and_lockfile(self):
        ctx_no_bazel_env = mock.patch.object(runner.shutil, "which", return_value=None)
        with ctx_no_bazel_env, mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertFalse(runner._run_test_phase("check"))

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

        with (
            mock.patch.object(runner.shutil, "which", return_value="/usr/bin/bazel"),
            mock.patch.object(
                gates,
                "exec_cmd",
                side_effect=[
                    subprocess.CompletedProcess([], 1, "", ""),  # lockfile already dirty
                    subprocess.CompletedProcess([], 1, "", ""),  # failing tests
                ],
            ) as fail_exec,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertFalse(runner._run_test_phase("check"))
        self.assertEqual(fail_exec.call_count, 2)

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
