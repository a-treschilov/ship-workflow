import importlib.util
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "agent_models.py"
spec = importlib.util.spec_from_file_location("agent_models", SCRIPT)
agent_models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_models)


class AgentModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".agents/roles").mkdir(parents=True)
        (self.root / ".agents/platforms/claude").mkdir(parents=True)
        (self.root / ".agents/platforms/codex").mkdir(parents=True)
        (self.root / ".claude/agents").mkdir(parents=True)
        (self.root / ".codex/agents").mkdir(parents=True)
        self.models = dict(zip(agent_models.ROLES, (
            "zai/glm-5.3-flash", "gpt-6.2-sol", "gpt-6.2-sol", "anthropic/claude-opus-5-5",
        )))
        self.aliases = {
            "zai/glm-5.3-flash": "ocx-claude-zai--glm-5.3-flash[1m]",
            "gpt-6.2-sol": "ocx-claude-native--gpt-6.2-sol[1m]",
            "anthropic/claude-opus-5-5": "claude-opus-5-5[1m]",
        }
        self.claude_roles = {}
        self.codex_roles = {}
        for role in agent_models.ROLES:
            (self.root / ".agents/roles" / f"{role}.md").write_text(f"Shared behavior for {role}.\n")
            self.claude_roles[role] = {
                "name": role,
                "description": f"Description for {role}.",
                "tools": ["Read", "Write"],
                "permissionMode": "acceptEdits",
            }
            self.codex_roles[role] = {
                "name": role,
                "description": f"Description for {role}.",
            }
        (self.root / ".agents/platforms/claude/roles.json").write_text(json.dumps(self.claude_roles))
        (self.root / ".agents/platforms/codex/roles.json").write_text(json.dumps(self.codex_roles))

    def tearDown(self):
        self.temp.cleanup()

    def config_text(self, available=()):
        mappings = "".join(f'  {role}: "{model}"\n' for role, model in self.models.items())
        catalog = "".join(f'  - "{item}"\n' for item in available)
        return f"models:\n{mappings}available_models:\n{catalog}"

    def test_parse_canonical_models_and_cached_claude_aliases(self):
        config = self.root / "models.yaml"
        ids = list(self.aliases)
        alias_section = "claude_aliases:\n" + "".join(
            f'  {model}: "{alias}"\n' for model, alias in self.aliases.items()
        )
        config.write_text(self.config_text(ids) + alias_section)
        self.assertEqual((self.models, ids, self.aliases), agent_models.parse_config(config))

    def test_legacy_role_prefix_is_preserved_as_model_assignments(self):
        text = self.config_text(sorted(set(self.models.values())))
        for role in agent_models.ROLES:
            text = text.replace(f"  {role}:", f"  old-{role}:")
        models, _, _ = agent_models.parse_config_text(text)
        self.assertEqual(self.models, models)

    def test_reject_duplicate_unknown_missing_and_empty_entries(self):
        config = self.root / "models.yaml"
        for body, expected in (
            ("  implementer: a\n  implementer: b\n", "duplicate"),
            ("  other: model\n", "unknown"),
            ("  implementer: ''\n", "empty"),
            ('  implementer: "bad id"\n', "invalid model ID"),
            ("  implementer: model\n", "missing"),
        ):
            config.write_text("models:\n" + body + "available_models:\n")
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                agent_models.parse_config(config)

    def test_catalog_parser_rejects_duplicate_and_invalid_entries(self):
        config = self.root / "models.yaml"
        for catalog, expected in (
            ('  - "one"\n  - "one"\n', "duplicate available"),
            ("  nope\n", "invalid available_models"),
        ):
            config.write_text(self.config_text() + catalog)
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                agent_models.parse_config(config)

    def test_sync_generates_both_native_formats_and_check_detects_drift(self):
        drift = agent_models.check(self.models, self.aliases, self.root)
        self.assertEqual(sorted(agent_models.ROLES), drift)
        self.assertEqual(sorted(agent_models.ROLES), agent_models.sync(self.models, self.aliases, self.root))
        self.assertEqual([], agent_models.check(self.models, self.aliases, self.root))

        self.assertEqual([], agent_models.sync(self.models, self.aliases, self.root))

        for role in agent_models.ROLES:
            claude = (self.root / ".claude/agents" / f"{role}.md").read_text()
            self.assertIn(f"model: {self.aliases[self.models[role]]}\n", claude)
            self.assertIn(f"Shared behavior for {role}.", claude)
            codex = tomllib.loads((self.root / ".codex/agents" / f"{role}.toml").read_text())
            self.assertEqual(role, codex["name"])
            self.assertEqual(self.models[role], codex["model"])
            self.assertNotIn("CCR-SUBAGENT-MODEL", codex["developer_instructions"])
            self.assertIn(f"Shared behavior for {role}.", codex["developer_instructions"])

        role = "implementer"
        (self.root / ".codex/agents" / f"{role}.toml").write_text("stale")
        self.assertEqual([role], agent_models.check(self.models, self.aliases, self.root))
        self.assertEqual([role], agent_models.sync(self.models, self.aliases, self.root))
        self.assertEqual([], agent_models.check(self.models, self.aliases, self.root))

    def test_inherited_models_do_not_require_a_gateway_or_aliases(self):
        models = {role: "inherit" for role in agent_models.ROLES}
        agent_models.sync(models, {}, self.root)
        self.assertEqual([], agent_models.check(models, {}, self.root))
        for role in agent_models.ROLES:
            claude = (self.root / ".claude/agents" / f"{role}.md").read_text()
            codex = tomllib.loads((self.root / ".codex/agents" / f"{role}.toml").read_text())
            self.assertIn("model: inherit\n", claude)
            self.assertNotIn("model", codex)

    def test_codex_only_does_not_require_claude_aliases(self):
        agent_models.sync(self.models, {}, self.root, host="codex")
        self.assertEqual([], agent_models.check(self.models, {}, self.root, host="codex"))
        self.assertEqual([], list((self.root / ".claude/agents").iterdir()))

    def test_sync_rejects_symlinked_agent_without_external_write(self):
        external = self.root / "external.txt"
        external.write_text("user data")
        target = self.root / ".codex/agents/implementer.toml"
        target.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "symlinked agent path"):
            agent_models.sync(self.models, self.aliases, self.root)
        self.assertEqual("user data", external.read_text())
        self.assertEqual([], list((self.root / ".claude/agents").iterdir()))

    def test_host_specific_model_assignments(self):
        models = {role: "inherit" for role in agent_models.ROLES}
        config = self.config_text(sorted(set(self.models.values()))) + (
            'claude_models:\n  implementer: "sonnet"\n'
            'codex_models:\n  implementer: "gpt-example"\n')
        overrides = agent_models.host_overrides(config)
        agent_models.sync(models, {}, self.root, overrides=overrides)
        self.assertIn("model: sonnet\n", (self.root / ".claude/agents/implementer.md").read_text())
        codex = tomllib.loads((self.root / ".codex/agents/implementer.toml").read_text())
        self.assertEqual("gpt-example", codex["model"])
        self.assertEqual([], agent_models.check(models, {}, self.root, overrides=overrides))

    def test_changed_role_assignment_updates_both_hosts(self):
        agent_models.sync(self.models, self.aliases, self.root)
        self.models["implementer"] = "gpt-6.2-sol"
        self.assertEqual(["implementer"], agent_models.check(self.models, self.aliases, self.root))
        self.assertEqual(["implementer"], agent_models.sync(self.models, self.aliases, self.root))
        claude = (self.root / ".claude/agents/implementer.md").read_text()
        codex = tomllib.loads((self.root / ".codex/agents/implementer.toml").read_text())
        self.assertIn("model: ocx-claude-native--gpt-6.2-sol[1m]\n", claude)
        self.assertEqual("gpt-6.2-sol", codex["model"])

    def test_missing_alias_prevents_any_writes(self):
        aliases = dict(self.aliases)
        del aliases["gpt-6.2-sol"]
        with self.assertRaisesRegex(ValueError, "missing or invalid"):
            agent_models.sync(self.models, aliases, self.root)
        self.assertEqual([], list((self.root / ".claude/agents").iterdir()))
        self.assertEqual([], list((self.root / ".codex/agents").iterdir()))

    def test_invalid_target_prevents_any_writes(self):
        broken = self.root / ".agents/roles" / f"{agent_models.ROLES[-1]}.md"
        broken.unlink()
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with self.assertRaises(OSError):
            agent_models.sync(self.models, self.aliases, self.root)
        self.assertEqual(before, {path: path.read_bytes() for path in before})



if __name__ == "__main__":
    unittest.main()
