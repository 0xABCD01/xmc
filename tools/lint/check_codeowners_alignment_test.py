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

"""Unit tests for tools.lint.check_codeowners_alignment."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.common.tool_utils import HandledError
from tools.lint import check_codeowners_alignment as cli


class CheckCodeownersAlignmentTest(unittest.TestCase):
    """Tests for CODEOWNERS and OWNERS alignment verification."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write_files(self, owners_content: str, codeowners_content: str) -> None:
        (self.root / "OWNERS").write_text(owners_content, encoding="utf-8")
        (self.root / "CODEOWNERS").write_text(codeowners_content, encoding="utf-8")

    def test_CheckedInRepositoryFiles_PassAlignment(self) -> None:
        args = cli.build_parser().parse_args([])
        result = cli.check_codeowners_alignment(args)
        self.assertGreater(result["owners_count"], 0)
        self.assertEqual(result["protected_paths_count"], len(cli.PROTECTED_PATHS))

    def test_ValidAlignment_Passes(self) -> None:
        owners = "# Reviewers\nalice\nbob # inline comment\n"
        rules = "\n".join(f"{path} @alice @bob" for path in cli.PROTECTED_PATHS)
        self._write_files(owners, f"# Header\n{rules}\n")
        args = cli.build_parser().parse_args(["--repo-root", str(self.root)])
        result = cli.check_codeowners_alignment(args)
        self.assertEqual(
            result,
            {"owners_count": 2, "protected_paths_count": len(cli.PROTECTED_PATHS)},
        )

    def test_MissingFiles_RaisesHandledError(self) -> None:
        (self.root / "OWNERS").write_text("alice\n", encoding="utf-8")
        args = cli.build_parser().parse_args(["--repo-root", str(self.root)])
        with self.assertRaises(HandledError) as ctx:
            cli.check_codeowners_alignment(args)
        self.assertIn("Missing OWNERS", str(ctx.exception))

    def test_EmptyOwners_RaisesHandledError(self) -> None:
        self._write_files("# only comments\n", "/CODEOWNERS @alice\n")
        args = cli.build_parser().parse_args(["--repo-root", str(self.root)])
        with self.assertRaises(HandledError) as ctx:
            cli.check_codeowners_alignment(args)
        self.assertIn("no active reviewer handles", str(ctx.exception))

    def test_MismatchedHandleOrMissingRule_RaisesHandledError(self) -> None:
        owners = "alice\nbob\n"
        rules = "\n".join(f"{path} @alice" for path in cli.PROTECTED_PATHS)
        self._write_files(owners, rules)
        args = cli.build_parser().parse_args(["--repo-root", str(self.root)])
        with self.assertRaises(HandledError) as ctx:
            cli.check_codeowners_alignment(args)
        self.assertIn("does not match the scoped rules", str(ctx.exception))

    def test_ExtraWildcardRule_RaisesHandledError(self) -> None:
        owners = "alice\n"
        rules = "\n".join(f"{path} @alice" for path in cli.PROTECTED_PATHS) + "\n* @alice\n"
        self._write_files(owners, rules)
        args = cli.build_parser().parse_args(["--repo-root", str(self.root)])
        with self.assertRaises(HandledError):
            cli.check_codeowners_alignment(args)

    def test_MainEntrypoint_ExitsZeroOnValidRepo(self) -> None:
        owners = "alice\n"
        rules = "\n".join(f"{path} @alice" for path in cli.PROTECTED_PATHS)
        self._write_files(owners, rules)
        with mock.patch(
            "sys.argv",
            ["check_codeowners_alignment.py", "--repo-root", str(self.root)],
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
            self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
