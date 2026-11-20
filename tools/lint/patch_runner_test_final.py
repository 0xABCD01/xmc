import re
with open('tools/lint/precommit_runner_test.py', 'r') as f:
    content = f.read()

tests = """
    def test_check_codeowners_alignment(self):
        ctx = gates.RunContext(mode="fix")
        with (
            mock.patch.object(gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 0, "", "")) as exec_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_codeowners_alignment(ctx)
        self.assertFalse(ctx.failed)
        exec_mock.assert_called_once()

    def test_check_profile_lock(self):
        ctx_fix = gates.RunContext(mode="fix")
        with (
            mock.patch.object(gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 0, "", "")) as exec_mock,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_profile_lock(ctx_fix)
        self.assertFalse(ctx_fix.failed)
        exec_mock.assert_called_once()
        self.assertIn("--staged", exec_mock.call_args.args[0])

        ctx_chk = gates.RunContext(mode="check")
        with (
            mock.patch.object(gates, "exec_cmd", return_value=subprocess.CompletedProcess([], 1, "", "")) as exec_mock_fail,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            runner.check_profile_lock(ctx_chk)
        self.assertTrue(ctx_chk.failed)
        exec_mock_fail.assert_called_once()
        self.assertNotIn("--staged", exec_mock_fail.call_args.args[0])

class OrchestrationAndMainTest"""

content = re.sub(r'class OrchestrationAndMainTest', tests, content)

orch_tests = """            for target, attr in (
                (runner, "check_codeowners_alignment"),
                (runner, "check_profile_lock"),
                (runner, "check_go"),"""
content = content.replace("""            for target, attr in (
                (runner, "check_go"),""", orch_tests)

with open('tools/lint/precommit_runner_test.py', 'w') as f:
    f.write(content)
