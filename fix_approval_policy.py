import re
with open('.github/scripts/approval-policy.js', 'r') as f:
    content = f.read()

# Make sure our additions are in PROTECTED_FILE_PATTERNS
additions = """  // Modifying GA-locked profile registries, locked profile charts, or lock/digest/alignment gates requires fresh human review.
  /^deploy\\/profiles\\/locked_profiles\\.ya?ml$/i,
  /^deploy\\/profiles\\/BUILD(\\.bazel)?$/i,
  /^deploy\\/profiles\\/stable-2026a\\//i,
  /^tools\\/components\\/check_profile_lock\\.py$/i,
  /^tools\\/components\\/BUILD(\\.bazel)?$/i,
  /^tools\\/lint\\/check_codeowners_alignment\\.py$/i,
  /^tools\\/lint\\/BUILD(\\.bazel)?$/i,
  /^test\\/helm\\/profiles\\/profilecontract\\//i,
  /^test\\/helm\\/profiles\\/stable2026a\\/(profile_digest_test\\.go|BUILD(\\.bazel)?)$/i
];"""

content = re.sub(r'  /\^api\\/BUILD\(\\\.bazel\)\?\$\/i,\n(?:.*\n)*\];', '  /^api\\/BUILD(\\.bazel)?$/i,\n' + additions, content)

with open('.github/scripts/approval-policy.js', 'w') as f:
    f.write(content)
