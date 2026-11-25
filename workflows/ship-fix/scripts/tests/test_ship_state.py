"""Isolated workflow tests; set SHIP_TEST_TMPDIR to the session scratchpad."""

import copy
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "ship_state.py"
spec = importlib.util.spec_from_file_location("ship_state", SCRIPT)
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


def finding(identifier="api-1", status="new"):
    return {"id": identifier, "file": "source.txt", "line": 1, "severity": "Warning",
            "status": status, "category": "Logic", "description": "Incorrect branch",
            "recommendation": "Fix the branch", "failure_scenario": "Null returns the wrong value",
            "verification_expectation": "Test null and non-null values", "criterion_ids": []}


class WorkflowStateTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get("SHIP_TEST_TMPDIR")
        if not scratch:
            self.fail("Set SHIP_TEST_TMPDIR to the session scratchpad; do not use system temp")
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix="ship-state-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.base = self.root / "baselines"
        self.source = self.repo / "source.txt"
        self.source.write_text("original\n")
        self.git("init", "-q")
        self.git("add", ".")
        # Local fixture index is enough; avoid commits and any user/global hooks.
        self.manifest = state.capture(self.repo, ["source.txt"])

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def save(self, name, value):
        path = self.root / name
        state.write_once(path, value)
        return path

    def review(self, entries=None, round_number=1):
        return {"repo": "api", "round": round_number, "source_fingerprint": self.manifest["fingerprint"],
                "coverage": {"read": ["source.txt"], "skipped": []},
                "findings": [finding()] if entries is None else entries}

    def test_same_file_wip_delta_excludes_preexisting_edit(self):
        self.source.write_text("existing WIP\nunchanged\n")
        state.baseline(self.repo, self.base, "source.txt")
        self.source.write_text("existing WIP\nnew implementation\n")
        changes = state.delta(self.repo, self.base, ["source.txt"])["changes"]
        self.assertEqual(1, len(changes))
        self.assertNotIn("+existing WIP", changes[0]["diff"])
        self.assertIn("+new implementation", changes[0]["diff"])
        state.baseline(self.repo, self.base, "source.txt")
        self.assertEqual("existing WIP\nunchanged\n", (self.base / "source.txt").read_text())

    def test_new_deleted_and_renamed_paths(self):
        for path in ("source.txt", "renamed.txt", "new.txt"):
            state.baseline(self.repo, self.base, path)
        self.source.rename(self.repo / "renamed.txt")
        (self.repo / "new.txt").write_text("new\n")
        changes = {entry["path"]: entry for entry in
                   state.delta(self.repo, self.base, ["source.txt", "renamed.txt", "new.txt"])["changes"]}
        self.assertEqual("missing", changes["source.txt"]["after"]["kind"])
        self.assertEqual("missing", changes["renamed.txt"]["before"]["kind"])
        self.assertIn("+new", changes["new.txt"]["diff"])

    def test_created_then_removed_is_empty_delta(self):
        state.baseline(self.repo, self.base, "new.txt")
        new = self.repo / "new.txt"
        new.write_text("transient")
        new.unlink()
        self.assertEqual([], state.delta(self.repo, self.base, ["new.txt"])["changes"])

    def test_untracked_source_mutation_changes_fingerprint(self):
        new = self.repo / "untracked.txt"
        new.write_text("one")
        first = state.capture(self.repo, ["source.txt"])
        new.write_text("two")
        self.assertNotEqual(first["fingerprint"], state.capture(self.repo, ["source.txt"])["fingerprint"])

    def test_project_ignored_dependencies_preserves_source_and_rejects_drift(self):
        (self.repo / ".gitignore").write_text("vendor/\n")
        (self.repo / "vendor").mkdir()
        dependency = self.repo / "vendor" / "shared.js"
        dependency.write_text("original")
        initial = self.save("initial.json", state.capture(self.repo, ["vendor/shared.js"]))
        scope = self.save("dependencies.json", ["vendor/shared.js"])
        projected = state.project_ignored_dependencies(initial, scope)
        self.assertNotIn("vendor/shared.js", projected["files"])
        self.assertEqual(state.capture(self.repo, [])['fingerprint'], projected['fingerprint'])
        original_file_state = state.file_state
        calls = []

        def changing_file_state(path):
            result = original_file_state(path)
            calls.append(path)
            if len(calls) == 1:
                dependency.write_text("changed mid-scan")
            return result

        with patch.object(state, "file_state", side_effect=changing_file_state):
            with self.assertRaisesRegex(ValueError, "changed during projection"):
                state.project_ignored_dependencies(initial, scope)
        dependency.write_text("changed")
        with self.assertRaisesRegex(ValueError, "Dependency changed"):
            state.project_ignored_dependencies(initial, scope)
        tracked = self.save("tracked.json", ["source.txt"])
        with self.assertRaisesRegex(ValueError, "tracked source"):
            state.project_ignored_dependencies(initial, tracked)

    def test_tracked_deletion_changes_fingerprint(self):
        self.source.unlink()
        final = state.capture(self.repo, ["source.txt"])
        self.assertEqual("missing", final["files"]["source.txt"]["kind"])
        with self.assertRaises(ValueError):
            state.match(self.manifest, final)

    def test_evidence_output_does_not_invalidate_source(self):
        directory = self.repo / ".claude"
        directory.mkdir()
        (directory / "review-findings.json").write_text("{}")
        state.write_once(self.root / "evidence.json", {"status": "passed"})
        self.assertEqual(self.manifest, state.capture(self.repo, ["source.txt"]))

    def test_exclusions_cannot_hide_tracked_or_scoped_source(self):
        self.source.write_text("changed")
        self.assertNotEqual(self.manifest["fingerprint"],
                            state.capture(self.repo, ["source.txt"], ["source.txt"])["fingerprint"])
        (self.repo / ".gitignore").write_text("ignored.txt\n")
        (self.repo / "ignored.txt").write_text("ignored source")
        self.assertIn("ignored.txt", state.capture(self.repo, ["ignored.txt"], ["ignored.txt"])["files"])

    def test_file_mode_changes_invalidate_state(self):
        self.source.chmod(0o755)
        self.assertNotEqual(self.manifest["fingerprint"], state.capture(self.repo, ["source.txt"])["fingerprint"])

    def test_symlink_hashes_target_not_external_contents(self):
        external = self.root / "outside.txt"
        external.write_text("secret")
        (self.repo / "link").symlink_to(external)
        before = state.capture(self.repo, ["link"])
        external.write_text("changed secret")
        self.assertEqual(before, state.capture(self.repo, ["link"]))
        state.baseline(self.repo, self.base, "link")
        self.assertTrue((self.base / "link").is_symlink())

    def test_parent_symlink_and_traversal_are_rejected(self):
        (self.repo / "outside").symlink_to(self.root, target_is_directory=True)
        for path in ("../outside.txt", "/absolute", ".git/config", "outside/file"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                state.capture(self.repo, [path])

    def test_baseline_tampering_is_rejected(self):
        state.baseline(self.repo, self.base, "source.txt")
        (self.base / "source.txt").write_text("tampered")
        with self.assertRaises(ValueError):
            state.delta(self.repo, self.base, ["source.txt"])
        with self.assertRaises(ValueError):
            state.baseline(self.repo, self.base, "source.txt")

    def test_pinned_baseline_preserves_allowed_symlink(self):
        link = self.repo / "link"
        link.symlink_to("source.txt")
        policy = ((str(self.repo), ("private",)),)
        state.baseline(self.repo, self.base, "link", pinned_directory=self.base.resolve(), policies=policy)
        self.assertTrue((self.base / "link").is_symlink())
        self.assertEqual("source.txt", os.readlink(self.base / "link"))

    def test_baseline_alias_uses_existing_first_touch_record(self):
        if not (self.repo / "SOURCE.TXT").exists():
            self.skipTest("filesystem distinguishes case")
        state.baseline(self.repo, self.base, "source.txt")
        state.baseline(self.repo, self.base, "SOURCE.TXT")
        self.source.write_text("changed\n")
        changes = state.delta(self.repo, self.base, ["SOURCE.TXT"])["changes"]
        self.assertEqual("SOURCE.TXT", changes[0]["path"])
        self.assertIn("+changed", changes[0]["diff"])
        (self.base / "source.txt").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "Baseline was modified"):
            state.baseline(self.repo, self.base, "SOURCE.TXT")

    def test_capture_uses_tracked_spelling_for_walked_and_explicit_aliases(self):
        if not (self.repo / "SOURCE.TXT").exists():
            self.skipTest("filesystem distinguishes case")
        self.source.rename(self.repo / "temporary.txt")
        (self.repo / "temporary.txt").rename(self.repo / "SOURCE.TXT")
        captured = state.capture(self.repo, [], read_excludes=["private"])
        self.assertEqual({"source.txt"}, captured["files"].keys())
        self.assertEqual(captured, state.capture(self.repo, ["SOURCE.TXT"], read_excludes=["private"]))
        self.assertEqual(captured, state.capture(self.repo, ["SOURCE.TXT"]))
        state.baseline(self.repo, self.base, "source.txt")
        state.baseline(self.repo, self.base, "SOURCE.TXT")
        (self.repo / "SOURCE.TXT").write_text("new\n")
        self.assertEqual(1, len(state.delta(self.repo, self.base, ["source.txt", "SOURCE.TXT"])["changes"]))

    def test_case_distinct_files_are_captured_separately(self):
        alternate = self.repo / "SOURCE.TXT"
        if alternate.exists():
            self.skipTest("filesystem ignores case")
        os.link(self.source, alternate)
        self.git("add", "SOURCE.TXT")
        capture = state.capture(self.repo, ["SOURCE.TXT"], read_excludes=["private"])
        self.assertEqual({"source.txt", "SOURCE.TXT"}, capture["files"].keys())
        self.assertEqual({"SOURCE.TXT"}, state.capture(self.repo, [], read_excludes=["source.txt"])["files"].keys())

    def test_capture_nested_directories_with_read_excludes_preserves_manifest(self):
        repo = self.repo.resolve()
        nested = repo / "outer" / "inner"
        nested.mkdir(parents=True)
        tracked = "outer/inner/Tracked.TXT"
        untracked = "outer/inner/Visible.txt"
        (repo / tracked).write_text("tracked")
        self.git("add", tracked)
        (repo / untracked).write_text("untracked")
        link = nested / "private-link"
        link.symlink_to("private", target_is_directory=True)
        read_excludes = ["outer/inner/private"]
        previous = self.save("before.json", state.capture(repo, [], read_excludes=read_excludes))
        previous_bytes = previous.read_bytes()

        private = nested / "private"
        private.mkdir()
        blocked_tracked = private / "protected.txt"
        blocked_tracked.write_text("protected")
        self.git("add", str(blocked_tracked))
        (private / "child").mkdir()
        (private / "child/untracked.txt").write_text("protected")
        metadata = nested / ".git"
        metadata.mkdir()
        (metadata / "config").write_text("metadata")
        scan, listdir, lstat, open_file = os.scandir, os.listdir, Path.lstat, Path.open

        def guarded(reader, path, *args, **kwargs):
            if not isinstance(path, int):
                self.assertFalse(Path(path).is_relative_to(private), "capture accessed excluded tree")
                if reader in (scan, listdir):
                    for blocked in (metadata, link):
                        self.assertFalse(Path(path).is_relative_to(blocked), "capture entered metadata or symlink")
            return reader(path, *args, **kwargs)

        real_run = subprocess.run
        index_calls = []

        def observed_git(args, **kwargs):
            result = real_run(args, **kwargs)
            if "ls-files" in args:
                paths = state.nul_paths(result.stdout)
                self.assertNotIn("outer/inner/private/protected.txt", paths, "Git enumerated excluded tracked path")
                index_calls.append(paths)
            if "check-ignore" in args:
                self.assertFalse(any(name.startswith("outer/inner/private/")
                                     for name in state.nul_paths(kwargs["input"])))
            return result

        with patch.object(state.os, "scandir", side_effect=lambda p: guarded(scan, p)), \
                patch.object(state.os, "listdir", side_effect=lambda p: guarded(listdir, p)), \
                patch.object(Path, "lstat", lambda p: guarded(lstat, p)), \
                patch.object(Path, "open", lambda p, *a, **kw: guarded(open_file, p, *a, **kw)), \
                patch.object(subprocess, "run", side_effect=observed_git):
            with self.assertRaisesRegex(AssertionError, "Git enumerated excluded tracked path"):
                state.git_paths(repo, "--cached")
            with self.assertRaisesRegex(AssertionError, "accessed excluded tree"):
                os.scandir(private)
            with self.assertRaisesRegex(AssertionError, "accessed excluded tree"):
                blocked_tracked.read_text()
            captured = state.capture(repo, [], read_excludes=read_excludes)
        link_name = "outer/inner/private-link"
        self.assertEqual([{"source.txt", tracked}] * 2, index_calls)
        self.assertEqual({"source.txt", tracked, untracked, link_name}, captured["files"].keys())
        self.assertEqual({"kind": "symlink", "target": "private"}, captured["files"][link_name])
        state.match(state.checked_manifest(previous), captured)
        self.assertEqual(previous_bytes, previous.read_bytes())
        for name in (tracked, untracked):
            (repo / name).write_text("changed")
        self.assertEqual({tracked, untracked}, state.PathNames(repo, read_excludes).changed(
            captured, state.capture(repo, [], read_excludes=read_excludes)))
        for name in ("outer/inner", link_name, f"{link_name}/child/untracked.txt"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "read_excludes|Symlinked parent"):
                state.capture(repo, [name], read_excludes=read_excludes)

    def test_read_exclusion_prunes_both_spellings_before_descending(self):
        if not (self.repo / "SOURCE.TXT").exists():
            self.skipTest("filesystem distinguishes case")
        private = self.repo / "Private"
        private.mkdir()
        (private / "tracked").write_text("protected")
        self.git("add", "Private/tracked")
        (private / "untracked").write_text("protected")
        scan, listdir = os.scandir, os.listdir

        def guarded(reader, path):
            if not isinstance(path, int):
                self.assertNotEqual(private, Path(path), "entered excluded directory")
            return reader(path)

        with patch.object(state.os, "scandir", side_effect=lambda p: guarded(scan, p)), \
                patch.object(state.os, "listdir", side_effect=lambda p: guarded(listdir, p)):
            read_excludes = ["private/tracked", "private", "SOURCE.TXT"]
            self.assertEqual({}, state.capture(self.repo, [], read_excludes=read_excludes)["files"])
            for name in ("Private/tracked", "source.txt"):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "read_excludes"):
                    state.capture(self.repo, [name], read_excludes=read_excludes)
        with self.assertRaisesRegex(ValueError, "read_excludes"):
            state.guard_excluded_path(self.repo / "PRIVATE/tracked", self.repo, ["Private"])

    def test_restricted_capture_omits_unicode_aliases_from_git_output(self):
        directory = self.repo / "Équipe"
        directory.mkdir()
        (directory / "private.txt").write_text("fixture")
        self.git("add", ".")
        excluded = "e\u0301quipe"
        if not (self.repo / excluded).exists():
            self.skipTest("filesystem distinguishes case or Unicode normalization")

        run = subprocess.run

        def observe(args, **kwargs):
            result = run(args, **kwargs)
            if "ls-files" in args:
                self.assertEqual(b"source.txt\0", result.stdout)
            return result

        with patch.object(subprocess, "run", side_effect=observe):
            self.assertEqual(self.manifest, state.capture(self.repo, [], read_excludes=[excluded]))

    def test_restricted_capture_preserves_allowed_stages_below_excluded_index_ancestor(self):
        folder = self.repo / "merge[é]"
        folder.mkdir()
        (folder / "allowed").write_text("fixture")
        (folder / "private").write_text("fixture")
        oid = self.git("hash-object", "-w", "source.txt").stdout.strip()
        entries = b"".join(b"100644 " + oid + f" {stage}\t{name}\n".encode()
                           for stage, name in ((2, folder.name), (3, f"{folder.name}/allowed"),
                                               (3, f"{folder.name}/private")))
        subprocess.run(["git", "-C", str(self.repo), "update-index", "--index-info"], check=True, input=entries)
        exclusion = f"{folder.name}/private"
        visible = f"{folder.name}/allowed"
        captured = state.capture(self.repo, [], read_excludes=[exclusion], excludes=[visible])
        self.assertEqual({"source.txt", visible}, captured["files"].keys())

    def test_restricted_capture_guards_metadata_pointers_before_following(self):
        root = self.repo.resolve()
        private = root / "private"
        private.mkdir()
        marker = root / ".git"
        index = marker / "index"
        index.rename(private / "index")
        index.symlink_to("../private/index")
        for pointer in ("index symlink", "GIT_INDEX_FILE", "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE",
                        "commondir", "gitfile", "gitdir symlink"):
            with self.subTest(pointer=pointer):
                if pointer == "commondir":
                    (marker / "commondir").write_text("../private\n")
                if pointer == "gitfile":
                    marker.rename(root / "ordinary-metadata")
                    marker.write_text("gitdir: private\n")
                if pointer == "gitdir symlink":
                    marker.unlink()
                    marker.symlink_to("private", target_is_directory=True)
                env = {}
                if pointer.startswith("GIT_"):
                    env[pointer] = "private/index" if pointer == "GIT_INDEX_FILE" else "private"
                with patch.dict(os.environ, env), patch.object(subprocess, "run", side_effect=AssertionError("Git started")):
                    with self.assertRaisesRegex(ValueError, "read_excludes"):
                        state.capture(root, [], read_excludes=["private"])
                if pointer == "index symlink":
                    index.unlink()
                    (private / "index").rename(index)
                if pointer == "commondir":
                    (marker / "commondir").unlink()

    def test_restricted_capture_isolates_config_and_git_environment(self):
        root = self.repo.resolve()
        private = root / "private"
        private.mkdir()
        include = root / "capture-config"
        include.write_text("[core]\nexcludesFile = in-tree-ignore\n")
        (root / "in-tree-ignore").write_text("global-hidden\n")
        (root / ".git/info/exclude").write_text("info-hidden\n")
        (root / ".gitignore").write_text("local-hidden\n")
        for name in ("global-hidden", "info-hidden", "local-hidden", "visible"):
            (root / name).write_text(name)
        self.git("config", "include.path", str(include))
        expected = state.capture(root, [])
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.excludesFile",
               "GIT_CONFIG_VALUE_0": str(private / "hidden"), "GIT_OBJECT_DIRECTORY": str(private),
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(private), "GIT_TRACE": str(private / "trace")}
        with patch.dict(os.environ, env):
            self.assertEqual(expected, state.capture(root, [], read_excludes=["private"]))
        self.assertEqual([], list(private.iterdir()))
        include.write_text("[core]\nexcludesFile = private/hidden\n")
        with self.assertRaisesRegex(ValueError, "read_excludes"):
            state.capture(root, [], read_excludes=["private"])
        self.git("config", "include.path", str(private / "config"))
        with self.assertRaisesRegex(ValueError, "read_excludes"):
            state.capture(root, [], read_excludes=["private"])

    def test_restricted_capture_preserves_split_index_packed_head_and_linked_worktree(self):
        private = self.repo / "private"
        private.mkdir()
        (private / "tracked").write_text("fixture")
        self.git("add", ".")
        self.git("-c", "user.name=test", "-c", "user.email=test@example.org", "commit", "-qm", "base")
        self.git("pack-refs", "--all")
        self.git("update-index", "--index-version", "4")
        expected = state.capture(self.repo, [])
        expected["files"].pop("private/tracked")
        expected["fingerprint"] = state.digest({k: v for k, v in expected.items() if k != "fingerprint"})
        self.git("update-index", "--split-index")
        self.assertEqual(expected, state.capture(self.repo, [], read_excludes=["private"]))
        linked = self.root / "linked"
        self.git("worktree", "add", "--detach", str(linked))
        self.assertEqual(expected["head"], state.capture(linked, [], read_excludes=["private"])["head"])

    def test_restricted_capture_supports_sha256_indexes(self):
        repo = self.root / "sha256"
        initialized = subprocess.run(["git", "init", "-q", "--object-format=sha256", str(repo)],
                                     capture_output=True)
        if initialized.returncode != 0 and b"object-format" in initialized.stderr:
            self.skipTest("Git does not support SHA-256 repositories")
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        self.repo = repo
        (repo / "allowed").write_text("fixture")
        (repo / "private").write_text("fixture")
        self.git("add", ".")
        self.git("-c", "user.name=test", "-c", "user.email=test@example.org", "commit", "-qm", "base")
        captured = state.capture(repo, [], read_excludes=["private"])
        self.assertEqual({"allowed"}, captured["files"].keys())
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.decode().strip(), captured["head"])

    def test_restricted_sparse_index_expands_only_allowed_trees(self):
        for name in ("retained/tracked", "collapsed/allowed/tracked", "collapsed/private/never-read"):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        self.git("add", ".")
        self.git("-c", "user.name=test", "-c", "user.email=test@example.org", "commit", "-qm", "base")
        protected = self.git("rev-parse", "HEAD:collapsed/private").stdout.strip()
        self.git("sparse-checkout", "set", "--sparse-index", "retained")
        exclusion = ["collapsed/private"]
        expected = state.capture(self.repo, [])
        expected["files"].pop("collapsed/private/never-read")
        expected["fingerprint"] = state.digest({k: v for k, v in expected.items() if k != "fingerprint"})
        run = subprocess.run

        def observe(args, **kwargs):
            self.assertNotIn(protected.decode(), args, "Git was asked to open the excluded tree OID")
            result = run(args, **kwargs)
            if "ls-files" in args:
                self.assertNotIn(b"collapsed/", result.stdout, "Git emitted a collapsed index path")
            if "cat-file" in args:
                self.assertNotIn(b"never-read", result.stdout, "Git emitted entries of an excluded tree")
            return result

        for packed in (False, True):
            if packed:
                self.git("gc")
            with self.subTest(packed=packed), patch.object(subprocess, "run", side_effect=observe):
                self.assertEqual(expected, state.capture(self.repo, [], read_excludes=exclusion))

    def test_restricted_capture_uses_system_and_conditional_ignore_config(self):
        root = self.repo.resolve()
        config = self.root.resolve() / "system-config"
        paths = [(root / "path-rules", "path-hidden\n"), (root / "branch-rules", "branch-hidden\n"),
                 (root / "remote-rules", "remote-hidden\n"), (root / "system-rules", "system-hidden\n")]
        for rules, pattern in paths:
            rules.write_text(pattern)
            rules.with_name(rules.name.replace("rules", "config")).write_text(f"[core]\nexcludesFile = {rules}\n")
        for hidden in ("path-hidden", "branch-hidden", "remote-hidden", "system-hidden"):
            (root / hidden).write_text("untracked")
        (root / "private").mkdir()
        branch = self.git("symbolic-ref", "--short", "HEAD").stdout.decode().strip()
        self.git("config", "remote.origin.url", "https://example.invalid/team/project")
        conditionals = (
            ("gitdir:**/repo/.git", "path-config", "path-hidden"),
            (f"onbranch:{branch}", "branch-config", "branch-hidden"),
            ("hasconfig:remote.*.url:https://example.invalid/**", "remote-config", "remote-hidden"),
        )
        with patch.dict(os.environ, {"GIT_CONFIG_SYSTEM": str(config), "GIT_CONFIG_NOSYSTEM": "0"}):
            for condition, source, hidden in conditionals:
                with self.subTest(condition=condition):
                    config.write_text(f'[includeIf "{condition}"]\npath = {root / source}\n'
                                      '[includeIf "onbranch:never-matches"]\npath = ' +
                                      str(root / "private/never-open") + "\n")
                    unrestricted = state.capture(root, [])
                    self.assertNotIn(hidden, unrestricted["files"])
                    self.assertEqual(unrestricted, state.capture(root, [], read_excludes=["private"]))
            # These conditions are unknown on older Git. Match the installed
            # implementation before deciding whether they can select a file.
            for condition in ("hostname:**", f"worktree:{root}", f"worktree/i:{str(root).upper()}"):
                config.write_text(f'[includeIf "{condition}"]\npath = {root / "system-config"}\n')
                with self.subTest(condition=condition):
                    self.assertEqual(state.capture(root, []), state.capture(root, [], read_excludes=["private"]))
            config.write_text(f"[include]\npath = {root / 'system-config'}\n")
            self.assertNotIn("system-hidden", state.capture(root, [], read_excludes=["private"])["files"])
            config.write_text(f'[includeIf "onbranch:{branch}"]\npath = {root / "private/never-open"}\n')
            with self.assertRaisesRegex(ValueError, "read_excludes"):
                state.capture(root, [], read_excludes=["private"])
        with patch.dict(os.environ, {"GIT_CONFIG_SYSTEM": str(root / "private/config"), "GIT_CONFIG_NOSYSTEM": "0"}):
            with self.assertRaisesRegex(ValueError, "read_excludes"):
                state.capture(root, [], read_excludes=["private"])
        self.git("config", "core.excludesFile", os.devnull)
        with patch.dict(os.environ, {"GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "0"}):
            self.assertEqual(state.capture(root, []), state.capture(root, [], read_excludes=["private"]))

    def test_restricted_capture_matches_gitdir_condition_with_symlinked_home(self):
        root = self.repo.resolve()
        home = root.parent
        alias = home / "home-alias"
        alias.symlink_to(home, target_is_directory=True)
        (home / ".gitconfig").write_text('[includeIf "gitdir:~/repo/.git"]\npath = conditional-config\n')
        (home / "conditional-config").write_text(f"[core]\nexcludesFile = {root / 'ignore'}\n")
        (root / "ignore").write_text("hidden\n")
        (root / "hidden").write_text("fixture")
        with patch.dict(os.environ, {"HOME": str(alias), "XDG_CONFIG_HOME": str(home / "xdg"),
                                     "GIT_CONFIG_NOSYSTEM": "1"}):
            unrestricted = state.capture(root, [])
            self.assertNotIn("hidden", unrestricted["files"])
            self.assertEqual(unrestricted, state.capture(root, [], read_excludes=["private"]))

    def test_restricted_capture_preserves_reftable_head_and_linked_worktree(self):
        repo = self.root / "table"
        initialized = subprocess.run(["git", "init", "-q", "--ref-format=reftable", str(repo)], capture_output=True)
        if initialized.returncode and b"ref-format" in initialized.stderr:
            self.skipTest("Git does not support reftable")
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        self.repo = repo
        (repo / "tracked").write_text("fixture")
        (repo / ".git/info").mkdir(exist_ok=True)
        (repo / ".git/info/exclude").write_text("ignored\n")
        self.git("add", ".")
        self.git("-c", "user.name=test", "-c", "user.email=test@example.org", "commit", "-qm", "base")
        linked = self.root / "linked-table"
        self.git("worktree", "add", "--detach", str(linked))
        for worktree in (repo, linked):
            (worktree / "ignored").write_text("fixture")
            with self.subTest(worktree=worktree):
                self.assertEqual(state.capture(worktree, []),
                                 state.capture(worktree, [], read_excludes=["private"]))

    def test_restricted_capture_denies_symlink_swaps_before_opening(self):
        root = self.repo.resolve()
        private = root / "private"
        private.mkdir()
        (private / "secret").write_text("never read")
        allowed_dir = root / "allowed"
        allowed_dir.mkdir()
        (allowed_dir / "untracked").write_text("fixture")
        real_open, real_scan = os.open, os.scandir
        private_info = private.stat()
        for path, target in ((root / "source.txt", private / "secret"), (allowed_dir, private),
                             (root / ".git/index", private / "secret")):
            switched = []
            parent = path.parent.stat()
            original = path.with_name(path.name + ".original")

            def swap(name, flags, *args, dir_fd=None):
                if name == path.name and dir_fd is not None and os.path.samestat(os.fstat(dir_fd), parent):
                    if not switched:
                        path.rename(original)
                        path.symlink_to(target, target_is_directory=target == private)
                        switched.append(True)
                    self.assertTrue(flags & os.O_NOFOLLOW, "capture tried to follow replacement link")
                return real_open(name, flags, *args, dir_fd=dir_fd)

            def scan(fd):
                if isinstance(fd, int):
                    self.assertFalse(os.path.samestat(os.fstat(fd), private_info), "enumerated replacement target")
                return real_scan(fd)

            with self.subTest(path=path), patch.object(os, "open", side_effect=swap), patch.object(os, "scandir", side_effect=scan):
                with self.assertRaises(OSError):
                    state.capture(root, [], read_excludes=["private"])
                self.assertEqual([True], switched)
            path.unlink()
            original.rename(path)

    def test_restricted_capture_does_not_create_git_scratch_in_caller_tmpdir(self):
        root = self.repo.resolve()
        protected = root / "private"
        protected.mkdir()
        supplied = self.root.resolve() / "caller-scratch"
        supplied.symlink_to(protected, target_is_directory=True)
        scratch = tempfile.TemporaryDirectory
        with patch.dict(os.environ, {"TMPDIR": str(supplied)}), \
                patch.object(tempfile, "TemporaryDirectory", wraps=scratch) as temporary:
            state.capture(root, [], read_excludes=["private"])
        self.assertEqual([Path("/tmp").resolve()] * 2, [call.kwargs["dir"] for call in temporary.call_args_list])
        self.assertEqual([], list(protected.iterdir()))

    def test_guard_blocks_excluded_root_alias_before_metadata_lookup(self):
        root = self.repo.resolve()
        protected = root / "private"
        protected.mkdir()
        private_alias = self.root / "a"
        private_alias.symlink_to(protected)
        repo_alias = self.root / "z"
        repo_alias.symlink_to(root)
        lstat = Path.lstat

        def guarded(path):
            self.assertFalse(path.is_relative_to(protected), "probed protected repository root")
            return lstat(path)

        with patch.object(Path, "lstat", guarded):
            for policies in (((private_alias, ("unused",)), (repo_alias, ("private",))),
                             ((protected, ("unused",)), (repo_alias, ("private",)))):
                with self.subTest(policies=policies), self.assertRaisesRegex(ValueError, "read_excludes"):
                    state.guard_excluded_paths(self.source, policies)

    def test_isolated_git_queries_entries_in_a_sparse_index(self):
        for directory in ("retained", "other"):
            (self.repo / directory).mkdir()
            (self.repo / directory / "tracked").write_text("fixture")
        self.git("add", ".")
        self.git("-c", "user.name=test", "-c", "user.email=test@example.org", "commit", "-qm", "base")
        self.git("sparse-checkout", "set", "--sparse-index", "retained")
        exclusions = ("other",)
        index_paths = state.scoped_tracked_paths(self.repo, ["retained/tracked"], exclusions,
                                               guard=lambda p: state.guard_excluded_path(p, self.repo, exclusions))
        self.assertEqual({"retained/tracked"}, index_paths)

    def test_manifest_alias_comparison_rejects_conflicting_or_stale_states(self):
        if not (self.repo / "SOURCE.TXT").exists():
            self.skipTest("filesystem distinguishes case")
        legacy = {k: copy.deepcopy(self.manifest[k]) for k in ("repo_path", "head", "files")}
        legacy["files"]["SOURCE.TXT"] = legacy["files"]["source.txt"]
        legacy["fingerprint"] = state.digest(legacy)
        path = self.save("legacy.json", legacy)
        original_bytes = path.read_bytes()
        state.match_source(state.checked_manifest(path), self.manifest)
        with self.assertRaisesRegex(ValueError, "Stale certification"):
            state.match(legacy, self.manifest)
        conflicting = copy.deepcopy(legacy)
        conflicting["files"]["SOURCE.TXT"] = state.MISSING
        conflicting.pop("fingerprint")
        conflicting["fingerprint"] = state.digest(conflicting)
        path_conflict = self.save("conflicting.json", conflicting)
        with self.assertRaisesRegex(ValueError, "Conflicting manifest entries"):
            state.match_source(state.checked_manifest(path_conflict), self.manifest)
        self.source.write_text("new source")
        with self.assertRaisesRegex(ValueError, "Stale certification"):
            state.match_source(state.checked_manifest(path), state.capture(self.repo, []))
        self.assertEqual(original_bytes, path.read_bytes())

    def test_unrecorded_alias_baseline_is_rejected(self):
        if not (self.repo / "SOURCE.TXT").exists():
            self.skipTest("filesystem distinguishes case")
        self.base.mkdir()
        (self.base / "source.txt").write_text("unrecorded")
        with self.assertRaisesRegex(ValueError, "Unrecorded baseline"):
            state.baseline(self.repo, self.base, "SOURCE.TXT")

    def test_hard_links_keep_distinct_first_touch_records(self):
        os.link(self.source, self.repo / "other.txt")
        state.baseline(self.repo, self.base, "source.txt")
        with self.assertRaisesRegex(ValueError, "Missing first-touch record"):
            state.delta(self.repo, self.base, ["other.txt"])
        state.baseline(self.repo, self.base, "other.txt")
        self.assertTrue((self.base / ".ship-state" / (state.digest("other.txt") + ".json")).is_file())

    def test_non_git_requires_scope(self):
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "nginx.conf").write_text("config")
        with self.assertRaises(ValueError):
            state.capture(plain, [])
        self.assertIn("nginx.conf", state.capture(plain, ["nginx.conf"])["files"])

    def test_unstable_capture_is_rejected(self):
        different = {**self.manifest, "fingerprint": "changed"}
        with patch.object(state, "capture_once", side_effect=[self.manifest, different]):
            with self.assertRaises(ValueError):
                state.capture(self.repo, ["source.txt"])

    def test_append_only_conflict_preserves_original(self):
        path = self.save("once.json", {"original": True})
        with self.assertRaises(FileExistsError):
            state.write_once(path, {"overwrite": True})
        self.assertEqual({"original": True}, state.load(path))
        path.write_bytes(b'{"incomplete":')
        with self.assertRaises(FileExistsError):
            state.write_once(path, {"overwrite": True})
        self.assertEqual(b'{"incomplete":', path.read_bytes())

    def test_partial_write_can_be_retried(self):
        path = self.root.resolve() / "partial.json"
        fdopen = os.fdopen

        @contextmanager
        def interrupted(fd, *args, **kwargs):
            with fdopen(fd, *args, **kwargs) as handle:
                class Partial:
                    def write(self, payload):
                        handle.write(payload[:5])
                        handle.flush()
                        raise OSError("partial write")
                yield Partial()

        with patch.object(state.os, "fdopen", interrupted):
            with self.assertRaisesRegex(OSError, "partial write"):
                state.write_once(path, {"value": 42}, pinned=True)
        self.assertFalse(path.exists())
        self.assertEqual([], list(self.root.glob(".ship-write-*.tmp")))
        state.write_once(path, {"value": 42}, pinned=True)
        self.assertEqual({"value": 42}, state.load(path))

    def test_concurrent_writers_publish_one_complete_document(self):
        path = self.root.resolve() / "race.json"
        barrier = threading.Barrier(2)
        link = os.link
        results = []

        def synchronized_link(*args, **kwargs):
            barrier.wait(timeout=10)
            return link(*args, **kwargs)

        def writer(number):
            try:
                state.write_once(path, {"writer": number}, pinned=True)
                results.append("published")
            except FileExistsError:
                results.append("exists")

        with patch.object(state.os, "link", synchronized_link):
            threads = [threading.Thread(target=writer, args=(number,)) for number in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
        self.assertEqual(["exists", "published"], sorted(results))
        self.assertIn(state.load(path), [{"writer": 1}, {"writer": 2}])
        self.assertEqual([], list(self.root.glob(".ship-write-*.tmp")))

    def test_process_exit_during_write_allows_retry(self):
        path = self.root.resolve() / "crash.json"
        script = '''
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("state", sys.argv[1])
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)
def crash(handle):
    handle.write(b'{"partial":')
    handle.flush()
    os._exit(17)
state.publish_once(sys.argv[2], crash, pinned=True)
'''
        result = subprocess.run(["python3", "-I", "-B", "-c", script, str(SCRIPT), str(path)])
        self.assertEqual(17, result.returncode)
        self.assertFalse(path.exists())
        self.assertEqual(1, len(list(path.parent.glob(".ship-write-*.tmp"))))
        state.write_once(path, {"recovered": True}, pinned=True)
        self.assertEqual({"recovered": True}, state.load(path))

    def test_pinned_publication_survives_parent_swap(self):
        parent = self.root.resolve() / "parent"
        parent.mkdir()
        moved = self.root.resolve() / "moved"
        outside = self.root.resolve() / "outside"
        outside.mkdir()
        link = os.link

        def swap(*args, **kwargs):
            parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)
            return link(*args, **kwargs)

        with patch.object(state.os, "link", swap):
            state.write_once(parent / "result.json", {"value": 1}, pinned=True)
        self.assertEqual({"value": 1}, state.load(moved / "result.json"))
        self.assertFalse((outside / "result.json").exists())

    def test_manifest_tampering_is_rejected(self):
        value = copy.deepcopy(self.manifest)
        value["files"]["source.txt"]["sha256"] = "tampered"
        with self.assertRaises(ValueError):
            state.checked_manifest(self.save("bad.json", value))

    def test_duplicate_and_stale_findings_are_rejected(self):
        for review in (self.review([finding(), finding()]),
                       {**self.review(), "source_fingerprint": "old"}):
            with self.assertRaises(ValueError):
                state.findings(review, "api", 1, self.manifest)
        state.findings(self.review([]), "api", 1, self.manifest)

    def test_carry_forward_ids_and_new_maximum(self):
        previous = self.review([finding("api-8")])
        current = self.review([finding("api-8", "regressed"), finding("api-9")], 2)
        state.findings(current, "api", 2, self.manifest, [previous])
        for entry in (finding("api-8"), finding("api-2"), finding("api-10", "recurring")):
            with self.assertRaises(ValueError):
                state.findings(self.review([entry], 2), "api", 2, self.manifest, [previous])

    def test_incomplete_or_duplicate_applied_results_are_rejected(self):
        good = {"round": 1, "applied": [{"id": "api-1", "verified": "Relevant assertion passed"}],
                "failed": [], "skipped": []}
        state.applied(good, self.review())
        for result in ({**good, "applied": []},
                       {**good, "skipped": [{"id": "api-1", "reason": "duplicated"}]},
                       {**good, "round": 2}):
            with self.assertRaises(ValueError):
                state.applied(result, self.review())

    def evidence_record(self):
        before = self.save("before.json", self.manifest)
        after = self.save("after.json", self.manifest)
        log = self.root / "check.log"
        log.write_text("Relevant assertion passed\n")
        return {"criterion_id": "item.ac-1", "producer": "orchestrator", "kind": "empirical",
                "status": "passed", "command": "test the fixture", "exit_code": 0,
                "environment": "scratch fixture", "data_scope": "null case",
                "before_manifest": str(before), "after_manifest": str(after),
                "artifact_paths": [str(log)]}

    def test_evidence_binds_to_final_source(self):
        proof = self.evidence_record()
        state.evidence(proof, self.manifest)
        self.source.write_text("gate fix")
        with self.assertRaises(ValueError):
            state.evidence(proof, state.capture(self.repo, ["source.txt"]))

    def test_source_changing_check_and_missing_log_rejected(self):
        proof = self.evidence_record()
        self.source.write_text("gate generated source")
        changed = self.save("changed.json", state.capture(self.repo, ["source.txt"]))
        with self.assertRaises(ValueError):
            state.evidence({**proof, "after_manifest": str(changed)}, state.checked_manifest(changed))
        with self.assertRaises(ValueError):
            state.evidence({**proof, "artifact_paths": [str(self.root / "missing.log")]}, self.manifest)

    def test_failed_unverified_and_false_green_evidence(self):
        proof = self.evidence_record()
        state.evidence({**proof, "status": "failed", "exit_code": 1, "reason": "assertion failed"}, self.manifest)
        state.evidence({**proof, "status": "unverified", "exit_code": None, "reason": "no test environment"}, self.manifest)
        for changes in ({"exit_code": 1}, {"exit_code": True}, {"status": "unverified", "exit_code": 0}):
            with self.assertRaises(ValueError):
                state.evidence({**proof, **changes}, self.manifest)

    def test_cli_capture_and_append_only(self):
        scope = self.save("scope.json", ["source.txt"])
        output = self.root / "cli.json"
        command = ["python3", "-I", str(SCRIPT), "capture", "--repo", str(self.repo),
                   "--scope", str(scope), "--out", str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.manifest, state.checked_manifest(output))
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(1, result.returncode)


if __name__ == "__main__":
    unittest.main()
