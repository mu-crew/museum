"""museum-search on claude and codex sessions beside pi's: one conversation in
each format, synthetic, and the counts that must agree after merging."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from helpers import SCRIPTS, load
from test_search import write_session

search = load("museum-search", SCRIPTS)

CLAUDE_ID = "0b6c2a40-1111-4222-8333-444455556666"
CODEX_ID = "019a0000-aaaa-7bbb-8ccc-dddddddddddd"
PI = "pc/--home-u-same--/2026-11-01T09-00-00-000Z_p1.jsonl"
CLAUDE = f"pc/claude/-home-u-same/{CLAUDE_ID}.jsonl"
# codex names a rollout by local time; its records say 09:00Z.
CODEX = f"pc/codex/-home-u-same/rollout-2026-11-01T10-00-00-{CODEX_ID}.jsonl"
LEGACY = "pc/2025-01-01T00-00-00-000Z_old.jsonl"


def at(sec: int) -> str:
    return f"2026-11-01T09:{sec // 60:02d}:{sec % 60:02d}.000Z"


def meta(agent: str) -> dict[str, Any]:
    return {"project": "same", "branch": "main", "workstream": "same-ws", "agent": agent}


# One conversation: a prompt; a reply that runs the gate (60 s); a reply; a
# second prompt; a reply with two calls at once, a read and a failing commit;
# a last reply. 2 user and 4 assistant messages, 3 calls. Model time
# 12 + 8 + 5 + 6, tool time 60 + 1 + 3, wait 120. pi stamps a reply when it
# ends, so its first reply is at 12 s, the time of the last of the lines or
# items claude and codex write it as.


def pi_session() -> list[dict[str, Any]]:
    def say(role: str, sec: int, content: Any, **extra: Any) -> dict[str, Any]:
        message = {"role": role, "content": content, **extra}
        return {"type": "message", "timestamp": at(sec), "message": message}

    def call(cid: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"type": "toolCall", "id": cid, "name": name, "arguments": args}

    def result(sec: int, cid: str, out: str, error: bool = False) -> dict[str, Any]:
        content = [{"type": "text", "text": out}]
        return say("toolResult", sec, content, toolCallId=cid, isError=error)

    return [
        {"type": "session", "cwd": "/home/u/same", "timestamp": at(0)},
        {"type": "custom", "customType": "museum", "data": meta("pi-1")},
        say("user", 0, "Fix the flaky lockfile test"),
        say(
            "assistant",
            12,
            [
                {"type": "thinking", "thinking": "Run the gate first"},
                {"type": "text", "text": "Running the gate."},
                call("c1", "bash", {"command": "make check"}),
            ],
        ),
        result(72, "c1", "ok"),
        say("assistant", 80, [{"type": "text", "text": "The gate passes; lockfile fixed."}]),
        say("user", 200, "commit it"),
        say(
            "assistant",
            205,
            [
                call("c2", "read", {"path": "README.md"}),
                call("c3", "bash", {"command": "git commit -am fix"}),
            ],
        ),
        result(206, "c2", "README contents"),
        result(209, "c3", "nothing to commit", error=True),
        say("assistant", 215, [{"type": "text", "text": "Commit failed: nothing to commit."}]),
    ]


def claude_session() -> list[dict[str, Any]]:
    base = {"sessionId": CLAUDE_ID, "cwd": "/home/u/same", "gitBranch": "main"}

    def user(sec: int, content: Any, **extra: Any) -> dict[str, Any]:
        message = {"role": "user", "content": content}
        return {"type": "user", "timestamp": at(sec), **base, "message": message, **extra}

    def reply(sec: int, mid: str, block: dict[str, Any]) -> dict[str, Any]:
        message = {"id": mid, "model": "claude-test", "role": "assistant", "content": [block]}
        return {"type": "assistant", "timestamp": at(sec), **base, "message": message}

    def tool_use(tid: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"type": "tool_use", "id": tid, "name": name, "input": args}

    def result(sec: int, tid: str, out: str, error: bool = False) -> dict[str, Any]:
        block = {"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": error}
        return user(sec, [block], toolUseResult={"stdout": out, "stderr": ""})

    def attachment(sec: int, attached: dict[str, Any]) -> dict[str, Any]:
        return {"type": "attachment", "timestamp": at(sec), **base, "attachment": attached}

    instructions = [{"path": "/home/u/same/CLAUDE.md", "content": "Feed the zebracorn."}]
    return [
        {"type": "permission-mode", "permissionMode": "default", "sessionId": CLAUDE_ID},
        attachment(0, {"type": "instructions", "files": instructions}),
        attachment(0, {"type": "skill_listing", "content": "- museum: zebracorn lookups"}),
        user(0, "Caveat: the messages below were generated locally.", isMeta=True),
        user(0, "<command-name>/model</command-name>\n<command-args></command-args>"),
        user(0, "<local-command-stdout>Set model to claude-test</local-command-stdout>"),
        user(0, "Fix the flaky lockfile test"),
        # One reply, one line per content block, all under msg_1.
        reply(10, "msg_1", {"type": "thinking", "thinking": "Run the gate first"}),
        reply(11, "msg_1", {"type": "text", "text": "Running the gate."}),
        reply(12, "msg_1", tool_use("toolu_1", "Bash", {"command": "make check"})),
        result(72, "toolu_1", "ok"),
        reply(80, "msg_2", {"type": "text", "text": "The gate passes; lockfile fixed."}),
        {"type": "system", "subtype": "informational", "content": "hook ran", "timestamp": at(81)},
        # A compaction's summary is no message, as pi's compaction entry is not.
        user(90, "Summary: a pangolin fixed the lockfile.", isCompactSummary=True),
        {"type": "queue-operation", "operation": "enqueue", "content": "commit it"},
        user(200, "commit it"),
        # Two calls at once: the first result lands between the reply's lines.
        reply(205, "msg_3", tool_use("toolu_2", "Read", {"file_path": "README.md"})),
        result(206, "toolu_2", "README contents"),
        reply(205, "msg_3", tool_use("toolu_3", "Bash", {"command": "git commit -am fix"})),
        result(209, "toolu_3", "nothing to commit", error=True),
        reply(215, "msg_4", {"type": "text", "text": "Commit failed: nothing to commit."}),
        {"type": "last-prompt", "lastPrompt": "commit it", "leafUuid": "x", "sessionId": CLAUDE_ID},
    ]


def codex_session() -> list[dict[str, Any]]:
    def item(sec: int, payload: dict[str, Any]) -> dict[str, Any]:
        return {"timestamp": at(sec), "type": "response_item", "payload": payload}

    def event(sec: int, payload: dict[str, Any]) -> dict[str, Any]:
        return {"timestamp": at(sec), "type": "event_msg", "payload": payload}

    def say(sec: int, role: str, said: str) -> dict[str, Any]:
        kind = "output_text" if role == "assistant" else "input_text"
        content = [{"type": kind, "text": said}]
        return item(sec, {"type": "message", "role": role, "content": content})

    def call(sec: int, cid: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        payload = {"type": "function_call", "name": name, "arguments": json.dumps(args)}
        return item(sec, {**payload, "call_id": cid})

    def output(sec: int, cid: str, out: str) -> dict[str, Any]:
        return item(sec, {"type": "function_call_output", "call_id": cid, "output": out})

    def shell(command: str, secs: float, code: int, out: str) -> str:
        return (
            f"Command: /bin/zsh -lc '{command}'\nChunk ID: ab12\nWall time: {secs} seconds\n"
            f"Process exited with code {code}\nOriginal token count: 4\nOutput:\n{out}"
        )

    turn = {"cwd": "/home/u/same", "model": "codex-test", "approval_policy": "never"}
    return [
        {
            "timestamp": at(0),
            "type": "session_meta",
            "payload": {
                "id": CODEX_ID,
                "session_id": CODEX_ID,
                "timestamp": at(0),
                "cwd": "/home/u/same",
                "base_instructions": {"text": "You are a coding agent."},
                "git": {"branch": "main"},
            },
        },
        {"timestamp": at(0), "type": "turn_context", "payload": turn},
        say(0, "developer", "Sandbox: workspace-write."),
        say(0, "user", "<environment_context>\n  <cwd>/home/u/same</cwd>\n</environment_context>"),
        say(0, "user", "Fix the flaky lockfile test"),
        event(0, {"type": "user_message", "message": "Fix the flaky lockfile test"}),
        event(0, {"type": "task_started", "turn_id": "t1"}),
        # One reply as three items, then the call's records under its call_id.
        item(
            10,
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Run the gate first"}],
            },
        ),
        say(11, "assistant", "Running the gate."),
        call(12, "c1", "exec_command", {"cmd": "make check", "workdir": "/home/u/same"}),
        event(13, {"type": "exec_command_begin", "call_id": "c1", "command": ["make", "check"]}),
        event(
            72,
            {
                "type": "exec_command_end",
                "call_id": "c1",
                "exit_code": 0,
                "duration": {"secs": 60, "nanos": 0},
            },
        ),
        output(72, "c1", shell("make check", 60.0, 0, "ok")),
        event(72, {"type": "token_count", "info": None}),
        say(80, "assistant", "The gate passes; lockfile fixed."),
        event(80, {"type": "agent_message", "message": "The gate passes; lockfile fixed."}),
        event(81, {"type": "task_complete", "turn_id": "t1"}),
        {"timestamp": at(200), "type": "turn_context", "payload": turn},
        say(200, "user", "commit it"),
        item(205, {"type": "reasoning", "summary": []}),
        call(205, "c2", "read_file", {"path": "README.md"}),
        call(205, "c3", "exec_command", {"cmd": "git commit -am fix"}),
        output(206, "c2", "README contents"),
        output(209, "c3", shell("git commit -am fix", 4.0, 1, "nothing to commit")),
        say(215, "assistant", "Commit failed: nothing to commit."),
    ]


class Formats(unittest.TestCase):
    rg: bool = True

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        write_session(self.dir, PI, pi_session())
        write_session(self.dir, CLAUDE, claude_session())
        write_session(self.dir, CODEX, codex_session())
        for harness, sid in (("claude", CLAUDE_ID), ("codex", CODEX_ID)):
            record = {"harness": harness, "session_id": sid, **meta(f"{harness}-1")}
            path = self.dir / "pc" / harness / "meta" / f"{sid}.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(record) + "\n")
        # From before pi kept a folder per cwd: at the machine folder's root.
        write_session(
            self.dir,
            LEGACY,
            [
                {"type": "session", "cwd": "/home/u", "timestamp": "2025-01-01T00:00:00Z"},
                {
                    "type": "message",
                    "timestamp": "2025-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "an old lockfile question"},
                },
            ],
        )
        # muse's format is not known yet: it stays out of every command.
        write_session(
            self.dir, "pc/muse/-home-u-same/m1.jsonl", [{"role": "user", "text": "lockfile"}]
        )

    def run_search(self, *args: str) -> str:
        env = dict(os.environ, PI_SESSION_ID="")
        if not self.rg:
            # A PATH with python3 but no rg exercises the pure-Python scan.
            env["PATH"] = str(Path(sys.executable).parent)
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "museum-search"), "--store", str(self.dir), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def files(self, out: str) -> list[str]:
        return [line.split()[1] for line in out.splitlines() if line.strip().startswith("file:")]

    def test_stats_counts_agree_across_formats_after_merging(self) -> None:
        roles = json.loads(self.run_search("stats", "--json"))["roles"]
        self.assertEqual(sorted(roles), ["claude", "codex", "other", "pi"])
        want = {"sessions": 1, "messages": 6, "calls": 3, "model": 31, "tool": 64, "wait": 120}
        for harness in ("pi", "claude", "codex"):
            with self.subTest(harness=harness):
                got = {k: roles[harness][k] for k in want}
                self.assertEqual(got, want)
        # The legacy root-level pi file is a session too.
        self.assertEqual((roles["other"]["sessions"], roles["other"]["messages"]), (1, 1))

    def test_calls_agree_across_formats(self) -> None:
        rows = json.loads(self.run_search("calls", "--tool", "bash", "--sort", "time", "--json"))
        by_agent: dict[str, list[tuple[str, str, float, bool]]] = {}
        for r in rows:
            by_agent.setdefault(r["agent"], []).append(
                (r["start"], r["command"], r["secs"], r["error"])
            )
        want = [
            ("2026-11-01T09:00:12", "make check", 60, False),
            ("2026-11-01T09:03:25", "git commit -am fix", 4, True),
        ]
        self.assertEqual(by_agent, {"pi-1": want, "claude-1": want, "codex-1": want})
        # codex records how long its calls took; the others are estimates.
        sources = {(r["agent"], r["duration_source"]) for r in rows}
        self.assertIn(("codex-1", "durationMs"), sources)
        self.assertIn(("claude-1", "estimated"), sources)
        # claude's Read names its file as file_path; the row shows it.
        reads = json.loads(self.run_search("calls", "--tool", "read", "--json"))
        self.assertEqual(
            {(r["agent"], r["command"]) for r in reads},
            {("pi-1", "README.md"), ("claude-1", "README.md")},
        )

    def test_filters_read_meta_records(self) -> None:
        roles = json.loads(self.run_search("stats", "same-ws", "--role", "c*", "--json"))["roles"]
        self.assertEqual(sorted(roles), ["claude", "codex"])
        rows = json.loads(self.run_search("calls", "--error", "--group-by", "role", "--json"))
        self.assertEqual({g["key"]: g["calls"] for g in rows}, {"pi": 1, "claude": 1, "codex": 1})

    def test_find_reads_every_format_on_normalised_text(self) -> None:
        out = self.run_search("find", "lockfile")
        self.assertIn('"lockfile": 4 sessions in conversation, 0 only in tool output.', out)
        self.assertEqual(sorted(self.files(out)), sorted([PI, CLAUDE, CODEX, LEGACY]))
        self.assertIn("pc  claude  same@main", out)
        self.assertIn("pc  codex  same@main", out)
        self.assertIn("first: Fix the flaky lockfile test", out)

    def test_find_counts_a_claude_reply_once(self) -> None:
        out = self.run_search("find", "gate")
        blocks = {
            b.split("file:  ")[1].split()[0]: b.splitlines()[0] for b in out.split("\n\n")[1:]
        }
        # msg_1's thinking and text are two lines of one reply.
        self.assertTrue(blocks[CLAUDE].endswith("(2 turns, 0 tool entries)"), blocks[CLAUDE])
        self.assertTrue(blocks[PI].endswith("(2 turns, 0 tool entries)"), blocks[PI])
        self.assertTrue(blocks[CODEX].endswith("(2 turns, 1 tool entries)"), blocks[CODEX])

    def test_find_tiers_tool_output_and_drops_instructions_and_echoes(self) -> None:
        out = self.run_search("find", "README contents")
        self.assertIn("0 sessions in conversation, 3 only in tool output.", out)
        terms = ("coding agent", "workspace-write", "environment_context", "Caveat", "zebracorn")
        for term in terms:
            with self.subTest(term=term):
                self.assertIn(f'no session mentions "{term}"', self.run_search("find", term))
        # A compaction's summary and a slash command's output are no turn.
        for term in ("pangolin", "Set model"):
            with self.subTest(term=term):
                out = self.run_search("find", term)
                self.assertIn("0 sessions in conversation, 1 only in tool output.", out)
        # claude's queue and last prompt repeat a prompt: one turn, as in pi.
        out = self.run_search("find", "commit it")
        self.assertIn("3 sessions in conversation, 0 only in tool output.", out)
        self.assertEqual(out.count("(1 turns, 0 tool entries)"), 3, out)
        # codex's events repeat its response items.
        write_session(
            self.dir,
            "pc/codex/-home-u-x/rollout-2026-11-02T00-00-00-e.jsonl",
            [
                {
                    "timestamp": at(0),
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "echoonly"},
                }
            ],
        )
        self.assertIn('no session mentions "echoonly"', self.run_search("find", "echoonly"))

    def test_find_reads_claude_workflow_subagents(self) -> None:
        run = f"pc/claude/-home-u-same/{CLAUDE_ID}/subagents/workflows/wf_1"
        line = {"type": "assistant", "timestamp": at(5), "cwd": "/home/u/same"}
        content = [{"type": "text", "text": "quokka found"}]
        reply = {**line, "message": {"id": "m9", "role": "assistant", "content": content}}
        write_session(self.dir, f"{run}/agent-abc.jsonl", [reply])
        write_session(self.dir, f"{run}/journal.jsonl", [{"type": "step", "note": "quokka"}])
        out = self.run_search("find", "quokka")
        self.assertEqual(self.files(out), [f"{run}/agent-abc.jsonl"])
        self.assertIn("pc  claude  same@main", out)  # the parent's meta record

    def test_find_project_selects_claude_and_codex_folders(self) -> None:
        out = self.run_search("find", "lockfile", "u-same")
        self.assertEqual(sorted(self.files(out)), sorted([PI, CLAUDE, CODEX]))

    def test_show_prints_the_same_turns_for_every_format(self) -> None:
        def turns(rel: str) -> list[str]:
            return [x for x in self.run_search("show", rel).splitlines() if x.startswith("[")]

        pi = turns(PI)
        self.assertEqual(len(pi), 5)
        self.assertEqual(turns(CLAUDE), pi)
        self.assertEqual(turns(CODEX), pi)
        head = self.run_search("show", CLAUDE).splitlines()[:2]
        self.assertEqual(head[0], "cwd: /home/u/same")
        self.assertIn('"agent": "claude-1"', head[1])

    def test_dates_come_from_the_first_record_and_are_cached(self) -> None:
        def harnesses(*args: str) -> list[str]:
            return sorted(json.loads(self.run_search("stats", *args, "--json"))["roles"])

        # The rollout's name says 10:00 local; its first record says 09:00Z.
        self.assertEqual(
            harnesses("--until", "2026-11-01T09:30"), ["claude", "codex", "other", "pi"]
        )
        self.assertEqual(harnesses("--since", "2026-11-02"), [])
        index = json.loads((self.dir / ".museum-index.json").read_text())["dates"]
        self.assertEqual(index[CLAUDE], "2026-11-01T09:00:00")
        self.assertNotIn(PI, index)  # a pi file's name says when it started
        # A cached date is not read again.
        index[CLAUDE] = "2030-01-01T00:00:00"
        (self.dir / ".museum-index.json").write_text(json.dumps({"dates": index}))
        self.assertEqual(harnesses("--since", "2030-01-01"), ["claude"])
        out = self.run_search("find", "lockfile", "--newest")
        self.assertEqual(self.files(out)[0], CLAUDE)
        self.assertIn("2030-01-01 00:00  pc  claude", out)


@unittest.skipUnless(shutil.which("rg"), "rg not installed")
class FormatsWithRg(Formats):
    rg = True


class FormatsWithoutRg(Formats):
    rg = False


del Formats  # only the two concrete variants run


def claude_line(mid: str, block: dict[str, Any], sec: int = 0) -> dict[str, Any]:
    message = {"id": mid, "role": "assistant", "model": "claude-test", "content": [block]}
    return {"type": "assistant", "timestamp": at(sec), "message": message}


class Units(unittest.TestCase):
    def test_session_parse_by_depth(self) -> None:
        cases = {
            "h/--p--/2026-01-01T00-00-00-000Z_a.jsonl": ("pi", "--p--"),
            "h/2026-01-01T00-00-00-000Z_a.jsonl": ("pi", ""),
            "h/claude/-p/s.jsonl": ("claude", "-p"),
            "h/claude/-p/s/subagents/agent-1.jsonl": ("claude", "-p"),
            "h/codex/-p/rollout-x.jsonl": ("codex", "-p"),
        }
        for path, (harness, folder) in cases.items():
            with self.subTest(path=path):
                s = search.Session.parse(path)
                self.assertIsNotNone(s)
                assert s is not None
                self.assertEqual((s.harness, s.folder), (harness, folder))
        for path in (
            "a.jsonl",
            "h/muse/-p/m.jsonl",
            "h/claude/meta/s.jsonl",
            "h/claude/s.jsonl",
            "h/codex/-p/x/y.jsonl",
            "h/claude/-p/s/other/a.jsonl",
            ".hidden/--p--/a.jsonl",
            "h/--p--/a.json",
        ):
            with self.subTest(path=path):
                self.assertIsNone(search.Session.parse(path))

    def test_session_ids(self) -> None:
        sub = search.Session.parse(f"h/claude/-p/{CLAUDE_ID}/subagents/agent-1.jsonl")
        assert sub is not None
        self.assertEqual(sub.session_ids(), [CLAUDE_ID])
        self.assertEqual(sub.id(), "agent-1")
        rollout = search.Session.parse(f"h/codex/-p/rollout-2026-11-01T10-00-00-{CODEX_ID}.jsonl")
        assert rollout is not None
        self.assertEqual(rollout.id(), CODEX_ID)

    def test_claude_merge_dedupes_repeated_blocks_and_keeps_the_model(self) -> None:
        text = {"type": "text", "text": "hello"}
        call = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
        lines = [claude_line("m1", text, 1), claude_line("m1", text, 2), claude_line("m1", call, 3)]
        lines[2]["message"]["content"].insert(0, text)  # a line repeating an earlier block
        (reply,) = search.claude_stream(lines)
        self.assertEqual(reply["timestamp"], at(3))  # pi stamps a reply when it ends
        self.assertEqual(reply["message"]["model"], "claude-test")
        self.assertEqual([b["type"] for b in reply["message"]["content"]], ["text", "toolCall"])
        self.assertEqual(reply["message"]["content"][1]["name"], "bash")

    def test_claude_merged_reply_time_stops_at_a_result_among_its_lines(self) -> None:
        def call(tid: str) -> dict[str, Any]:
            return {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": tid}}

        result = {
            "type": "user",
            "timestamp": at(40),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a"}]},
        }
        lines = [
            claude_line("m1", {"type": "thinking", "thinking": "plan"}, 10),
            claude_line("m1", call("a"), 30),
            result,
            claude_line("m1", call("b"), 45),
        ]
        reply, _ = search.claude_stream(lines)
        self.assertEqual(reply["timestamp"], at(30))
        calls = [(b["id"], b["timestamp"]) for b in reply["message"]["content"][1:]]
        self.assertEqual(calls, [("a", at(30)), ("b", at(45))])

    def test_stats_and_calls_time_a_merged_reply_by_its_last_line(self) -> None:
        # Thinking at 10 s, a long Write at 40 s, its result at 45 s: 40 s of
        # model time and a 5 s call, as pi would record it.
        write = {"type": "tool_use", "id": "w", "name": "Write", "input": {"file_path": "a"}}
        records = [
            {"type": "user", "timestamp": at(0), "message": {"role": "user", "content": "go"}},
            claude_line("m1", {"type": "thinking", "thinking": "plan"}, 10),
            claude_line("m1", write, 40),
            {
                "type": "user",
                "timestamp": at(45),
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "w"}],
                },
            },
        ]
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        write_session(d, f"pc/claude/-p/{CLAUDE_ID}.jsonl", records)
        cwd = Path.cwd()
        os.chdir(d)
        self.addCleanup(os.chdir, cwd)
        s = search.Session.parse(f"pc/claude/-p/{CLAUDE_ID}.jsonl")
        assert s is not None
        flt = search.Filters(None, None, set(), None, None, None, False, None, None, None, None)
        got = search.timeline(s, 0, flt)
        assert got is not None
        tl, calls = got
        spans: dict[str, float] = {}
        for _, kind, secs in tl.spans:
            spans[kind] = spans.get(kind, 0) + secs
        self.assertEqual(spans, {"model": 40, "tool": 5})
        self.assertEqual([(c.start, c.secs) for c in calls], [("2026-11-01T09:00:40", 5)])

    def test_codex_merges_by_call_id(self) -> None:
        def item(sec: int, payload: dict[str, Any]) -> dict[str, Any]:
            return {"timestamp": at(sec), "type": "response_item", "payload": payload}

        call = {"type": "function_call", "name": "shell", "call_id": "c1"}
        call["arguments"] = json.dumps({"command": ["bash", "-lc", "make && make test"]})
        out = {"type": "function_call_output", "call_id": "c1", "output": "done"}
        end = {"type": "exec_command_end", "call_id": "c1", "exit_code": 2}
        records = [
            {"timestamp": at(0), "type": "turn_context", "payload": {"model": "codex-test"}},
            item(1, call),
            item(1, call),  # the same call again, as a resumed rollout repeats it
            {
                "timestamp": at(5),
                "type": "event_msg",
                "payload": {**end, "duration": {"secs": 3, "nanos": 500_000_000}},
            },
            item(5, out),
            item(6, out),
        ]
        reply, result = search.codex_stream(records)
        self.assertEqual(reply["message"]["model"], "codex-test")
        (block,) = reply["message"]["content"]
        self.assertEqual(
            (block["name"], block["arguments"]["command"]), ("bash", "make && make test")
        )
        self.assertEqual(
            (result["message"]["durationMs"], result["message"]["isError"]), (3500, True)
        )

    def test_codex_output_formats(self) -> None:
        items = [
            {"type": "input_text", "text": "Script completed\nWall time 0.6 seconds\nOutput:\n"},
            {"type": "input_text", "text": "Wall time: 99 seconds"},
        ]
        self.assertEqual(search.codex_output(items)[1:], (600, False))
        wrapped = json.dumps({"output": "x", "metadata": {"exit_code": 1, "duration_seconds": 2}})
        self.assertEqual(search.codex_output(wrapped), ("x", 2000, True))
        self.assertEqual(search.codex_output("Exit code: 0\nOutput:\nfine")[1:], (None, False))
        # A command codex left running: its wall time is only the wait.
        running = (
            "Chunk ID: 1\nWall time: 10.0 seconds\nProcess running with session ID 7\nOutput:\n"
        )
        self.assertEqual(search.codex_output(running)[1:], (None, False))

    def test_codex_injected_user_messages_are_system(self) -> None:
        def user(said: str) -> dict[str, Any]:
            content = [{"type": "input_text", "text": said}]
            payload = {"type": "message", "role": "user", "content": content}
            return {"timestamp": at(0), "type": "response_item", "payload": payload}

        roles = [
            search.codex_entries(user(s))[0]["message"]["role"]
            for s in ("# AGENTS.md instructions for /w\n...", "<turn_aborted>x", "fix #12")
        ]
        self.assertEqual(roles, ["system", "system", "user"])

    def test_label_of_a_claude_workspace(self) -> None:
        self.assertEqual(
            search.label("-home-u--local-state-mu-workspaces-hail-worker-1"), "mu:hail-worker-1"
        )
        self.assertEqual(search.label("-Users-u-hacking-hail"), "hacking-hail")


if __name__ == "__main__":
    unittest.main()
