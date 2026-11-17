"""Trusted tool decisions on isolated fixtures; never execute approved runners."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HOOK = Path(__file__).resolve().parents[2] / "hooks/trusted-ship-helpers.py"
CODEX_HOOK = Path(__file__).resolve().parents[2] / "hooks/codex_pre_tool_use.py"
HERE = Path(__file__).resolve()
INSTALLED = HERE.parents[4].name == ".agents"
WORKSPACE = HERE.parents[5] if INSTALLED else HERE.parents[4]
spec = importlib.util.spec_from_file_location("trusted_helpers", HOOK)
trusted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trusted)
codex_spec = importlib.util.spec_from_file_location("codex_trusted_helpers", CODEX_HOOK)
codex_trusted = importlib.util.module_from_spec(codex_spec)
codex_spec.loader.exec_module(codex_trusted)


class TrustedHelpersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("SHIP_TEST_TMPDIR"), prefix="trusted-tools-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.go_repo = self.root / sorted(trusted.policy.GO_REPOS)[0]
        self.frontend = self.root / sorted(trusted.policy.NODE_REPOS)[0]
        for repo in (self.go_repo, self.frontend, self.root / sorted(trusted.policy.NODE_REPOS)[-1]):
            repo.mkdir()
        for name in ("agent_models", "ship_state", "ship_artifacts", "ship_run_checks"):
            self.file(self.root / f".agents/workflows/ship-fix/scripts/{name}.py", "raise RuntimeError('must not execute')")
        for tool, package in trusted.policy.NODE_TOOLS.items():
            self.file(self.frontend / "node_modules" / package / "package.json", json.dumps({"name": package}))
            self.file(self.frontend / "node_modules/.bin" / tool, "must not execute")
        self.root_patch = patch.object(trusted.policy, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.codex_root_patch = patch.object(codex_trusted.policy, "ROOT", self.root)
        self.codex_root_patch.start()
        self.addCleanup(self.codex_root_patch.stop)

    def file(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def payload(self, command, cwd=None):
        return {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "cwd": str(cwd or self.root), "tool_input": {"command": command}}

    def approved(self, command, cwd=None):
        return trusted.decide(self.payload(command, cwd)).get("hookSpecificOutput", {}).get("permissionDecision") == "allow"

    def test_python_helper_forms_and_child_command_trust(self):
        for command in (
            "python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check",
            "python3 -I -B ./.agents/workflows/ship-fix/scripts/ship_state.py --help",
            "python3 -I -B .agents/workflows/ship-fix/scripts/ship_artifacts.py --task /run/task.json run unit -- npm run test",
            "python3 -I -B .agents/workflows/ship-fix/scripts/ship_run_checks.py --contract /run/contract.json --repo app",
        ):
            with self.subTest(command=command):
                self.assertTrue(self.approved(command))

    def test_frontend_runners(self):
        for command in (
            "npx jest", "npx jest --runInBand tests/unit.test.ts", "npx jest --maxWorkers=2 --coverage=false",
            "npx jest --testNamePattern 'keeps valid quotes'", "npx jest --runTestsByPath tests/a.test.ts",
            "npx eslint src --max-warnings=0", "npx eslint --ext .ts,.tsx src",
            "npx tsc --noEmit --pretty false", "npx tsc -p tsconfig.json --incremental=false",
            "npx prettier --check src", "npx prettier --list-different src", "npx jest --version",
            "npm run lint", "npm run format:check", "npm run build", "npm run sync",
        ):
            with self.subTest(command=command):
                self.assertTrue(self.approved(command, self.frontend))

    def test_general_read_only_tools(self):
        for command, cwd in (
            ("pwd", self.root), ("ls", self.root), ("ls -la app", self.root),
            ("cat README.md", self.root), ("head -20 file.txt", self.root),
            ("grep -rn TODO app", self.root), ("rg pattern src", self.frontend),
            ("find . -name '*.py'", self.root), ("jq .keys data.json", self.root),
            ("diff a.txt b.txt", self.root), ("which node", self.root),
            ("sed -n '1,10p' file.txt", self.root), ("sed -n 5p file.txt", self.root),
            ("env", self.root), ("node --check script.js", self.root),
            ("git status", self.frontend), ("git diff HEAD", self.frontend),
            ("git log --oneline -8", self.frontend), ("git stash list", self.frontend),
            ("git config --get user.name", self.frontend),
            (f"git -C {self.frontend} status", self.root),
            (f"git -C {self.frontend} diff HEAD", self.root),
            ("go vet ./...", self.go_repo),
        ):
            with self.subTest(command=command):
                self.assertTrue(self.approved(command, cwd))

    def test_general_tools_fall_through_on_unsafe_forms(self):
        for command, cwd in (
            ("ls; pwd", self.root), ("cat file > out", self.root), ("cat *.py", self.root),
            ("grep -r x $(pwd)", self.root), ("sed -i 's/a/b/' file.txt", self.root),
            ("sed -n 5p file.txt; rm x", self.root), ("find . -delete", self.root),
            ("jq -r '.a' | cat", self.root), ("env FOO=1", self.root), ("printenv", self.root),
            ("git push", self.frontend), ("git commit -m x", self.frontend),
            ("git add file", self.frontend), ("git checkout -b x", self.frontend),
            ("git clean -f", self.frontend), ("git config user.name x", self.frontend),
            ("git stash pop", self.frontend), ("git -C /outside status", self.root),
            (f"git -C {self.root / 'outside'} status", self.root),
            (f"git -C {self.frontend} diff ../outside.txt", self.root),
            ("node -e 'code'", self.root), ("node script.js", self.root),
            ("git status", self.root / "nonexistent"),
            ("go vet ./...", self.root),
        ):
            with self.subTest(command=command):
                self.assertEqual({}, trusted.decide(self.payload(command, cwd)))

    def test_missing_package_and_binary_never_approve_npx(self):
        (self.frontend / "node_modules/jest/package.json").unlink()
        self.assertFalse(self.approved("npx jest", self.frontend))
        (self.frontend / "node_modules/.bin/eslint").unlink()
        self.assertFalse(self.approved("npx eslint src", self.frontend))
        self.assertFalse(self.approved("npx jest", self.root / sorted(trusted.policy.NODE_REPOS)[-1]))

    def test_wrong_package_identity(self):
        self.file(self.frontend / "node_modules/jest/package.json", '{"name":"not-jest"}')
        self.assertFalse(self.approved("npx jest", self.frontend))

    def test_node_configuration_and_modifying_flags_fall_through(self):
        for command in (
            "npx --yes jest", "npx --package evil jest", "npx evil", "npm test", "node -e 'code'",
            "npx jest --config external.json", "npx jest --testRunner evil", "npx jest --setupFiles evil.js",
            "npx jest --watch", "npx jest --watchAll=true", "npx jest --maxWorkers",
            "npx eslint --fix src", "npx eslint --config external.js src", "npx eslint --output-file out src",
            "npx prettier --write src", "npx prettier --check --write src", "npx prettier src",
            "npx prettier --plugin evil --check src", "npx tsc --outDir elsewhere", "npx next build",
            "npm run lint:fix", "npm run dev", "npm run format",
        ):
            with self.subTest(command=command):
                self.assertEqual({}, trusted.decide(self.payload(command, self.frontend)))

    def test_go_commands(self):
        for command in (
            "go vet ./...", "go vet ./internal/http", "go test ./...",
            "go test ./internal/users -run TestProfile -bench .",
        ):
            with self.subTest(command=command):
                self.assertTrue(self.approved(command, self.go_repo))

    def test_go_modifying_flags_fall_through(self):
        for command in (
            "go build ./...", "go run .", "go vet -flags evil ./...", "go test -c",
            "go test -exec evil ./...", "go generate ./...", "go mod tidy", "go install ./...",
        ):
            with self.subTest(command=command):
                self.assertEqual({}, trusted.decide(self.payload(command, self.go_repo)))

    def test_wrong_cwd(self):
        self.assertFalse(self.approved("npx jest"))
        self.assertFalse(self.approved("go vet ./..."))
        self.assertFalse(self.approved("python3 -I -B .agents/workflows/ship-fix/scripts/ship_state.py --help", self.go_repo))
        self.assertFalse(self.approved("npm run lint", self.go_repo))
        self.assertFalse(self.approved("go vet ./...", self.frontend))

    def test_shell_forms_never_auto_approve(self):
        prefix = "python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check"
        for suffix in ("; pwd", " && pwd", " | cat", " > result", " $(pwd)", " `pwd`", "\n", "\t", " # comment", " *.py", " &"):
            with self.subTest(suffix=suffix):
                self.assertEqual({}, trusted.decide(self.payload(prefix + suffix)))

    def test_quoted_helper_arguments_are_literal_data(self):
        prefix = "python3 -I -B .agents/workflows/ship-fix/scripts/ship_artifacts.py record proof "
        for argument in (
            "--claim 'AC=passed; result (exit 0)'",
            '--claim "AC=passed; result (exit 0)"',
            "--claim AC='passed; result (exit 0)'",
            "--claim 'AC=literal & | < > * ? [x] {x} # ~ !'",
            '--claim "AC=literal & | < > * ? [x] {x} # ~"',
            "--claim 'AC=literal $HOME $(pwd) `pwd`'",
            r'''--claim 'AC=can'\''t; (literal)' ''',
            r'''--claim "AC=\"quoted\"; (literal)"''',
            r'''--claim 'AC=literal \\ and \" quotes' ''',
            r'''--claim "AC=literal \\ path; (exit 0)"''',
        ):
            with self.subTest(argument=argument):
                self.assertTrue(self.approved(prefix + argument))

    def test_quoted_runner_arguments_keep_existing_validation(self):
        self.assertTrue(self.approved('npx jest --testNamePattern "keeps (valid|empty) values"', self.frontend))
        self.assertTrue(self.approved("go test -run 'TestQuotes|TestEmpty' ./...", self.go_repo))
        for command, cwd in (
            ("npx jest '../outside;file.ts'", self.frontend),
            ("npx eslint '--fix' src", self.frontend),
            ("go test '../outside'", self.go_repo),
            ("python3 -I -c 'print(1); print(2)'", self.root),
            ("bash -c 'pwd; whoami'", self.root),
        ):
            with self.subTest(command=command):
                self.assertEqual({}, trusted.decide(self.payload(command, cwd)))

    def test_quoted_child_argv_requires_trusted_helper(self):
        child = "npm run test -- tests/diary.test.ts"
        prefix = "python3 -I -B .agents/workflows/ship-fix/scripts/ship_artifacts.py run proof -- "
        self.assertTrue(self.approved(prefix + child))
        self.assertEqual({}, trusted.decide(self.payload(child)))
        self.assertEqual({}, trusted.decide(self.payload(prefix + child + "; pwd")))

    def test_quoted_data_cannot_hide_shell_operations(self):
        prefix = "python3 -I -B .agents/workflows/ship-fix/scripts/ship_artifacts.py record proof "
        for argument in (
            "--claim 'AC=ok'; pwd", "--claim 'AC=ok' && pwd", "--claim 'AC=ok' || pwd",
            "--claim 'AC=ok' | cat", "--claim 'AC=ok' > result", "--claim 'AC=ok' 2>&1",
            "--claim 'AC=ok' &", "--claim 'AC=ok' # comment", "--claim 'AC=ok'*.py",
            "--claim 'AC=ok'$(pwd)", "--claim 'AC=ok'`pwd`", "--claim 'AC=ok'<(pwd)",
            '--claim "AC=$HOME"', '--claim "AC=${HOME}"', '--claim "AC=$(pwd)"',
            '--claim "AC=$((1+1))"', '--claim "AC=`pwd`"', '--claim "AC=!!"',
            r'''--claim "AC=\$(pwd)"''', r'''--claim "AC=\`pwd\`"''',
            r'''--claim "AC=\\$(pwd)"''', r'''--claim "AC=\\"; pwd''',
            r'''--claim 'AC=can'\''t'; pwd''', r'''--claim \"AC=ok; pwd\"''',
            "--claim 'AC=unterminated", '--claim "AC=unterminated',
            "--claim 'AC=ok'\\", "--claim 'AC=line\nbreak'", "--claim 'AC=tab\there'",
        ):
            with self.subTest(argument=argument):
                self.assertEqual({}, trusted.decide(self.payload(prefix + argument)))

    def test_paths_cannot_escape_or_select_external_scripts(self):
        for command, cwd in (
            ("python3 -I -B .agents/workflows/ship-fix/scripts/../../outside.py", self.root),
            ("python3 -I -B /absolute/script.py", self.root),
            ("python3 -I -B .agents/workflows/ship-fix/scripts/absent.py", self.root),
            ("python3 -I -c code", self.root),
            ("python3 .agents/workflows/ship-fix/scripts/ship_state.py", self.root),
            ("npx jest ../outside", self.frontend),
            ("npx jest /outside/test.ts", self.frontend),
            ("npx tsc --project ../outside.json", self.frontend),
            ("go test ../outside", self.go_repo),
        ):
            with self.subTest(command=command):
                self.assertEqual({}, trusted.decide(self.payload(command, cwd)))

    def test_symlink_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "other.py").write_text("must not execute")
        (self.root / ".agents/workflows/ship-fix/scripts/link.py").symlink_to(outside / "other.py")
        self.assertFalse(self.approved("python3 -I -B .agents/workflows/ship-fix/scripts/link.py"))
        (self.frontend / "escape").symlink_to(outside, target_is_directory=True)
        self.assertFalse(self.approved("npx jest escape/test.ts", self.frontend))
        binary = self.frontend / "node_modules/.bin/jest"
        binary.unlink()
        binary.symlink_to(outside / "other.py")
        self.assertFalse(self.approved("npx jest", self.frontend))

    def test_other_events_and_sandbox_override(self):
        payload = self.payload("python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check")
        for field, value in (("tool_name", "Read"), ("hook_event_name", "PostToolUse"), ("cwd", None)):
            with self.subTest(field=field):
                self.assertEqual({}, trusted.decide({**payload, field: value}))
        payload["tool_input"]["dangerouslyDisableSandbox"] = True
        self.assertEqual({}, trusted.decide(payload))
        for payload in (None, [], {}, {"tool_input": None}):
            self.assertEqual({}, trusted.decide(payload))

    def test_codex_adapter_uses_the_shared_policy_and_native_response_shape(self):
        for command in (
            "python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check",
            "git status",
        ):
            payload = self.payload(command)
            self.assertEqual(trusted.decide(payload), codex_trusted.decide(payload))
        self.assertEqual({}, codex_trusted.decide(self.payload("python3 -I -B malicious.py")))

    @unittest.skipUnless(INSTALLED, "project installation only")
    def test_configured_codex_hook_resolves_shared_adapter_from_subrepo_cwd(self):
        workspace = WORKSPACE
        config = json.loads((workspace / ".codex/hooks.json").read_text())
        command = config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        cwd = next((workspace / name for name in sorted(trusted.policy.GO_REPOS | trusted.policy.NODE_REPOS)
                    if (workspace / name).is_dir()), None)
        if cwd is None:
            self.skipTest("No trusted repository layout in this installed project")
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(cwd),
                   "tool_input": {"command": "git status"}}
        result = subprocess.run(command, shell=True, input=json.dumps(payload), text=True,
                                capture_output=True, cwd=cwd, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual("allow", response["hookSpecificOutput"]["permissionDecision"])

    @unittest.skipUnless(INSTALLED, "project installation only")
    def test_claude_worktree_adapters_resolve_the_shared_workspace_root(self):
        workspace = WORKSPACE
        create = subprocess.run([str(workspace / ".claude/hooks/worktree-create.sh")],
                                input=json.dumps({"hook_event_name": "WorktreeCreate",
                                                  "cwd": str(workspace), "name": "agent"}),
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(0, create.returncode, create.stderr)
        self.assertEqual(str(workspace), create.stdout.strip())

        remove = subprocess.run([str(workspace / ".claude/hooks/worktree-remove.sh")],
                                input=json.dumps({"hook_event_name": "WorktreeRemove",
                                                  "worktree_path": str(workspace)}),
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(0, remove.returncode, remove.stderr)

    def test_subprocess_json_protocol_and_malformed_input(self):
        hook_dir = self.root / ".agents/workflows/ship-fix/hooks"
        hook_copy = hook_dir / "trusted-ship-helpers.py"
        hook_copy.parent.mkdir()
        shutil.copyfile(HOOK, hook_copy)
        shutil.copyfile(HOOK.parent / "trusted_policy.py", hook_dir / "trusted_policy.py")
        for body, allowed in (
            (json.dumps(self.payload("python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check")), True),
            (json.dumps(self.payload("python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check; pwd")), False),
            (json.dumps(self.payload("npx jest '", self.frontend)), False),
            ("not json", False), ("[]", False),
        ):
            with self.subTest(body=body):
                result = subprocess.run([sys.executable, "-I", "-B", str(hook_copy)], input=body,
                                        text=True, capture_output=True, timeout=5, cwd=self.root)
                self.assertEqual(0, result.returncode, result.stderr)
                response = json.loads(result.stdout)
                self.assertEqual(allowed, response.get("hookSpecificOutput", {}).get("permissionDecision") == "allow")
                if not allowed:
                    self.assertEqual({}, response)


if __name__ == "__main__":
    unittest.main()
