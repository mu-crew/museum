# Changelog

All notable changes to museum. Versions follow [Semantic Versioning](https://semver.org/);
before 1.0.0 a minor release may change on-disk state or setup.

## [0.5.0] — 2026-11-11

museum installs like mu, murmur and muzzle: `npm i -g @mu-crew/museum`, then
`museum init`, which sets up everything else, pi included, and cleans up what
older installs left behind.

### Added

- **`museum init [STORE [MACHINE]]`.** Writes the config on the first run,
  adds `museum hook <harness>` to the session start and end hooks of Claude
  Code, Codex, cursor-agent and Muse Code where each is set up (`--hooks
  LIST|all` for others, `--no-hooks` for none), links the skill into each
  harness, installs the pi package, runs the first sync and installs the
  timer. It owns only museum's own hook handlers, as murmur owns murmur's,
  and leaves every other entry alone. Run it again after an upgrade.
- **`museum status`**: this machine's version and last sync, then every
  machine's museum version and newest session.
- `museum` is the package's `bin`, so `npm i -g` puts it on PATH.

### Changed

- `museum fleet` and `museum-search fleet` are now `museum status` and
  `museum-search machines`, after the family's verbs (`muzzle status`,
  `murmur status`; `murmur peer list`, `mule host list`).
- pi's extension runs the museum-sync of the `museum` on PATH (asking it,
  for a version manager's shim), so one copy does every sync on a machine
  whatever version pi's package copy is.
- `install.sh` runs `museum init`, for installs from a checkout.

### Cleaned up by `museum init`

- A `~/.local/bin/museum` link to another museum (0.4.0's into pi's copy, or
  a hand-made one to a checkout).
- Every museum pi package but this install's kind: the npm package for an npm
  install, the checkout for a checkout.
- Hook handlers that run museum by path (`/…/bin/museum hook …`,
  `museum-sync --hook …`), replaced by `museum hook <harness>`.
- The `~/.agents/skills/museum` and `~/.pi/agent/extensions/museum.ts` links
  and the pre-0.2 `~/.cache/museum` folder.

### Upgrading from 0.4.0

```sh
rm -f ~/.local/bin/museum   # 0.4.0's link into pi's copy
npm i -g @mu-crew/museum
museum init
```

Remove the old link first: where npm's prefix is `~/.local`, `npm i -g`
refuses to replace it, and elsewhere it can shadow the new `museum` on PATH,
so `museum init` would run 0.4.0, which has no `init`.

`museum init` replaces the pi package entry and re-points the timer and the
skill links at the global copy.

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
