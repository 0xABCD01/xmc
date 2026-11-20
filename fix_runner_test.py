import re
with open('tools/lint/precommit_runner_test.py', 'r') as f:
    content = f.read()
if "def test_check_codeowners_alignment(" not in content:
    with open('patch_runner_test_2.py', 'r') as p:
        exec(p.read())
