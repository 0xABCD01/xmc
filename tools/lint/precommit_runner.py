#!/usr/bin/env python3
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

"""CLI runner orchestrating all XMC pre-commit linter and affected-test gates."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.lint import (  # noqa: E402
    precommit_deploy_checks as deploy_checks,
    precommit_gates as gates,
)

_LICENSE_IGNORES = (
    "**/node_modules/**",
    "**/.angular/**",
    "**/dist/**",
    "**/venv/**",
    "**/ctk-out/**",
    "**.json",
    "**.lock",
    "pyproject.toml",
    "package.json",
    "go.mod",
    "go.sum",
)


def check_bazel(ctx: gates.RunContext) -> None:
    """Formats or validates Bazel files via `buildifier`."""
    buildifier = gates.resolve_go_tool(
        ctx,
        "buildifier",
        "buildifier not found. To install: go install github.com/bazelbuild/buildtools/buildifier@latest",
    )
    if not buildifier:
        return
    print("--> Running Bazel Formatter & Linter (Buildifier)...")
    files = gates.walk_repo_files(
        ["."],
        suffixes=(".bzl",),
        exact_names=("BUILD", "BUILD.bazel", "WORKSPACE", "MODULE.bazel"),
    )
    if not files:
        return
    gates.run_mode_command(
        ctx,
        [buildifier],
        check_args=["-lint=warn", "-warnings=unused-variable", "-mode=check", *files],
        fix_args=["-lint=fix", "-warnings=unused-variable", "-mode=fix", *files],
        failure_msg="Bazel buildifier check failed.",
    )


def check_go(ctx: gates.RunContext) -> None:
    """Runs `gofmt`, `golangci-lint`, Go determinism checker, and `go mod tidy`."""
    if shutil.which("gofmt"):
        print("--> Running Go Formatter (gofmt)...")
        res = gates.exec_cmd(["gofmt", "-s", "-l", "."], capture=True, quiet_err=True)
        bad = [line for line in res.stdout.splitlines() if line and "vendor/" not in line]
        if bad:
            print("❌ Unformatted Go files found:\n" + "\n".join(bad))
            if ctx.mode == "check":
                ctx.failed = True
        if ctx.mode == "fix" and (
            go_files := gates.walk_repo_files(
                ["."], suffixes=(".go",), excluded_dirs=frozenset({"vendor", ".git"})
            )
        ):
            for idx in range(0, len(go_files), 500):
                gates.exec_cmd(["gofmt", "-s", "-w", *go_files[idx : idx + 500]])
    if linter := gates.resolve_go_tool(
        ctx,
        "golangci-lint",
        "golangci-lint not found. Go linting skipped.",
        "To install: go install github.com/golangci/golangci-lint/cmd/golangci-lint@v1.64.6",
    ):
        print("--> Running Golang Linter (golangci-lint)...")
        msg = (
            "golangci-lint check failed."
            if ctx.mode == "check"
            else "golangci-lint run --fix failed or found unfixed issues."
        )
        gates.run_mode_command(
            ctx, [linter], check_args=["run"], fix_args=["run", "--fix"], failure_msg=msg
        )
    if not shutil.which("go"):
        return
    if os.path.isdir("tools/lint/check_go_determinism"):
        print("--> Running Go Test Determinism & State Leakage Checker...")
        if gates.exec_cmd(["go", "run", "./tools/lint/check_go_determinism"]).returncode != 0:
            ctx.failed = True
    print("--> Checking Go Module Dependencies (go mod tidy)...")
    if ctx.mode != "check":
        gates.exec_cmd(["go", "mod", "tidy"])
    elif (
        gates.exec_cmd(["go", "mod", "tidy", "-diff"], quiet_out=True, quiet_err=True).returncode
        != 0
    ):
        ctx.fail("Unused or missing Go module dependencies found. Run 'go mod tidy' to sync.")


def check_ui(ctx: gates.RunContext) -> None:
    """Runs Prettier and ESLint on Angular UI sources."""
    files = gates.collect_target_files(
        ctx.mode,
        staged_regex=r"^cmd/ui/.*\.(ts|html|scss)$",
        roots=("cmd/ui",),
        suffixes=(".ts", ".html", ".scss"),
    )
    if not files:
        return
    if prettier := gates.resolve_ui_binary(ctx, "prettier"):
        print("--> Running UI Formatter (Prettier)...")
        gates.run_mode_command(
            ctx,
            [prettier, "--config", "cmd/ui/.prettierrc"],
            check_args=["--check", *files],
            fix_args=["--write", *files],
            failure_msg="Prettier check failed.",
        )
    if eslint := gates.resolve_ui_binary(ctx, "eslint"):
        print("--> Running UI Linter (ESLint)...")
        gates.run_mode_command(
            ctx,
            [eslint, "--config", "cmd/ui/eslint.config.js"],
            check_args=files,
            fix_args=["--fix", *files],
            failure_msg="ESLint check failed.",
        )


def check_license(ctx: gates.RunContext) -> None:
    """Verifies or adds Apache license headers using `addlicense`."""
    addlicense = gates.resolve_go_tool(
        ctx, "addlicense", "addlicense not found. License check skipped."
    )
    if not addlicense:
        return
    print("--> Running License Header Check (addlicense)...")
    flags = ["-c", "Google LLC", "-l", "apache"]
    for pattern in _LICENSE_IGNORES:
        flags.extend(["-ignore", pattern])
    if ctx.mode == "check":
        if gates.exec_cmd([addlicense, "-check", *flags, "."]).returncode != 0:
            ctx.fail("License header check failed.")
    elif staged := gates.git_staged_paths("ACMR"):
        gates.exec_cmd([addlicense, *flags, *staged])


def check_python(ctx: gates.RunContext) -> None:
    """Runs Ruff lint and formatting locally or inside Docker."""
    files = gates.collect_target_files(
        ctx.mode, staged_regex=r"\.py$", roots=(".",), suffixes=(".py",)
    )
    if not files:
        return
    resolved = gates.resolve_local_or_docker(
        ctx,
        "ruff",
        docker_image="ghcr.io/astral-sh/ruff:latest",
        mount="/io",
        missing_label="Python linting skipped.",
        extra_dirs=(Path.home() / ".local/bin",),
        as_user=True,
    )
    if not resolved:
        return
    base, is_docker = resolved
    print(
        "--> Running Ruff via Docker container..."
        if is_docker
        else "--> Running Python Linter & Formatter (Ruff)..."
    )
    extra = ["--no-cache"] if is_docker else []
    if ctx.mode == "check":
        if gates.exec_cmd([*base, "check", *extra, *files]).returncode != 0:
            ctx.fail("Ruff lint check failed.")
        if gates.exec_cmd([*base, "format", "--check", *extra, *files]).returncode != 0:
            ctx.fail("Ruff format check failed.")
        return
    gates.exec_cmd([*base, "check", "--fix", *extra, *files])
    gates.exec_cmd([*base, "format", *extra, *files])


def _resolve_mypy_cmd() -> list[str]:
    for path in ("./tools/common/venv/bin/mypy", "./tools/venv/bin/mypy"):
        if os.access(path, os.X_OK):
            return [path]
    if found := gates.resolve_binary("mypy", (Path.home() / ".local/bin",)):
        return [found]
    mod = [sys.executable, "-m", "mypy"]
    if gates.exec_cmd([*mod, "--version"], quiet_out=True, quiet_err=True).returncode == 0:
        return mod
    print("--> mypy not found; attempting automatic installation into ./tools/common/venv...")
    venv_pip = "./tools/common/venv/bin/pip"
    if not os.access(venv_pip, os.X_OK):
        gates.exec_cmd(
            [sys.executable, "-m", "venv", "./tools/common/venv"], quiet_out=True, quiet_err=True
        )
    install = [venv_pip, "install", "--quiet", "mypy"]
    if (
        os.access(venv_pip, os.X_OK)
        and gates.exec_cmd(install, quiet_out=True, quiet_err=True).returncode == 0
    ):
        return ["./tools/common/venv/bin/mypy"]
    return []


def check_mypy(ctx: gates.RunContext) -> None:
    """Runs mypy static type checker when Python files or config change."""
    if ctx.mode == "fix" and not gates.staged_paths_match(
        "AMD", r"\.pyi?$|^pyproject\.toml$|(^|/)\.gitignore$"
    ):
        return
    if not (cmd := _resolve_mypy_cmd()):
        ctx.fail(
            "ERROR: mypy not found and automatic installation failed. "
            "Install mypy (e.g. 'pip install mypy') to proceed."
        )
        return
    print("--> Running Mypy Static Type Checker...")
    if gates.exec_cmd(cmd).returncode != 0:
        ctx.fail("Mypy type check failed.")


def _autostage(subcommand: str, state_path: str) -> bool:
    cmd = [sys.executable, "tools/lint/precommit_autostage.py", subcommand, "--state", state_path]
    return gates.exec_cmd(cmd, quiet_out=True).returncode == 0


def _run_linter_phase(mode: str) -> bool:
    ctx = gates.RunContext(mode=mode)
    autostage = mode == "fix" and os.environ.get("XMC_PRECOMMIT_NO_AUTOSTAGE", "0") != "1"
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = str(Path(tmpdir) / "autostage.json") if autostage else None
        if state_path is not None:
            _autostage("snapshot", state_path)
        check_bazel(ctx)
        check_go(ctx)
        for gate in gates.PYTHON_GATES:
            gates.run_python_gate(ctx, gate)
        check_ui(ctx)
        check_license(ctx)
        check_python(ctx)
        check_mypy(ctx)
        deploy_checks.check_deploy(ctx)
        if mode == "fix" and state_path is not None and not _autostage("restage", state_path):
            print(
                '\n❌ PRE-COMMIT HOOK FAILED: Linters fixed files that were not fully staged (see "blocked" above).\n'
                "   Please review the modified files (git diff), stage them, and re-run git commit."
            )
            return False
        if (
            mode == "fix"
            and state_path is None
            and gates.exec_cmd(["git", "diff", "--quiet"]).returncode != 0
        ):
            print(
                "\n❌ PRE-COMMIT HOOK FAILED: Linters automatically fixed formatting issues in your code!\n"
                "   Please review the modified files (git diff), stage them (git add -u), and re-run git commit."
            )
            return False
    if ctx.failed:
        print("=== ❌ Linter checks failed! ===")
        return False
    return True


def _run_test_phase(mode: str) -> bool:
    ctx = gates.RunContext(mode=mode)
    if mode == "fix":
        staged = gates.git_staged_paths("ACDMRTUXB")
        if staged and all(p.endswith(".md") or p.startswith("docs/") for p in staged):
            print("--> Skipping Affected Bazel Unit Tests: every staged path is documentation.")
            return True
    if not shutil.which("bazel"):
        ctx.warn_missing("bazel not found. Affected Bazel unit tests skipped.")
        return not ctx.failed
    print("--> Running Affected Bazel Unit Tests...")
    lockfile_clean = (
        gates.exec_cmd(["git", "diff", "--quiet", "--", "MODULE.bazel.lock"]).returncode == 0
    )
    test_cmd = [sys.executable, "./tools/run_affected_tests.py", "--skip-emulator-check"]
    if gates.exec_cmd(test_cmd, quiet_out=True).returncode != 0:
        ctx.fail("Affected Bazel unit tests failed.")
    if lockfile_clean:
        gates.exec_cmd(["git", "checkout", "--", "MODULE.bazel.lock"], quiet_err=True)
    if ctx.failed:
        print("=== ❌ Pre-commit checks failed! ===")
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    """Runs all XMC pre-commit checks and returns 0 on success or 1 on failure."""
    os.environ.setdefault("XMC_BYPASS_VENV", "1")
    parser = argparse.ArgumentParser(description="Run XMC pre-commit hooks.")
    parser.add_argument("--check", action="store_true", help="Run in read-only check mode.")
    mode = "check" if parser.parse_args(argv).check else "fix"
    print(f"=== Running XMC Pre-Commit Linter Hooks (Mode: {mode}) ===")
    if not _run_linter_phase(mode) or not _run_test_phase(mode):
        return 1
    print("=== ✅ All pre-commit checks passed! ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
