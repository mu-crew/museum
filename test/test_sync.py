"""museum-sync against a local store, run as a real subprocess."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

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
            # Never the real ones: these tests must not stage this machine's
            # claude, codex or cursor-agent sessions.
            "CLAUDE_CONFIG_DIR": str(self.dir / "claude"),
            "CODEX_HOME": str(self.dir / "codex"),
            "CURSOR_HOME": str(self.dir / "cursor"),
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
        copied = self.store / "box" / "pi" / "--proj--"
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
        self.assertTrue((self.store / "box" / "pi" / "--proj--" / "s1.jsonl").exists())
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
        copied = self.store / "box" / "pi" / "--proj--"
        self.assertTrue((copied / "s2.jsonl").exists())
        self.assertEqual((copied / "s1.jsonl").read_text(), '{"a":1}\n{"b":2}\n')

    def test_updates_a_local_store_turn_index(self) -> None:
        turn = {"type": "message", "message": {"role": "user", "content": "the quokka plan"}}
        (self.src / "--proj--" / "s1.jsonl").write_text(json.dumps(turn) + "\n")
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        db = sqlite3.connect(self.store / ".museum-turns.sqlite")
        self.assertEqual(db.execute("SELECT body FROM turn").fetchall(), [("the quokka plan",)])

    def test_reports_its_version_through_the_store(self) -> None:
        self.assertEqual(self.run_sync().returncode, 0)
        version = json.loads((ROOT / "package.json").read_text())["version"]
        marks = list((self.store / "box" / ".museum").glob("*.json"))
        self.assertEqual(len(marks), 1)
        doc = json.loads(marks[0].read_text())
        self.assertEqual((doc["version"], doc["machine"]), (version, "box"))
        self.assertTrue(marks[0].name.startswith(version))
        # The next sync with the same version adds nothing.
        self.assertEqual(self.run_sync().returncode, 0)
        self.assertEqual(list((self.store / "box" / ".museum").glob("*.json")), marks)
        out = self.run_sync("--version").stdout
        self.assertTrue(out.startswith(f"museum {version}"), out)

    def test_no_index_when_told_not_to(self) -> None:
        self.assertEqual(self.run_sync(MUSEUM_NO_INDEX="1").returncode, 0)
        self.assertFalse((self.store / ".museum-turns.sqlite").exists())

    def test_no_index_after_a_failed_sync(self) -> None:
        self.store.chmod(0o500)
        self.addCleanup(self.store.chmod, 0o700)
        self.assertEqual(self.run_sync().returncode, 1)
        self.assertFalse((self.store / ".museum-turns.sqlite").exists())

    def test_if_due_skips_a_recent_sync(self) -> None:
        self.run_sync()
        before = (self.state / "last-start").read_text()
        (self.src / "--proj--" / "s3.jsonl").write_text("{}\n")
        self.assertEqual(self.run_sync("--if-due").returncode, 0)
        self.assertEqual((self.state / "last-start").read_text(), before)
        self.assertFalse((self.store / "box" / "pi" / "--proj--" / "s3.jsonl").exists())
        self.run_sync("--if-due", MUSEUM_INTERVAL="0")
        self.assertTrue((self.store / "box" / "pi" / "--proj--" / "s3.jsonl").exists())

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
        self.assertTrue((self.store / "box" / "pi" / "--proj--" / "s1.jsonl").exists())
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
        self.assertIn("run museum init", self.state_field("message"))
        self.assertIn("missing", self.state_field("warning"))  # cut at 120 characters

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


PI_FAKE = """#!/bin/sh
echo "$*" >> "$PI_LOG"
case "$1" in
  list) cat "$PI_LIST" 2> /dev/null ;;
  remove) grep -v "$2" "$PI_LIST" > "$PI_LIST.new" 2> /dev/null; mv "$PI_LIST.new" "$PI_LIST" ;;
esac
exit 0
"""


@unittest.skipUnless(RSYNC, "rsync 3 not installed")
class Init(unittest.TestCase):
    """`museum init` in a sandbox home: a fake pi, the real rsync, no timer."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.home = self.dir / "home"
        self.home.mkdir()
        self.store = self.dir / "store"
        fake = self.dir / "fakebin"
        fake.mkdir()
        (fake / "pi").write_text(PI_FAKE)
        (fake / "pi").chmod(0o755)
        self.pi_log = self.dir / "pi.log"
        self.pi_list = self.dir / "pi-list"
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CURSOR_HOME", "MUSEUM_CONFIG")
        }
        self.env |= {
            "HOME": str(self.home),
            "PATH": f"{fake}:{self.home}/.local/bin:{Path(sys.executable).parent}:"
            f"{Path(RSYNC or '').parent}:/usr/bin:/bin",
            "MUSEUM_STATE_DIR": str(self.dir / "state"),
            "PI_SESSIONS_DIR": str(self.dir / "sessions"),
            "MUSEUM_TIMER_KIND": "none",  # never launchd or systemd
            "PI_LOG": str(self.pi_log),
            "PI_LIST": str(self.pi_list),
        }

    def init(self, *args: str, root: Path = ROOT) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(root / "bin/museum"), "init", *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def config(self) -> Path:
        return self.home / ".config/museum/config.toml"

    def test_first_run_writes_the_config_and_syncs(self) -> None:
        r = self.init(str(self.store), "box")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = sync.load_config(self.config())
        self.assertEqual((c.machine, c.host, c.path), ("box", None, str(self.store)))
        self.assertEqual(self.config().read_text(), sync.toml(c))
        self.assertTrue((self.store / "box" / ".museum").is_dir())  # the first sync ran
        # A later run keeps the config, and needs no store.
        self.assertEqual(self.init().returncode, 0)
        self.assertEqual(sync.load_config(self.config()), c)

    def test_first_run_needs_a_store_and_a_free_machine_name(self) -> None:
        r = self.init()
        self.assertEqual(r.returncode, 1)
        self.assertIn("first run needs the store", r.stderr)
        me = socket.gethostname().split(".")[0]
        (self.store / me).mkdir(parents=True)
        r = self.init(str(self.store))
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"{me} already exists", r.stderr)
        self.assertFalse(self.config().exists())
        # Naming it says the reuse is intended.
        self.assertEqual(self.init(str(self.store), me).returncode, 0)

    def test_hooks_keep_other_entries_and_replace_older_museum_ones(self) -> None:
        claude = self.home / ".claude/settings.json"
        claude.parent.mkdir()
        other = {"type": "command", "command": "murmur hook claude"}
        old = {"type": "command", "command": "/opt/museum/bin/museum hook claude"}
        claude.write_text(
            json.dumps({"model": "x", "hooks": {"SessionStart": [{"hooks": [other, old]}]}})
        )
        (self.home / ".cursor").mkdir()
        self.assertEqual(self.init(str(self.store), "box").returncode, 0)
        doc = json.loads(claude.read_text())
        self.assertEqual(doc["model"], "x")
        start = [h for g in doc["hooks"]["SessionStart"] for h in g["hooks"]]
        self.assertEqual(
            [h["command"] for h in start], ["murmur hook claude", "museum hook claude"]
        )
        # Hooks start no syncs, so museum has no SessionEnd entry.
        self.assertNotIn("SessionEnd", doc["hooks"])
        cursor = json.loads((self.home / ".cursor/hooks.json").read_text())
        self.assertEqual(cursor["version"], 1)
        self.assertEqual(
            cursor["hooks"], {"sessionStart": [{"command": "museum hook cursor-agent"}]}
        )
        # Harnesses not set up here get nothing, unless asked for.
        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse((self.home / ".config/muse").exists())
        # A second run changes nothing, not even formatting.
        before = claude.read_text()
        r = self.init("--hooks", "muse")
        self.assertIn("hooks muse: added SessionStart (", r.stdout)
        self.assertEqual(claude.read_text(), before)
        self.assertTrue((self.home / ".config/muse/settings.json").is_file())

    def test_hooks_follow_moved_config_dirs_and_symlinks(self) -> None:
        moved = self.dir / "claude-elsewhere"
        moved.mkdir()
        dotfiles = self.dir / "dotfiles/settings.json"
        dotfiles.parent.mkdir()
        dotfiles.write_text('{"statusLine": "→ é"}')
        (moved / "settings.json").symlink_to(dotfiles)
        self.env["CLAUDE_CONFIG_DIR"] = str(moved)
        r = self.init(str(self.store), "box")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.home / ".claude").exists())
        # Written through the link, which stays a link, and non-ASCII stays as it was.
        self.assertTrue((moved / "settings.json").is_symlink())
        text = dotfiles.read_text()
        self.assertIn("museum hook claude", text)
        self.assertIn('"→ é"', text)
        self.assertEqual((moved / "skills/museum").readlink(), ROOT / "skills/museum")

    def test_hooks_refuse_invalid_json_constants_and_unknown_harnesses(self) -> None:
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text('{"x": NaN}')
        r = self.init(str(self.store), "box")
        self.assertIn("not valid JSON", r.stderr)
        self.assertEqual(settings.read_text(), '{"x": NaN}')
        r = self.init("--hooks", "claude,vim")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown harness vim", r.stderr)

    def test_first_run_refuses_what_the_sync_would_not_take(self) -> None:
        for args, message in (
            ((str(self.store), "a/b"), "without /"),
            ((str(self.store), ".."), "plain folder name"),
            ((":" + str(self.store),), "no host before the colon"),
        ):
            with self.subTest(args=args):
                r = self.init(*args)
                self.assertEqual(r.returncode, 1)
                self.assertIn(message, r.stderr)
                self.assertFalse(self.config().exists())
        self.assertFalse(self.store.exists())

    def test_first_run_stops_when_the_store_host_cannot_be_asked(self) -> None:
        fake = Path(self.env["PATH"].split(":")[0])
        (fake / "ssh").write_text("#!/bin/sh\nexit 255\n")
        (fake / "ssh").chmod(0o755)
        r = self.init("devbox:/data/museum")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot reach devbox", r.stderr)
        self.assertFalse(self.config().exists())

    def test_status_shows_this_machine_then_every_machine(self) -> None:
        self.assertEqual(self.init(str(self.store), "box").returncode, 0)
        r = subprocess.run(
            [str(ROOT / "bin/museum"), "status"],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        version = json.loads((ROOT / "package.json").read_text())["version"]
        lines = r.stdout.splitlines()
        self.assertTrue(lines[0].startswith(f"museum {version}"), lines[0])
        self.assertRegex(lines[1], r"^last sync: \d{4}-\d\d-\d\d \d\d:\d\d ok$")
        self.assertTrue(any(line.startswith("box ") and version in line for line in lines))
        (self.dir / "state/state.json").write_text("{broken")
        r = subprocess.run(
            [str(ROOT / "bin/museum"), "status"],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertIn("last sync: none yet", r.stdout)

    def test_no_hooks_leaves_harness_configs_alone(self) -> None:
        (self.home / ".claude").mkdir()
        self.assertEqual(self.init(str(self.store), "box", "--no-hooks").returncode, 0)
        self.assertFalse((self.home / ".claude/settings.json").exists())

    def test_an_unreadable_config_file_is_named_and_left_alone(self) -> None:
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text("{not json")
        r = self.init(str(self.store), "box")
        self.assertIn("left", r.stderr)
        self.assertEqual(settings.read_text(), "{not json")

    def test_skills_and_older_installs_leftovers(self) -> None:
        (self.home / ".codex").mkdir()
        (self.home / ".cursor").mkdir()
        agents = self.home / ".agents/skills/museum"
        agents.parent.mkdir(parents=True)
        agents.symlink_to(ROOT / "skills/museum")
        cursor = self.home / ".cursor/skills/museum"
        cursor.parent.mkdir(parents=True)
        cursor.symlink_to(ROOT / "skills/museum")  # before codex held it
        cache = self.home / ".cache/museum"
        cache.mkdir(parents=True)
        (cache / "last-start").write_text("1\n")
        self.assertEqual(self.init(str(self.store), "box").returncode, 0)
        self.assertEqual((self.home / ".codex/skills/museum").readlink(), ROOT / "skills/museum")
        # cursor-agent reads codex's copy, so it gets none of its own.
        self.assertFalse(cursor.is_symlink())
        self.assertFalse(agents.is_symlink())
        self.assertFalse(cache.exists())

    def test_a_checkout_links_itself_and_reports_what_pi_needs(self) -> None:
        old = self.dir / "old-museum"
        (old / "bin").mkdir(parents=True)
        (old / "package.json").write_text('{"name": "@mu-crew/museum"}')
        (old / "bin/museum").write_text("#!/bin/sh\n")
        link = self.home / ".local/bin/museum"
        link.parent.mkdir(parents=True)
        link.symlink_to(old / "bin/museum")
        self.pi_list.write_text(
            f"User packages:\n  npm:@mu-crew/museum\n    {old}\n  npm:@mu-crew/mu\n    /nowhere\n"
        )
        r = self.init(str(self.store), "box")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(link.readlink(), ROOT / "bin/museum")
        # pi's package list is reported on, never edited, as mu init does.
        self.assertEqual(self.pi_log.read_text(), "list\n")
        self.assertIn(f"pi: run `pi install {ROOT}` once", r.stdout)
        self.assertIn(
            f"pi: also loads museum from {old}; run `pi remove npm:@mu-crew/museum`", r.stdout
        )

    def test_an_npm_install_reports_what_pi_needs(self) -> None:
        package = self.dir / "lib/node_modules/@mu-crew/museum"
        package.parent.mkdir(parents=True)
        shutil.copytree(ROOT, package, ignore=shutil.ignore_patterns(".git", "test", "__pycache__"))
        checkout = self.dir / "src/museum"
        checkout.mkdir(parents=True)
        (checkout / "package.json").write_text('{"name": "@mu-crew/museum"}')
        old = self.dir / "pi-npm/@mu-crew/museum"
        old.mkdir(parents=True)
        (old / "package.json").write_text('{"name": "@mu-crew/museum", "version": "0.4.0"}')
        self.pi_list.write_text(
            f"User packages:\n  ../src/museum\n    {checkout}\n  npm:@mu-crew/museum\n    {old}\n"
        )
        (checkout / "bin").mkdir()
        (checkout / "bin/museum").write_text("#!/bin/sh\n")
        link = self.home / ".local/bin/museum"
        link.parent.mkdir(parents=True)
        link.symlink_to(checkout / "bin/museum")  # a hand-made link from an older install
        r = self.init(str(self.store), "box", root=package)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.pi_log.read_text(), "list\n")
        self.assertNotIn("pi install", r.stdout)  # pi has the npm package already
        # A local source is named by its folder: pi reads a relative one from the cwd.
        self.assertIn(f"run `pi remove {checkout}`", r.stdout)
        # npm's own bin link puts it on PATH: the older link goes, none replaces it.
        self.assertFalse(link.exists() or link.is_symlink())
        self.assertIn("not on PATH", r.stderr)


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

    def test_plan_hooks_keeps_a_users_fields_and_drops_filtered_handlers(self) -> None:
        mine = {"type": "command", "command": "museum hook codex", "timeout": 5}
        filtered = {"matcher": "startup", "hooks": [{"command": "museum hook codex"}]}
        root = {
            "hooks": {
                "SessionStart": [{"hooks": [mine]}, filtered],
                "Stop": [{"hooks": [{"command": "museum-sync --hook codex"}]}],
            }
        }
        added, fixed, removed = sync.plan_hooks("codex", root)
        self.assertEqual((added, fixed), ([], []))
        self.assertEqual(
            removed, ["SessionStart: museum hook codex", "Stop: museum-sync --hook codex"]
        )
        self.assertEqual(root["hooks"]["SessionStart"], [{"hooks": [mine]}])
        self.assertNotIn("Stop", root["hooks"])
        self.assertEqual(sync.plan_hooks("codex", root), ([], [], []))
        for bad in ({"hooks": {"SessionStart": {}}}, {"hooks": []}):
            with self.subTest(bad=bad), self.assertRaises(sync.Unworkable):
                sync.plan_hooks("codex", bad)
        # A kept handler without a type gets one, and that is a change to write;
        # one of another type is not museum's command handler, and is replaced.
        typeless = {
            "hooks": {
                "SessionStart": [{"hooks": [{"command": "museum hook codex"}]}],
                "SessionEnd": [{"hooks": [{"type": "prompt", "command": "museum hook codex"}]}],
            }
        }
        added, fixed, removed = sync.plan_hooks("codex", typeless)
        self.assertEqual(fixed, ["SessionStart: type"])
        self.assertEqual((added, removed), ([], ["SessionEnd: museum hook codex"]))
        # An older install's SessionEnd entry goes: hooks no longer start syncs.
        old = {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "museum hook codex"}]}],
                "SessionEnd": [{"hooks": [{"type": "command", "command": "museum hook codex"}]}],
            }
        }
        self.assertEqual(sync.plan_hooks("codex", old), ([], [], ["SessionEnd: museum hook codex"]))
        self.assertNotIn("SessionEnd", old["hooks"])
        # Older hand-written forms: quoted, or through python3.
        for old in ('"$HOME/m/bin/museum" hook claude', "python3 /x/bin/museum-sync --hook claude"):
            with self.subTest(old=old):
                self.assertTrue(sync.OWNED.match(old))
        # Not museum's: a tool whose name only starts with museum.
        other = {"hooks": {"SessionStart": [{"hooks": [{"command": "museum-pull dev0"}]}]}}
        sync.plan_hooks("claude", other)
        self.assertEqual(
            other["hooks"]["SessionStart"][0], {"hooks": [{"command": "museum-pull dev0"}]}
        )

    def test_npm_install_is_a_copy_of_the_package_not_any_node_modules_path(self) -> None:
        cases = {
            "/u/.local/lib/node_modules/@mu-crew/museum": True,
            "/u/.pi/agent/npm/node_modules/@mu-crew/museum": True,
            "/u/src/node_modules/museum": False,
            "/u/hacking/crew/museum": False,
        }
        for root, npm in cases.items():
            with self.subTest(root=root), mock.patch.object(sync, "ROOT", Path(root)):
                self.assertEqual(sync.npm_install(), npm)

    def test_package_holds_what_museum_runs(self) -> None:
        """npm publishes only package.json's `files`: each must exist, the
        scripts must stay executable, and the version needs a CHANGELOG
        section, which the release workflow publishes as its notes."""
        package = json.loads((ROOT / "package.json").read_text())
        for entry in package["files"]:
            self.assertTrue((ROOT / entry).exists(), entry)
        for script in ("bin/museum", "bin/museum-sync", "install.sh"):
            self.assertIn(script, package["files"])
            self.assertTrue(os.access(ROOT / script, os.X_OK), script)
        search_script = "skills/museum/scripts/museum-search"
        self.assertIn(search_script, package["files"])
        self.assertEqual(sync.SEARCH, ROOT / search_script)
        r = subprocess.run(
            [sys.executable, "scripts/changelog-section.py", "CHANGELOG.md", package["version"]],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

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
