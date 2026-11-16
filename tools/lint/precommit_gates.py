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

"""Shared execution gates and declarative Python linter runner for pre-commit hooks."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMMON_EXCLUDES = frozenset({".git", "node_modules", ".angular", "venv", ".venv", "ctk-out"})


@dataclass(frozen=True)
class PythonGate:
    """Declarative specification for a standalone Python lint script gate."""

    banner: str
    script: str
    failure_msg: str
    staged_am_pattern: str | None = None
    check_deletes: bool = False
    baseline_path: str | None = None


PYTHON_GATES: tuple[PythonGate, ...] = (
    PythonGate(
        banner="--> Running google3 Legacy Import Ratchet...",
        script="tools/lint/check_legacy_imports.py",
        failure_msg="google3 legacy import ratchet failed (see JSON error above).",
        baseline_path="tools/lint/legacy_imports_baseline.json",
    ),
    PythonGate(
        banner="--> Checking Nightly E2E Verification Coverage for examples/...",
        script="tools/nightly_tests/check_examples_coverage.py",
        failure_msg="Nightly examples verification coverage check failed.",
    ),
    PythonGate(
        banner="--> Checking Nightly Matrix Launch Args vs Verifier Expectations...",
        script="tools/nightly_tests/check_matrix_contract.py",
        failure_msg="Nightly matrix contract check failed (see JSON error above).",
    ),
    PythonGate(
        banner="--> Checking Agent Doc Path References...",
        script="tools/lint/check_agent_doc_paths.py",
        failure_msg="Agent docs reference non-existent repository paths (see JSON error above).",
        staged_am_pattern=r"^\.agents/.*\.md$|(^|/)AGENTS\.md$",
        check_deletes=True,
    ),
    PythonGate(
        banner="--> Checking UI & Workflow File Naming Conventions...",
        script="tools/lint/check_file_naming.py",
        failure_msg="UI or workflow file naming check failed (see JSON error above).",
    ),
    PythonGate(
        banner="--> Checking Nightly UI Selector Contract...",
        script="tools/lint/check_ui_selector_contract.py",
        failure_msg=(
            "Nightly UI selectors reference missing DOM or unannotated copy (see JSON error above)."
        ),
    ),
)


@dataclass
class RunContext:
    """Tracks hook execution mode (`fix` or `check`) and cumulative failure state."""

    mode: str
    failed: bool = False

    def fail(self, message: str) -> None:
        """Prints an error banner and marks the hook run failed."""
        print(f"❌ {message}")
        self.failed = True

    def warn_missing(self, warning: str, hint: str = "") -> None:
        """Prints a missing-tool warning and fails when running in `check` mode."""
        print(f"--> WARNING: {warning}")
        if hint:
            print(f"   {hint}")
        if self.mode == "check":
            self.failed = True


def exec_cmd(
    cmd: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
    quiet_out: bool = False,
    quiet_err: bool = False,
    errors: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Executes `cmd` cleanly, returning returncode `127` on `OSError`."""
    out = subprocess.PIPE if capture else (subprocess.DEVNULL if quiet_out else None)
    err = subprocess.PIPE if capture else (subprocess.DEVNULL if quiet_err else None)
    cmd_list = list(cmd)
    text_errors = errors if errors is not None else ("surrogateescape" if capture else None)
    try:
        return subprocess.run(
            cmd_list,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdout=out,
            stderr=err,
            text=True,
            errors=text_errors,
            check=False,
        )
    except OSError as exc:
        if not capture and not quiet_err and cmd_list:
            print(f"❌ Failed to run {cmd_list[0]}: {exc}", file=sys.stderr)
        return subprocess.CompletedProcess(cmd_list, 127, stdout="", stderr=str(exc))


def git_staged_paths(diff_filter: str, *, no_renames: bool = False) -> list[str]:
    """Lists staged paths matching `diff_filter`, exiting 1 if git fails."""
    cmd = ["git", "diff", "--cached", "--name-only", "-z"]
    if no_renames:
        cmd.append("--no-renames")
    res = exec_cmd(
        [*cmd, f"--diff-filter={diff_filter}"],
        cwd=REPO_ROOT,
        capture=True,
        errors="surrogateescape",
    )
    if res.returncode != 0:
        print("❌ git diff --cached failed; cannot select pre-commit checks.", file=sys.stderr)
        if res.stderr:
            print(res.stderr.strip(), file=sys.stderr)
        raise SystemExit(1)
    return [path for path in res.stdout.split("\0") if path]


def staged_paths_match(diff_filter: str, pattern: str) -> bool:
    """Returns True when any staged path in `diff_filter` matches regex `pattern`."""
    regex = re.compile(pattern)
    return any(regex.search(path) for path in git_staged_paths(diff_filter, no_renames=True))


def walk_repo_files(
    roots: Sequence[str],
    *,
    suffixes: Sequence[str] = (),
    excluded_dirs: frozenset[str] = COMMON_EXCLUDES,
    base_dir: Path = REPO_ROOT,
) -> list[str]:
    """Walks `roots` relative to `base_dir` and returns sorted unique matching paths."""
    matched: set[str] = set()
    suffix_tuple = tuple(suffixes)
    for root in roots:
        root_path = base_dir if root == "." else base_dir / root
        if not root_path.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = sorted(d for d in dirnames if d not in excluded_dirs)
            matched.update(
                Path(dirpath, name).relative_to(base_dir).as_posix()
                for name in filenames
                if not suffix_tuple or name.endswith(suffix_tuple)
            )
    return sorted(matched)


def collect_target_files(
    mode: str,
    *,
    staged_regex: str,
    roots: Sequence[str] = (".",),
    suffixes: Sequence[str] = (),
    excluded_dirs: frozenset[str] = COMMON_EXCLUDES,
    base_dir: Path = REPO_ROOT,
) -> list[str]:
    """Collects repo files under `roots` in `check` mode or staged `ACM` files in `fix` mode."""
    if mode == "check":
        return walk_repo_files(
            roots, suffixes=suffixes, excluded_dirs=excluded_dirs, base_dir=base_dir
        )
    if not roots:
        return []
    staged = git_staged_paths("ACM", no_renames=True)
    normalized_roots = tuple(
        PurePosixPath(r.replace(os.sep, "/")).as_posix().rstrip("/") if r else "" for r in roots
    )
    if "." not in normalized_roots:
        staged = [
            path
            for path in staged
            if any(path == root or path.startswith(f"{root}/") for root in normalized_roots if root)
        ]
    regex = re.compile(staged_regex)
    return [path for path in staged if regex.search(path)]


def should_run_scope(
    mode: str,
    *,
    roots: Sequence[str],
    suffixes: Sequence[str],
    fix_pattern: str,
    base_dir: Path = REPO_ROOT,
) -> bool:
    """Returns True when `roots` contain files in `check` mode or `AMD` paths match `fix_pattern`."""
    if mode == "check":
        return bool(walk_repo_files(roots, suffixes=suffixes, base_dir=base_dir))
    return staged_paths_match("AMD", fix_pattern)


def docker_prefix(image: str, mount: str, *, entrypoint: str | None = None) -> list[str]:
    """Builds a `docker run --rm` command prefix mounting `REPO_ROOT` at `mount`."""
    cmd = ["docker", "run", "--rm", "-v", f"{REPO_ROOT}:{mount}", "-w", mount]
    if entrypoint is not None:
        cmd.extend(["--entrypoint", entrypoint])
    return [*cmd, image]


def resolve_local_or_docker(
    ctx: RunContext,
    tool: str,
    *,
    docker_image: str,
    mount: str,
    missing_label: str,
    entrypoint: str | None = None,
) -> tuple[list[str], bool] | None:
    """Returns `(cmd_prefix, is_docker)` for `tool`, falling back to `docker_image` or None."""
    if local := shutil.which(tool):
        return [local], False
    if shutil.which("docker"):
        return docker_prefix(docker_image, mount, entrypoint=entrypoint), True
    ctx.warn_missing(f"{tool} or docker not found. {missing_label}")
    return None


def run_mode_command(
    ctx: RunContext,
    cmd_base: Sequence[str],
    *,
    check_args: Sequence[str],
    fix_args: Sequence[str],
    failure_msg: str,
    ignore_fix_error: bool = False,
    cwd: str | Path | None = None,
) -> None:
    """Runs `cmd_base` with `check_args` or `fix_args` and records failures."""
    args = check_args if ctx.mode == "check" else fix_args
    res = exec_cmd([*cmd_base, *args], cwd=cwd)
    if res.returncode != 0 and (ctx.mode == "check" or not ignore_fix_error):
        ctx.fail(failure_msg)


def _update_baseline(ctx: RunContext, gate: PythonGate) -> None:
    if not gate.baseline_path or ctx.mode != "fix":
        return
    if (
        exec_cmd(
            [sys.executable, gate.script, "--update-baseline"], cwd=REPO_ROOT, quiet_out=True
        ).returncode
        != 0
    ):
        ctx.fail(f"Failed to update baseline for {gate.script}.")
        return
    diff_res = exec_cmd(["git", "diff", "--quiet", "--", gate.baseline_path], cwd=REPO_ROOT)
    if diff_res.returncode not in (0, 1):
        ctx.fail(f"git diff failed checking {gate.baseline_path}.")
        return
    if diff_res.returncode == 1:
        if exec_cmd(["git", "add", "--", gate.baseline_path], cwd=REPO_ROOT).returncode != 0:
            ctx.fail(f"Failed to stage updated {gate.baseline_path}.")
            return
        print(f"--> Lowered and staged {gate.baseline_path}.")


def run_python_gate(ctx: RunContext, gate: PythonGate) -> None:
    """Executes one declarative `PythonGate` and updates its baseline in `fix` mode."""
    if (
        ctx.mode == "fix"
        and gate.staged_am_pattern is not None
        and not staged_paths_match("AM", gate.staged_am_pattern)
        and not (gate.check_deletes and staged_paths_match("D", "."))
    ):
        return
    print(gate.banner)
    if exec_cmd([sys.executable, gate.script], cwd=REPO_ROOT, quiet_out=True).returncode != 0:
        ctx.fail(gate.failure_msg)
        return
    _update_baseline(ctx, gate)


def main(argv: Sequence[str] | None = None) -> int:
    """Runs all declarative `PYTHON_GATES` for the requested hook mode."""
    os.environ.setdefault("XMC_BYPASS_VENV", "1")
    parser = argparse.ArgumentParser(description="Run declarative Python pre-commit gates.")
    parser.add_argument("--mode", choices=("fix", "check"), default="fix")
    args = parser.parse_args(list(argv) if argv is not None else None)
    ctx = RunContext(mode=args.mode)
    for gate in PYTHON_GATES:
        run_python_gate(ctx, gate)
    return 1 if ctx.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
