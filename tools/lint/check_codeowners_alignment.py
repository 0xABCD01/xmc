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

import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    owners_file = repo_root / "OWNERS"
    codeowners_file = repo_root / "CODEOWNERS"

    if not owners_file.is_file() or not codeowners_file.is_file():
        print(
            f"Error: Missing OWNERS ({owners_file.is_file()}) or CODEOWNERS ({codeowners_file.is_file()})",
            file=sys.stderr,
        )
        sys.exit(1)

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

    expected_owners = " ".join(f"@{u}" for u in owners)

    expected_lines = [
        f"/CODEOWNERS {expected_owners}",
        f"/OWNERS {expected_owners}",
        f"/deploy/profiles/locked_profiles.yaml {expected_owners}",
        f"/deploy/profiles/BUILD.bazel {expected_owners}",
        f"/deploy/profiles/stable-2026a/ {expected_owners}",
        f"/tools/components/check_profile_lock.py {expected_owners}",
        f"/tools/components/BUILD.bazel {expected_owners}",
        f"/test/helm/profiles/stable2026a/ {expected_owners}",
    ]

    if codeowner_lines != expected_lines:
        print(
            "Error: CODEOWNERS does not match the scoped rules for the OWNERS reviewer pool.",
            file=sys.stderr,
        )
        print("Expected:", file=sys.stderr)
        for line in expected_lines:
            print(f"  {line}", file=sys.stderr)
        print("Got:", file=sys.stderr)
        for line in codeowner_lines:
            print(f"  {line}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
