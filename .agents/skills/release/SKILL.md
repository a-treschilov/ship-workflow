---
name: release
description: Publish a ship-workflow release from its source checkout when the user asks to create a version tag and GitHub Release.
---

# Release ship-workflow

Use the version supplied with `/release vX.Y.Z` or `$release vX.Y.Z`. Ask for the exact version if it is missing.
Read [Development](../../../docs/DEVELOPMENT.md#release) for version selection and release notes.

Prepare the requested version's dated `CHANGELOG.md` section from `Unreleased`, using the user's current date.
Inspect the staged and unstaged changes and commit the release contents. Keep unrelated work out of the release.
If the version section is already committed and the user is resuming publication, use that same commit.

From the source repository root, run the publisher with the supplied version:

```sh
python3 -I -B .agents/skills/release/scripts/publish.py vX.Y.Z
```

The publisher runs the test suite, creates an annotated tag, and verifies a scratch installation. It then
fast-forwards GitHub `main` and pushes the tag in one atomic operation, verifies both refs point to the tested
commit, and publishes the GitHub Release with the version as its exact title. Report the verified release URL
and commit. If publication is interrupted, retry from the same commit. A rejected branch update stops
publication. Preserve published tags and releases.
