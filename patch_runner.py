import re
with open('tools/lint/precommit_runner.py', 'r') as f:
    content = f.read()

# Add check_codeowners_alignment and check_profile_lock explicitly after check_python_gates
# Or we can just add them to precommit_runner.py
additions = """
def check_codeowners_alignment(ctx: gates.RunContext) -> None:
    print("--> Checking CODEOWNERS Alignment...")
    if gates.exec_cmd([sys.executable, "tools/lint/check_codeowners_alignment.py"]).returncode != 0:
        ctx.fail("CODEOWNERS alignment check failed (see JSON error above).")

def check_profile_lock(ctx: gates.RunContext) -> None:
    print("--> Checking Component Profile Release Locks...")
    args = [sys.executable, "tools/components/check_profile_lock.py"]
    if ctx.mode != "check":
        args.append("--staged")
    if gates.exec_cmd(args).returncode != 0:
        ctx.fail("GA-locked component profile check failed (see JSON error above).")
"""

content = re.sub(r'def _run_linter_phase\(mode: str\) -> bool:', additions + '\n\ndef _run_linter_phase(mode: str) -> bool:', content)

run_phase = """        for gate in gates.PYTHON_GATES:
            gates.run_python_gate(ctx, gate)
        for fn in (check_codeowners_alignment, check_profile_lock):
            fn(ctx)
        for fn in ("""

content = content.replace("""        for gate in gates.PYTHON_GATES:
            gates.run_python_gate(ctx, gate)
        for fn in (""", run_phase)

with open('tools/lint/precommit_runner.py', 'w') as f:
    f.write(content)
