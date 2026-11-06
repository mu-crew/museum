# Agent notes

[README.md](README.md) covers what museum does, its configuration and, under
Develop, the `make check` gate. [docs/design.md](docs/design.md) has the
decisions behind it.

## State directories

A change that breaks on-disk state compatibility with the released version gets
a new versioned directory under `~/.local/state` (`<tool><major>`, e.g. `mu5`,
`murmur2`), never a `<tool>-next` name; additive changes stay in the current
directory. Never run a development build against the real state directory:
tests and manual runs set `MUSEUM_STATE_DIR` (and `HOME`) to a temporary
directory.
