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

"""Verifies that the repository CODEOWNERS matches the OWNERS reviewer pool."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.common.tool_utils import HandledError, run_tool  # noqa: E402

PROTECTED_PATHS: tuple[str, ...] = (
    "/CODEOWNERS",
    "/OWNERS",
    "/deploy/profiles/locked_profiles.yaml",
    "/deploy/profiles/BUILD.bazel",
    "/deploy/profiles/stable-2026a/",
    "/tools/components/check_profile_lock.py",
    "/tools/components/BUILD.bazel",
    "/tools/lint/check_codeowners_alignment.py",
    "/tools/lint/BUILD.bazel",
    "/test/helm/profiles/stable2026a/",
)


def _resolve_default_repo_root() -> Path:
    """Resolve repository root in-tree or under Bazel runfiles."""
    if (_REPO_ROOT / "OWNERS").is_file() and (_REPO_ROOT / "CODEOWNERS").is_file():
        return _REPO_ROOT
    for env_key in ("BUILD_WORKSPACE_DIRECTORY", "TEST_SRCDIR"):
        raw = os.environ.get(env_key, "").strip()
        if not raw:
            continue
        candidate = Path(raw)
        if (candidate / "OWNERS").is_file():
            return candidate
        workspace = os.environ.get("TEST_WORKSPACE", "_main").strip() or "_main"
        ws_candidate = candidate / workspace
        if (ws_candidate / "OWNERS").is_file():
            return ws_candidate
    if (Path.cwd() / "OWNERS").is_file():
        return Path.cwd()
    return _REPO_ROOT


def _strip_comments(text: str) -> list[str]:
    """Return non-empty lines with trailing `#` comments removed."""
    cleaned: list[str] = []
    for raw_line in text.splitlines():
        entry = raw_line.split("#", 1)[0].strip()
        if entry:
            cleaned.append(entry)
    return cleaned


def check_codeowners_alignment(args: argparse.Namespace) -> dict[str, Any]:
    """Validate that CODEOWNERS rules match the scoped paths and OWNERS reviewer pool."""
    repo_root = Path(args.repo_root).resolve() if args.repo_root else _resolve_default_repo_root()
    owners_file = repo_root / "OWNERS"
    codeowners_file = repo_root / "CODEOWNERS"

    if not owners_file.is_file() or not codeowners_file.is_file():
        raise HandledError(
            f"Missing OWNERS ({owners_file.is_file()}) or CODEOWNERS ({codeowners_file.is_file()}) under {repo_root}.",
            "Ensure both OWNERS and CODEOWNERS exist at the repository root.",
            details={
                "repo_root": str(repo_root),
                "owners_exists": owners_file.is_file(),
                "codeowners_exists": codeowners_file.is_file(),
            },
        )

    owners = _strip_comments(owners_file.read_text(encoding="utf-8"))
    if not owners:
        raise HandledError(
            "OWNERS contains no active reviewer handles.",
            "Populate OWNERS with at least one GitHub username.",
        )
    codeowner_lines = _strip_comments(codeowners_file.read_text(encoding="utf-8"))
    expected_owners = " ".join(f"@{u.lstrip('@')}" for u in owners)
    expected_lines = [f"{path} {expected_owners}" for path in PROTECTED_PATHS]

    if codeowner_lines != expected_lines:
        raise HandledError(
            "CODEOWNERS does not match the scoped rules for the OWNERS reviewer pool.",
            "Update CODEOWNERS so every protected path lists the exact @<user> pool from OWNERS.",
            details={
                "expected": expected_lines,
                "actual": codeowner_lines,
            },
        )

    return {
        "owners_count": len(owners),
        "protected_paths_count": len(expected_lines),
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Verify that CODEOWNERS aligns with the OWNERS reviewer pool.",
    )
    parser.add_argument(
        "--repo-root",
        default="",
        help="Repository root directory containing OWNERS and CODEOWNERS.",
    )
    return parser


def main() -> None:
    """Run the CODEOWNERS alignment linter CLI."""
    run_tool(build_parser(), check_codeowners_alignment)


if __name__ == "__main__":
    main()
