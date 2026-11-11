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

Machines pick the release up with `pi update --extensions`, or
`pi update npm:@mu-crew/museum`. `museum fleet` shows which have. Mention in
the CHANGELOG when a release needs `install.sh` run again (a new timer, say):
an update replaces the package's files, but not what `install.sh` set up.

## Trust setup

`@mu-crew/museum` trusts owner `mu-crew`, repository `museum`, workflow
`release.yml`: npmjs.com → the package → Settings → Trusted publishing →
GitHub Actions.

A package cannot be trusted before it exists, so its first version is
published by hand: `npm login`, then `npm publish --access public` from a
clean checkout of the tag. Add the trusted publisher, and CI publishes from
then on.
