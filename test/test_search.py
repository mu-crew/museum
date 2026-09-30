"""museum-search against a small fixture store, run as a real subprocess."""

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

search = load("museum-search", SCRIPTS)

HAIL = "--home-u-hacking-hail--"
WORKER = "--home-u-.local-state-mu-workspaces-hail-worker-1--"
SELF = "0000-self"


def msg(role: str, content: Any, ts: str = "2026-01-01T00:00:00Z") -> dict[str, Any]:
    return {"type": "message", "timestamp": ts, "message": {"role": role, "content": content}}


def write_session(root: Path, rel: str, entries: list[dict[str, Any]], torn: bool = False) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    # As pi writes them: compact, non-ASCII unescaped.
    lines = [json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in entries]
    body = "\n".join(lines) + "\n"
    if torn:
        body += '{"type":"message","message":{"role":"user","content":"Stalwart'
    path.write_text(body)


class Store(unittest.TestCase):
    rg: bool = True

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        header = {"type": "session", "cwd": "/home/u/hacking/hail"}
        write_session(
            self.dir,
            f"pc/{HAIL}/2026-05-22T16-32-24-344Z_a.jsonl",
            [
                header,
                msg("user", "brainstorm a hey.com clone"),
                msg("assistant", [{"type": "text", "text": "Use Stalwart as the mail server."}]),
                msg("user", "go is out, rust vs ts"),
                msg("assistant", [{"type": "text", "text": "Rust it is."}]),
            ],
        )
        write_session(
            self.dir,
            f"pc/{HAIL}/2026-06-07T09-42-00-000Z_b.jsonl",
            [
                header,
                {
                    "type": "custom",
                    "customType": "museum",
                    "data": {"project": "hail", "branch": "main"},
                },
                msg("user", "look at this repo"),
                {
                    "type": "message",
                    "message": {
                        "role": "toolResult",
                        "content": [{"type": "text", "text": "│ stalwart │ table │"}],
                    },
                },
                msg("assistant", [{"type": "text", "text": "Hail runs on Stalwart + JMAP."}]),
            ],
            torn=True,
        )
        # Newer than both project sessions, yet ranked after them.
        write_session(
            self.dir,
            f"pc/{WORKER}/2026-06-08T00-00-00-000Z_c.jsonl",
            [msg("user", "New task: fix stalwart config")],
        )
        write_session(
            self.dir,
            "mac/--Users-u-notes--/2026-07-01T00-00-00-000Z_d.jsonl",
            [msg("user", 'she said "it\'s done" \\ café')],
        )
        # Stalwart only in an assistant's thinking and tool-call arguments: an
        # assistant line, but not a turn that says it.
        write_session(
            self.dir,
            f"pc/{HAIL}/2026-09-01T00-00-00-000Z_f.jsonl",
            [
                msg("user", "check the config"),
                msg(
                    "assistant",
                    [
                        {"type": "thinking", "thinking": "look at stalwart"},
                        {"type": "toolCall", "name": "bash", "arguments": {"cmd": "rg stalwart"}},
                    ],
                ),
            ],
        )
        # Stalwart only in the recorded system prompt (AGENTS.md): no mention.
        write_session(
            self.dir,
            f"pc/{HAIL}/2026-09-02T00-00-00-000Z_g.jsonl",
            [
                {
                    "type": "message",
                    "message": {"role": "system", "content": "", "sections": {"x": "Stalwart"}},
                },
                msg("user", "hello"),
            ],
        )
        # The session doing the search, left out by --skip.
        write_session(
            self.dir,
            f"pc/{HAIL}/2026-09-03T00-00-00-000Z_{SELF}.jsonl",
            [msg("user", "what did we decide about stalwart?")],
        )

    def run_search(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        if not self.rg:
            # A PATH with python3 but no rg exercises the pure-Python scan.
            env["PATH"] = str(Path(sys.executable).parent)
        env["PI_SESSION_ID"] = SELF
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "museum-search"), "--store", str(self.dir), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def files(self, out: str) -> list[str]:
        return [line.split()[1] for line in out.splitlines() if line.strip().startswith("file:")]

    def test_find_ranks_conversation_then_projects_then_newest(self) -> None:
        out = self.run_search("find", "STALWART").stdout
        self.assertEqual(
            self.files(out),
            [
                # Both project sessions say it in one turn: newest first.
                f"pc/{HAIL}/2026-06-07T09-42-00-000Z_b.jsonl",
                f"pc/{HAIL}/2026-05-22T16-32-24-344Z_a.jsonl",
                f"pc/{WORKER}/2026-06-08T00-00-00-000Z_c.jsonl",
                # Newest project session, but only thinking and a tool call.
                f"pc/{HAIL}/2026-09-01T00-00-00-000Z_f.jsonl",
            ],
        )
        self.assertIn('"STALWART": 3 sessions in conversation, 1 only in tool output.', out)

    def test_find_ranks_the_longer_discussion_first_unless_newest(self) -> None:
        older = f"pc/{HAIL}/2025-01-01T00-00-00-000Z_i.jsonl"
        turns = [msg("user", "zeta?"), msg("assistant", "zeta is a"), msg("user", "and zeta b")]
        write_session(self.dir, older, turns)
        newer = f"pc/{HAIL}/2026-12-01T00-00-00-000Z_j.jsonl"
        write_session(self.dir, newer, [msg("user", "zeta once")])
        self.assertEqual(self.files(self.run_search("find", "zeta").stdout), [older, newer])
        out = self.run_search("find", "zeta", "--newest").stdout
        self.assertEqual(self.files(out), [newer, older])
        self.assertIn("newest first.", out)

    def test_find_skips_the_searching_session_unless_told_otherwise(self) -> None:
        self.assertNotIn(SELF, self.run_search("find", "decide about").stdout)
        out = self.run_search("find", "decide about", "--skip", "").stdout
        self.assertEqual(self.files(out), [f"pc/{HAIL}/2026-09-03T00-00-00-000Z_{SELF}.jsonl"])

    def test_find_with_only_system_prompt_mentions(self) -> None:
        write_session(
            self.dir,
            "mac/--Users-u-x--/2026-01-01T00-00-00-000Z_h.jsonl",
            [{"type": "message", "message": {"role": "system", "sections": {"a": "zebra"}}}],
        )
        out = self.run_search("find", "zebra").stdout
        self.assertEqual(
            out.strip(),
            'no session mentions "zebra" outside a system prompt (AGENTS.md, docs, skills)',
        )

    def test_find_block_prefers_conversation_text_and_metadata(self) -> None:
        out = self.run_search("find", "stalwart", "hail", "--limit", "1").stdout
        # The torn last line counts as tool output: it cannot be parsed.
        self.assertIn("2026-06-07 09:42  pc  hail@main  (1 turns, 2 tool entries)", out)
        self.assertIn("first: look at this repo", out)
        self.assertIn("match: Hail runs on Stalwart + JMAP.", out)
        self.assertNotIn("table", out)
        self.assertIn("Showing 1:", out)

    def test_find_labels_folders_without_metadata(self) -> None:
        out = self.run_search("find", "fix stalwart").stdout
        self.assertIn("pc  mu:hail-worker-1  (1 turns, 0 tool entries)", out)

    def test_find_matches_json_escaped_and_unicode_terms(self) -> None:
        for term in ['"it\'s done"', "\\ café", "CAFÉ"]:
            with self.subTest(term=term):
                out = self.run_search("find", term).stdout
                self.assertIn("1 sessions in conversation", out)

    def test_find_misses(self) -> None:
        self.assertIn('no session mentions "zzqq"', self.run_search("find", "zzqq").stdout)
        r = self.run_search("find", "x", "nosuchproject")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no store folder matches", r.stderr)

    def test_show_window(self) -> None:
        r = self.run_search("show", f"pc/{HAIL}/2026-05-22T16-32-24-344Z_a.jsonl", "rust it")
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], "cwd: /home/u/hacking/hail")
        turns = [line.split("] ", 1)[1] for line in lines[1:]]
        self.assertEqual(turns, ["user: go is out, rust vs ts", "assistant: Rust it is."])

    def test_show_marks_gaps_between_windows(self) -> None:
        rel = "pc/--x--/2026-01-01T00-00-00-000Z_e.jsonl"
        write_session(self.dir, rel, [msg("user", t) for t in ["X", "a", "b", "c", "X"]])
        out = self.run_search("show", rel, "x").stdout.splitlines()
        self.assertEqual([line[-1] for line in out], ["X", "a", ".", "c", "X"])

    def test_show_everything_skips_tool_output_and_torn_line(self) -> None:
        out = self.run_search("show", f"pc/{HAIL}/2026-06-07T09-42-00-000Z_b.jsonl").stdout
        self.assertIn('repo: {"project": "hail", "branch": "main"}', out)
        self.assertNotIn("table", out)
        self.assertEqual(out.count("] user:"), 1)
        self.assertNotIn("...", out)

    def test_show_users_prints_the_arc(self) -> None:
        out = self.run_search("show", f"pc/{HAIL}/2026-05-22T16-32-24-344Z_a.jsonl", "--users")
        turns = [line.split("] ", 1)[1] for line in out.stdout.splitlines()[1:]]
        self.assertEqual(
            turns,
            [
                "user: brainstorm a hey.com clone",
                # Terse, so it comes with the turn it answers.
                "  asked: Use Stalwart as the mail server.",
                "user: go is out, rust vs ts",
            ],
        )

    def test_show_users_pairs_a_numbered_answer_with_its_question(self) -> None:
        rel = "pc/--x--/2026-01-01T00-00-00-000Z_q.jsonl"
        long_intro = "context " * 60
        write_session(
            self.dir,
            rel,
            [
                msg("user", "the tools need a home: pick a GitHub org name for all three of them"),
                msg("assistant", f"{long_intro} Q1: 1. mu-crew 2. mu-works. I recommend **1**."),
                # Over TERSE, but it starts by picking an option.
                msg("user", "1, also consider renaming coop to mu-something"),
                msg("assistant", "Done."),
                msg("user", "now write the README for the new org, with the zen section first"),
                msg("assistant", "Should it link pi?"),
                msg("user", "yes"),
            ],
        )
        # No session header in this fixture, so no cwd: line to skip.
        lines = self.run_search("show", rel, "--users").stdout.splitlines()
        body = [line.split("] ", 1)[1] for line in lines]
        self.assertEqual(
            body[0], "user: the tools need a home: pick a GitHub org name for all three of them"
        )
        # The asking turn's tail, cut from the front, keeps the question.
        self.assertTrue(body[1].startswith("  asked: ..."))
        self.assertTrue(body[1].endswith("Q1: 1. mu-crew 2. mu-works. I recommend **1**."))
        self.assertEqual(body[2], "user: 1, also consider renaming coop to mu-something")
        # A long request stands alone; a terse reply gets its question.
        self.assertEqual(
            body[3:],
            [
                "user: now write the README for the new org, with the zen section first",
                "  asked: Should it link pi?",
                "user: yes",
            ],
        )

    def test_show_missing_file(self) -> None:
        r = self.run_search("show", "nope.jsonl")
        self.assertEqual(r.returncode, 1)


@unittest.skipUnless(shutil.which("rg"), "rg not installed")
class StoreWithRg(Store):
    rg = True


class StoreWithoutRg(Store):
    rg = False


del Store  # only the two concrete variants run


class Units(unittest.TestCase):
    def test_label(self) -> None:
        self.assertEqual(search.label("--var-home-u-hacking-hail--"), "hacking-hail")
        self.assertEqual(search.label("--Users-u-notes--"), "notes")
        self.assertEqual(search.label(WORKER), "mu:hail-worker-1")

    def test_window(self) -> None:
        texts = ["a", "b", "X", "c", "d", "e", "X"]
        self.assertEqual(search.window(texts, "x"), [1, 2, 3, 5, 6])
        self.assertEqual(search.window(texts, None), list(range(7)))

    def test_scan_drops_system_prompt_only_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            write_session(
                Path(d),
                "h/f/s.jsonl",
                [{"type": "message", "message": {"role": "system", "sections": {"a": "Zed"}}}],
            )
            self.assertIsNone(search.scan(Path(d, "h/f/s.jsonl"), "zed"))
            write_session(Path(d), "h/f/t.jsonl", [msg("user", "zed"), msg("user", "zed again")])
            self.assertEqual(search.scan(Path(d, "h/f/t.jsonl"), "ZED"), (search.CONVERSATION, 2))

    def test_forward_carries_the_limit(self) -> None:
        ns = search.parse(["find", "t", "--limit", "3", "--skip", "me"])
        self.assertEqual(search.forward(ns), ["find", "t", "--limit", "3", "--skip", "me"])
        self.assertEqual(search.forward(search.parse(["show", "f", "t"])), ["show", "f", "t"])
        ns = search.parse(["find", "t", "--newest", "--skip", ""])
        self.assertEqual(search.forward(ns)[-1], "--newest")
        self.assertEqual(search.forward(search.parse(["show", "f", "--users"]))[-1], "--users")

    def test_remote_command_is_one_shell_command(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as conf:
            conf.write('machine = "m"\n[store]\nhost = "box"\npath = "/srv/museum"\n')
            conf.flush()
            r = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "museum-search"),
                    "--remote-command",
                    "find",
                    "it's",
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "MUSEUM_CONFIG": conf.name},
                check=True,
            )
        # A POSIX shell parses it back to python3 -c SRC --store ... find it's.
        words = subprocess.run(
            ["sh", "-c", 'eval "set -- $1"; printf "%s\\n" "$1" "$4" "$6" "$7"', "sh", r.stdout],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        self.assertEqual(words, ["python3", "--store", "find", "it's"])


if __name__ == "__main__":
    unittest.main()
