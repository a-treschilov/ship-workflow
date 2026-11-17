"""Installer contract on a temporary project; no network or model gateway."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve()
INSTALLED = HERE.parents[4].name == ".agents"
PACKAGE = HERE.parents[4]
INSTALLER = PACKAGE / "install/install.sh"


@unittest.skipIf(INSTALLED, "package installation test only")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get("SHIP_TEST_TMPDIR")
        if not scratch:
            self.fail("Set SHIP_TEST_TMPDIR to the session scratchpad")
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix="installer-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project with spaces"
        (self.project / ".agents").mkdir(parents=True)

    def install(self, ref="HEAD", host="both", models=None):
        env = os.environ.copy()
        env["SHIP_SRC"] = str(PACKAGE)
        command = [str(INSTALLER), ref, "--dev", "--host", host, "--sync"]
        if models:
            command.extend(["--models", models])
        return subprocess.run(
            command,
            cwd=self.project,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_installs_portable_roles_and_resolvable_links(self):
        result = self.install()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["implementer.md", "plan-checker.md", "reviewer.md", "slicer.md"],
            sorted(path.name for path in (self.project / ".agents/roles").glob("*.md")),
        )
        protocol = self.project / ".claude/references/ship-protocol.md"
        self.assertTrue(protocol.is_symlink())
        self.assertEqual(
            (self.project / ".agents/workflows/ship-fix/PROTOCOL.md").resolve(),
            protocol.resolve(),
        )
        self.assertTrue((self.project / ".claude/agents/implementer.md").is_file())
        self.assertTrue((self.project / ".codex/agents/implementer.toml").is_file())
        version = json.loads((self.project / ".agents/.ship-version").read_text())
        self.assertEqual("HEAD", version["ref"])
        self.assertNotEqual("unknown", version["commit"])
        create = subprocess.run([str(self.project / ".claude/hooks/worktree-create.sh")],
                                cwd=self.project, input=json.dumps({"hook_event_name": "WorktreeCreate",
                                "cwd": str(self.project), "name": "test"}), capture_output=True, text=True)
        self.assertEqual(0, create.returncode, create.stderr)
        self.assertEqual(str(self.project.resolve()), create.stdout.strip())
        remove = subprocess.run([str(self.project / ".claude/hooks/worktree-remove.sh")],
                                cwd=self.project, input=json.dumps({"hook_event_name": "WorktreeRemove",
                                "worktree_path": str(self.project)}), capture_output=True, text=True)
        self.assertEqual(0, remove.returncode, remove.stderr)
        settings = json.loads((self.project / ".claude/settings.json").read_text())
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = str(self.project)
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(self.project.resolve()),
                   "tool_input": {"command": "python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check"}}
        hook = subprocess.run(["bash", "-c", command], cwd=self.project, env=env,
                              input=json.dumps(payload), capture_output=True, text=True)
        self.assertEqual(0, hook.returncode, hook.stderr)
        self.assertEqual("allow", json.loads(hook.stdout)["hookSpecificOutput"]["permissionDecision"])

    def test_upgrade_preserves_project_configuration(self):
        config = self.project / ".agents/agent-models.yaml"
        config.write_text("custom: project configuration\n")
        result = self.install()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("custom: project configuration\n", config.read_text())

    def test_failed_upgrade_restores_existing_installation(self):
        self.assertEqual(0, self.install().returncode)
        helper = self.project / ".agents/workflows/ship-fix/PROTOCOL.md"
        marker = self.project / ".agents/.ship-version"
        original_helper, original_marker = helper.read_bytes(), marker.read_bytes()
        config = self.project / ".agents/agent-models.yaml"
        config.write_text("invalid: project configuration\n")
        result = self.install()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(original_helper, helper.read_bytes())
        self.assertEqual(original_marker, marker.read_bytes())
        self.assertEqual("invalid: project configuration\n", config.read_text())

    def test_existing_settings_are_merged_idempotently(self):
        path = self.project / ".claude/settings.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"permissions": {"deny": ["Bash(rm *)"]}, "env": {"CUSTOM": "yes"}}))
        result = self.install()
        self.assertEqual(0, result.returncode, result.stderr)
        first = json.loads(path.read_text())
        self.assertEqual(["Bash(rm *)"], first["permissions"]["deny"])
        self.assertEqual("yes", first["env"]["CUSTOM"])
        self.assertIn("WorktreeCreate", first["hooks"])
        self.assertIn("PreToolUse", first["hooks"])
        result = self.install()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(first, json.loads(path.read_text()))

    def test_codex_only_install_uses_inherited_model(self):
        result = self.install(host="codex")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.project / ".codex/agents/implementer.toml").is_file())
        self.assertFalse((self.project / ".claude/agents/implementer.md").exists())
        version = json.loads((self.project / ".agents/.ship-version").read_text())
        self.assertEqual("codex", version["host"])
        check = subprocess.run(["python3", "-I", "-B", ".agents/workflows/ship-fix/scripts/agent_models.py", "check"],
                               cwd=self.project, capture_output=True, text=True)
        self.assertEqual(0, check.returncode, check.stderr)

    def test_install_in_git_project(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        result = self.install(host="claude")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.project / ".claude/agents/implementer.md").is_file())
        self.assertFalse((self.project / ".codex/agents/implementer.toml").exists())

    def test_upgrade_can_change_host_and_preserve_model_config(self):
        result = self.install(host="both")
        self.assertEqual(0, result.returncode, result.stderr)
        config = self.project / ".agents/agent-models.yaml"
        original = config.read_bytes()
        result = self.install(host="codex", models="ocx")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(original, config.read_bytes())
        self.assertFalse((self.project / ".claude/agents/implementer.md").exists())
        self.assertTrue((self.project / ".codex/agents/implementer.toml").is_file())
        self.assertEqual("codex", json.loads((self.project / ".agents/.ship-version").read_text())["host"])
        result = subprocess.run(["python3", "-I", "-B", str(PACKAGE / "install/uninstall.py"), str(self.project)],
                                cwd=self.project, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_ocx_opt_in_and_legacy_role_names(self):
        result = self.install(host="both", models="ocx")
        self.assertEqual(0, result.returncode, result.stderr)
        config = self.project / ".agents/agent-models.yaml"
        self.assertIn('implementer: "gpt-6-luna"', config.read_text())
        for role in ("reviewer", "plan-checker", "slicer"):
            self.assertIn(f'{role}: "gpt-6.2-sol"', config.read_text())
            self.assertIn('model = "gpt-6.2-sol"', (self.project / f".codex/agents/{role}.toml").read_text())
            self.assertIn('model: ocx-claude-native--gpt-6.2-sol[1m]', (self.project / f".claude/agents/{role}.md").read_text())
        assignments, remaining = config.read_text().split("ocx_models:\n", 1)
        for role in ("implementer", "reviewer", "plan-checker", "slicer"):
            assignments = assignments.replace(f"  {role}:", f"  prior-{role}:")
        legacy = assignments + "ocx_models:\n" + remaining
        config.write_text(legacy)
        result = self.install(host="both")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(legacy, config.read_text())
        self.assertIn('model = "gpt-6-luna"', (self.project / ".codex/agents/implementer.toml").read_text())

    def test_worktree_hook_conflict_rolls_back(self):
        path = self.project / ".claude/settings.json"
        path.parent.mkdir()
        original = '{"hooks":{"WorktreeCreate":[{"hooks":[{"type":"command","command":"custom"}]}]}}'
        path.write_text(original)
        result = self.install()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(original, path.read_text())
        self.assertFalse((self.project / ".agents/workflows/ship-fix").exists())

    def test_unmanaged_agent_collision_refused_without_overwrite(self):
        path = self.project / ".claude/agents/implementer.md"
        path.parent.mkdir(parents=True)
        path.write_text("custom agent")
        result = self.install(host="both")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("custom agent", path.read_text())
        self.assertFalse((self.project / ".agents/workflows/ship-fix").exists())
        result = self.install(host="codex")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("custom agent", path.read_text())

    def test_symlinked_settings_refused_without_external_write(self):
        external = Path(self.temp.name) / "external.json"
        external.write_text('{}')
        path = self.project / ".claude/settings.json"
        path.parent.mkdir()
        path.symlink_to(external)
        result = self.install()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual('{}', external.read_text())
        self.assertFalse((self.project / ".agents/workflows/ship-fix").exists())

    def test_symlinked_agent_directory_refused(self):
        external = Path(self.temp.name) / "external-agents"
        external.mkdir()
        (external / "implementer.md").write_text("custom")
        path = self.project / ".claude/agents"
        path.parent.mkdir()
        path.symlink_to(external, target_is_directory=True)
        result = self.install(host="claude")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("custom", (external / "implementer.md").read_text())

    def test_uninstall_removes_owned_hooks_and_preserves_existing_settings(self):
        path = self.project / ".claude/settings.json"
        path.parent.mkdir()
        defaults = json.loads((PACKAGE / "install/templates/claude-settings.json").read_text())
        preserved_hook = defaults["hooks"]["WorktreeCreate"][0]
        path.write_text(json.dumps({"hooks": {"WorktreeCreate": [preserved_hook]},
                                    "permissions": {"deny": ["Bash(rm *)"]}}))
        result = self.install()
        self.assertEqual(0, result.returncode, result.stderr)
        result = subprocess.run(["python3", "-I", "-B", str(PACKAGE / "install/uninstall.py"), str(self.project)],
                                cwd=self.project, capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Unowned workflow hook", result.stderr)
        self.assertTrue((self.project / ".agents/workflows/ship-fix").exists())
        existing = json.loads(path.read_text())
        del existing["hooks"]["WorktreeCreate"]
        path.write_text(json.dumps(existing))
        result = subprocess.run(["python3", "-I", "-B", str(PACKAGE / "install/uninstall.py"), str(self.project)],
                                cwd=self.project, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        settings = json.loads(path.read_text())
        self.assertNotIn("WorktreeRemove", settings.get("hooks", {}))
        self.assertEqual(["Bash(rm *)"], settings["permissions"]["deny"])
        self.assertFalse((self.project / ".agents/workflows/ship-fix").exists())
        self.assertFalse((self.project / ".codex/hooks.json").read_text().find("codex_pre_tool_use.py") >= 0)
        self.assertFalse((self.project / ".agents/roles/implementer.md").exists())
        self.assertFalse((self.project / ".agents/platforms/codex/roles.json").exists())

    def test_rejects_unknown_ref(self):
        result = self.install("nonexistent-release-ref")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.project / ".agents/.ship-version").exists())

    def test_pinned_ref_uses_committed_bytes(self):
        env = os.environ.copy()
        env["SHIP_SRC"] = str(PACKAGE)
        result = subprocess.run([str(INSTALLER), "HEAD", "--host", "both"], cwd=self.project, env=env,
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        expected = subprocess.run(["git", "-C", str(PACKAGE), "show", "HEAD:skills/ship/SKILL.md"],
                                  check=True, capture_output=True).stdout
        self.assertEqual(expected, (self.project / ".agents/skills/ship/SKILL.md").read_bytes())

    def test_remote_install_accepts_commit_ref(self):
        revision = subprocess.run(["git", "-C", str(PACKAGE), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
        env = os.environ.copy()
        env.pop("SHIP_SRC", None)
        env["SHIP_REMOTE"] = str(PACKAGE)
        result = subprocess.run([str(INSTALLER), revision, "--host", "both"], cwd=self.project,
                                env=env, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        version = json.loads((self.project / ".agents/.ship-version").read_text())
        self.assertEqual(revision, version["commit"])

    def test_legacy_pinned_ref_uses_its_own_ocx_profile(self):
        if subprocess.run(["git", "-C", str(PACKAGE), "rev-parse", "--verify", "v1.1.0^{commit}"],
                          capture_output=True).returncode:
            self.skipTest("v1.1.0 is not present in this checkout")
        env = os.environ.copy()
        env["SHIP_SRC"] = str(PACKAGE)
        result = subprocess.run([str(INSTALLER), "v1.1.0", "--host", "both", "--models", "ocx"],
                                cwd=self.project, env=env, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        config = (self.project / ".agents/agent-models.yaml").read_text()
        self.assertNotIn('reviewer: "inherit"', config)
        self.assertTrue((self.project / ".codex/agents/reviewer.toml").is_file())

    def test_rejects_a_local_checkout_at_another_ref(self):
        result = self.install("HEAD~1")
        self.assertEqual(2, result.returncode)
        self.assertIn("SHIP_SRC is at", result.stderr)
        self.assertFalse((self.project / ".agents/.ship-version").exists())


if __name__ == "__main__":
    unittest.main()
