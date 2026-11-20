#!/usr/bin/env python3
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

"""Lint rejecting out-of-order ESS migrations (version <= max on base ref)."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.common.tool_utils import HandledError, run_command, run_tool  # noqa: E402

DEFAULT_MIGRATION_DIRS: tuple[str, ...] = (
    "internal/storage/spanner/migrations/ess",
    "internal/storage/postgres/migrations/ess",
)

_MIGRATION_FILE_RE = re.compile(r"^(\d+)_.*\.sql$")
_MIGRATION_DIRECTION_RE = re.compile(r"\.(?:up|down)\.sql$")


def parse_migration_version(path: str) -> int | None:
    """Parse the numeric version prefix from a migration SQL filename.

    Args:
        path: Repository-relative POSIX path or bare filename.

    Returns:
        Integer migration version if the filename matches ``<digits>_*.sql``,
        otherwise ``None``.
    """
    match = _MIGRATION_FILE_RE.match(PurePosixPath(path).name)
    if match is None:
        return None
    return int(match.group(1))


def _group_added_version_names(added_paths: Sequence[str]) -> dict[int, set[str]]:
    """Group added migration base names (without `.up.sql`/`.down.sql`) by version."""
    by_version: dict[int, set[str]] = {}
    for path in added_paths:
        version = parse_migration_version(path)
        if version is None:
            continue
        base_name = _MIGRATION_DIRECTION_RE.sub("", PurePosixPath(path).name)
        by_version.setdefault(version, set()).add(base_name)
    return by_version


def find_out_of_order_migrations(
    migration_dir: str,
    base_paths: Sequence[str],
    current_paths: Sequence[str],
) -> tuple[int, list[dict[str, Any]]]:
    """Identify added migrations that conflict with or precede the base ref maximum.

    Existing numbering gaps on the base ref are tolerated (no contiguity check).
    Modifications to or deletions of existing migration files are ignored.

    Args:
        migration_dir: Repository-relative migration directory being checked.
        base_paths: Migration file paths present at the base ref.
        current_paths: Migration file paths present in the current git index.

    Returns:
        Tuple of ``(base_max, violations)`` where ``base_max`` is the highest
        migration version on the base ref (``0`` if none) and ``violations`` is
        a list of structured violation dictionaries.
    """
    base_versions = [
        version for path in base_paths if (version := parse_migration_version(path)) is not None
    ]
    base_max = max(base_versions, default=0)
    added_paths = sorted(set(current_paths) - set(base_paths))
    added_by_version = _group_added_version_names(added_paths)

    violations: list[dict[str, Any]] = []
    for path in added_paths:
        version = parse_migration_version(path)
        if version is None:
            continue
        bases = added_by_version.get(version, set())
        if len(bases) > 1:
            violations.append(
                {
                    "dir": migration_dir,
                    "path": path,
                    "version": version,
                    "base_max": base_max,
                    "reason": (
                        f"Duplicate migration version {version} detected with different names: "
                        f"{', '.join(sorted(bases))}."
                    ),
                }
            )
            continue
        if base_versions and version <= base_max:
            violations.append(
                {
                    "dir": migration_dir,
                    "path": path,
                    "version": version,
                    "base_max": base_max,
                    "reason": (
                        f"Added migration version {version} must be strictly greater than "
                        f"max version {base_max} on base ref."
                    ),
                }
            )
    return base_max, violations


def _validate_repo_and_base(
    repo_root: Path,
    base_ref: str,
    migration_dirs: Sequence[str],
) -> None:
    """Validate git repository root, target directories, and base ref."""
    if not base_ref or base_ref.startswith("-"):
        raise HandledError(
            f"Invalid base ref: {base_ref!r}",
            "Pass a non-empty --base-ref that does not start with a hyphen.",
        )
    if not repo_root.is_dir() or not (repo_root / ".git").exists():
        raise HandledError(
            f"Invalid git repository root: {repo_root}",
            "Pass --repo-root pointing to the top-level git working tree.",
        )
    missing_dirs = [d for d in migration_dirs if not (repo_root / d).is_dir()]
    if missing_dirs:
        raise HandledError(
            f"Migration directory not found under {repo_root}: {', '.join(missing_dirs)}",
            "Verify --repo-root and --dir paths point to existing migration directories.",
            details={"missing_dirs": missing_dirs},
        )
    run_command(
        ["git", "rev-parse", "--verify", f"{base_ref}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        log_cmd=False,
        errors="replace",
        handled_instruction=(
            f"Base ref '{base_ref}' could not be resolved. Run 'git fetch origin main' "
            "or pass a valid --base-ref."
        ),
    )


def _list_base_paths(repo_root: Path, base_ref: str, migration_dir: str) -> list[str]:
    """List repository-relative paths under ``migration_dir`` at ``base_ref``."""
    proc = run_command(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "ls-tree",
            "-r",
            "--name-only",
            "-z",
            base_ref,
            "--",
            migration_dir,
        ],
        cwd=repo_root,
        capture_output=True,
        log_cmd=False,
        errors="replace",
        handled_instruction=f"Failed to list '{migration_dir}' at base ref '{base_ref}'.",
    )
    return [entry for entry in proc.stdout.split("\0") if entry]


def _list_current_paths(repo_root: Path, migration_dir: str) -> list[str]:
    """List git-indexed repository-relative paths under ``migration_dir``."""
    proc = run_command(
        ["git", "-c", "core.quotePath=false", "ls-files", "-z", "--", migration_dir],
        cwd=repo_root,
        capture_output=True,
        log_cmd=False,
        errors="replace",
        handled_instruction=f"Failed to list indexed files under '{migration_dir}'.",
    )
    return [entry for entry in proc.stdout.split("\0") if entry]


def check_migration_order(args: argparse.Namespace) -> dict[str, Any]:
    """Verify no added ESS migration has a version ``<=`` the base ref maximum.

    Args:
        args: Parsed CLI namespace with ``repo_root``, ``base_ref``, and
            optional ``dirs``.

    Returns:
        Summary dictionary containing ``base_ref``, per-directory ``checked_dirs``
        metadata, and ``violations`` count (``0`` on success).

    Raises:
        HandledError: If repository/base validation fails or any added migration
            is numbered ``<=`` the maximum version on ``base_ref``.
    """
    repo_root = Path(args.repo_root).resolve()
    raw_dirs = args.dirs if args.dirs else DEFAULT_MIGRATION_DIRS
    migration_dirs = tuple(d.strip("/") for d in raw_dirs)
    _validate_repo_and_base(repo_root, args.base_ref, migration_dirs)

    checked_dirs: dict[str, dict[str, int]] = {}
    all_violations: list[dict[str, Any]] = []
    for migration_dir in migration_dirs:
        base_paths = _list_base_paths(repo_root, args.base_ref, migration_dir)
        current_paths = _list_current_paths(repo_root, migration_dir)
        base_max, violations = find_out_of_order_migrations(
            migration_dir, base_paths, current_paths
        )
        added_count = len(set(current_paths) - set(base_paths))
        checked_dirs[migration_dir] = {"base_max": base_max, "added_files": added_count}
        all_violations.extend(violations)

    if all_violations:
        raise HandledError(
            f"Found {len(all_violations)} out-of-order migration file(s) "
            f"(version <= max on '{args.base_ref}').",
            "Renumber the added migration(s) strictly above the max version on "
            f"'{args.base_ref}' (keeping Spanner and PostgreSQL versions in lockstep).",
            details={
                "base_ref": args.base_ref,
                "checked_dirs": checked_dirs,
                "violations": all_violations,
            },
        )
    return {
        "base_ref": args.base_ref,
        "checked_dirs": checked_dirs,
        "violations": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Reject added ESS migrations whose version number is <= the max "
            "migration version on the base ref."
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=str(_REPO_ROOT),
        help="Repository root directory.",
    )
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Git ref representing the target branch (default: origin/main).",
    )
    parser.add_argument(
        "--dir",
        action="append",
        dest="dirs",
        help=(
            "Migration directory relative to repo root (repeatable; defaults to "
            "Spanner and PostgreSQL ESS migration directories)."
        ),
    )
    return parser


def main() -> None:
    """Run the migration ordering linter CLI."""
    run_tool(build_parser(), check_migration_order)


if __name__ == "__main__":
    main()
