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

"""Unit tests for publish_release tool."""

import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from tools.common.tool_utils import HandledError
from tools.release.publish_release import (
    create_parser,
    publish_release,
    read_notes_file,
)


class PublishReleaseTest(unittest.TestCase):
    """Test suite for publish_release CLI orchestrator."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.notes_file = Path(self.temp_dir.name) / "notes.md"
        self.notes_file.write_text("# Release Notes\n- Some changes\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_read_notes_file_valid_and_errors(self) -> None:
        content = read_notes_file(self.notes_file)
        self.assertIn("# Release Notes", content)

        missing = Path(self.temp_dir.name) / "nonexistent.md"
        with self.assertRaises(HandledError):
            read_notes_file(missing)

        empty = Path(self.temp_dir.name) / "empty.md"
        empty.write_text("   \n", encoding="utf-8")
        with self.assertRaises(HandledError):
            read_notes_file(empty)

    @patch("tools.release.publish_release.get_git_file_content", return_value="0.6.0\n")
    @patch("tools.release.publish_release.publish_via_gh_cli")
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_publish_release_post_push_failure_adds_rollback_note(
        self,
        mock_subprocess: MagicMock,
        mock_which: MagicMock,
        mock_publish_gh: MagicMock,
        _mock_version: MagicMock,
    ) -> None:
        mock_which.return_value = "/usr/bin/gh"
        mock_subprocess.return_value = MagicMock(stdout="", returncode=0)
        mock_publish_gh.side_effect = HandledError("gh failed", "fix gh")

        args = argparse.Namespace(
            tag="0.6.0",
            notes_file=str(self.notes_file),
            title="Release 0.6.0",
            target_commit="HEAD",
            repo_dir=".",
            remote="origin",
            draft=False,
            prerelease=False,
            skip_push=False,
            force=False,
        )
        with self.assertRaises(HandledError) as ctx:
            publish_release(args)
        self.assertIn("git tag -d 0.6.0", ctx.exception.instruction)
        self.assertIn("pushed to", ctx.exception.instruction)

        mock_publish_gh.side_effect = HandledError("gh failed", "fix gh")
        args.skip_push = True
        with self.assertRaises(HandledError) as ctx:
            publish_release(args)
        self.assertIn("created locally", ctx.exception.instruction)

        # When tag already existed and skip_push is True, no git mutations occurred;
        # deletion advice should NOT be appended.
        mock_publish_gh.side_effect = HandledError("gh failed", "fix gh")
        with patch("tools.release.publish_release.verify_local_tag", return_value=True):
            with self.assertRaises(HandledError) as ctx:
                publish_release(args)
            self.assertNotIn("created locally", ctx.exception.instruction)
            self.assertNotIn("pushed to", ctx.exception.instruction)

    @patch("tools.release.publish_release.get_git_file_content", return_value="0.6.0\n")
    @patch("tools.release.git_ops.get_git_remote_url")
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_publish_release_end_to_end(
        self,
        mock_subprocess: MagicMock,
        mock_which: MagicMock,
        mock_remote_url: MagicMock,
        _mock_version: MagicMock,
    ) -> None:
        mock_remote_url.return_value = "https://github.com/google/xmc"
        mock_which.return_value = "/usr/bin/gh"

        def side_effect(cmd: list[str], **_kwargs: object) -> MagicMock:
            if cmd[:2] == ["gh", "release"]:
                return MagicMock(
                    stdout="https://github.com/google/xmc/releases/tag/0.6.0\n",
                    returncode=0,
                )
            return MagicMock(stdout="", returncode=0)

        mock_subprocess.side_effect = side_effect

        args = argparse.Namespace(
            tag="0.6.0",
            notes_file=str(self.notes_file),
            title="Release 0.6.0",
            target_commit="HEAD",
            repo_dir=".",
            remote="origin",
            draft=False,
            prerelease=False,
            skip_push=False,
            force=False,
        )
        res = publish_release(args)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["tag"], "0.6.0")

    @patch("tools.release.publish_release.get_git_file_content", return_value="0.6.0\n")
    @patch("tools.release.git_ops.get_git_remote_url")
    @patch("shutil.which")
    @patch("subprocess.run")
    @patch("tools.release.github_release_client.call_github_api")
    def test_publish_release_fallback_to_rest(
        self,
        mock_api: MagicMock,
        mock_subprocess: MagicMock,
        mock_which: MagicMock,
        mock_remote_url: MagicMock,
        _mock_version: MagicMock,
    ) -> None:
        mock_remote_url.return_value = "https://github.com/google/xmc"
        mock_which.return_value = None
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")
        mock_api.return_value = {
            "html_url": "https://github.com/google/xmc/releases/tag/0.6.0",
            "id": 9999,
        }

        args = argparse.Namespace(
            tag="0.6.0",
            notes_file=str(self.notes_file),
            title="Release 0.6.0",
            target_commit="HEAD",
            repo_dir=".",
            remote="origin",
            draft=False,
            prerelease=False,
            skip_push=False,
            force=False,
        )
        res = publish_release(args)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["release_url"], "https://github.com/google/xmc/releases/tag/0.6.0")
        self.assertEqual(res["release_id"], 9999)

    def test_publish_release_empty_tag(self) -> None:
        for invalid_tag in ["   ", "-v0.6.0"]:
            args = argparse.Namespace(
                tag=invalid_tag,
                notes_file=str(self.notes_file),
                title=None,
                target_commit="HEAD",
                repo_dir=".",
                remote="origin",
                draft=False,
                prerelease=False,
                skip_push=True,
                force=False,
            )
            with self.assertRaises(HandledError):
                publish_release(args)

    def test_publish_release_invalid_repo_dir(self) -> None:
        args = argparse.Namespace(
            tag="0.6.0",
            notes_file=str(self.notes_file),
            title=None,
            target_commit="HEAD",
            repo_dir="/path/to/nonexistent/directory",
            remote="origin",
            draft=False,
            prerelease=False,
            skip_push=True,
            force=False,
        )
        with self.assertRaises(HandledError) as ctx:
            publish_release(args)
        self.assertIn("does not exist", str(ctx.exception))

    @patch("tools.release.publish_release.get_git_file_content", return_value="0.6.0\n")
    @patch("tools.release.git_ops.get_git_remote_url")
    @patch("shutil.which")
    @patch("subprocess.run")
    def test_publish_release_empty_url(
        self,
        mock_subprocess: MagicMock,
        mock_which: MagicMock,
        mock_remote_url: MagicMock,
        _mock_version: MagicMock,
    ) -> None:
        mock_remote_url.return_value = "https://github.com/google/xmc"
        mock_which.return_value = "/usr/bin/gh"
        mock_subprocess.return_value = MagicMock(
            stdout="",
            returncode=0,
        )

        args = argparse.Namespace(
            tag="0.6.0",
            notes_file=str(self.notes_file),
            title="Release 0.6.0",
            target_commit="HEAD",
            repo_dir=".",
            remote="origin",
            draft=False,
            prerelease=False,
            skip_push=True,
            force=False,
        )
        with self.assertRaises(HandledError) as ctx:
            publish_release(args)
        self.assertIn("created locally", ctx.exception.instruction)

        args.skip_push = False
        with self.assertRaises(HandledError) as ctx:
            publish_release(args)
        self.assertIn("Release URL is empty", str(ctx.exception))
        self.assertIn("pushed to", ctx.exception.instruction)

    @patch("tools.release.publish_release.get_git_file_content", return_value="0.6.0\n")
    @patch("tools.release.git_ops.get_git_remote_url")
    @patch("subprocess.run")
    def test_publish_release_dry_run(
        self,
        mock_subprocess: MagicMock,
        mock_remote_url: MagicMock,
        _mock_version: MagicMock,
    ) -> None:
        mock_remote_url.return_value = "https://github.com/google/xmc"
        mock_subprocess.return_value = MagicMock(stdout="", returncode=0)
        args = argparse.Namespace(
            tag="0.6.0",
            notes_file=str(self.notes_file),
            title="Release 0.6.0",
            target_commit="HEAD",
            repo_dir=".",
            remote="origin",
            draft=False,
            prerelease=False,
            skip_push=False,
            dry_run=True,
            force=False,
        )
        res = publish_release(args)
        self.assertEqual(res["status"], "dry_run")
        self.assertEqual(res["release_url"], "https://github.com/google/xmc/releases/tag/0.6.0")

    @patch("tools.release.git_ops.get_git_remote_url")
    @patch("subprocess.run")
    def test_publish_release_version_file_validation(
        self,
        mock_subprocess: MagicMock,
        mock_remote_url: MagicMock,
    ) -> None:
        mock_remote_url.return_value = "https://github.com/google/xmc"
        mock_subprocess.return_value = MagicMock(stdout="", returncode=0)
        base_args = {
            "notes_file": str(self.notes_file),
            "title": None,
            "target_commit": "HEAD",
            "repo_dir": ".",
            "remote": "origin",
            "draft": False,
            "prerelease": False,
            "skip_push": False,
            "dry_run": True,
            "force": False,
        }

        # Mismatched VERSION fails preflight with exact update guidance.
        with patch(
            "tools.release.publish_release.get_git_file_content", return_value="1.2.0\n"
        ) as mock_read:
            with self.assertRaises(HandledError) as ctx:
                publish_release(argparse.Namespace(tag="v1.3.0", **base_args))
            self.assertIn("does not match release tag", str(ctx.exception))
            self.assertIn("Update the root VERSION file to '1.3.0'", ctx.exception.instruction)
            mock_read.assert_called_once_with(ANY, "VERSION", ANY)

        # Mismatched VERSION takes precedence over the prerelease guard.
        with patch("tools.release.publish_release.get_git_file_content", return_value="1.2.0\n"):
            with self.assertRaises(HandledError) as ctx:
                publish_release(argparse.Namespace(tag="1.2.0-dev", **base_args))
            self.assertIn("does not match release tag", str(ctx.exception))

        # Prerelease/-dev tags are rejected unless --prerelease is set.
        for pre_ver in ("1.2.0-dev", "1.2.0-dev.1", "1.2.0-rc.1"):
            with self.subTest(pre_ver=pre_ver):
                with patch(
                    "tools.release.publish_release.get_git_file_content",
                    return_value=f"{pre_ver}\n",
                ):
                    with self.assertRaises(HandledError) as ctx:
                        publish_release(argparse.Namespace(tag=pre_ver, **base_args))
                    self.assertIn("prerelease/development version tag", str(ctx.exception))
                    self.assertIn("--tag", ctx.exception.instruction)

                    prerelease_args = {**base_args, "prerelease": True}
                    res = publish_release(argparse.Namespace(tag=pre_ver, **prerelease_args))
                    self.assertEqual(res["status"], "dry_run")

        # Leading 'v' on tag matches bare SemVer in VERSION.
        with patch("tools.release.publish_release.get_git_file_content", return_value="1.2.0\n"):
            res = publish_release(argparse.Namespace(tag="v1.2.0", **base_args))
            self.assertEqual(res["status"], "dry_run")

        # Historical commits without VERSION are permitted and emit a stderr note.
        with (
            patch("tools.release.publish_release.get_git_file_content", return_value=None),
            patch("sys.stderr", new_callable=io.StringIO) as err_buf,
        ):
            res = publish_release(argparse.Namespace(tag="0.5.0", **base_args))
            self.assertEqual(res["status"], "dry_run")
            self.assertIn("Root VERSION not found", err_buf.getvalue())

    @patch("tools.release.publish_release.get_git_file_content", return_value="1.2.0\n")
    @patch("tools.release.git_ops.get_git_remote_url", return_value="https://github.com/google/xmc")
    @patch("tools.release.publish_release.verify_remote_tag")
    @patch("tools.release.publish_release.verify_local_tag", return_value=False)
    @patch("tools.release.publish_release.resolve_target_commit")
    def test_publish_release_state_file_pinning(
        self,
        mock_resolve: MagicMock,
        _mock_local: MagicMock,
        _mock_remote: MagicMock,
        _mock_url: MagicMock,
        _mock_version: MagicMock,
    ) -> None:
        state_file = Path(self.temp_dir.name) / "release_state.json"
        base_args = {
            "tag": "1.2.0",
            "notes_file": str(self.notes_file),
            "title": None,
            "target_commit": None,
            "state_file": str(state_file),
            "repo_dir": ".",
            "remote": "origin",
            "draft": True,
            "prerelease": False,
            "skip_push": False,
            "dry_run": True,
            "force": False,
        }

        # 1. No state file yet: dry-run resolves origin/main and persists pinned SHA/version.
        mock_resolve.return_value = "aaa111"
        res = publish_release(argparse.Namespace(**base_args))
        mock_resolve.assert_called_with("origin/main", ANY)
        self.assertEqual(res["target_commit"], "aaa111")
        self.assertEqual(
            json.loads(state_file.read_text(encoding="utf-8")),
            {"target_sha": "aaa111", "version": "1.2.0"},
        )

        # 2. Matching state file with extra keys (e.g., dispatch_and_watch): reused & preserved.
        state_file.write_text(
            json.dumps({"target_sha": "bbb222", "version": "1.2.0", "run_id": 42}),
            encoding="utf-8",
        )
        mock_resolve.return_value = "bbb222"
        res = publish_release(argparse.Namespace(**base_args))
        mock_resolve.assert_called_with("bbb222", ANY)
        self.assertEqual(res["target_commit"], "bbb222")
        self.assertEqual(json.loads(state_file.read_text(encoding="utf-8"))["run_id"], 42)

        # 3. Stale or pre-VERSION state file (mismatched/missing version): ignored & overwritten.
        for stale in (
            {"target_sha": "old000", "version": "1.1.0"},
            {"target_sha": "old000"},
        ):
            with self.subTest(stale=stale):
                state_file.write_text(json.dumps(stale), encoding="utf-8")
                mock_resolve.return_value = "ccc333"
                res = publish_release(argparse.Namespace(**base_args))
                mock_resolve.assert_called_with("origin/main", ANY)
                self.assertEqual(res["target_commit"], "ccc333")
                self.assertEqual(
                    json.loads(state_file.read_text(encoding="utf-8")),
                    {"target_sha": "ccc333", "version": "1.2.0"},
                )

        # 4. Explicit --target-commit overrides a same-version state file and refreshes it.
        state_file.write_text(
            json.dumps({"target_sha": "broken111", "version": "1.2.0"}),
            encoding="utf-8",
        )
        mock_resolve.return_value = "fixed222"
        res = publish_release(argparse.Namespace(**{**base_args, "target_commit": "origin/main"}))
        mock_resolve.assert_called_with("origin/main", ANY)
        self.assertEqual(
            json.loads(state_file.read_text(encoding="utf-8")),
            {"target_sha": "fixed222", "version": "1.2.0"},
        )

        # 5. Non-dry-run publish with --state-file fails fast if missing or version-mismatched.
        publish_args = {**base_args, "dry_run": False}
        state_file.write_text(
            json.dumps({"target_sha": "old000", "version": "1.1.0"}), encoding="utf-8"
        )
        with self.assertRaises(HandledError) as ctx:
            publish_release(argparse.Namespace(**publish_args))
        self.assertIn("does not contain a matching target_sha", str(ctx.exception))

        state_file.unlink()
        with self.assertRaises(HandledError):
            publish_release(argparse.Namespace(**publish_args))

        # 6. Non-dry-run publish with matching --state-file tags the pinned commit.
        state_file.write_text(
            json.dumps({"target_sha": "fixed222", "version": "1.2.0"}), encoding="utf-8"
        )
        mock_resolve.return_value = "fixed222"
        with (
            patch("tools.release.publish_release.create_git_tag") as mock_tag,
            patch("tools.release.publish_release.push_git_tag"),
            patch("shutil.which", return_value="/usr/bin/gh"),
            patch(
                "tools.release.publish_release.publish_via_gh_cli",
                return_value="https://github.com/google/xmc/releases/tag/1.2.0",
            ),
        ):
            res = publish_release(argparse.Namespace(**publish_args))
        mock_resolve.assert_called_with("fixed222", ANY)
        mock_tag.assert_called_once_with("1.2.0", "fixed222", "Release 1.2.0", ANY, False)
        self.assertEqual(res["target_commit"], "fixed222")

    def test_create_parser(self) -> None:
        parser = create_parser()
        self.assertIsNotNone(parser)
        args = parser.parse_args(
            [
                "--tag",
                "0.6.0",
                "--notes-file",
                "notes.md",
                "--remote",
                "upstream",
                "--state-file",
                "/tmp/state.json",
            ]
        )
        self.assertEqual(args.remote, "upstream")
        self.assertEqual(args.state_file, "/tmp/state.json")
        self.assertIsNone(args.target_commit)


if __name__ == "__main__":
    unittest.main()
