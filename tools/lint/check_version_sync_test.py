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

"""Unit tests for tools.lint.check_version_sync."""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

from tools.common.tool_utils import HandledError
from tools.lint import check_version_sync as cli


def _write(root: Path, rel_path: str, content: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_repo(root: Path, *, version: str = "1.0.0", chart_version: str = "1.0.0") -> None:
    _write(root, "VERSION", f"{version}\n")
    _write(
        root,
        "MODULE.bazel",
        f'module(\n    name = "xmc",\n    version = "{version}",\n)\n'
        'bazel_dep(name = "rules_go", version = "0.60.0")\n',
    )
    _write(
        root,
        "deploy/helm/Chart.yaml",
        f'apiVersion: v2\nname: xmanager-cloud\nversion: {chart_version}\nappVersion: "{chart_version}"\n',
    )
    _write(
        root,
        "cmd/ui/package.json",
        f'{{\n  "name": "xmanager-cloud-ui",\n  "version": "{version}"\n}}\n',
    )
    _write(
        root,
        "cmd/ui/package-lock.json",
        f'{{\n  "name": "xmanager-cloud-ui",\n  "version": "{version}",\n'
        f'  "packages": {{\n    "": {{\n      "name": "xmanager-cloud-ui",\n      "version": "{version}",\n'
        '      "dependencies": {"a": "^1.0.0"}\n    }\n  }\n}\n',
    )
    _write(
        root,
        "sdks/python/xmc/pyproject.toml",
        f'[project]\nname = "xmc"\nversion = "{version}"\n',
    )
    _write(root, "sdks/python/xmc/_version.py", f'__version__ = "{version}"\n')
    _write(
        root,
        "sdks/python/xdash/pyproject.toml",
        f'[project]\nname = "xdash"\nversion = "{version}"\n',
    )


class CheckVersionSyncTest(unittest.TestCase):
    def test_in_sync_repo_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed_repo(root, version="1.2.3", chart_version="1.2.3")
            res = cli.check_version_sync(argparse.Namespace(repo_root=str(root), fix=False))
            self.assertEqual(res["version"], "1.2.3")
            self.assertEqual(res["drifted"], 0)
            self.assertEqual(res["targets_checked"], len(cli.TARGETS))

    def test_drifted_manifest_fails_and_fix_propagates_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed_repo(root, version="1.0.0", chart_version="0.1.2")
            _write(root, "VERSION", "2.0.0\n")
            _write(
                root,
                "deploy/helm/Chart.yaml",
                "apiVersion: v2\nname: xmanager-cloud\nversion: 0.1.2\n",
            )

            with self.assertRaises(HandledError) as cm:
                cli.check_version_sync(argparse.Namespace(repo_root=str(root), fix=False))
            self.assertIn("out of sync", cm.exception.message)

            fixed = cli.check_version_sync(argparse.Namespace(repo_root=str(root), fix=True))
            self.assertEqual(fixed["version"], "2.0.0")
            self.assertEqual(fixed["drifted"], 0)
            self.assertEqual(len(fixed["updated"]), len(cli.TARGETS))

            # bazel_dep version must remain untouched.
            module_text = (root / "MODULE.bazel").read_text(encoding="utf-8")
            self.assertIn('version = "2.0.0"', module_text)
            self.assertIn('bazel_dep(name = "rules_go", version = "0.60.0")', module_text)
            chart_text = (root / "deploy/helm/Chart.yaml").read_text(encoding="utf-8")
            self.assertIn("version: 2.0.0", chart_text)
            self.assertIn('appVersion: "2.0.0"', chart_text)

            # Second check pass succeeds with zero updates.
            recheck = cli.check_version_sync(argparse.Namespace(repo_root=str(root), fix=False))
            self.assertEqual(recheck["drifted"], 0)
            self.assertEqual(recheck["updated"], [])

    def test_invalid_version_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed_repo(root)
            _write(root, "VERSION", "not-a-semver\n")
            with self.assertRaises(HandledError):
                cli.check_version_sync(argparse.Namespace(repo_root=str(root), fix=False))

    def test_overlong_k8s_label_version_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _seed_repo(root)
            _write(root, "VERSION", f"1.0.0-{'a' * 60}\n")
            with self.assertRaises(HandledError):
                cli.check_version_sync(argparse.Namespace(repo_root=str(root), fix=False))


if __name__ == "__main__":
    unittest.main()
