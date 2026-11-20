"""Publish a committed release of the workflow package from its source checkout."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def output(*command):
    return subprocess.check_output(command, text=True).strip()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def release_notes(version):
    changelog = output("git", "show", "HEAD:CHANGELOG.md")
    heading = re.escape(version)
    matches = re.findall(rf"^## {heading} - \d{{4}}-\d{{2}}-\d{{2}}\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    require(len(matches) == 1 and matches[0].strip(),
            f"Commit one dated {version} section with release notes in CHANGELOG.md.")
    return matches[0].strip() + "\n"


def remote_tag(origin, ref):
    lines = output("git", "ls-remote", "--tags", origin, ref, ref + "^{}").splitlines()
    return {name: digest for digest, name in (line.split("\t") for line in lines)}


def ensure_tag(version, origin, commit):
    ref = "refs/tags/" + version
    remote = remote_tag(origin, ref)
    if remote:
        require(remote.get(ref + "^{}") == commit,
                f"Remote {version} must be an annotated tag for {commit}.")
    if not output("git", "tag", "--list", version):
        if remote:
            subprocess.run(["git", "fetch", "--no-tags", origin, f"{ref}:{ref}"], check=True)
        else:
            subprocess.run(["git", "tag", "-a", version, commit, "-m", version], check=True)
    require(output("git", "cat-file", "-t", ref) == "tag"
            and output("git", "rev-parse", ref + "^{commit}") == commit,
            f"Local {version} must be an annotated tag for {commit}.")
    return ref, output("git", "rev-parse", ref)


def check_installation(root, version, commit, project):
    (project / ".agents").mkdir(parents=True)
    env = dict(os.environ, SHIP_SRC=str(root))
    subprocess.run([str(root / "install/install.sh"), version, "--host", "both"],
                   cwd=project, env=env, check=True)
    marker = json.loads((project / ".agents/.ship-version").read_text())
    require(marker["ref"] == version and marker["commit"] == commit,
            "Scratch installation does not match the release tag.")
    subprocess.run(["python3", "-I", "-B", ".agents/workflows/ship-fix/scripts/agent_models.py", "check"],
                   cwd=project, check=True)


def publish(version):
    require(re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version),
            "Supply a stable semver version in the form vX.Y.Z.")
    root = Path(output("git", "rev-parse", "--show-toplevel")).resolve()
    require(root == Path.cwd().resolve() and (root / "install/install.sh").is_file(),
            "Run the publisher from the source repository root.")
    require(not output("git", "status", "--porcelain"), "Commit the release contents first.")
    commit = output("git", "rev-parse", "HEAD")
    notes = release_notes(version)
    origin = output("git", "remote", "get-url", "--push", "origin")
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                         r"([A-Za-z0-9-]+/[A-Za-z0-9_.-]+)", origin.rstrip("/"))
    require(match, "Configure an origin push URL on github.com.")
    repository = match[1].removesuffix(".git")
    require(output("gh", "repo", "view", repository, "--json", "nameWithOwner", "--jq", ".nameWithOwner").lower()
            == repository.lower(), "GitHub repository does not match origin.")
    subprocess.run(["make", "test"], check=True)
    require(commit == output("git", "rev-parse", "HEAD") and not output("git", "status", "--porcelain"),
            "The release checkout changed during testing.")
    ref, tag_object = ensure_tag(version, origin, commit)

    with tempfile.TemporaryDirectory(prefix="workflow-release-") as scratch:
        directory = Path(scratch)
        check_installation(root, version, commit, directory / "project")
        notes_path = directory / "release-notes.md"
        notes_path.write_text(notes)
        published_tag = remote_tag(origin, ref)
        if not published_tag:
            subprocess.run(["git", "push", origin, ref], check=True)
            published_tag = remote_tag(origin, ref)
        require(published_tag == {ref: tag_object, ref + "^{}": commit},
                f"GitHub tag {version} differs from the verified local tag.")
        create = subprocess.run(
            ["gh", "release", "create", version, "--repo", repository, "--verify-tag",
             "--title", version, "--notes-file", str(notes_path)],
            capture_output=True, text=True)
        fields = "name,tagName,isDraft,isPrerelease,url"
        try:
            release = json.loads(output("gh", "release", "view", version, "--repo", repository, "--json", fields))
        except subprocess.CalledProcessError:
            raise ValueError(create.stderr.strip() or f"Could not verify GitHub Release {version}.") from None
        require(release["name"] == release["tagName"] == version and not release["isDraft"]
                and not release["isPrerelease"], f"GitHub Release {version} does not match the requested publication.")
        print(f"{release['url']} ({commit})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", metavar="vX.Y.Z")
    try:
        publish(parser.parse_args().version)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Release stopped: {error}", file=sys.stderr)
        sys.exit(1)
