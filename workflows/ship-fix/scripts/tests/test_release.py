"""Exercise the release ref update with a local Git origin."""

from contextlib import chdir
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PUBLISHER = Path(__file__).resolve().parents[4] / ".agents/skills/release/scripts/publish.py"
MAIN = "refs/heads/main"
TAG = "refs/tags/v9.9.9"


@unittest.skipUnless(PUBLISHER.is_file(), "release publisher is part of the source checkout")
class ReleaseRefTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ["SHIP_TEST_TMPDIR"], prefix="release-refs-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = root / "source"
        self.source.mkdir()
        self.origin = str(root / "origin.git")
        self.git("init", "--initial-branch=main")
        self.git("init", "--bare", "--initial-branch=main", self.origin)
        self.git("config", "user.name", "Release fixture")
        self.git("config", "user.email", "release@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")
        self.git("commit", "--allow-empty", "-m", "Initial main")
        self.initial = self.git("rev-parse", "HEAD")
        self.git("push", self.origin, f"HEAD:{MAIN}")
        self.git("commit", "--allow-empty", "-m", "Release commit")
        self.commit = self.git("rev-parse", "HEAD")
        self.git("tag", "-a", "v9.9.9", "-m", "Release fixture")
        self.tag_object = self.git("rev-parse", TAG)
        spec = importlib.util.spec_from_file_location("release_publisher", PUBLISHER)
        self.publisher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.publisher)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.source, capture_output=True, text=True,
                              check=True).stdout.strip()

    def publish(self):
        with chdir(self.source):
            self.publisher.publish_refs(self.origin, self.commit, TAG, self.tag_object)

    def remote(self):
        return self.publisher.remote_refs(self.origin, MAIN, TAG, TAG + "^{}")

    def test_fast_forwards_main_and_pushes_annotated_tag(self):
        self.git("config", "push.followTags", "true")
        self.git("tag", "-a", "unrelated", "-m", "Unrelated tag")
        self.publish()
        self.assertEqual({MAIN: self.commit, TAG: self.tag_object, TAG + "^{}": self.commit}, self.remote())
        self.assertEqual({}, self.publisher.remote_refs(self.origin, "refs/tags/unrelated"))

    def test_resumes_with_a_published_tag_and_retries_completed_refs(self):
        self.git("push", self.origin, TAG)
        self.assertEqual(self.initial, self.remote()[MAIN])
        self.publish()
        self.publish()
        self.assertEqual({MAIN: self.commit, TAG: self.tag_object, TAG + "^{}": self.commit}, self.remote())

    def test_diverged_main_leaves_the_new_tag_local(self):
        self.git("switch", "-c", "other", self.initial)
        self.git("commit", "--allow-empty", "-m", "Concurrent update to main")
        other = self.git("rev-parse", "HEAD")
        self.git("push", self.origin, f"HEAD:{MAIN}")
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.assertEqual({MAIN: other}, self.remote())
        self.assertEqual(self.tag_object, self.git("rev-parse", TAG))
