"""museum-sync against a local store, run as a real subprocess."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from helpers import BIN, ROOT, SCRIPTS, load

sync = load("museum-sync")
RSYNC = sync.find_rsync()


class Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.src = self.dir / "sessions"
        self.store = self.dir / "store"
        self.store.mkdir()  # a local store folder exists: install.sh made it
        self.state = self.dir / "state"
        (self.src / "--proj--").mkdir(parents=True)
        (self.src / "--proj--" / "s1.jsonl").write_text('{"a":1}\n')
        (self.src / "--proj--" / "notes.txt").write_text("not a session\n")
        self.config = self.dir / "config.toml"
        self.write_config(f'machine = "box"  # pinned\n\n[store]\npath = "{self.store}"\n')

    def write_config(self, body: str) -> None:
        self.config.write_text(body)

    def env(self, **extra: str) -> dict[str, str]:
        return {
            **os.environ,
            "MUSEUM_CONFIG": str(self.config),
            "MUSEUM_STATE_DIR": str(self.state),
            "PI_SESSIONS_DIR": str(self.src),
            **extra,
        }

    def run_sync(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(BIN / "museum-sync"), *args],
            capture_output=True,
            text=True,
            env=self.env(**env),
            check=False,
            timeout=60,
        )

    def state_field(self, key: str) -> Any:
        return json.loads((self.state / "state.json").read_text())[key]


@unittest.skipUnless(RSYNC, "rsync 3 not installed")
class LocalStore(Sandbox):
    def test_copies_sessions_only_and_records_ok(self) -> None:
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        copied = self.store / "box" / "--proj--"
        self.assertEqual(sorted(p.name for p in copied.iterdir()), ["s1.jsonl"])

    def test_syncs_from_a_deleted_cwd(self) -> None:
        # pi starts the sync in its own cwd, often a mu workspace that has since
        # been deleted; rsync's getcwd() then fails with code 3.
        gone = self.dir / "gone"
        gone.mkdir()
        r = subprocess.run(
            [
                "sh",
                "-c",
                'rmdir "$PWD" && exec "$0" "$1"',
                sys.executable,
                str(BIN / "museum-sync"),
            ],
            capture_output=True,
            text=True,
            env=self.env(),
            check=False,
            timeout=60,
            cwd=gone,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state_field("status"), "ok")
        self.assertTrue((self.store / "box" / "--proj--" / "s1.jsonl").exists())
        self.assertEqual(self.state_field("status"), "ok")
        self.assertEqual(self.state_field("warning"), "")
        self.assertFalse((self.state / "pending").exists())

    def test_warning_follows_the_latest_sync(self) -> None:
        # Every pi on the node shows this one file, so a recovery by any
        # agent's sync clears the label for all of them.
        self.store.chmod(0o500)
        self.run_sync()
        self.assertTrue(self.state_field("warning").startswith("museum: backup failing: mkdir"))
        self.store.chmod(0o700)
        self.run_sync()
        self.assertEqual(self.state_field("warning"), "")

    def test_append_only_keeps_deleted_sessions_and_appends(self) -> None:
        self.run_sync()
        (self.src / "--proj--" / "s1.jsonl").write_text('{"a":1}\n{"b":2}\n')
        (self.src / "--proj--" / "s2.jsonl").write_text("{}\n")
        self.run_sync()
        (self.src / "--proj--" / "s2.jsonl").unlink()
        self.run_sync()
        copied = self.store / "box" / "--proj--"
        self.assertTrue((copied / "s2.jsonl").exists())
        self.assertEqual((copied / "s1.jsonl").read_text(), '{"a":1}\n{"b":2}\n')

    def test_if_due_skips_a_recent_sync(self) -> None:
        self.run_sync()
        before = (self.state / "last-start").read_text()
        (self.src / "--proj--" / "s3.jsonl").write_text("{}\n")
        self.assertEqual(self.run_sync("--if-due").returncode, 0)
        self.assertEqual((self.state / "last-start").read_text(), before)
        self.assertFalse((self.store / "box" / "--proj--" / "s3.jsonl").exists())
        self.run_sync("--if-due", MUSEUM_INTERVAL="0")
        self.assertTrue((self.store / "box" / "--proj--" / "s3.jsonl").exists())

    def test_held_lock_leaves_pending_for_the_holder(self) -> None:
        self.state.mkdir()
        fd = os.open(self.state / "lock", os.O_WRONLY | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            start = time.monotonic()
            r = self.run_sync("--now")
            self.assertEqual(r.returncode, 0)
            self.assertLess(time.monotonic() - start, 5)
            self.assertTrue((self.state / "pending").exists())
            self.assertFalse((self.store / "box").exists())
        finally:
            os.close(fd)
        # The next caller (the holder, in real life) picks up the mark.
        self.run_sync("--if-due")
        self.assertTrue((self.store / "box" / "--proj--" / "s1.jsonl").exists())
        self.assertFalse((self.state / "pending").exists())

    def test_unwritable_store_records_the_error(self) -> None:
        self.store.chmod(0o500)
        self.addCleanup(self.store.chmod, 0o700)
        r = self.run_sync()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.state_field("status"), "error")
        self.assertIn("mkdir", self.state_field("message"))

    def test_forgotten_host_fails_instead_of_backing_up_locally(self) -> None:
        # An ssh store's path, with the host line gone: no such folder here.
        missing = self.dir / "home" / "u" / "museum"
        self.write_config(f'machine = "box"\n[store]\npath = "{missing}"\n')
        r = self.run_sync()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(
            self.state_field("message"),
            f"store.path {missing} does not exist here (is store.host missing?)",
        )
        self.assertFalse(missing.exists())


class Failures(Sandbox):
    def test_mux_only_without_a_master_skips(self) -> None:
        self.write_config(
            'machine = "box"\n[store]\nhost = "museum-test-no-such-host.invalid"\n'
            'path = "/srv"\nssh_mux_only = true\n'
        )
        r = self.run_sync()
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("retrying", r.stderr)
        self.assertEqual(
            self.state_field("message"),
            "no ssh master for museum-test-no-such-host.invalid "
            "(run: ssh -MNf museum-test-no-such-host.invalid)",
        )

    def test_missing_config(self) -> None:
        self.config.unlink()
        r = self.run_sync()
        self.assertEqual(r.returncode, 1)
        self.assertIn("missing", r.stderr)
        # Recorded, so pi's footer says what is wrong.
        self.assertEqual(self.state_field("status"), "error")
        self.assertIn("run install.sh", self.state_field("message"))
        self.assertIn("run install.sh", self.state_field("warning"))

    def test_bad_config_is_recorded_not_synced(self) -> None:
        self.write_config('machine = "box"\n[store]\npath = "/tmp/x"\nhots = "typo"\n')
        r = self.run_sync()
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown key store.hots", self.state_field("message"))
        self.assertFalse((self.state / "last-start").exists())

    def test_old_python_says_so_in_the_footer(self) -> None:
        old = shutil.which("python3.9") or "/usr/bin/python3"
        version = subprocess.run(
            [
                old,
                "-c",
                "import sys; print(sys.version_info >= (3, 9) and sys.version_info < (3, 11))",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if version.stdout.strip() != "True":
            self.skipTest("no Python 3.9 or 3.10 here")
        r = subprocess.run(
            [old, str(BIN / "museum-sync")],
            capture_output=True,
            text=True,
            env=self.env(),
            check=False,
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("needs Python 3.11+", self.state_field("message"))

    def test_bad_flag(self) -> None:
        self.assertEqual(self.run_sync("--later").returncode, 2)
        self.assertEqual(self.run_sync("--now", "--if-due").returncode, 2)


# Stands in for rsync (and ssh, for `-O check`) so the tests can pause, fail or
# orphan a sync on cue. Controlled through files in $FAKE:
#   hold      the first call to take it (an atomic mv) waits for `release`
#   fail      the first N calls, N being its content, fail the way an ssh
#             connection failure does
#   exit      every other call exits with this status (default 0)
# Every call logs `start <pid>` to $FAKE/log.
FAKE_RSYNC = r"""#!/bin/sh
[ "$1" = --version ] && { echo "rsync  version 3.9.9  protocol version 32"; exit 0; }
echo "start $$" >> "$FAKE/log"
n=$(grep -c . "$FAKE/log")
if mv "$FAKE/hold" "$FAKE/hold.taken" 2> /dev/null; then
  i=0
  while [ ! -e "$FAKE/release" ] && [ $i -lt 400 ]; do sleep 0.05; i=$((i + 1)); done
fi
if [ "$n" -le "$(cat "$FAKE/fail" 2> /dev/null || echo 0)" ]; then
  echo "ssh: connect to host box port 22: Connection refused" >&2
  echo "rsync: connection unexpectedly closed (0 bytes received so far) [sender]" >&2
  echo "rsync error: error in rsync protocol data stream (code 12)" >&2
  exit 12
fi
exit "$(cat "$FAKE/exit" 2> /dev/null || echo 0)"
"""
FAKE_SSH = "#!/bin/sh\nexit 0\n"  # every `ssh -O check` finds a master


class FakeRsync(Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.fake = self.dir / "fake"
        (self.fake / "bin").mkdir(parents=True)
        for name, body in (("rsync", FAKE_RSYNC), ("ssh", FAKE_SSH)):
            path = self.fake / "bin" / name
            path.write_text(body)
            path.chmod(0o755)
        self.addCleanup(self.release)

    def env(self, **extra: str) -> dict[str, str]:
        path = f"{self.fake / 'bin'}{os.pathsep}{os.environ['PATH']}"
        return super().env(FAKE=str(self.fake), PATH=path, MUSEUM_RETRY_DELAY="0", **extra)

    def spawn(self, *args: str) -> subprocess.Popen[bytes]:
        p = subprocess.Popen(
            [sys.executable, str(BIN / "museum-sync"), *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self.env(),
        )
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def release(self) -> None:
        (self.fake / "release").touch()

    def starts(self) -> list[int]:
        log = self.fake / "log"
        return (
            [int(line.split()[1]) for line in log.read_text().splitlines()] if log.exists() else []
        )

    def wait_for_starts(self, n: int) -> None:
        deadline = time.monotonic() + 10
        while len(self.starts()) < n:
            self.assertLess(time.monotonic(), deadline, f"rsync started {len(self.starts())}x")
            time.sleep(0.02)

    def test_flush_mid_sync_makes_the_holder_sync_again(self) -> None:
        (self.fake / "hold").touch()
        holder = self.spawn("--now")
        self.wait_for_starts(1)
        # The lock is taken: the flush leaves its mark and returns at once.
        start = time.monotonic()
        self.assertEqual(self.run_sync("--now").returncode, 0)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(len(self.starts()), 1)
        self.release()
        self.assertEqual(holder.wait(timeout=10), 0)
        self.assertEqual(len(self.starts()), 2)
        self.assertFalse((self.state / "pending").exists())

    def test_if_due_callers_during_a_sync_add_nothing(self) -> None:
        (self.fake / "hold").touch()
        holder = self.spawn("--now")
        self.wait_for_starts(1)
        for _ in range(3):
            self.assertEqual(self.run_sync("--if-due", MUSEUM_INTERVAL="0").returncode, 0)
        self.release()
        self.assertEqual(holder.wait(timeout=10), 0)
        self.assertEqual(len(self.starts()), 1)

    def test_killed_holder_frees_the_lock_despite_an_orphaned_rsync(self) -> None:
        (self.fake / "hold").touch()
        holder = self.spawn("--now")
        self.wait_for_starts(1)
        orphan = self.starts()[0]
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)
        os.kill(orphan, 0)  # still running, and it must not hold the lock
        r = self.run_sync("--now")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.starts()), 2)
        self.assertEqual(self.state_field("status"), "ok")

    def mux(self) -> None:
        self.write_config(
            'machine = "box"\n[store]\nhost = "box"\npath = "/srv/museum"\nssh_mux_only = true\n'
        )

    def test_mux_only_retries_a_failure_once(self) -> None:
        self.mux()
        (self.fake / "fail").write_text("1")
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("retrying in 0s", r.stderr)
        self.assertEqual(len(self.starts()), 2)
        self.assertEqual(self.state_field("status"), "ok")

    def test_mux_only_records_the_ssh_line_after_the_retry_fails(self) -> None:
        self.mux()
        (self.fake / "fail").write_text("2")
        self.assertEqual(self.run_sync().returncode, 1)
        self.assertEqual(len(self.starts()), 2)
        self.assertEqual(self.state_field("status"), "error")
        self.assertEqual(
            self.state_field("message"), "ssh: connect to host box port 22: Connection refused"
        )

    def test_without_mux_only_a_failure_waits_for_the_next_sync(self) -> None:
        self.write_config('machine = "box"\n[store]\nhost = "box"\npath = "/srv/museum"\n')
        (self.fake / "fail").write_text("1")
        r = self.run_sync()
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("retrying", r.stderr)
        self.assertEqual(len(self.starts()), 1)

    def test_vanished_files_count_as_success(self) -> None:
        (self.fake / "exit").write_text("24")
        self.assertEqual(self.run_sync().returncode, 0)
        self.assertEqual(self.state_field("status"), "ok")
        (self.fake / "exit").write_text("23")
        self.assertEqual(self.run_sync().returncode, 1)
        self.assertEqual(self.state_field("status"), "error")


class Units(unittest.TestCase):
    def test_error_line_prefers_ssh_over_rsync_tail(self) -> None:
        out = (
            "ssh: connect to host x port 22: Connection refused\n"
            "rsync: connection unexpectedly closed (0 bytes received so far)\n"
            "rsync error: error in rsync protocol data stream (code 12)\n"
        )
        self.assertEqual(sync.error_line(out), "ssh: connect to host x port 22: Connection refused")
        self.assertEqual(sync.error_line("a\nlast\n\n"), "last")
        self.assertEqual(sync.error_line(""), "")

    def load(self, body: str) -> Any:
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as f:
            f.write(body)
            f.flush()
            return sync.load_config(Path(f.name))

    def test_load_config(self) -> None:
        c = self.load('machine = "x"\n[store]\nhost = "devbox"\npath = "/data/a b/"\n')
        self.assertEqual(c, sync.Config("x", "devbox", "/data/a b", False))
        self.assertEqual(c.target(), "devbox:/data/a b")
        local = self.load('machine = "x"\n[store]\npath = "/srv"\n')
        self.assertEqual(local.target(), "/srv")

    def test_load_config_rejects_what_would_silently_misbehave(self) -> None:
        cases = {
            "unknown key store.hots": '[store]\nhots = "x"\npath = "/a"\nmachine = "m"',
            "unknown key mashine": 'mashine = "m"\n[store]\npath = "/a"',
            "machine must be a non-empty string": '[store]\npath = "/a"',
            "store must be a table": 'machine = "m"\nstore = "devbox:/a"',
            "put the folder in store.path": 'machine = "m"\n[store]\nhost = "devbox:/a"\npath = "/a"',
            "store.path must be absolute": 'machine = "m"\n[store]\npath = "museum"',
            "true or false": 'machine = "m"\n[store]\nhost = "h"\npath = "/a"\nssh_mux_only = 1',
            "needs store.host": 'machine = "m"\n[store]\npath = "/a"\nssh_mux_only = true',
            "without /": 'machine = "a/b"\n[store]\npath = "/a"',
        }
        for message, body in cases.items():
            with self.subTest(message), self.assertRaises(sync.ConfigError) as e:
                self.load(body)
            self.assertIn(message, str(e.exception))

    def test_install_and_sync_write_the_same_config(self) -> None:
        """install.sh and convert_legacy each write config.toml; the files, and
        so their comments, must not drift."""
        for store, machine in (("devbox:/data/a b", 'mac"mini'), ("LOCAL", "pc")):
            with self.subTest(store=store), tempfile.TemporaryDirectory() as d:
                home = Path(d)
                if store == "LOCAL":  # install.sh creates it: keep it in the temp dir
                    store = str(home / "store")
                # install.sh up to the config, without the ssh probe, links
                # and first sync that need a real host and pi.
                src = (ROOT / "install.sh").read_text()
                probe = 'ssh -o BatchMode=yes "$HOST" "test -d \'$STORE_PATH/$MACHINE\'"'
                self.assertIn(probe, src)
                src = src[: src.index("# Skill: symlink")].replace(probe, "false")
                script = home / "install.sh"
                script.write_text(
                    src.replace('ROOT="$(cd "$(dirname "$0")" && pwd)"', f"ROOT={ROOT}")
                )
                subprocess.run(
                    ["sh", str(script), store, machine],
                    env={**os.environ, "HOME": str(home)},
                    capture_output=True,
                    check=True,
                )
                if not store.count(":"):
                    self.assertTrue(Path(store).is_dir())
                written = (home / ".config/museum/config.toml").read_text()
                c = sync.load_config(home / ".config/museum/config.toml")
                self.assertEqual(c.machine, machine)
                self.assertEqual(written, sync.toml(c))

    def test_legacy_config_converts_once(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            old = Path(d, "config")
            old.write_text("STORE='devbox:/data/museum'\nNAME=mac  # pinned\nSSH_MUX_ONLY=1\n")
            new = Path(d, "config.toml")
            sync.convert_legacy(new)
            self.assertEqual(
                sync.load_config(new), sync.Config("mac", "devbox", "/data/museum", True)
            )
            self.assertFalse(old.exists())
            self.assertTrue(Path(d, "config.old").exists())
            Path(d, "config").write_text("STORE=/elsewhere\n")
            sync.convert_legacy(new)  # config.toml exists: left alone
            self.assertEqual(sync.load_config(new).host, "devbox")
            local = Path(d, "local.toml")
            Path(d, "config").write_text("STORE=/srv/museum\nNAME=pc\n")
            sync.convert_legacy(local)
            self.assertEqual(sync.load_config(local), sync.Config("pc", None, "/srv/museum", False))

    def test_both_scripts_carry_the_same_config_code(self) -> None:
        def block(path: Path) -> str:
            src = path.read_text()
            start = src.index("\n", src.index("# --- config:"))
            return src[start : src.index("# --- end config ---")]

        self.assertEqual(block(BIN / "museum-sync"), block(SCRIPTS / "museum-search"))


if __name__ == "__main__":
    unittest.main()
