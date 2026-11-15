# ship-workflow package

This repository is the single source for the ship/fix/ship-analyze skills and the ship-fix lifecycle
helpers. Projects install a pinned snapshot into `.agents/`; this repo itself is not a project.

- Edit the shared role text in `roles/`, host role metadata in `platforms/`, and the lifecycle in
  `workflows/ship-fix/`. Keep `PROTOCOL.md` as the single lifecycle source.
- `agent_models.py` resolves the project root from `CRUMB_SHIP_ROOT` or the nearest
  `.agents/agent-models.yaml`; never reintroduce a fixed install depth.
- Run `make test` before tagging. Tests use `SHIP_TEST_TMPDIR` for scratch state.
- Tag releases with semver `vX.Y.Z` and record user-visible changes in `CHANGELOG.md`.
