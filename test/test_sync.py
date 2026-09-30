"""museum-sync against a local store, run as a real subprocess."""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import BIN, load

sync = load("museum-sync")
RSYNC = sync.find_rsync()


class Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.src = self.dir / "sessions"
        self.store = self.dir / "store"
        self.state = self.dir / "state"
        (self.src / "--proj--").mkdir(parents=True)
        (self.src / "--proj--" / "s1.jsonl").write_text('{"a":1}\n')
        (self.src / "--proj--" / "notes.txt").write_text("not a session\n")
        self.config = self.dir / "config"
        self.config.write_text(f"STORE='{self.store}'\nNAME=box  # pinned\n")

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

    def state_field(self, key: str) -> str:
        for line in (self.state / "state").read_text().splitlines():
            k, _, v = line.partition("=")
            if k == key:
                return v
        raise KeyError(key)


@unittest.skipUnless(RSYNC, "rsync 3 not installed")
class LocalStore(Sandbox):
    def test_copies_sessions_only_and_records_ok(self) -> None:
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        copied = self.store / "box" / "--proj--"
        self.assertEqual(sorted(p.name for p in copied.iterdir()), ["s1.jsonl"])
        self.assertEqual(self.state_field("status"), "ok")
        self.assertFalse((self.state / "pending").exists())

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
        self.config.write_text("STORE=/dev/null/store\n")
        r = self.run_sync()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.state_field("status"), "error")
        self.assertIn("mkdir", self.state_field("message"))


class Failures(Sandbox):
    def test_mux_only_without_a_master_skips(self) -> None:
        self.config.write_text("STORE=museum-test-no-such-host.invalid:/srv\nSSH_MUX_ONLY=1\n")
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
        self.config.write_text("STORE=box:/srv/museum\nSSH_MUX_ONLY=1\n")

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
        self.config.write_text("STORE=box:/srv/museum\n")
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

    def test_read_config(self) -> None:
        with tempfile.NamedTemporaryFile("w") as f:
            f.write("# c\nSTORE='host:/a b'\nNAME=x # pinned\n\n")
            f.flush()
            self.assertEqual(sync.read_config(Path(f.name)), {"STORE": "host:/a b", "NAME": "x"})


if __name__ == "__main__":
    unittest.main()
