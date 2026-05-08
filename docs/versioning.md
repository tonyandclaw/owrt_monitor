# Versioning Policy

`owrt_monitor` follows [Semantic Versioning 2.0.0](https://semver.org/).

## Version format

`MAJOR.MINOR.PATCH`, with optional pre-release / build metadata suffixes
(e.g. `0.2.0-rc1`, `1.0.0+lab-2025q3`).

## When to bump each part

| Part | Bump when… |
| --- | --- |
| MAJOR | Breaking changes to: YAML config schema, CLI argument names, run-directory layout, on-disk SQLite schema, or `report.json` shape that consumers might parse. |
| MINOR | New behavior that's backwards-compatible: new transfer modes, new CLI subcommands, additional config fields with defaults, expanded state machine, etc. |
| PATCH | Bug fixes, doc updates, internal refactors that don't change public surface. |

The `0.x.y` line (where MAJOR == 0) is a development phase. Within `0.x.y`,
both `MINOR` and `PATCH` may include breaking changes, but the project will
call them out in `CHANGELOG.md` and bump `MINOR` rather than `PATCH` for them.

After we tag `1.0.0`, the strict SemVer rules above apply unconditionally.

## Public surface

What's covered by this policy:

- The CLI commands and their flags.
- The YAML config schema (every `Pydantic` field documented in
  `docs/config-reference.md`).
- The on-disk run-directory layout (`config.snapshot.yaml`, `events.jsonl`,
  `report.json`, `report.md`, `firmware/<file>.bin`, `serial.log`,
  `build.log`).
- The SQLite tables documented in `ARCHITECTURE.md`.
- The Python module surface re-exported from `owrt_monitor.__init__`
  (`OwrtConfig`, `load_config`, `__version__`).

What's *not* covered (subject to change without a MAJOR bump):

- Internal helpers (anything under `_private`, `tests/python/`, or not
  re-exported).
- Wire format of `events.jsonl` `fields` payloads beyond the documented
  state-transition envelope.
- Specific text of error messages.
- Python implementation details that the YAML schema does not constrain.

## Reading the version at runtime

```python
from owrt_monitor import __version__
print(__version__)
```

```sh
owrt-monitor --version
owrt-monitor -V
```

## Release process

1. Update `CHANGELOG.md`: move the `Unreleased` section to a dated version
   header.
2. Bump `__version__` in `python/owrt_monitor/__init__.py` and the
   `version` field in `pyproject.toml`. They must match.
3. Run `make lint && make test` — every test must pass.
4. Tag the commit: `git tag -a vX.Y.Z -m "owrt_monitor X.Y.Z"`.
5. Push the tag: `git push origin vX.Y.Z`.

## Deprecation policy

- A field, command, or behavior is deprecated by being announced as such
  in `CHANGELOG.md` with the version that introduced the deprecation.
- Deprecations are removed in the next MAJOR bump (or after at least one
  MINOR cycle for `0.x.y` development).
- Where possible, the workflow logs a `WARN`-level `deprecated_*` event
  when a deprecated input is loaded, so consumers see it in
  `events.jsonl` without needing to read release notes.
