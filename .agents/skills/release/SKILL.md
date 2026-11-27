---
name: release
description: Publish a ship-workflow release from its source checkout when the user asks to create a version tag and GitHub Release.
---

# Release ship-workflow

Use the version supplied with `/release vX.Y.Z` or `$release vX.Y.Z`. Ask for the exact version if it is missing.
Read [Development](../../../docs/DEVELOPMENT.md#release) for version selection and release notes.

Fetch `origin/main` and merge it into the source checkout's current branch before preparing the release.
Resolve any merge conflicts in the checkout and preserve both histories. Include the resulting merge in the
release commit's history. If the checkout is behind `origin/main`, update it before preparing the release.
The publisher later merges the tested history into `origin/main` by fast-forwarding it together with the tag.

Prepare the requested version's dated `CHANGELOG.md` section from `Unreleased`, using the user's current date.
Inspect the staged and unstaged changes and commit the release contents. Keep unrelated work out of the release.
If the version section is already committed and the user is resuming publication, use that same commit.

From the source repository root, run the publisher with the supplied version:

```sh
python3 -I -B .agents/skills/release/scripts/publish.py vX.Y.Z
```

## Merge into main

The release includes the committed changes on the source checkout in GitHub `main`. The publisher performs
this step after tests and scratch installation pass: it fast-forwards `origin/main` to the tested release
commit and pushes the annotated version tag in the same atomic operation. Verify both remote refs point to
that commit before publishing the GitHub Release. If `main` advances and the push is rejected, fetch and merge
the new `origin/main` into the checkout, prepare a new release commit and rerun the publisher. Preserve any
already published tag and resume only with the commit it identifies. If the rejected atomic push left an
unpublished local tag, verify that the remote tag is absent, delete that local tag and let the publisher
create a new annotated tag for the newly tested commit.

The publisher creates and verifies the GitHub Release with the version as its exact title. Report the verified
release URL and commit. If publication is interrupted, retry from the same commit. Preserve published tags and
releases.
