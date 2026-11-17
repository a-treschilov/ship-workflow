import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

SCRIPT = Path(__file__).resolve().parents[1] / "ocx_models.py"
spec = importlib.util.spec_from_file_location("ocx_models", SCRIPT)
ocx_models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ocx_models)


class OcxCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "agent-models.yaml"
        self.assignments = "models:\n" + "".join(
            f'  {role}: "gpt-6.2-sol"\n' for role in ocx_models.agent_models.ROLES
        )
        self.original = self.assignments + 'available_models:\n  - "old-model"\n'
        self.path.write_text(self.original)

    def response(self, ids=None, payload=None):
        if payload is None:
            payload = {"data": [{"id": model_id} for model_id in ids]}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode()

        return Response()

    @patch.object(ocx_models, "urlopen")
    def test_canonical_request_has_no_anthropic_header_and_aliases_keep_context(self, urlopen):
        ids = ["gpt-6.2-sol", "zai/glm-5.3-flash", "anthropic/claude-opus-5-5"]
        urlopen.side_effect = [self.response(ids), self.response([
            "ocx-claude-native--gpt-6.2-sol[1m]", "ocx-claude-native--gpt-6.2-sol",
            "ocx-claude-zai--glm-5.3-flash[1m]", "claude-opus-5-5[1m]",
        ])]
        available, aliases = ocx_models.fetch_catalog()
        canonical_request = urlopen.call_args_list[0].args[0]
        claude_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(ocx_models.GATEWAY_MODELS_URL, canonical_request.full_url)
        self.assertIsNone(canonical_request.get_header("Anthropic-version"))
        self.assertEqual("2023-06-01", claude_request.get_header("Anthropic-version"))
        self.assertEqual(ids, available)
        self.assertEqual("ocx-claude-native--gpt-6.2-sol[1m]", aliases["gpt-6.2-sol"])
        self.assertEqual("ocx-claude-zai--glm-5.3-flash[1m]", aliases["zai/glm-5.3-flash"])
        self.assertEqual("claude-opus-5-5[1m]", aliases["anthropic/claude-opus-5-5"])
        ocx_models.update_catalog(self.path, available, aliases)
        models, stored_ids, stored_aliases = ocx_models.agent_models.parse_config(self.path)
        self.assertTrue(self.path.read_text().startswith(self.assignments))
        self.assertEqual(set(models.values()), {"gpt-6.2-sol"})
        self.assertEqual((available, aliases), (stored_ids, stored_aliases))
        before = self.path.read_bytes()
        ocx_models.update_catalog(self.path, available, aliases)
        self.assertEqual(before, self.path.read_bytes())

    def test_alias_decoding_handles_escaped_routes_and_unmarked_models(self):
        self.assertEqual("openrouter/anthropic/claude-opus", ocx_models.canonical_claude_id(
            "ocx-claude2-openrouter--anthropic~sclaude-opus[1m]"))
        self.assertEqual("zai/glm-5-turbo", ocx_models.canonical_claude_id("ocx-claude-zai--glm-5-turbo"))

    def test_catalog_refresh_preserves_profile(self):
        profile = "ocx_models:\n" + "".join(
            f'  {role}: "old-model"\n' for role in ocx_models.agent_models.ROLES
        )
        self.path.write_text(self.original + profile)
        ocx_models.update_catalog(self.path, ["new-model"], {"new-model": "new-alias"})
        self.assertIn(profile, self.path.read_text())
        self.assertTrue(self.path.read_text().startswith(self.assignments))

    @patch.object(ocx_models, "urlopen")
    def test_invalid_catalogs_do_not_change_file(self, urlopen):
        for payload in (
            {"wrong": []}, {"data": []}, {"data": [{"id": ""}]},
            {"data": [{"id": "bad id"}]}, {"data": [{"id": "same"}, {"id": "same"}]},
            {"data": [{"id": "gpt-6.2-sol"}], "has_more": True},
        ):
            with self.subTest(payload=payload):
                urlopen.return_value = self.response(payload=payload)
                with self.assertRaises(ValueError):
                    ocx_models.fetch_catalog()
                self.assertEqual(self.original, self.path.read_text())

    @patch.object(ocx_models, "urlopen")
    def test_failure_of_claude_catalog_leaves_both_catalogs_unchanged(self, urlopen):
        urlopen.side_effect = [self.response(["gpt-6.2-sol"]), HTTPError("http://ocx", 503, "down", {}, None)]
        with patch.object(ocx_models, "CONFIG", self.path), patch("sys.argv", [str(SCRIPT)]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(2, ocx_models.main())
        self.assertEqual(self.original, self.path.read_text())

    @patch.object(ocx_models, "urlopen")
    def test_unmatched_alias_catalog_is_rejected(self, urlopen):
        urlopen.side_effect = [self.response(["gpt-6.2-sol"]), self.response(["claude-other-model"])]
        with self.assertRaisesRegex(ValueError, "no matching Claude aliases"):
            ocx_models.fetch_catalog()

    def test_failed_atomic_replacement_preserves_original_and_cleans_temporary(self):
        with patch.object(ocx_models.os, "replace", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(OSError, "write failed"):
                ocx_models.update_catalog(self.path, ["gpt-6.2-sol"], {"gpt-6.2-sol": "ocx-claude-native--gpt-6.2-sol"})
        self.assertEqual(self.original, self.path.read_text())
        self.assertEqual([self.path], list(self.path.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
