# Agent notes

[README.md](README.md) covers installing and using museum and, under Develop,
the `make check` gate; [docs/reference.md](docs/reference.md) the details,
configuration and environment variables; [docs/design.md](docs/design.md) the
decisions behind them. Keep the README short: details go in the reference. Releases: [RELEASING.md](RELEASING.md); add a
[CHANGELOG.md](CHANGELOG.md) entry for user-visible changes, in the top
section when it has no date yet, else in a new `## [Unreleased]` one.

## State directories

A change that breaks on-disk state compatibility with the released version gets
a new versioned directory under `~/.local/state` (`<tool><major>`, e.g. `mu5`,
`murmur2`), never a `<tool>-next` name; additive changes stay in the current
directory. Never run a development build against the real state directory:
tests and manual runs set `MUSEUM_STATE_DIR` (and `HOME`) to a temporary
directory.
