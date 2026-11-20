import re
with open('tools/lint/precommit_runner_test.py', 'r') as f:
    content = f.read()

# Add mock targets to OrchestrationAndMainTest
additions = """            for target, attr in (
                (runner, "check_codeowners_alignment"),
                (runner, "check_profile_lock"),
                (runner, "check_go"),"""

content = content.replace("""            for target, attr in (
                (runner, "check_go"),""", additions)

with open('tools/lint/precommit_runner_test.py', 'w') as f:
    f.write(content)
