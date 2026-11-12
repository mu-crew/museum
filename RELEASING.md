# Releasing

A `v*` tag publishes `@mu-crew/museum` to npm and creates the GitHub release.
There is no npm token: npmjs.com trusts this repo's
`.github/workflows/release.yml` through OIDC (trusted publishing).

1. Bump `version` in `package.json`.
2. Move the `CHANGELOG.md` entries into a `## [x.y.z] — YYYY-MM-DD` section.
   The workflow fails without one: it is the release's notes.
3. Run `make check` and commit the release (`release: museum x.y.z (...)`).
4. Tag the same version and push the commit and tag:

   ```sh
   git tag -a v0.4.0 -m "museum 0.4.0"
   git push origin main v0.4.0
   ```

The workflow checks that the tag matches `package.json`, runs `make check`,
creates the GitHub release, then publishes. `npm pack --dry-run` shows what
the package holds (`files` in `package.json`); a test checks every entry
exists.

Machines pick the release up with `npm update -g @mu-crew/museum` and
`museum init`, and pi's copy with `pi update --extensions`; `museum status`
shows which have. A release that changes the store needs more care: see
below.

## Rolling out

Every machine syncs into one store on its own timer, and machines update
whenever someone gets to them, so versions are always mixed for a while. A
release is safe to roll out machine by machine when each machine changes only
its own `<machine>/` folder and an older museum still reads what a newer one
writes. When one does not hold (0.6 moved pi's sessions into `pi/`, which
0.5's search cannot see), say so in the CHANGELOG and follow these steps.

1. **Look first.** `museum status` lists every machine's version. A machine
   that reports none (before 0.4, or never synced) needs a visit.
2. **Snapshot the store** on the store host: a filesystem snapshot (APFS,
   ZFS, btrfs) or a full copy. Not a hard-link copy (`cp -al`): rsync's
   `--append` writes into existing files in place, so the snapshot would
   change with them.
3. **Canary on one machine.** `npm update -g @mu-crew/museum`, `museum init`,
   `pi update --extensions`, then `museum sync` and `museum doctor`. Check its
   folder in the store and compare a few `museum-search find` counts with
   ones taken before.
4. **Every other machine**, in any order, whenever: each moves only its own
   folder, and no two machines' ticks interact. Update the global copy before
   pi's: pi's extension runs the `museum-sync` beside the `museum` on PATH, so
   a newer extension over an older global copy may call what it lacks (0.6's
   calls `museum hook pi`, which 0.5 does not have).
5. **While versions are mixed**, search from an updated machine: the search
   runs the searching machine's copy, whatever the store host has. When a
   release changes how the turn index is filled, it bumps `TURNS_VERSION`, so
   an older search leaves the index alone and reads the files instead of
   fighting a newer one over it.
6. **Done** when `museum status` shows every machine on the release and
   `museum doctor` warns about none.

**Rollback** one machine with `npm i -g @mu-crew/museum@<previous>` and its
`pi install` of the same version. Restore from the snapshot only if a session
is missing from the store. For 0.6: a machine rolled back to 0.5 syncs pi's
sessions to the old place again, and its next sync with 0.6 moves them back,
keeping the longer copy of any session found in both places when the other
is a prefix of it, which is the only deletion museum makes.

## Trust setup

`@mu-crew/museum` trusts owner `mu-crew`, repository `museum`, workflow
`release.yml`: npmjs.com → the package → Settings → Trusted publishing →
GitHub Actions.

A package cannot be trusted before it exists, so its first version is
published by hand: `npm login`, then `npm publish --access public` from a
clean checkout of the tag. Add the trusted publisher, and CI publishes from
then on.
