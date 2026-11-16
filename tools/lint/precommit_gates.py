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
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


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


def exec_cmd(
    cmd: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
    quiet_out: bool = False,
    quiet_err: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Executes `cmd` with explicit `check=False` and optional stdio redirection."""
    out = subprocess.PIPE if capture else (subprocess.DEVNULL if quiet_out else None)
    err = subprocess.PIPE if capture else (subprocess.DEVNULL if quiet_err else None)
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=out,
        stderr=err,
        text=True,
        check=False,
    )


def git_staged_paths(diff_filter: str, *, no_renames: bool = False) -> list[str]:
    """Lists staged paths matching `diff_filter`, exiting 1 if git fails."""
    cmd = ["git", "-c", "core.quotePath=false", "diff", "--cached", "--name-only"]
    if no_renames:
        cmd.append("--no-renames")
    res = exec_cmd([*cmd, f"--diff-filter={diff_filter}"], capture=True)
    if res.returncode != 0:
        print("❌ git diff --cached failed; cannot select pre-commit checks.", file=sys.stderr)
        raise SystemExit(1)
    return [line for line in res.stdout.splitlines() if line]


def staged_paths_match(diff_filter: str, pattern: str) -> bool:
    """Returns True when any staged path in `diff_filter` matches regex `pattern`."""
    regex = re.compile(pattern)
    return any(regex.search(path) for path in git_staged_paths(diff_filter, no_renames=True))


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
    if exec_cmd([sys.executable, gate.script], quiet_out=True).returncode != 0:
        ctx.fail(gate.failure_msg)
        return
    if gate.baseline_path and ctx.mode == "fix":
        exec_cmd([sys.executable, gate.script, "--update-baseline"], quiet_out=True)
        if exec_cmd(["git", "diff", "--quiet", "--", gate.baseline_path]).returncode != 0:
            exec_cmd(["git", "add", "--", gate.baseline_path])
            print(f"--> Lowered and staged {gate.baseline_path}.")


def main(argv: Sequence[str] | None = None) -> int:
    """Runs all declarative `PYTHON_GATES` for the requested hook mode."""
    parser = argparse.ArgumentParser(description="Run declarative Python pre-commit gates.")
    parser.add_argument("--mode", choices=("fix", "check"), default="fix")
    args = parser.parse_args(list(argv) if argv is not None else None)
    ctx = RunContext(mode=args.mode)
    for gate in PYTHON_GATES:
        run_python_gate(ctx, gate)
    return 1 if ctx.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
