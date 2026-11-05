"""claude and codex backups: the filter, `museum hook`, `museum tick` and the timer."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import plistlib
import shlex
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


class Harness(Sandbox):
    """A sandbox with a claude and a codex session beside the pi one."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.dir / "home"
        self.home.mkdir()
        self.projects = self.dir / "claude" / "projects" / "-work-proj"
        self.claude_file = self.projects / f"{SID}.jsonl"
        self.subagent = self.projects / SID / "subagents" / "agent-a1.jsonl"
        self.codex_file = self.dir / "codex" / "sessions" / "2026" / "11" / "05" / ROLLOUT
        for path, body in (
            (self.claude_file, jsonl(*CLAUDE_SESSION)),
            (self.subagent, jsonl({"type": "user", "message": {"role": "user", "content": "sub"}})),
            (self.codex_file, jsonl(*CODEX_SESSION)),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        self.box = self.store / "box"

    def env(self, **extra: str) -> dict[str, str]:
        # This suite may itself run under a mu agent, whose identity must not
        # leak into the meta records under test, and under a git hook, whose
        # GIT_DIR and GIT_INDEX_FILE would point git at the repo committing.
        env = super().env(HOME=str(self.home), **extra)
        leaks = ("MU_", "GIT_")
        return {k: v for k, v in env.items() if not k.startswith(leaks) or k in extra}

    def records(self, path: Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in path.read_text().splitlines()]

    def hook(self, harness: str, payload: object, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(BIN / "museum"), "hook", harness],
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
        self.assertTrue(
            (self.box / "claude" / "-work-proj" / SID / "subagents" / "agent-a1.jsonl").exists()
        )
        self.assertTrue((self.box / "--proj--" / "s1.jsonl").exists())

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
        self.assertEqual(sorted(p.name for p in self.box.iterdir()), ["claude", "codex"])

    def test_meta_record_reaches_the_store(self) -> None:
        start = {"hook_event_name": "SessionStart", "session_id": SID, "cwd": str(self.dir)}
        self.assertEqual(self.hook("claude", start, MU_AGENT_NAME="worker-2").returncode, 0)
        self.assertEqual(self.run_sync().returncode, 0)
        record = json.loads((self.box / "claude" / "meta" / f"{SID}.json").read_text())
        self.assertEqual((record["harness"], record["agent"]), ("claude", "worker-2"))

    def test_final_turn_reaches_the_store_on_the_next_tick_after_session_end(self) -> None:
        """What the 5-minute timer does: a SessionEnd's flush stamp makes the
        next tick sync, though the 10-minute interval has not passed."""
        self.run_sync()
        stored = self.box / "claude" / "-work-proj" / f"{SID}.jsonl"
        final = {"type": "assistant", "message": {"role": "assistant", "content": "final turn"}}
        with self.claude_file.open("a") as f:
            f.write(json.dumps(final) + "\n")
        end = {"hook_event_name": "SessionEnd", "session_id": SID, "reason": "exit"}
        self.assertEqual(self.hook("claude", end).returncode, 0)
        tick = subprocess.run(
            ["sh", str(BIN / "museum"), "tick"],
            capture_output=True,
            text=True,
            env=self.env(),
            check=True,
            timeout=30,
        )
        self.assertEqual(tick.stdout, "")
        self.wait_for(lambda: "final turn" in stored.read_text(), "final turn not synced")
        self.assertFalse((self.state / "flush").exists())
        # The detached sync still writes state.json after the copy: let it
        # finish before the sandbox is removed.
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
        self.assertTrue((self.box / "--proj--" / "s1.jsonl").exists())
        self.assertEqual(self.state_field("status"), "ok")


class PiUnchanged(Harness):
    """pi's copy is the same rsync command whether or not other harnesses exist."""

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

    def test_without_staged_copies_the_command_is_the_old_one(self) -> None:
        old = [
            "rsync",
            "-a",
            "--append-verify",
            "--timeout=120",
            "--include=*/",
            "--include=*.jsonl",
            "--exclude=*",
            "-e",
            " ".join(sync.SSH),
            f"{self.src}/",
            f"{self.store}/box/",
        ]
        self.assertEqual(self.command(stage=False), old)
        self.assertFalse((self.state / "stage").exists())

    def test_staged_copies_ride_the_same_rsync_after_pi(self) -> None:
        cmd = self.command(stage=True)
        self.assertEqual(cmd[-3:], [f"{self.src}/", f"{self.state}/stage/", f"{self.store}/box/"])
        self.assertIn("--include=/*/meta/*.json", cmd)


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
        last = f"sh {shlex.quote(str(BIN / 'museum'))} hook claude; true\n"
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

    def test_session_end_only_leaves_the_flush_stamp(self) -> None:
        r = self.hook(
            "codex", {"hook_event_name": "SessionEnd", "session_id": SID, "transcript_path": "/t"}
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertTrue((self.state / "flush").exists())
        self.assertFalse((self.state / "meta").exists())
        self.assertFalse((self.state / "last-start").exists())

    def test_codex_title_session_is_dropped(self) -> None:
        for event in ("SessionStart", "SessionEnd"):
            payload = {"hook_event_name": event, "session_id": SID, "transcript_path": None}
            self.assertEqual(self.hook("codex", payload).returncode, 0)
        self.assertFalse(self.state.exists())

    def test_bad_input_never_fails_the_hook(self) -> None:
        cases = {
            "not json": ("claude", "{"),
            "unsafe id": ("claude", {"hook_event_name": "SessionStart", "session_id": "../x"}),
            "no backup": ("cursor-agent", {"hook_event_name": "sessionEnd"}),
        }
        for name, (harness, payload) in cases.items():
            with self.subTest(name):
                r = self.hook(harness, payload)
                self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertFalse((self.state / "meta").exists())
        self.assertFalse((self.state / "flush").exists())


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
            ["sh", str(BIN / "museum"), "tick"],
            capture_output=True,
            text=True,
            env=self.env(MUSEUM_SYNC=str(self.fake), FAKE_LOG=str(self.log)),
            check=False,
            timeout=10,
        )
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_not_due_starts_nothing(self) -> None:
        (self.state / "last-start").write_text(f"{int(time.time())}\n")
        self.tick()
        time.sleep(0.3)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.state / "last-tick").exists())

    def test_interval_due_starts_one_sync_until_the_next_interval(self) -> None:
        (self.state / "last-start").write_text(f"{int(time.time()) - 601}\n")
        self.tick()
        self.wait_for(lambda: self.calls() == ["--if-due"], "no sync started")
        # The sync could not start (it never wrote last-start): no retry yet.
        self.tick()
        time.sleep(0.3)
        self.assertEqual(self.calls(), ["--if-due"])

    def test_flush_stamp_starts_a_sync_once(self) -> None:
        (self.state / "last-start").write_text(f"{int(time.time())}\n")
        (self.state / "flush").touch()
        self.tick()
        self.wait_for(lambda: self.calls() == ["--if-due"], "no sync started")
        self.assertFalse((self.state / "flush").exists())
        self.assertTrue((self.state / "pending").exists())  # what makes it due
        self.tick()
        time.sleep(0.3)
        self.assertEqual(self.calls(), ["--if-due"])

    def test_hook_and_tick_agree_on_the_flush_stamp_whatever_the_env(self) -> None:
        """A claude hook sees the shell's XDG_STATE_HOME, a Muse hook and the
        timer may not: the stamp sits at one fixed path either way."""
        xdg = self.dir / "xdg"
        for hook_env in ({"XDG_STATE_HOME": str(xdg)}, {}):
            with self.subTest(hook_env=hook_env):
                self.log.unlink(missing_ok=True)
                end = {"hook_event_name": "SessionEnd", "session_id": SID}
                env = {k: v for k, v in self.env(**hook_env).items() if k != "MUSEUM_STATE_DIR"}
                subprocess.run(
                    ["sh", str(BIN / "museum"), "hook", "claude"],
                    input=json.dumps(end),
                    text=True,
                    env={**env, "MUSEUM_SYNC": str(BIN / "museum-sync")},
                    check=True,
                    timeout=30,
                )
                stamp = self.home / ".local/state/museum/flush"
                self.assertTrue(stamp.exists())
                tick_env = self.env(MUSEUM_SYNC=str(self.fake), FAKE_LOG=str(self.log))
                tick_env.pop("MUSEUM_STATE_DIR")
                tick_env["XDG_STATE_HOME"] = str(xdg)
                subprocess.run(
                    ["sh", str(BIN / "museum"), "tick"], env=tick_env, check=True, timeout=10
                )
                self.wait_for(lambda: self.calls() == ["--if-due"], "no sync started")
                self.assertFalse(stamp.exists())
                self.assertTrue((xdg / "museum" / "pending").exists())


# Stand-ins that log their arguments. `launchctl print` finds no job.
FAKE_CTL = '#!/bin/sh\necho "$(basename "$0") $*" >> "$FAKE_LOG"\n[ "$1" != print ]\n'


class Timer(Harness):
    def run_timer(self, kind: str, action: str) -> list[str]:
        bin_dir = self.dir / "ctl"
        bin_dir.mkdir(exist_ok=True)
        for name in ("launchctl", "systemctl"):
            (bin_dir / name).write_text(FAKE_CTL)
            (bin_dir / name).chmod(0o755)
        log = self.dir / "ctl.log"
        log.unlink(missing_ok=True)
        path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        env = self.env(MUSEUM_TIMER_KIND=kind, FAKE_LOG=str(log), PATH=path)
        env.pop("XDG_CONFIG_HOME", None)
        r = subprocess.run(
            ["sh", str(BIN / "museum"), "timer", action],
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
        self.assertIn("OnUnitActiveSec=300", (units / "museum-tick.timer").read_text())
        self.assertEqual(
            calls,
            ["systemctl --user daemon-reload", "systemctl --user enable --now museum-tick.timer"],
        )
        self.run_timer("systemd", "remove")
        self.assertFalse((units / "museum-tick.timer").exists())


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
        self.assertTrue((self.store / "box" / "--proj--" / "s1.jsonl").exists())

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
