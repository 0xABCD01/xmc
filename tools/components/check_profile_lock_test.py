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

"""Unit tests for tools/components/check_profile_lock.py."""

import argparse
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.common.tool_utils import HandledError
from tools.components import check_profile_lock

_SAMPLE_LOCKED_YAML = """\
# Comment line
stable-2026a:
  locked_since: "1.0.0"
  chart_digest: "abc123"
  embedded_charts_digest: "def456"
"""


class ParseLockedProfilesTest(unittest.TestCase):
    """Tests for YAML parsing of deploy/profiles/locked_profiles.yaml."""

    def test_parse_block_and_inline_mappings(self) -> None:
        yaml_text = (
            "# Header comment\n"
            "stable-2026a:\n"
            '  locked_since: "1.0.0" # inline comment\n'
            '  chart_digest: "digest-a"\n'
            '  embedded_charts_digest: "embed-a"\n'
            "stable-2026b: {locked_since: \"1.1.0\", chart_digest: 'digest-b'}\n"
        )
        got = check_profile_lock.parse_locked_profiles_yaml(yaml_text)
        self.assertEqual(
            got,
            {
                "stable-2026a": check_profile_lock.LockedProfile(
                    name="stable-2026a",
                    locked_since="1.0.0",
                    chart_digest="digest-a",
                    embedded_charts_digest="embed-a",
                ),
                "stable-2026b": check_profile_lock.LockedProfile(
                    name="stable-2026b",
                    locked_since="1.1.0",
                    chart_digest="digest-b",
                    embedded_charts_digest="",
                ),
            },
        )

    def test_parse_empty_or_comments_only(self) -> None:
        self.assertEqual(check_profile_lock.parse_locked_profiles_yaml(""), {})
        self.assertEqual(check_profile_lock.parse_locked_profiles_yaml("# only comment\n\n"), {})

    def test_parse_rejects_malformed_inputs(self) -> None:
        cases = [
            ("no-colon-line", "Invalid line"),
            ("  orphan_indent: 1.0.0", "Unexpected indentation"),
            ("stable-2026a: {bad_entry}", "Malformed inline mapping"),
            ("stable-2026a: {}", "missing 'locked_since'"),
            ("stable-2026a:\n  chart_digest: abc\n", "missing 'locked_since'"),
        ]
        for raw, want_substr in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(HandledError) as ctx:
                    check_profile_lock.parse_locked_profiles_yaml(raw)
                self.assertIn(want_substr, ctx.exception.message)


class EvaluateProfileLocksUnitTest(unittest.TestCase):
    """Hermetic unit tests for path classification, trailers, and lock evaluation."""

    def setUp(self) -> None:
        self.locked = check_profile_lock.parse_locked_profiles_yaml(_SAMPLE_LOCKED_YAML)

    def test_no_change(self) -> None:
        violations, overrides = check_profile_lock.evaluate_profile_locks([], self.locked, [])
        self.assertEqual(violations, [])
        self.assertEqual(overrides, {})

    def test_change_outside_locked_profile_and_unlocked_profile(self) -> None:
        changed = [
            "README.md",
            "deploy/profiles/locked_profiles.yaml",
            "deploy/profiles/BUILD.bazel",
            "deploy/profiles/stable-2026b/Chart.yaml",
            "deploy/profiles/stable-2026b/components/hpo/values.yaml",
        ]
        violations, overrides = check_profile_lock.evaluate_profile_locks(changed, self.locked, [])
        self.assertEqual(violations, [])
        self.assertEqual(overrides, {})

    def test_change_to_locked_file_without_override_fails(self) -> None:
        changed = [
            "deploy/profiles/stable-2026a/values.yaml",
            "deploy/profiles/stable-2026a/components/redpanda/Chart.yaml",
        ]
        violations, overrides = check_profile_lock.evaluate_profile_locks(
            changed,
            self.locked,
            ["feat(components): add experimental flag to stable-2026a"],
        )
        self.assertEqual(overrides, {})
        self.assertEqual(
            violations,
            [
                check_profile_lock.LockViolation(
                    profile="stable-2026a",
                    locked_since="1.0.0",
                    files=(
                        "deploy/profiles/stable-2026a/components/redpanda/Chart.yaml",
                        "deploy/profiles/stable-2026a/values.yaml",
                    ),
                )
            ],
        )

    def test_override_trailer_present_passes(self) -> None:
        changed = ["deploy/profiles/stable-2026a/Chart.yaml"]
        msg = (
            "fix(profiles): bump redpanda chart patch version\n\n"
            "PROFILE-LOCK-OVERRIDE: stable-2026a patch release 1.0.1 for CVE fix — रफ़्तार\n"
            "Requested-by: dev@example.com\n"
        )
        violations, overrides = check_profile_lock.evaluate_profile_locks(
            changed,
            self.locked,
            [msg],
        )
        self.assertEqual(violations, [])
        self.assertEqual(
            overrides,
            {"stable-2026a": "patch release 1.0.1 for CVE fix — रफ़्तार"},
        )

    def test_override_trailer_missing_reason_or_wrong_profile_fails(self) -> None:
        changed = ["deploy/profiles/stable-2026a/Chart.yaml"]
        bad_messages = [
            "PROFILE-LOCK-OVERRIDE: stable-2026a",
            "PROFILE-LOCK-OVERRIDE: stable-2026a   ",
            "PROFILE-LOCK-OVERRIDE: stable-2026b wrong profile reason",
            "PROFILE-LOCK-OVERRIDE:",
        ]
        violations, overrides = check_profile_lock.evaluate_profile_locks(
            changed,
            self.locked,
            bad_messages,
        )
        self.assertEqual(overrides, {})
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].profile, "stable-2026a")


class GitIntegrationProfileLockTest(unittest.TestCase):
    """Integration tests against a temporary Git repository for staged and branch-diff modes."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name)
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Test User")
        self._git("config", "user.email", "test@example.com")

        self._write_file("deploy/profiles/locked_profiles.yaml", _SAMPLE_LOCKED_YAML)
        self._write_file("deploy/profiles/stable-2026a/Chart.yaml", "version: 1.0.0\n")
        self._write_file(
            "deploy/profiles/stable-2026a/components/redpanda/values.yaml",
            "enabled: false\n",
        )
        self._write_file("deploy/profiles/stable-2026b/Chart.yaml", "version: 0.1.0\n")
        self._git("add", ".")
        self._git("commit", "-m", "chore: initial commit")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout

    def _write_file(self, rel_path: str, content: str) -> Path:
        full_path = self.repo_root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return full_path

    def _make_args(self, **kwargs: object) -> argparse.Namespace:
        defaults = {
            "repo_root": str(self.repo_root),
            "staged": False,
            "base_ref": "main",
            "head_ref": "HEAD",
            "commit_message": "",
            "commit_message_file": "",
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_staged_no_changes_passes(self) -> None:
        res = check_profile_lock.check_profile_lock(self._make_args(staged=True))
        self.assertEqual(res["changed_files_count"], 0)
        self.assertEqual(res["locked_profiles"], ["stable-2026a"])

    def test_staged_unlocked_profile_edit_passes(self) -> None:
        self._write_file("deploy/profiles/stable-2026b/Chart.yaml", "version: 0.2.0\n")
        self._git("add", "deploy/profiles/stable-2026b/Chart.yaml")
        res = check_profile_lock.check_profile_lock(self._make_args(staged=True))
        self.assertEqual(res["changed_files_count"], 1)
        self.assertEqual(res["overrides"], {})

    def test_staged_locked_file_edit_fails_with_offending_file_and_procedure(self) -> None:
        self._write_file("deploy/profiles/stable-2026a/Chart.yaml", "version: 1.0.1\n")
        self._git("add", "deploy/profiles/stable-2026a/Chart.yaml")
        with self.assertRaises(HandledError) as ctx:
            check_profile_lock.check_profile_lock(self._make_args(staged=True))
        self.assertIn("deploy/profiles/stable-2026a/Chart.yaml", ctx.exception.message)
        self.assertIn("PROFILE-LOCK-OVERRIDE: stable-2026a <reason>", ctx.exception.instruction)

    def test_staged_locked_file_delete_and_rename_fail(self) -> None:
        self._git(
            "mv",
            "deploy/profiles/stable-2026a/components/redpanda/values.yaml",
            "deploy/profiles/stable-2026b/redpanda-values.yaml",
        )
        with self.assertRaises(HandledError) as ctx:
            check_profile_lock.check_profile_lock(self._make_args(staged=True))
        self.assertIn(
            "deploy/profiles/stable-2026a/components/redpanda/values.yaml",
            ctx.exception.message,
        )

    def test_staged_env_override_passes(self) -> None:
        self._write_file("deploy/profiles/stable-2026a/Chart.yaml", "version: 1.0.1\n")
        self._git("add", "deploy/profiles/stable-2026a/Chart.yaml")
        with patch.dict(
            os.environ,
            {"PROFILE_LOCK_OVERRIDE": "stable-2026a bump chart patch version"},
        ):
            res = check_profile_lock.check_profile_lock(self._make_args(staged=True))
        self.assertEqual(res["overrides"], {"stable-2026a": "bump chart patch version"})

    def test_branch_diff_locked_file_without_trailer_fails(self) -> None:
        self._git("checkout", "-b", "feat/edit-locked")
        self._git("rm", "deploy/profiles/stable-2026a/components/redpanda/values.yaml")
        self._git("commit", "-m", "feat: delete redpanda values from stable-2026a")
        with self.assertRaises(HandledError) as ctx:
            check_profile_lock.check_profile_lock(self._make_args(base_ref="main"))
        self.assertIn(
            "deploy/profiles/stable-2026a/components/redpanda/values.yaml",
            ctx.exception.message,
        )

    def test_branch_diff_with_override_trailer_in_commit_passes(self) -> None:
        self._git("checkout", "-b", "fix/patch-locked")
        self._write_file("deploy/profiles/stable-2026a/Chart.yaml", "version: 1.0.1\n")
        self._git("add", "deploy/profiles/stable-2026a/Chart.yaml")
        self._git(
            "commit",
            "-m",
            "fix(profiles): patch stable-2026a\n\nPROFILE-LOCK-OVERRIDE: stable-2026a patch 1.0.1",
        )
        res = check_profile_lock.check_profile_lock(self._make_args(base_ref="main"))
        self.assertEqual(res["overrides"], {"stable-2026a": "patch 1.0.1"})

    def test_branch_diff_cannot_bypass_by_removing_profile_from_lock_file(self) -> None:
        self._git("checkout", "-b", "feat/bypass-attempt")
        self._write_file("deploy/profiles/locked_profiles.yaml", "# emptied lock file\n")
        self._write_file("deploy/profiles/stable-2026a/Chart.yaml", "version: 2.0.0\n")
        self._git("add", ".")
        self._git("commit", "-m", "feat: try to unlock and edit stable-2026a")
        with self.assertRaises(HandledError) as ctx:
            check_profile_lock.check_profile_lock(self._make_args(base_ref="main"))
        self.assertIn("deploy/profiles/stable-2026a/Chart.yaml", ctx.exception.message)

    def test_commit_message_file_and_missing_files_errors(self) -> None:
        msg_file = self._write_file(
            "commit_msg.txt",
            "fix: patch\n\nPROFILE-LOCK-OVERRIDE: stable-2026a emergency patch\n",
        )
        self._write_file("deploy/profiles/stable-2026a/Chart.yaml", "version: 1.0.1\n")
        self._git("add", "deploy/profiles/stable-2026a/Chart.yaml")
        res = check_profile_lock.check_profile_lock(
            self._make_args(staged=True, commit_message_file=str(msg_file))
        )
        self.assertEqual(res["overrides"], {"stable-2026a": "emergency patch"})

        with self.assertRaises(HandledError):
            check_profile_lock.check_profile_lock(
                self._make_args(staged=True, commit_message_file=str(self.repo_root / "nope.txt"))
            )

        (self.repo_root / "deploy/profiles/locked_profiles.yaml").unlink()
        with self.assertRaises(HandledError):
            check_profile_lock.load_locked_profiles(self.repo_root)


class RepositoryCodeownersAlignmentTest(unittest.TestCase):
    """Verifies that the repository CODEOWNERS matches the OWNERS reviewer pool."""

    def test_codeowners_matches_owners_pool(self) -> None:
        repo_root = next(
            (
                base
                for base in (
                    Path(__file__).absolute().parents[2],
                    Path(__file__).resolve().parents[2],
                )
                if (base / "OWNERS").is_file() and (base / "CODEOWNERS").is_file()
            ),
            Path(__file__).resolve().parents[2],
        )
        owners_file = repo_root / "OWNERS"
        codeowners_file = repo_root / "CODEOWNERS"
        self.assertTrue(owners_file.is_file(), f"Missing OWNERS file at {owners_file}")
        self.assertTrue(codeowners_file.is_file(), f"Missing CODEOWNERS file at {codeowners_file}")

        owners = [
            line.split("#", 1)[0].strip()
            for line in owners_file.read_text(encoding="utf-8").splitlines()
            if line.split("#", 1)[0].strip()
        ]
        codeowner_lines = [
            line.split("#", 1)[0].strip()
            for line in codeowners_file.read_text(encoding="utf-8").splitlines()
            if line.split("#", 1)[0].strip()
        ]
        self.assertEqual(codeowner_lines, [f"* {' '.join(f'@{u}' for u in owners)}"])


if __name__ == "__main__":
    unittest.main()
