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

"""Lint and propagation tool keeping repository manifests synced with root VERSION."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.common.tool_utils import HandledError, run_tool  # noqa: E402
from tools.common.workspace_status import read_version  # noqa: E402

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_MODULE_RE = re.compile(r'(module\s*\([^)]*?version\s*=\s*")([^"]+)(")', re.DOTALL)
_CHART_VER_RE = re.compile(r"^(version:\s*)([^\s#]+)", re.MULTILINE)
_CHART_APP_RE = re.compile(r'^(appVersion:\s*)"?([^"\s#]+)"?', re.MULTILINE)
_PYPROJECT_RE = re.compile(
    r'(\[project\][^\[]*?^version\s*=\s*")([^"]+)(")', re.MULTILINE | re.DOTALL
)
_PY_VER_RE = re.compile(r'^(__version__\s*=\s*")([^"]+)(")', re.MULTILINE)
_JSON_VER_RE = re.compile(r'^(\s*"version"\s*:\s*")([^"]+)(")', re.MULTILINE)

SyncFn = Callable[[str, str], tuple[str, list[str]]]


def _sync_regex(text: str, version: str, pat: re.Pattern[str], field: str) -> tuple[str, list[str]]:
    match = pat.search(text)
    if not match:
        return text, [f"missing {field}"]
    if match.group(2) == version:
        return text, []
    return pat.sub(rf"\g<1>{version}\g<3>", text, count=1), [
        f"{field}: {match.group(2)!r} != {version!r}"
    ]


def _sync_chart_yaml(text: str, version: str) -> tuple[str, list[str]]:
    issues: list[str] = []
    updated = text
    ver_m = _CHART_VER_RE.search(updated)
    if not ver_m:
        issues.append("missing version")
    elif ver_m.group(2) != version:
        issues.append(f"version: {ver_m.group(2)!r} != {version!r}")
        updated = _CHART_VER_RE.sub(rf"\g<1>{version}", updated, count=1)

    app_m = _CHART_APP_RE.search(updated)
    if not app_m:
        issues.append("missing appVersion")
        if _CHART_VER_RE.search(updated):
            updated = _CHART_VER_RE.sub(rf'\g<0>\nappVersion: "{version}"', updated, count=1)
    elif app_m.group(2) != version:
        issues.append(f"appVersion: {app_m.group(2)!r} != {version!r}")
        updated = _CHART_APP_RE.sub(rf'\g<1>"{version}"', updated, count=1)
    return updated, issues


def _sync_npm_json(text: str, version: str, *, is_lockfile: bool = False) -> tuple[str, list[str]]:
    data = json.loads(text)
    issues: list[str] = []
    if data.get("version") != version:
        issues.append(f"version: {data.get('version')!r} != {version!r}")
    if is_lockfile:
        pkg_root = data.get("packages", {}).get("", {})
        pkg_ver = pkg_root.get("version") if isinstance(pkg_root, dict) else None
        if pkg_ver != version:
            issues.append(f'packages[""].version: {pkg_ver!r} != {version!r}')
    if not issues:
        return text, []

    if is_lockfile:
        end = text.find('"dependencies"')
        end = end if end != -1 else min(len(text), 1024)
        return _JSON_VER_RE.sub(rf"\g<1>{version}\g<3>", text[:end], count=2) + text[end:], issues
    return _JSON_VER_RE.sub(rf"\g<1>{version}\g<3>", text, count=1), issues


TARGETS: tuple[tuple[str, SyncFn], ...] = (
    ("MODULE.bazel", lambda t, v: _sync_regex(t, v, _MODULE_RE, "module.version")),
    ("deploy/helm/Chart.yaml", _sync_chart_yaml),
    ("cmd/ui/package.json", lambda t, v: _sync_npm_json(t, v)),
    ("cmd/ui/package-lock.json", lambda t, v: _sync_npm_json(t, v, is_lockfile=True)),
    (
        "sdks/python/xmc/pyproject.toml",
        lambda t, v: _sync_regex(t, v, _PYPROJECT_RE, "project.version"),
    ),
    ("sdks/python/xmc/_version.py", lambda t, v: _sync_regex(t, v, _PY_VER_RE, "__version__")),
    (
        "sdks/python/xdash/pyproject.toml",
        lambda t, v: _sync_regex(t, v, _PYPROJECT_RE, "project.version"),
    ),
)


def check_version_sync(args: argparse.Namespace) -> dict[str, Any]:
    """Verifies or propagates root VERSION across all configured manifests."""
    repo_root = Path(args.repo_root).resolve()
    version_file = repo_root / "VERSION"
    try:
        version = read_version(version_file)
    except (OSError, ValueError) as exc:
        raise HandledError(
            f"Unable to read repository VERSION file '{version_file}': {exc}",
            "Ensure a root VERSION file exists with a single semantic version (e.g. 1.0.0).",
        ) from exc
    if not _SEMVER_RE.fullmatch(version) or len(f"xmc-{version}") > 63:
        raise HandledError(
            f"Invalid version '{version}' in VERSION.",
            "Set root VERSION to a semantic version (e.g. '1.0.0') with 'xmc-<version>' <= 63 chars.",
        )

    drifted: list[dict[str, Any]] = []
    updated_files: list[str] = []
    for rel_path, sync_fn in TARGETS:
        path = repo_root / rel_path
        if not path.is_file():
            drifted.append({"path": rel_path, "issues": ["file not found"]})
            continue
        original = path.read_text(encoding="utf-8")
        updated, issues = sync_fn(original, version)
        if not issues:
            continue
        if args.fix and updated != original:
            path.write_text(updated, encoding="utf-8")
            _, remaining = sync_fn(updated, version)
            if remaining:
                drifted.append({"path": rel_path, "issues": remaining})
            else:
                updated_files.append(rel_path)
        else:
            drifted.append({"path": rel_path, "issues": issues})

    if drifted:
        raise HandledError(
            f"Found {len(drifted)} manifest(s) out of sync with root VERSION ({version}).",
            "Run 'python3 tools/lint/check_version_sync.py --fix' to propagate VERSION across all manifests.",
            details={"version": version, "drifted": drifted},
        )
    return {
        "version": version,
        "targets_checked": len(TARGETS),
        "updated": updated_files,
        "drifted": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    """Builds the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Verify or propagate root VERSION across repository manifests."
    )
    parser.add_argument("--repo-root", default=str(_REPO_ROOT), help="Repository root directory.")
    parser.add_argument("--fix", action="store_true", help="Propagate VERSION to drifted files.")
    return parser


def main() -> None:
    """Runs the version-sync linter CLI."""
    run_tool(build_parser(), check_version_sync)


if __name__ == "__main__":
    main()
