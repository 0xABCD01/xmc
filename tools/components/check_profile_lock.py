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

"""Enforces GA release locks on component profiles under deploy/profiles/."""

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ensure repo root is on sys.path for direct script invocation.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.common.tool_utils import HandledError, run_command, run_tool  # noqa: E402

LOCKED_PROFILES_REL_PATH = "deploy/profiles/locked_profiles.yaml"
PROFILES_DIR_PREFIX = "deploy/profiles/"
OVERRIDE_TRAILER_KEY = "PROFILE-LOCK-OVERRIDE"
OVERRIDE_ENV_VAR = "PROFILE_LOCK_OVERRIDE"
_ZERO_SHA = "0" * 40
_TRAILER_RE = re.compile(
    rf"^\s*{re.escape(OVERRIDE_TRAILER_KEY)}\s*:\s*(\S+)(?:\s+(.*\S))?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LockedProfile:
    """Metadata for a GA-locked component profile in locked_profiles.yaml."""

    name: str
    locked_since: str
    chart_digest: str = ""
    embedded_charts_digest: str = ""


@dataclass(frozen=True)
class LockViolation:
    """Represents a locked profile modified without a valid override trailer."""

    profile: str
    locked_since: str
    files: tuple[str, ...]


def _strip_yaml_comment_and_quotes(raw: str) -> str:
    """Strips inline comments and surrounding quotes from a scalar YAML value."""
    val = raw.strip()
    if val.startswith(('"', "'")):
        quote = val[0]
        end_idx = val.find(quote, 1)
        if end_idx != -1:
            return val[1:end_idx]
    val = val.split("#", 1)[0].strip()
    if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
        return val[1:-1]
    return val


def _parse_inline_mapping(profile_name: str, raw_body: str) -> dict[str, str]:
    """Parses a single-line flow mapping like `{locked_since: "1.0.0"}`."""
    inner = raw_body.strip()[1:-1].strip()
    if not inner:
        return {}
    fields: dict[str, str] = {}
    for item in inner.split(","):
        if ":" not in item:
            raise HandledError(
                f"Malformed inline mapping for profile '{profile_name}' in {LOCKED_PROFILES_REL_PATH}.",
                instruction=f"Use 'key: value' entries under '{profile_name}' in {LOCKED_PROFILES_REL_PATH}.",
            )
        k, v = item.split(":", 1)
        fields[k.strip()] = _strip_yaml_comment_and_quotes(v)
    return fields


def _build_locked_profile(name: str, fields: Mapping[str, str]) -> LockedProfile:
    """Constructs and validates a LockedProfile from parsed YAML key/value pairs."""
    locked_since = fields.get("locked_since", "").strip()
    if not locked_since:
        raise HandledError(
            f"Locked profile '{name}' in {LOCKED_PROFILES_REL_PATH} is missing 'locked_since'.",
            instruction=f"Set 'locked_since: \"<xmc-version>\"' for '{name}' in {LOCKED_PROFILES_REL_PATH}.",
        )
    return LockedProfile(
        name=name,
        locked_since=locked_since,
        chart_digest=fields.get("chart_digest", "").strip(),
        embedded_charts_digest=fields.get("embedded_charts_digest", "").strip(),
    )


def parse_locked_profiles_yaml(content: str) -> dict[str, LockedProfile]:
    """Parses deploy/profiles/locked_profiles.yaml into LockedProfile records.

    Args:
        content: Raw YAML string from locked_profiles.yaml.

    Returns:
        Mapping of profile name to LockedProfile metadata.
    """
    raw_entries: dict[str, dict[str, str]] = {}
    current_profile: str | None = None

    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if ":" not in stripped:
            raise HandledError(
                f"Invalid line in {LOCKED_PROFILES_REL_PATH}: {stripped}",
                instruction=f"Fix YAML syntax in {LOCKED_PROFILES_REL_PATH}.",
            )
        key, rest = stripped.split(":", 1)
        key = key.strip()
        val_part = rest.strip()
        if not val_part.startswith(("'", '"')):
            val_part = val_part.split("#", 1)[0].strip()

        if indent == 0:
            if val_part.startswith("{") and val_part.endswith("}"):
                raw_entries[key] = _parse_inline_mapping(key, val_part)
                current_profile = None
                continue
            raw_entries[key] = {}
            current_profile = key
            continue

        if current_profile is None:
            raise HandledError(
                f"Unexpected indentation in {LOCKED_PROFILES_REL_PATH}: {raw_line}",
                instruction=f"Ensure nested keys belong to a top-level profile in {LOCKED_PROFILES_REL_PATH}.",
            )
        raw_entries[current_profile][key] = _strip_yaml_comment_and_quotes(val_part)

    return {name: _build_locked_profile(name, fields) for name, fields in raw_entries.items()}


def _read_git_file_if_present(repo_root: Path, rev: str, rel_path: str) -> str | None:
    """Returns file contents at `<rev>:<rel_path>`, or None if rev/file does not exist."""
    proc = subprocess.run(
        ["git", "show", f"{rev}:{rel_path}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def load_locked_profiles(
    repo_root: Path,
    base_ref: str | None = None,
) -> dict[str, LockedProfile]:
    """Loads locked profiles from the working tree and merges any entries from base_ref.

    Args:
        repo_root: Repository root directory.
        base_ref: Optional base git reference to union locked profiles from.

    Returns:
        Mapping of profile name to LockedProfile metadata.
    """
    lock_file = repo_root / LOCKED_PROFILES_REL_PATH
    if not lock_file.is_file():
        raise HandledError(
            f"Locked profiles registry not found at {lock_file}.",
            instruction=f"Ensure {LOCKED_PROFILES_REL_PATH} exists in the repository.",
        )
    profiles = parse_locked_profiles_yaml(lock_file.read_text(encoding="utf-8"))
    if base_ref:
        base_content = _read_git_file_if_present(repo_root, base_ref, LOCKED_PROFILES_REL_PATH)
        if base_content:
            for name, entry in parse_locked_profiles_yaml(base_content).items():
                profiles.setdefault(name, entry)
    return profiles


def classify_locked_profile_changes(
    changed_files: Sequence[str],
    locked_profiles: Mapping[str, LockedProfile],
) -> dict[str, list[str]]:
    """Groups changed file paths under any locked profile directory by profile name.

    Args:
        changed_files: Repository-relative paths touched in the diff.
        locked_profiles: Active locked profile registry.

    Returns:
        Sorted mapping of locked profile name to sorted list of touched file paths.
    """
    touched: dict[str, set[str]] = {}
    for raw_path in changed_files:
        norm = raw_path.replace("\\", "/").strip().lstrip("./")
        if not norm.startswith(PROFILES_DIR_PREFIX):
            continue
        remainder = norm[len(PROFILES_DIR_PREFIX) :]
        parts = [p for p in remainder.split("/") if p]
        if len(parts) < 2:
            # Files directly under deploy/profiles/ (e.g. locked_profiles.yaml) are outside profile dirs.
            continue
        profile_name = parts[0]
        if profile_name in locked_profiles:
            touched.setdefault(profile_name, set()).add(norm)

    return {prof: sorted(paths) for prof, paths in sorted(touched.items())}


def parse_override_trailers(messages: Sequence[str]) -> dict[str, str]:
    """Extracts valid `PROFILE-LOCK-OVERRIDE: <profile> <reason>` trailers from commit messages.

    Args:
        messages: Sequence of commit message strings to inspect.

    Returns:
        Mapping of profile name to non-empty override reason.
    """
    overrides: dict[str, str] = {}
    for msg in messages:
        for line in msg.splitlines():
            match = _TRAILER_RE.match(line)
            if not match:
                continue
            profile_name = match.group(1).strip()
            reason = (match.group(2) or "").strip()
            if profile_name and reason:
                overrides[profile_name] = reason
    return overrides


def get_staged_files(repo_root: Path) -> list[str]:
    """Returns staged file paths (including additions, modifications, deletions, and renames).

    Args:
        repo_root: Repository root directory.

    Returns:
        Sorted list of staged repository-relative file paths.
    """
    res = run_command(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "-z",
        ],
        cwd=repo_root,
        capture_output=True,
        check=True,
        log_cmd=False,
        log_on_error=False,
        handled_instruction="Ensure git is available and run from a valid repository checkout.",
    )
    return sorted({entry for entry in res.stdout.split("\0") if entry})


def _resolve_effective_base_ref(repo_root: Path, base_ref: str | None) -> str:
    """Resolves a usable git base reference for branch diff comparisons."""
    candidate = (base_ref or "").strip()
    if not candidate or candidate == _ZERO_SHA:
        gh_base = os.environ.get("GITHUB_BASE_REF", "").strip()
        candidate = f"origin/{gh_base}" if gh_base else "origin/main"
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode != 0 and not candidate.startswith("origin/"):
        origin_cand = f"origin/{candidate}"
        verify_origin = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{origin_cand}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if verify_origin.returncode == 0:
            return origin_cand
    return candidate


def get_branch_diff_files(repo_root: Path, base_ref: str, head_ref: str = "HEAD") -> list[str]:
    """Returns file paths changed between the merge-base of base_ref and head_ref.

    Args:
        repo_root: Repository root directory.
        base_ref: Base git reference (e.g., origin/main or PR base SHA).
        head_ref: Head git reference (default: HEAD).

    Returns:
        Sorted list of changed repository-relative file paths.
    """
    res = run_command(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            f"{base_ref}...{head_ref}",
        ],
        cwd=repo_root,
        capture_output=True,
        check=True,
        log_cmd=False,
        log_on_error=False,
        handled_instruction=f"Fetch the base branch first (e.g. 'git fetch origin') so '{base_ref}' resolves.",
    )
    return sorted({entry for entry in res.stdout.split("\0") if entry})


def get_commit_messages(
    repo_root: Path,
    base_ref: str | None,
    head_ref: str = "HEAD",
    *,
    staged: bool = False,
) -> list[str]:
    """Collects commit messages (and staged env override if applicable) for trailer inspection.

    Args:
        repo_root: Repository root directory.
        base_ref: Base git reference for commit range lookup.
        head_ref: Head git reference (default: HEAD).
        staged: Whether running in pre-commit staged mode.

    Returns:
        List of commit message strings to scan for override trailers.
    """
    messages: list[str] = []
    if staged:
        env_override = os.environ.get(OVERRIDE_ENV_VAR, "").strip()
        if env_override:
            if env_override.upper().startswith(f"{OVERRIDE_TRAILER_KEY}:"):
                messages.append(env_override)
            else:
                messages.append(f"{OVERRIDE_TRAILER_KEY}: {env_override}")

    log_range = f"{base_ref}..{head_ref}" if base_ref else ""
    if log_range:
        proc = subprocess.run(
            ["git", "log", "--format=%B", "-z", log_range],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            messages.extend(m for m in proc.stdout.split("\0") if m.strip())
            return messages

    if not staged:
        head_proc = subprocess.run(
            ["git", "log", "-1", "--format=%B", head_ref],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if head_proc.returncode == 0 and head_proc.stdout.strip():
            messages.append(head_proc.stdout)
    return messages


def evaluate_profile_locks(
    changed_files: Sequence[str],
    locked_profiles: Mapping[str, LockedProfile],
    commit_messages: Sequence[str],
) -> tuple[list[LockViolation], dict[str, str]]:
    """Evaluates touched files against locked profiles and override trailers.

    Args:
        changed_files: Repository-relative paths touched in the diff.
        locked_profiles: Active locked profile registry.
        commit_messages: Commit messages to search for PROFILE-LOCK-OVERRIDE trailers.

    Returns:
        Tuple of (unresolved LockViolation list, applied overrides dict).
    """
    touched = classify_locked_profile_changes(changed_files, locked_profiles)
    if not touched:
        return [], {}

    overrides = parse_override_trailers(commit_messages)
    violations: list[LockViolation] = []
    applied_overrides: dict[str, str] = {}

    for prof_name, files in touched.items():
        if prof_name in overrides:
            applied_overrides[prof_name] = overrides[prof_name]
            continue
        prof_meta = locked_profiles[prof_name]
        violations.append(
            LockViolation(
                profile=prof_name,
                locked_since=prof_meta.locked_since,
                files=tuple(files),
            )
        )
    return violations, applied_overrides


def _format_violation_error(violations: Sequence[LockViolation]) -> HandledError:
    """Formats a HandledError detailing locked profile violations and override instructions."""
    summary_parts: list[str] = []
    example_profile = violations[0].profile
    for v in violations:
        files_str = ", ".join(v.files)
        summary_parts.append(
            f"Profile '{v.profile}' (deploy/profiles/{v.profile}/) is GA-locked since XMC "
            f"{v.locked_since}; offending file(s): {files_str}"
        )
    message = "GA-locked component profile modification detected. " + "; ".join(summary_parts)
    instruction = (
        "Revert edits to GA-locked profile directories and move new component or feature changes "
        "to a new profile under deploy/profiles/ (for example, deploy/profiles/stable-2026b/). "
        "If this is a legitimate patch release permitted by the profile README, add the commit "
        f"trailer '{OVERRIDE_TRAILER_KEY}: {example_profile} <reason>' to your commit message "
        f'(for local pre-commit hooks, set {OVERRIDE_ENV_VAR}="{example_profile} <reason>"), '
        f"update {LOCKED_PROFILES_REL_PATH} digests if needed, and obtain CODEOWNERS approval."
    )
    details = {
        "locked_profiles_file": LOCKED_PROFILES_REL_PATH,
        "override_trailer_format": f"{OVERRIDE_TRAILER_KEY}: <profile> <reason>",
        "violations": [
            {
                "profile": v.profile,
                "locked_since": v.locked_since,
                "files": list(v.files),
            }
            for v in violations
        ],
    }
    return HandledError(message, instruction=instruction, details=details)


def check_profile_lock(args: argparse.Namespace) -> dict[str, Any]:
    """Main execution handler for checking component profile locks.

    Args:
        args: Parsed CLI arguments.

    Returns:
        JSON-serializable status dictionary when all checks pass.
    """
    repo_root = Path(args.repo_root).resolve()
    effective_base: str | None = None
    if args.staged:
        changed_files = get_staged_files(repo_root)
        effective_base = _resolve_effective_base_ref(repo_root, args.base_ref)
    else:
        effective_base = _resolve_effective_base_ref(repo_root, args.base_ref)
        changed_files = get_branch_diff_files(repo_root, effective_base, args.head_ref)

    locked_profiles = load_locked_profiles(repo_root, base_ref=effective_base)

    commit_messages: list[str] = []
    if args.commit_message:
        commit_messages.append(args.commit_message)
    if args.commit_message_file:
        msg_path = Path(args.commit_message_file)
        if not msg_path.is_file():
            raise HandledError(
                f"Commit message file not found: {msg_path}",
                instruction="Pass a valid file path to --commit-message-file.",
            )
        commit_messages.append(msg_path.read_text(encoding="utf-8"))
    if not commit_messages:
        commit_messages = get_commit_messages(
            repo_root,
            effective_base,
            args.head_ref,
            staged=args.staged,
        )

    violations, applied_overrides = evaluate_profile_locks(
        changed_files,
        locked_profiles,
        commit_messages,
    )
    if violations:
        raise _format_violation_error(violations)

    return {
        "mode": "staged" if args.staged else "branch-diff",
        "base_ref": effective_base,
        "locked_profiles": sorted(locked_profiles.keys()),
        "changed_files_count": len(changed_files),
        "overrides": applied_overrides,
    }


def build_parser() -> argparse.ArgumentParser:
    """Builds the CLI argument parser for check_profile_lock.py."""
    parser = argparse.ArgumentParser(
        description=(
            "Fail when a diff modifies files under a GA-locked component profile in "
            f"{LOCKED_PROFILES_REL_PATH} without a '{OVERRIDE_TRAILER_KEY}: <profile> <reason>' "
            "commit trailer."
        )
    )
    parser.add_argument(
        "--repo-root",
        default=str(_REPO_ROOT),
        help="Repository root directory.",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Check staged files in the git index (used by .githooks/pre-commit).",
    )
    parser.add_argument(
        "--base-ref",
        default="",
        help="Git base reference for branch diffs (default: $GITHUB_BASE_REF or origin/main).",
    )
    parser.add_argument(
        "--head-ref",
        default="HEAD",
        help="Git head reference for branch diffs (default: HEAD).",
    )
    parser.add_argument(
        "--commit-message",
        default="",
        help="Explicit commit message text to inspect for override trailers.",
    )
    parser.add_argument(
        "--commit-message-file",
        default="",
        help="Path to a commit message file to inspect for override trailers.",
    )
    return parser


def main() -> None:
    """CLI entrypoint."""
    run_tool(build_parser(), check_profile_lock)


if __name__ == "__main__":
    main()
