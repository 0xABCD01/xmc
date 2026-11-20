import re
with open('tools/lint/precommit_gates.py', 'r') as f:
    content = f.read()

new_gates = """    PythonGate(
        banner="--> Checking CODEOWNERS Alignment...",
        script="tools/lint/check_codeowners_alignment.py",
        failure_msg="CODEOWNERS alignment check failed (see JSON error above).",
    ),
    PythonGate(
        banner="--> Checking Component Profile Release Locks...",
        script="tools/components/check_profile_lock.py",
        failure_msg="GA-locked component profile check failed (see JSON error above).",
        staged_am_pattern=r"", # Always run to check diff
    ),
)"""

content = re.sub(r'\)\n\n\n@dataclass', new_gates + '\n\n\n@dataclass', content)
# We can just rely on the script checking staged vs PR instead of staged_am_pattern, but check_profile_lock.py uses `--staged` natively in precommit.
# Let's write a small wrapper function in precommit_runner.py instead, since check_profile_lock needs `--staged`.
