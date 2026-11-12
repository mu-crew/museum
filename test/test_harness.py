"""claude and codex backups: the filter, `museum hook`, `museum tick` and the timer."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from helpers import BIN
from test_sync import RSYNC, Sandbox, sync

CAP = sync.CAP
SID = "0b6c2a40-1111-4222-8333-444455556666"


def jsonl(*records: dict[str, Any]) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


CLAUDE_SESSION = [
    {
        "type": "user",
        "sessionId": SID,
        "cwd": "/work/proj",
        "message": {"role": "user", "content": "hi"},
    },
    {"type": "api-request", "body": "request copy"},
    {"type": "api-request-blob", "data": "blob"},
    {
        "type": "api-request-shape",
        "shapeHash": "h1",
        "shape": {"system": [{"type": "text", "text": "the whole system prompt"}]},
    },
    {"type": "file-history-snapshot", "snapshot": {"files": {}}},
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * (2 * CAP)}],
        },
        "toolUseResult": {"stdout": "y" * (2 * CAP), "stderr": ""},
    },
    {
        "type": "assistant",
        "message": {
            "id": "m1",
            "role": "assistant",
            "content": "file-history-snapshot is fine to say",
        },
    },
]
CODEX_SESSION = [
    {"type": "session_meta", "payload": {"id": "c0de", "cwd": "/work/proj"}},
    {
        "type": "response_item",
        "payload": {"type": "reasoning", "summary": ["thinking"], "encrypted_content": "gAAAA"},
    },
    {
        "type": "compacted",
        "payload": {
            "message": "summary",
            "replacement_history": [{"a": 1}],
            "guardian_history": [{"content": [{"image_url": "data:image/png;base64,AAAA"}]}],
        },
    },
    {
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "c1", "output": "z" * (2 * CAP)},
    },
]
ROLLOUT = "rollout-2026-11-05T10-00-00-c0de.jsonl"


CURSOR_SESSION = [
    {
        "role": "user",
        "message": {"content": [{"type": "text", "text": "hi from cursor"}]},
    },
    {
        "role": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "running"},
                {
                    "type": "tool_use",
                    "name": "Shell",
                    "input": {"command": "x" * (2 * CAP)},
                },
            ]
        },
    },
    {"type": "turn_ended", "status": "success"},
]


class Harness(Sandbox):
    """A sandbox with a claude, codex and cursor-agent session beside the pi one."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.dir / "home"
        self.home.mkdir()
        self.projects = self.dir / "claude" / "projects" / "-work-proj"
        self.claude_file = self.projects / f"{SID}.jsonl"
        self.subagent = self.projects / SID / "subagents" / "agent-a1.jsonl"
        self.codex_file = self.dir / "codex" / "sessions" / "2026" / "11" / "05" / ROLLOUT
        self.cursor_file = (
            self.dir
            / "cursor"
            / "projects"
            / "tmp-work-proj"
            / "agent-transcripts"
            / SID
            / f"{SID}.jsonl"
        )
        for path, body in (
            (self.claude_file, jsonl(*CLAUDE_SESSION)),
            (self.subagent, jsonl({"type": "user", "message": {"role": "user", "content": "sub"}})),
            (self.codex_file, jsonl(*CODEX_SESSION)),
            (self.cursor_file, jsonl(*CURSOR_SESSION)),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        # Noise under projects/ that must not be staged.
        noise = self.dir / "cursor" / "projects" / "tmp-work-proj" / "other.jsonl"
        noise.write_text('{"role":"user","message":{"content":"skip me"}}\n')
        self.box = self.store / "box"

    def env(self, **extra: str) -> dict[str, str]:
        # This suite may itself run under a mu agent, whose identity must not
        # leak into the meta records under test, and under a git hook, whose
        # GIT_DIR and GIT_INDEX_FILE would point git at the repo committing.
        env = super().env(HOME=str(self.home), **extra)
        leaks = ("MU_", "GIT_")
        return {k: v for k, v in env.items() if not k.startswith(leaks) or k in extra}

    def age_sources(self, seconds: float = 700) -> None:
        """Every session file and meta record, `seconds` old."""
        old = time.time() - seconds
        for root in (self.src, self.dir / "claude", self.dir / "codex", self.dir / "cursor"):
            for f in root.rglob("*"):
                os.utime(f, (old, old))
        for f in (self.state / "meta").rglob("*") if (self.state / "meta").is_dir() else ():
            os.utime(f, (old, old))

    def records(self, path: Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in path.read_text().splitlines()]

    def hook(self, harness: str, payload: object, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(BIN / "museum"), "hook", harness],
            input=payload if isinstance(payload, str) else json.dumps(payload),
            capture_output=True,
            text=True,
            env={**self.env(**env), "MUSEUM_SYNC": str(BIN / "museum-sync")},
            check=False,
            timeout=30,
        )

    def wait_for(self, check: Any, what: str) -> None:
        deadline = time.monotonic() + 15
        while not check():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.05)


@unittest.skipUnless(RSYNC, "rsync 3 not installed")
class Store(Harness):
    def test_claude_and_codex_sessions_reach_the_store_filtered(self) -> None:
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        claude = self.box / "claude" / "-work-proj" / f"{SID}.jsonl"
        codex = self.box / "codex" / "-work-proj" / ROLLOUT
        cursor = self.box / "cursor-agent" / "tmp-work-proj" / f"{SID}.jsonl"
        self.assertTrue(
            (self.box / "claude" / "-work-proj" / SID / "subagents" / "agent-a1.jsonl").exists()
        )
        self.assertTrue((self.box / "pi" / "--proj--" / "s1.jsonl").exists())
        self.assertTrue(cursor.exists())
        self.assertFalse((self.box / "cursor-agent" / "tmp-work-proj" / "other.jsonl").exists())

        kept = self.records(claude)
        self.assertEqual([r["type"] for r in kept], ["user", "user", "assistant"])
        self.assertNotIn("api-request", claude.read_text())
        self.assertNotIn("system prompt", claude.read_text())
        tool = kept[1]
        self.assertIn(f"[museum: cut at {CAP} of", tool["toolUseResult"])
        self.assertLess(len(tool["toolUseResult"].encode()), CAP + 100)
        self.assertLess(len(tool["message"]["content"][0]["content"].encode()), CAP + 100)
        # Lines the filter does not change are copied byte for byte.
        source = self.claude_file.read_text().splitlines()
        self.assertEqual(claude.read_text().splitlines()[2], source[6])

        rollout = codex.read_text()
        self.assertNotIn("encrypted_content", rollout)
        self.assertNotIn("replacement_history", rollout)
        self.assertNotIn("guardian_history", rollout)
        payloads = [r["payload"] for r in self.records(codex)]
        self.assertEqual(payloads[1]["summary"], ["thinking"])
        self.assertEqual(payloads[2]["message"], "summary")
        self.assertLess(len(payloads[3]["output"].encode()), CAP + 100)

        cursor_kept = self.records(cursor)
        self.assertEqual([r["role"] for r in cursor_kept], ["user", "assistant"])
        self.assertNotIn("turn_ended", cursor.read_text())
        cut = cursor_kept[1]["message"]["content"][1]["input"]
        self.assertIsInstance(cut, str)
        self.assertIn(f"[museum: cut at {CAP} of", cut)
        self.assertLess(len(cut.encode()), CAP + 100)

    def test_a_growing_session_is_appended_and_a_partial_line_waits(self) -> None:
        self.run_sync()
        stored = self.box / "claude" / "-work-proj" / f"{SID}.jsonl"
        before = stored.read_text()
        with self.claude_file.open("a") as f:
            f.write('{"type":"assistant","message":{"role":"assistant","content":"par')
        self.assertEqual(self.run_sync().returncode, 0)
        self.assertEqual(stored.read_text(), before)
        with self.claude_file.open("a") as f:
            f.write('t two"}}\n')
        self.assertEqual(self.run_sync().returncode, 0)
        after = stored.read_text()
        self.assertTrue(after.startswith(before))
        self.assertEqual(json.loads(after.splitlines()[-1])["message"]["content"], "part two")

    def test_a_session_claude_deleted_stays_in_the_store(self) -> None:
        self.run_sync()
        self.claude_file.unlink()
        self.assertEqual(self.run_sync().returncode, 0)
        self.assertTrue((self.box / "claude" / "-work-proj" / f"{SID}.jsonl").exists())

    def test_a_claude_only_machine_syncs_without_pi_sessions(self) -> None:
        r = self.run_sync(PI_SESSIONS_DIR=str(self.dir / "no-pi"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state_field("status"), "ok")
        self.assertEqual(
            sorted(p.name for p in self.box.iterdir()),
            [".museum", "claude", "codex", "cursor-agent"],
        )

    def test_meta_record_reaches_the_store(self) -> None:
        start = {"hook_event_name": "SessionStart", "session_id": SID, "cwd": str(self.dir)}
        self.assertEqual(self.hook("claude", start, MU_AGENT_NAME="worker-2").returncode, 0)
        self.assertEqual(self.run_sync().returncode, 0)
        record = json.loads((self.box / "claude" / "meta" / f"{SID}.json").read_text())
        self.assertEqual((record["harness"], record["agent"]), ("claude", "worker-2"))

    def tick(self, **env: str) -> None:
        r = subprocess.run(
            [str(BIN / "museum"), "tick"],
            capture_output=True,
            text=True,
            env=self.env(MUSEUM_INTERVAL="0", **env),
            check=True,
            timeout=30,
        )
        self.assertEqual(r.stdout, "")

    def test_a_tick_syncs_what_changed_since_the_last_good_sync(self) -> None:
        """What the 5-minute timer does: past the interval, a tick syncs when
        a session file changed since the last sync that worked."""
        self.run_sync()
        stored = self.box / "claude" / "-work-proj" / f"{SID}.jsonl"
        final = {"type": "assistant", "message": {"role": "assistant", "content": "final turn"}}
        with self.claude_file.open("a") as f:
            f.write(json.dumps(final) + "\n")
        self.tick()
        self.wait_for(lambda: "final turn" in stored.read_text(), "final turn not synced")
        # The detached sync still writes state.json after the copy: let it
        # finish before the sandbox is removed.
        self.wait_for(self.sync_idle, "sync still running")
        self.assertEqual(self.state_field("status"), "ok")

    def test_a_tick_with_nothing_changed_starts_no_sync(self) -> None:
        # last-ok is a second early, so a file written in the sync's own
        # second counts as changed: the fixtures are older than that.
        self.age_sources()
        self.run_sync()
        before = (self.state / "last-start").read_text()
        self.tick()
        time.sleep(0.5)
        self.assertEqual((self.state / "last-start").read_text(), before)

    def test_a_tick_sees_changes_in_a_linked_sessions_folder(self) -> None:
        link = self.dir / "sessions-link"
        link.symlink_to(self.src)
        self.age_sources()
        self.run_sync(PI_SESSIONS_DIR=str(link))
        (self.src / "--proj--" / "s8.jsonl").write_text("{}\n")
        self.tick(PI_SESSIONS_DIR=str(link))
        target = self.box / "pi" / "--proj--" / "s8.jsonl"
        self.wait_for(target.exists, "change in a linked folder not synced")
        self.wait_for(self.sync_idle, "sync still running")

    def test_a_failed_sync_leaves_its_changes_for_the_next_tick(self) -> None:
        self.run_sync()
        ok = (self.state / "last-ok").stat().st_mtime
        last_ok = self.state_field("last_ok")
        # An rsync 3 that fails, as a dropped connection would.
        broken = self.dir / "broken-rsync"
        broken.write_text(
            '#!/bin/sh\n[ "$1" = --version ] && echo "rsync  version 3.4.1" && exit 0\n'
            'echo "rsync: connection unexpectedly closed" >&2\nexit 12\n'
        )
        broken.chmod(0o755)
        good = self.config.read_text()
        self.write_config(f'rsync_path = "{broken}"\n' + good)
        (self.src / "--proj--" / "s9.jsonl").write_text("{}\n")
        self.assertEqual(self.run_sync().returncode, 1)
        # Neither the tick's mark nor state.json's last_ok moves on a failure.
        self.assertEqual((self.state / "last-ok").stat().st_mtime, ok)
        self.assertEqual(self.state_field("last_ok"), last_ok)
        self.assertEqual(self.state_field("status"), "error")
        self.write_config(good)
        self.tick()
        target = self.box / "pi" / "--proj--" / "s9.jsonl"
        self.wait_for(target.exists, "change missed by the failed sync not synced")
        self.wait_for(self.sync_idle, "sync still running")

    def sync_idle(self) -> bool:
        fd = os.open(self.state / "lock", os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        finally:
            os.close(fd)
        return True

    def test_a_filter_crash_still_backs_up_pi(self) -> None:
        s = sync.Settings(
            sync.load_config(self.config),
            self.src,
            self.state,
            600,
            0,
            sync.harness_sources(self.env()),
        )
        self.state.mkdir(exist_ok=True)
        err = io.StringIO()
        with (
            mock.patch.object(sync, "stage", side_effect=RuntimeError("boom")),
            contextlib.redirect_stderr(err),
        ):
            self.assertTrue(sync.sync_once(s, RSYNC))
        self.assertIn("RuntimeError: boom", err.getvalue())
        self.assertTrue((self.box / "pi" / "--proj--" / "s1.jsonl").exists())
        self.assertEqual(self.state_field("status"), "ok")


class OneRsync(Harness):
    """Every harness rides one rsync: pi's sessions through the pi link, the
    rest from the stage folder, each landing in <machine>/<harness>/."""

    def command(self, stage: bool) -> list[str]:
        s = sync.Settings(
            sync.load_config(self.config),
            self.src,
            self.state,
            600,
            0,
            sync.harness_sources(self.env()),
        )
        self.state.mkdir(exist_ok=True)
        if stage:
            sync.stage(s.state, s.sources, s.state)
        with mock.patch.object(sync, "run", return_value=sync.Result(0, "")) as run:
            sync.copy(s, "rsync")
        return run.call_args.args[0]

    def test_pi_alone(self) -> None:
        cmd = self.command(stage=False)
        self.assertEqual(cmd[:3], ["rsync", "-a", "--relative"])
        self.assertEqual(cmd[-2:], [f"{self.state}/pi-link/./pi/", f"{self.store}/box/"])
        self.assertEqual((self.state / "pi-link" / "pi").readlink(), self.src)
        self.assertFalse((self.state / "stage").exists())

    def test_staged_copies_ride_the_same_rsync_as_pi(self) -> None:
        cmd = self.command(stage=True)
        self.assertEqual(
            cmd[-3:],
            [f"{self.state}/pi-link/./pi/", f"{self.state}/stage/./", f"{self.store}/box/"],
        )
        self.assertIn("--include=/*/meta/*.json", cmd)

    def test_an_ssh_store_migrates_in_rsyncs_own_connection(self) -> None:
        self.write_config('machine = "box"\n[store]\nhost = "devbox"\npath = "/data/m"\n')
        cmd = self.command(stage=False)
        remote = next(a for a in cmd if a.startswith("--rsync-path="))
        self.assertTrue(remote.startswith("--rsync-path=mkdir -p /data/m/box && sh -c "))
        self.assertTrue(remote.endswith(" migrate /data/m/box && rsync"))
        self.assertEqual(cmd[-1], "devbox:/data/m/box/")

    def test_every_login_shell_runs_the_remote_migration(self) -> None:
        """The store host's login shell parses --rsync-path first; csh and
        tcsh reject a newline inside quotes and expand `!x` inside them."""
        store = self.dir / "st ore"
        self.write_config(f'machine = "box"\n[store]\nhost = "devbox"\npath = "{store}"\n')
        remote = next(a for a in self.command(stage=False) if a.startswith("--rsync-path="))
        line = remote.removeprefix("--rsync-path=").removesuffix("rsync") + "echo synced"
        shells = [sh for sh in ("sh", "dash", "bash", "zsh", "csh", "tcsh") if shutil.which(sh)]
        for sh in shells:
            with self.subTest(sh):
                old = store / "box" / "--proj--" / f"{sh}.jsonl"
                old.parent.mkdir(parents=True, exist_ok=True)
                old.write_text("{}\n")
                r = subprocess.run(
                    [sh, "-c", line], capture_output=True, text=True, timeout=30, check=False
                )
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "synced\n", ""))
                self.assertTrue((store / "box" / "pi" / "--proj--" / f"{sh}.jsonl").exists())
                self.assertFalse(old.parent.exists())


@unittest.skipUnless(RSYNC, "rsync 3 not installed")
class Migrate(Harness):
    """A machine's first sync since 0.6 moves pi's old top-level folders into pi/."""

    def old(self, rel: str, body: str) -> Path:
        path = self.box / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def test_old_layout_moves_into_pi(self) -> None:
        self.old("--proj--/s1.jsonl", '{"a":1}\n')
        self.old("--gone--/s0.jsonl", "{}\n")  # deleted locally: kept, and moved
        self.old("2026-04-17T07-33-11-729Z_x.jsonl", "{}\n")
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            sorted(p.name for p in self.box.iterdir()),
            [".museum", "claude", "codex", "cursor-agent", "pi"],
        )
        pi = self.box / "pi"
        self.assertTrue((pi / "--gone--" / "s0.jsonl").exists())
        self.assertTrue((pi / "2026-04-17T07-33-11-729Z_x.jsonl").exists())
        self.assertEqual((pi / "--proj--" / "s1.jsonl").read_text(), '{"a":1}\n')
        # Nothing to move the second time.
        self.assertEqual(self.run_sync().returncode, 0)

    def test_a_session_in_both_places_keeps_the_longer_copy(self) -> None:
        """An older museum on this machine grew the old copy after a new one
        migrated: the longer one is kept, the prefix goes."""
        (self.src / "--proj--" / "s1.jsonl").write_text('{"a":1}\n{"b":2}\n{"c":3}\n')
        self.old("pi/--proj--/s1.jsonl", '{"a":1}\n')
        self.old("--proj--/s1.jsonl", '{"a":1}\n{"b":2}\n')
        self.old("pi/--proj--/s2.jsonl", '{"z":1}\n{"y":2}\n')
        self.old("--proj--/s2.jsonl", '{"z":1}\n')
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.box / "--proj--").exists())
        pi = self.box / "pi" / "--proj--"
        self.assertEqual((pi / "s1.jsonl").read_text(), '{"a":1}\n{"b":2}\n{"c":3}\n')
        self.assertEqual((pi / "s2.jsonl").read_text(), '{"z":1}\n{"y":2}\n')

    def test_copies_that_differ_are_both_kept(self) -> None:
        self.old("pi/--proj--/s5.jsonl", '{"new":1}\n')
        self.old("--proj--/s5.jsonl", '{"old":1}\n')
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("left ./--proj--/s5.jsonl", r.stderr)
        self.assertTrue((self.box / "--proj--" / "s5.jsonl").exists())
        self.assertEqual((self.box / "pi" / "--proj--" / "s5.jsonl").read_text(), '{"new":1}\n')


class Filter(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.dir)], check=False))
        self.root = self.dir / "projects"
        self.state = self.dir / "state"
        self.src = self.root / "-p" / "s.jsonl"
        self.src.parent.mkdir(parents=True)
        self.sources = (sync.Source("claude", self.root),)
        self.staged = self.state / "stage" / "claude" / "-p" / "s.jsonl"

    def test_filter_line(self) -> None:
        drop = b'{"type":"api-request","x":1}\n'
        self.assertIsNone(sync.filter_line("claude", drop))
        same = b'{"type":"user", "message": {"content": "api-request"}}\n'
        self.assertEqual(sync.filter_line("claude", same), same)
        self.assertEqual(sync.filter_line("claude", b"not json\n"), b"not json\n")
        big = json.dumps({"toolUseResult": "a" * (CAP + 1)}).encode() + b"\n"
        cut = json.loads(sync.filter_line("claude", big) or b"")
        self.assertTrue(cut["toolUseResult"].startswith("a" * CAP + "\n[museum: cut at"))
        nested = b'{"payload":{"content":[{"type":"reasoning","encrypted_content":"e"}]}}\n'
        self.assertEqual(
            sync.filter_line("codex", nested), b'{"payload":{"content":[{"type":"reasoning"}]}}\n'
        )
        self.assertIsNone(sync.filter_line("cursor-agent", b'{"role":"turn_ended"}\n'))
        self.assertIsNone(
            sync.filter_line("cursor-agent", b'{"type":"turn_ended","status":"success"}\n')
        )
        fat = {
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Shell", "input": {"command": "z" * (CAP + 1)}}
                ]
            },
        }
        cut_c = json.loads(
            sync.filter_line("cursor-agent", json.dumps(fat).encode() + b"\n") or b""
        )
        self.assertIn("[museum: cut at", cut_c["message"]["content"][0]["input"])

    def test_cursor_agent_destination(self) -> None:
        root = self.dir / "cursor" / "projects"
        source = sync.Source("cursor-agent", root)
        good = root / "proj" / "agent-transcripts" / SID / f"{SID}.jsonl"
        self.assertEqual(sync.destination(source, good), f"cursor-agent/proj/{SID}.jsonl")
        self.assertIsNone(sync.destination(source, root / "proj" / "other.jsonl"))
        self.assertIsNone(
            sync.destination(source, root / "proj" / "agent-transcripts" / SID / "other.jsonl")
        )

    def test_every_api_request_record_type_is_dropped(self) -> None:
        for kind in (
            "api-request",
            "api-request-blob",
            "api-request-shape",
            "file-history-snapshot",
        ):
            with self.subTest(kind):
                line = json.dumps({"type": kind, "shape": {"system": "x"}}).encode() + b"\n"
                self.assertIsNone(sync.filter_line("claude", line))

    def test_odd_records_do_not_break_the_filter(self) -> None:
        big = "a" * (CAP + 1)
        odd = json.dumps({"type": ["x"], "toolUseResult": big}).encode() + b"\n"
        self.assertIn(b"[museum: cut at", sync.filter_line("claude", odd) or b"")
        deep = b"[" * 100_000 + b"]" * 100_000 + b"\n"
        self.assertEqual(sync.filter_line("claude", deep), deep)

    def test_lone_surrogate_over_the_cap_is_filtered_and_the_rest_staged(self) -> None:
        """JS writes a string cut inside a surrogate pair as a lone \\ud83d."""
        line = (
            '{"type":"user","message":{"content":"x\\ud83d"},'
            f'"toolUseResult":"{"a" * (CAP + 10)}\\ud83d"}}\n'
        )
        (self.root / "-p" / "a.jsonl").write_text(line)
        self.src.write_text('{"n":1}\n')
        sync.stage(self.state, self.sources, self.state)
        kept = json.loads((self.staged.parent / "a.jsonl").read_text())
        self.assertEqual(kept["message"]["content"], "x\ud83d")
        self.assertIn("[museum: cut at", kept["toolUseResult"])
        self.assertEqual(self.staged.read_text(), '{"n":1}\n')
        self.assertEqual(len(json.loads((self.state / "stage.json").read_text())), 2)

    def test_a_file_the_filter_fails_on_is_skipped_and_the_rest_staged(self) -> None:
        bad = self.root / "-p" / "a.jsonl"
        bad.write_text('{"boom":1}\n')
        self.src.write_text('{"n":1}\n')
        real = sync.filter_line

        def filter_line(harness: str, line: bytes) -> bytes | None:
            if b"boom" in line:
                raise RuntimeError("boom")
            return real(harness, line)

        err = io.StringIO()
        with mock.patch.object(sync, "filter_line", filter_line), contextlib.redirect_stderr(err):
            sync.stage(self.state, self.sources, self.state)
        self.assertIn(f"skipped {bad}: RuntimeError: boom", err.getvalue())
        self.assertEqual(self.staged.read_text(), '{"n":1}\n')
        self.assertEqual(list(json.loads((self.state / "stage.json").read_text())), [str(self.src)])

    def test_cap_keeps_short_values_as_they_are(self) -> None:
        value = {"stdout": "ok"}
        self.assertIs(sync.cap(value), value)
        self.assertLessEqual(len(str(sync.cap("é" * CAP)).encode()), CAP + 60)

    def test_codex_project_comes_from_session_meta(self) -> None:
        rollout = self.dir / "r.jsonl"
        rollout.write_text('{"type":"session_meta","payload":{"cwd":"/work/a.b"}}\n')
        self.assertEqual(sync.codex_project(rollout), "-work-a-b")
        rollout.write_text('{"type":"session_meta"}\n')
        self.assertEqual(sync.codex_project(rollout), "-unknown")
        rollout.write_text('{"type":"session_meta","payload":{"cwd":"/wo')
        self.assertIsNone(sync.codex_project(rollout))

    def test_shrunk_source_is_filtered_again_from_scratch(self) -> None:
        self.src.write_text('{"n":1}\n{"n":2}\n{"n":3}\n')
        sync.stage(self.state, self.sources, self.state)
        self.src.write_text('{"m":1}\n')
        sync.stage(self.state, self.sources, self.state)
        self.assertEqual(self.staged.read_text(), '{"m":1}\n')

    def test_copy_longer_than_its_cursor_is_cut_back(self) -> None:
        """A crash between writing the copy and saving the cursor."""
        self.src.write_text('{"n":1}\n')
        sync.stage(self.state, self.sources, self.state)
        with self.staged.open("a") as f:
            f.write('{"n":1}\n')
        with self.src.open("a") as f:
            f.write('{"n":2}\n')
        sync.stage(self.state, self.sources, self.state)
        self.assertEqual(self.staged.read_text(), '{"n":1}\n{"n":2}\n')

    def test_unchanged_sources_are_not_read(self) -> None:
        self.src.write_text('{"n":1}\n')
        sync.stage(self.state, self.sources, self.state)
        self.src.chmod(0)  # stat'ed, never opened
        self.addCleanup(self.src.chmod, 0o600)
        sync.stage(self.state, self.sources, self.state)
        self.assertEqual(self.staged.read_text(), '{"n":1}\n')


class Hook(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.dir / "proj"
        self.repo.mkdir()
        git = ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@localhost"]
        for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run([*git, *args], check=True, capture_output=True, env=self.env())
        self.anchors = self.home / ".local/state/mu-next/anchors"

    def start(self, harness: str = "claude", **env: str) -> subprocess.CompletedProcess[str]:
        payload = {
            "hook_event_name": "SessionStart",
            "session_id": SID,
            "cwd": str(self.repo),
            "source": "startup",
            "transcript_path": str(self.claude_file),
        }
        return self.hook(harness, payload, **env)

    def meta(self, harness: str = "claude") -> dict[str, Any]:
        return json.loads((self.state / "meta" / harness / f"{SID}.json").read_text())

    def anchor(self, **fields: object) -> None:
        """An anchor for this test process, the hook's parent."""
        self.anchors.mkdir(parents=True, exist_ok=True)
        doc = {"start_time": sync.started(os.getpid()), **fields}
        (self.anchors / f"{os.getpid()}.json").write_text(json.dumps(doc))

    def test_session_start_records_git_state_and_env_identity(self) -> None:
        r = self.start(MU_AGENT_NAME="worker-1", MU_WORKSTREAM="ws")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        m = self.meta()
        self.assertEqual(m["session_id"], SID)
        self.assertEqual(m["harness"], "claude")
        self.assertEqual(m["project"], "proj")
        self.assertEqual(m["branch"], "main")
        self.assertEqual(Path(m["repo"]).resolve(), self.repo.resolve())
        self.assertEqual(len(m["commit"]), 40)
        self.assertEqual((m["agent"], m["workstream"]), ("worker-1", "ws"))
        self.assertNotIn("role", m)

    def test_anchor_wins_over_env(self) -> None:
        self.anchor(agent="lead", workstream="ws2", cli="claude", orchestrator=True)
        self.start(MU_AGENT_NAME="worker-1", MU_WORKSTREAM="ws")
        m = self.meta()
        self.assertEqual((m["agent"], m["workstream"], m["role"]), ("lead", "ws2", "orchestrator"))

    def test_anchor_role_wins_over_its_orchestrator_flag(self) -> None:
        self.anchor(agent_name="w1", cli="claude", role="worker", orchestrator=True)
        self.start()
        m = self.meta()
        self.assertEqual((m["agent"], m["role"]), ("w1", "worker"))

    def test_env_role(self) -> None:
        cases = (
            ({"MU_MANAGED_AGENT": "1"}, "worker"),
            ({"MU_MANAGED_AGENT": "1", "MU_ORCHESTRATOR": "1"}, "orchestrator"),
            ({"MU_MANAGED_AGENT": "1", "MU_ORCHESTRATOR": "0"}, "worker"),
            ({}, None),
            ({"MU_ORCHESTRATOR": "1"}, "orchestrator"),
        )
        for env, want in cases:
            with self.subTest(env=env):
                (self.state / "meta" / "claude" / f"{SID}.json").unlink(missing_ok=True)
                self.start(MU_AGENT_NAME="a", **env)
                self.assertEqual(self.meta().get("role"), want)

    def test_anchor_from_a_reused_pid_is_ignored(self) -> None:
        self.anchor(agent="lead", cli="claude", start_time=12345)
        self.start(MU_AGENT_NAME="worker-1")
        self.assertEqual(self.meta()["agent"], "worker-1")

    def test_session_nested_under_another_harness_has_no_identity(self) -> None:
        self.anchor(agent="pi-worker", workstream="ws2", cli="pi")
        self.start(MU_AGENT_NAME="pi-worker")
        self.assertNotIn("agent", self.meta())

    def start_in_chain(self, *names: str, **env: str) -> None:
        """A SessionStart hook under processes whose argv0 is NAMES, the
        outermost first, all below this test process."""
        scripts = self.dir / "chain"
        scripts.mkdir(exist_ok=True)
        last = f"{shlex.quote(str(BIN / 'museum'))} hook claude; true\n"
        for i, _ in enumerate(names):
            nxt = scripts / f"{i + 1}.sh"
            body = (
                last
                if i == len(names) - 1
                else f"(exec -a {names[i + 1]} bash {shlex.quote(str(nxt))}); true\n"
            )
            (scripts / f"{i}.sh").write_text(body)
        payload = {"hook_event_name": "SessionStart", "session_id": SID, "cwd": str(self.repo)}
        subprocess.run(
            [names[0], str(scripts / "0.sh")],
            executable="/bin/bash",
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={**self.env(**env), "MUSEUM_SYNC": str(BIN / "museum-sync")},
            check=True,
            timeout=30,
        )

    def test_launcher_and_binary_are_one_harness(self) -> None:
        self.anchor(agent="lead", cli="claude")
        self.start_in_chain("claude", "claude.real")
        self.assertEqual(self.meta()["agent"], "lead")

    def test_session_nested_under_the_same_harness_has_no_identity(self) -> None:
        """A `claude -p` checker that a claude agent started, behind a shell."""
        self.anchor(agent="lead", cli="claude")
        self.start_in_chain("claude", "bash", "claude", MU_AGENT_NAME="lead")
        self.assertNotIn("agent", self.meta())

    def test_resumed_session_keeps_its_first_record(self) -> None:
        self.start(MU_AGENT_NAME="first")
        self.start(MU_AGENT_NAME="second")
        self.assertEqual(self.meta()["agent"], "first")

    def test_session_end_does_nothing(self) -> None:
        """An older install's SessionEnd entry: hooks no longer start syncs."""
        r = self.hook(
            "codex", {"hook_event_name": "SessionEnd", "session_id": SID, "transcript_path": "/t"}
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertFalse(self.state.exists())

    def test_pi_extension_payload_writes_pis_record(self) -> None:
        payload = {"hook_event_name": "SessionStart", "session_id": SID, "cwd": str(self.repo)}
        r = self.hook("pi", payload, MU_AGENT_NAME="w1", MU_WORKSTREAM="ws")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        record = json.loads((self.state / "meta" / "pi" / f"{SID}.json").read_text())
        self.assertEqual(
            (record["harness"], record["agent"], record["workstream"], record["branch"]),
            ("pi", "w1", "ws", "main"),
        )
        self.assertEqual(record["project"], "proj")

    def test_codex_title_session_is_dropped(self) -> None:
        for event in ("SessionStart", "SessionEnd"):
            payload = {"hook_event_name": event, "session_id": SID, "transcript_path": None}
            self.assertEqual(self.hook("codex", payload).returncode, 0)
        self.assertFalse(self.state.exists())

    def test_bad_input_never_fails_the_hook(self) -> None:
        cases = {
            "not json": ("claude", "{"),
            "unsafe id": ("claude", {"hook_event_name": "SessionStart", "session_id": "../x"}),
            "no backup": ("not-a-harness", {"hook_event_name": "sessionEnd"}),
        }
        for name, (harness, payload) in cases.items():
            with self.subTest(name):
                r = self.hook(harness, payload)
                self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertFalse((self.state / "meta").exists())

    def test_cursor_agent_meta_uses_workspace_roots(self) -> None:
        root = str(self.repo)
        payload = {
            "hook_event_name": "sessionStart",
            "session_id": SID,
            "workspace_roots": [root],
        }
        r = self.hook("cursor-agent", payload, MU_AGENT_NAME="c1")
        self.assertEqual((r.returncode, r.stdout), (0, "{}\n"))
        record = json.loads((self.state / "meta" / "cursor-agent" / f"{SID}.json").read_text())
        self.assertEqual(record["cwd"], root)
        self.assertEqual(record["agent"], "c1")
        end = self.hook("cursor-agent", {"hook_event_name": "sessionEnd"})
        self.assertEqual((end.returncode, end.stdout), (0, "{}\n"))


class Launcher(unittest.TestCase):
    """bin/museum names the commands and runs museum-sync's."""

    def museum(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(BIN / "museum"), *args],
            capture_output=True,
            text=True,
            env={**os.environ, **env},
            check=False,
            timeout=30,
        )

    def test_each_command_runs_museum_syncs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "log"
            fake = Path(d) / "fake-sync"
            fake.write_text('#!/bin/sh\necho "$*" >> "$FAKE_LOG"\n')
            fake.chmod(0o755)
            for args, want in [
                (["tick"], "tick"),
                (["hook", "pi"], "--hook pi"),
                (["sync", "--now"], "--now"),
                (["sync"], ""),
                (["init", "/s", "box"], "init /s box"),
                (["setup"], "init"),
                (["status"], "status"),
                (["doctor"], "doctor"),
                (["version"], "--version"),
                (["timer", "install"], "timer install"),
                (["timer", "remove"], "timer remove"),
            ]:
                with self.subTest(args):
                    log.unlink(missing_ok=True)
                    r = self.museum(*args, MUSEUM_SYNC=str(fake), FAKE_LOG=str(log))
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertEqual(log.read_text(), want + "\n")

    def test_anything_else_prints_usage(self) -> None:
        for args in ([], ["bogus"], ["hook"], ["timer"], ["timer", "start"], ["status", "x"]):
            with self.subTest(args):
                r = self.museum(*args, MUSEUM_SYNC="/nonexistent")
                self.assertEqual(r.returncode, 2)
                self.assertTrue(r.stderr.startswith("museum tick "), r.stderr)

    def test_sync_path_is_the_museum_sync_beside_it(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "MUSEUM_SYNC"}
        r = subprocess.run(
            [str(BIN / "museum"), "sync-path"], capture_output=True, text=True, env=env, check=True
        )
        self.assertEqual(r.stdout, f"{(BIN / 'museum-sync').resolve()}\n")


# Stands in for museum-sync, so tick's gating is checked without a sync.
FAKE_SYNC = '#!/bin/sh\necho "$@" >> "$FAKE_LOG"\n'


class Tick(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.fake = self.dir / "fake-sync"
        self.fake.write_text(FAKE_SYNC)
        self.fake.chmod(0o755)
        self.log = self.dir / "sync.log"
        self.state.mkdir()

    def tick(self) -> None:
        r = subprocess.run(
            # museum-sync itself: through museum, MUSEUM_SYNC would replace it.
            [str(BIN / "museum-sync"), "tick"],
            capture_output=True,
            text=True,
            env=self.env(MUSEUM_SYNC=str(self.fake), FAKE_LOG=str(self.log)),
            check=False,
            timeout=10,
        )
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def synced(self, ago: float) -> None:
        """A sync that worked, `ago` seconds back, as museum-sync records it."""
        when = int(time.time() - ago)
        (self.state / "last-start").write_text(f"{when}\n")
        sync.mark_ok(self.state, when)

    def test_not_due_starts_nothing(self) -> None:
        (self.state / "last-start").write_text(f"{int(time.time())}\n")
        self.tick()
        time.sleep(0.3)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.state / "last-tick").exists())

    def test_due_but_unchanged_starts_nothing(self) -> None:
        self.synced(ago=601)
        self.age_sources()
        self.tick()
        time.sleep(0.3)
        self.assertEqual(self.calls(), [])
        # The check counts as a try: the next find waits for the interval.
        self.assertTrue((self.state / "last-tick").exists())

    def test_a_change_past_the_interval_starts_one_sync(self) -> None:
        self.synced(ago=601)
        (self.src / "--proj--" / "s1.jsonl").write_text('{"a":2}\n')
        self.tick()
        self.wait_for(lambda: self.calls() == ["--if-due"], "no sync started")
        # The sync could not start (it never wrote last-start): no retry yet.
        self.tick()
        time.sleep(0.3)
        self.assertEqual(self.calls(), ["--if-due"])

    def test_no_good_sync_yet_is_a_change(self) -> None:
        (self.state / "last-start").write_text(f"{int(time.time()) - 601}\n")
        self.tick()
        self.wait_for(lambda: self.calls() == ["--if-due"], "no sync started")

    def test_a_meta_record_is_a_change(self) -> None:
        self.synced(ago=601)
        self.age_sources()
        meta = self.state / "meta" / "pi" / f"{SID}.json"
        meta.parent.mkdir(parents=True)
        meta.write_text("{}\n")
        self.tick()
        self.wait_for(lambda: self.calls() == ["--if-due"], "no sync started")


# Stand-ins that log their arguments. `launchctl print` finds no job.
FAKE_CTL = '#!/bin/sh\necho "$(basename "$0") $*" >> "$FAKE_LOG"\n[ "$1" != print ]\n'


class Timer(Harness):
    def run_timer(self, kind: str, action: str, **extra: str) -> list[str]:
        bin_dir = self.dir / "ctl"
        bin_dir.mkdir(exist_ok=True)
        for name in ("launchctl", "systemctl"):
            (bin_dir / name).write_text(FAKE_CTL)
            (bin_dir / name).chmod(0o755)
        log = self.dir / "ctl.log"
        log.unlink(missing_ok=True)
        path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        env = self.env(MUSEUM_TIMER_KIND=kind, FAKE_LOG=str(log), PATH=path, **extra)
        env.pop("XDG_CONFIG_HOME", None)
        r = subprocess.run(
            [str(BIN / "museum"), "timer", action],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=30,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return log.read_text().splitlines()

    def test_launchd(self) -> None:
        calls = self.run_timer("launchd", "install")
        plist = self.home / "Library/LaunchAgents/museum.tick.plist"
        with plist.open("rb") as f:
            job = plistlib.load(f)
        self.assertEqual(job["ProgramArguments"], [str((BIN / "museum").resolve()), "tick"])
        self.assertEqual(job["StartInterval"], 300)
        self.assertTrue(job["AbandonProcessGroup"])
        self.assertIn("PATH", job["EnvironmentVariables"])
        self.assertEqual(job["EnvironmentVariables"]["MUSEUM_STATE_DIR"], str(self.state))
        # The timer's syncs back up the claude and codex sessions this shell's
        # harnesses write, not those under the default folders.
        self.assertEqual(job["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"], str(self.dir / "claude"))
        self.assertEqual(job["EnvironmentVariables"]["CODEX_HOME"], str(self.dir / "codex"))
        self.assertEqual(job["EnvironmentVariables"]["CURSOR_HOME"], str(self.dir / "cursor"))
        self.assertEqual(calls[-1], f"launchctl bootstrap gui/{os.getuid()} {plist}")
        self.run_timer("launchd", "remove")
        self.assertFalse(plist.exists())

    def test_systemd(self) -> None:
        calls = self.run_timer("systemd", "install")
        units = self.home / ".config/systemd/user"
        service = (units / "museum-tick.service").read_text()
        self.assertIn(f'ExecStart="{(BIN / "museum").resolve()}" tick', service)
        self.assertIn("KillMode=process", service)
        self.assertIn(f'Environment="MUSEUM_STATE_DIR={self.state}"', service)
        self.assertIn(f'Environment="CLAUDE_CONFIG_DIR={self.dir / "claude"}"', service)
        self.assertIn(f'Environment="CODEX_HOME={self.dir / "codex"}"', service)
        self.assertIn(f'Environment="CURSOR_HOME={self.dir / "cursor"}"', service)
        self.assertIn("OnUnitActiveSec=300", (units / "museum-tick.timer").read_text())
        self.assertEqual(
            calls,
            ["systemctl --user daemon-reload", "systemctl --user enable --now museum-tick.timer"],
        )
        self.run_timer("systemd", "remove")
        self.assertFalse((units / "museum-tick.timer").exists())

    def test_a_moved_pi_sessions_folder_reaches_the_timer(self) -> None:
        """The timer does every sync, pi's included, so it must look where pi writes."""
        self.run_timer("launchd", "install")
        plist = self.home / "Library/LaunchAgents/museum.tick.plist"
        with plist.open("rb") as f:
            job_env = plistlib.load(f)["EnvironmentVariables"]
        self.assertEqual(job_env["PI_SESSIONS_DIR"], str(self.src))
        self.run_timer("systemd", "install")
        service = (self.home / ".config/systemd/user/museum-tick.service").read_text()
        self.assertIn(f'Environment="PI_SESSIONS_DIR={self.src}"', service)

    def test_empty_harness_homes_are_not_forwarded(self) -> None:
        """An empty CLAUDE_CONFIG_DIR, CODEX_HOME or CURSOR_HOME means the
        default folder to museum-sync; the timer must not pin it as empty."""
        self.run_timer("launchd", "install", CLAUDE_CONFIG_DIR="", CODEX_HOME="", CURSOR_HOME="")
        plist = self.home / "Library/LaunchAgents/museum.tick.plist"
        with plist.open("rb") as f:
            job_env = plistlib.load(f)["EnvironmentVariables"]
        self.assertNotIn("CLAUDE_CONFIG_DIR", job_env)
        self.assertNotIn("CODEX_HOME", job_env)
        self.assertNotIn("CURSOR_HOME", job_env)
        self.run_timer("systemd", "install", CLAUDE_CONFIG_DIR="", CODEX_HOME="", CURSOR_HOME="")
        service = (self.home / ".config/systemd/user/museum-tick.service").read_text()
        self.assertNotIn("CLAUDE_CONFIG_DIR", service)
        self.assertNotIn("CODEX_HOME", service)
        self.assertNotIn("CURSOR_HOME", service)


@unittest.skipUnless(RSYNC, "rsync 3 not installed")
class RsyncPath(Sandbox):
    def test_configured_rsync_is_used_without_one_on_path(self) -> None:
        empty = self.dir / "empty-path"
        empty.mkdir()
        self.write_config(
            f'machine = "box"\nrsync_path = "{RSYNC}"\n[store]\npath = "{self.store}"\n'
        )
        r = self.run_sync(PATH=str(empty))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.store / "box" / "pi" / "--proj--" / "s1.jsonl").exists())

    def test_configured_rsync_that_is_not_rsync_3_is_an_error(self) -> None:
        self.write_config(
            f'machine = "box"\nrsync_path = "{sys.executable}"\n[store]\npath = "{self.store}"\n'
        )
        self.assertEqual(self.run_sync().returncode, 1)
        self.assertEqual(
            self.state_field("message"),
            f"rsync_path {sys.executable} is not rsync 3.x (macOS: brew install rsync)",
        )

    def test_config_round_trip_and_checks(self) -> None:
        c = sync.Config("m", None, "/srv", False, "/opt/rsync/bin/rsync")
        self.config.write_text(sync.toml(c))
        self.assertEqual(sync.load_config(self.config), c)
        self.config.write_text('machine = "m"\nrsync_path = "rsync"\n[store]\npath = "/a"\n')
        with self.assertRaises(sync.ConfigError) as e:
            sync.load_config(self.config)
        self.assertIn("rsync_path must be absolute", str(e.exception))


if __name__ == "__main__":
    unittest.main()


class Doctor(Harness):
    def doctor(self, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(BIN / "museum"), "doctor"],
            capture_output=True,
            text=True,
            env={
                **self.env(MUSEUM_TIMER_KIND="systemd", **env),
                "MUSEUM_SYNC": str(BIN / "museum-sync"),
            },
            check=False,
            timeout=60,
        )

    def test_names_each_problem_with_its_fix(self) -> None:
        old = self.box / "--proj--" / "s1.jsonl"
        old.parent.mkdir(parents=True)
        old.write_text("{}\n")
        r = self.doctor()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        out = r.stdout
        self.assertIn(f"ok    store: {self.store} reachable", out)
        self.assertIn("warn  store: box's pi sessions are in the old layout", out)
        self.assertIn("FAIL  no timer: not installed\n      fix: museum timer install", out)
        self.assertIn("FAIL  no sync has worked yet", out)

    @unittest.skipUnless(RSYNC, "rsync 3 not installed")
    def test_after_a_sync_the_backup_is_fresh_until_it_is_stale(self) -> None:
        self.assertEqual(self.run_sync().returncode, 0)
        out = self.doctor().stdout
        self.assertIn("ok    last sync ok, 0m ago", out)
        self.assertNotIn("old layout", out)
        # The last good sync was an hour ago, and a session changed just now,
        # as after a break: a working timer has not had its chance yet.
        doc = json.loads((self.state / "state.json").read_text())
        doc["last_ok"] -= 3600
        (self.state / "state.json").write_text(json.dumps(doc))
        fresh = self.src / "--proj--" / "s7.jsonl"
        fresh.write_text("{}\n")
        out = self.doctor().stdout
        self.assertNotIn("stale", out)
        # The change has waited 40 minutes: the timer would have synced it.
        old = time.time() - 2400
        os.utime(fresh, (old, old))
        out = self.doctor().stdout
        self.assertRegex(
            out,
            r"FAIL  backup stale: \d+ session files changed since the last good sync 1h ago, the first 40m ago",
        )

    def test_an_empty_store_has_no_machines(self) -> None:
        out = self.doctor().stdout
        self.assertNotIn("machine *", out)

    def test_is_read_only_even_when_state_json_is_odd(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "state.json").write_text("[]")
        r = self.doctor()
        self.assertEqual((self.state / "state.json").read_text(), "[]")
        self.assertIn("FAIL  no sync has worked yet", r.stdout, r.stderr)

    def test_a_missing_config_says_to_run_init(self) -> None:
        self.config.unlink()
        r = self.doctor()
        self.assertEqual(r.returncode, 1)
        self.assertIn("fix: museum init STORE", r.stdout)
