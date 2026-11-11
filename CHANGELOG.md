# Changelog

All notable changes to museum. Versions follow [Semantic Versioning](https://semver.org/);
before 1.0.0 a minor release may change on-disk state or setup.

## [0.4.0] — 2026-11-11

museum is released to npm as `@mu-crew/museum`, so every machine installs and
updates it like mu and the other tools: `pi install npm:@mu-crew/museum`, then
`pi update --extensions`. A machine running from a checkout keeps working.

### Added

- **npm package and releases.** A `v*` tag publishes `@mu-crew/museum` from
  CI with trusted publishing, and a GitHub release with this file's section
  as its notes (RELEASING.md).
- **`museum fleet`: what every machine runs.** Each sync leaves
  `<store>/<machine>/.museum/<version>.json`, and `museum fleet` (or
  `museum-search fleet`) lists each machine's version, since when, and its
  newest session, and marks machines behind the newest release.
- **`museum version`** prints the version, its commit for a checkout, and
  where it runs from.
- **The turn index.** `find` answers from `.museum-turns.sqlite`, an FTS5
  trigram index of user and assistant text at the store's root, kept up to
  date by every search and by each sync to a local store
  (`museum-search index`). `find` drops from about 3s to 0.7s on a 4 GB
  store. An index written by a newer museum is left alone.
- **`install.sh` links `museum` into `~/.local/bin`**, and warns when pi
  loads museum from two places.

### Changed

- `find` ranks by the turns whose text says the term (the `N turns` shown),
  and skips the tool-output scan when conversation matches fill the listing.

### Upgrading

- From a checkout: `pi remove <checkout>` (`pi list` shows it), then
  `pi install npm:@mu-crew/museum` and run the package's `install.sh` once,
  which re-points the timer, the skill links and `~/.local/bin/museum` at the
  package: `~/.pi/agent/npm/node_modules/@mu-crew/museum/install.sh`.

## [0.3.0] — 2026-11-07

Claude Code and Codex sessions are backed up, filtered, beside pi's, and
searchable with the same commands; `museum hook` and a timer drive the sync
for harnesses whose hooks run in a sandbox.

## [0.2.0] — 2026-10-21

`museum-search stats` and `calls` show where agent time goes: model, tool and
wait time by role, and the tool calls behind it.

## [0.1.0] — 2026-09-30

pi sessions from every machine, rsynced append-only into one store, searchable
with `museum-search find` and `show`; config in `~/.config/museum/config.toml`.
