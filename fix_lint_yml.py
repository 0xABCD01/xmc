import re
with open('.github/workflows/lint.yml', 'r') as f:
    content = f.read()

# Make sure our additions are in lint.yml
check_alignment = """      - name: 'Check CODEOWNERS Alignment'
        env:
          XMC_BYPASS_VENV: '1'
        run: |
          python3 tools/lint/check_codeowners_alignment.py

      - name: 'Check Component Profile Release Locks'
        env:
          XMC_BYPASS_VENV: '1'
          BASE_REF: ${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha || github.event.before || 'origin/main' }}
          HEAD_REF: ${{ github.event.pull_request.head.sha || github.event.merge_group.head_sha || github.sha || 'HEAD' }}
        run: |
          python3 tools/components/check_profile_lock.py --base-ref "${BASE_REF}" --head-ref "${HEAD_REF}"
"""
if "Check CODEOWNERS Alignment" not in content:
    content = content.replace("      - name: 'Verify Go Dependencies (go mod tidy)'", check_alignment + "\n      - name: 'Verify Go Dependencies (go mod tidy)'")
    with open('.github/workflows/lint.yml', 'w') as f:
        f.write(content)
