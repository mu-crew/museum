"""museum-search against a small fixture store, run as a real subprocess."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
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
    index: bool = True

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
        env.pop("MUSEUM_NO_INDEX", None)
        if not self.index:
            env["MUSEUM_NO_INDEX"] = "1"
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

    def test_find_sees_turns_appended_since_the_last_search(self) -> None:
        rel = f"pc/{HAIL}/2026-05-22T16-32-24-344Z_a.jsonl"
        self.assertIn("no session mentions", self.run_search("find", "quokka").stdout)
        with (self.dir / rel).open("a") as f:
            f.write(json.dumps(msg("user", "and the quokka?")) + "\n")
        self.assertEqual(self.files(self.run_search("find", "quokka").stdout), [rel])

    def test_find_forgets_a_replaced_file(self) -> None:
        rel = f"pc/{HAIL}/2026-05-22T16-32-24-344Z_a.jsonl"
        path = self.dir / rel
        self.assertIn(rel, self.run_search("find", "hey.com").stdout)
        # The same size, so only the head hash tells it from the old file.
        before = path.read_text()
        path.write_text(before.replace("hey.com", "wombatx"))
        self.assertEqual(len(path.read_text()), len(before))
        self.assertIn("no session mentions", self.run_search("find", "hey.com").stdout)
        # Larger, and different from the first line on.
        write_session(self.dir, rel, [msg("user", "a different start, much longer " * 9)])
        self.assertIn("no session mentions", self.run_search("find", "wombatx").stdout)

    def test_find_rereads_a_changed_claude_session(self) -> None:
        rel = "pc/claude/-home-u-proj/11111111-2222-3333-4444-555555555555.jsonl"

        def turn(mid: str, words: str) -> dict[str, Any]:
            content = [{"type": "text", "text": words}]
            message = {"id": mid, "role": "assistant", "content": content}
            return {"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "message": message}

        # A first line over HEAD bytes, so a grown file keeps its head hash.
        first = turn("m1", "about the numbat " + "x" * search.HEAD)
        write_session(self.dir, rel, [first])
        self.assertIn(rel, self.run_search("find", "numbat").stdout)
        # Grown, but read again whole: claude's lines are merged into turns.
        write_session(self.dir, rel, [first, turn("m2", "the bilby")])
        self.assertIn("1 sessions in conversation", self.run_search("find", "bilby").stdout)
        if self.index:  # from the index, not from the scan of the tool tier
            self.assertEqual(self.indexed_turns("bilby"), 1)
            self.assertEqual(self.indexed_turns("numbat"), 1)  # not added twice
        # claude's staged copy is filtered again from scratch: not an append.
        write_session(self.dir, rel, [turn("m1", "about the quoll")])
        self.assertIn("no session mentions", self.run_search("find", "numbat").stdout)

    def test_index_prunes_deleted_files_but_not_outside_a_project(self) -> None:
        if not self.index:
            self.skipTest("no index")
        self.run_search("find", "stalwart")
        indexed = self.indexed()
        self.run_search("find", "stalwart", "notes")  # narrowed: prunes nothing
        self.assertEqual(self.indexed(), indexed)
        (self.dir / f"pc/{HAIL}/2026-05-22T16-32-24-344Z_a.jsonl").unlink()
        self.run_search("index")
        self.assertEqual(self.indexed(), indexed - 1)

    def test_find_reads_the_files_when_the_index_is_unusable(self) -> None:
        tally = '"stalwart": 3 sessions in conversation, 1 only in tool output.'
        db = self.dir / search.TURNS
        db.write_text("not a database")
        self.assertIn(tally, self.run_search("find", "stalwart").stdout)
        db.unlink()
        if not self.rg:
            return  # the scan without rg stops at an unreadable file too
        # A session the index cannot read fails its update, not the search.
        notes = self.dir / "mac/--Users-u-notes--/2026-07-01T00-00-00-000Z_d.jsonl"
        notes.chmod(0)
        self.addCleanup(notes.chmod, 0o644)
        self.assertIn(tally, self.run_search("find", "stalwart").stdout)

    def test_find_leaves_a_newer_museums_index_alone(self) -> None:
        db = self.dir / search.TURNS
        con = sqlite3.connect(db)
        con.execute(f"PRAGMA user_version={search.TURNS_VERSION + 1}")
        con.close()
        out = self.run_search("find", "stalwart").stdout
        self.assertIn('"stalwart": 3 sessions in conversation, 1 only in tool output.', out)
        con = sqlite3.connect(db)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], search.TURNS_VERSION + 1)
        con.close()

    def test_machines_lists_each_machines_version(self) -> None:
        def mark(machine: str, name: str, doc: dict[str, str], mtime: int) -> None:
            path = self.dir / machine / ".museum" / f"{name}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(doc))
            os.utime(path, (mtime, mtime))

        mark("pc", "0.9.0", {"version": "0.9.0", "source": "npm"}, 1_000)
        mark("pc", "0.10.0", {"version": "0.10.0", "source": "npm"}, 2_000)
        mark("mac", "0.9.1+abc", {"version": "0.9.1", "commit": "abc", "source": "checkout"}, 9)
        write_session(self.dir, "old/--x--/2026-01-01T00-00-00-000Z_o.jsonl", [msg("user", "hi")])
        out = self.run_search("machines")
        self.assertEqual(out.returncode, 0, out.stderr)
        rows = {line.split()[0]: line for line in out.stdout.splitlines()[1:] if line.strip()}
        # The newest marker counts, and versions order as numbers.
        self.assertIn("0.10.0 (npm)", rows["pc"])
        self.assertNotIn("behind", rows["pc"])
        self.assertIn("0.9.1+abc (checkout)", rows["mac"])
        self.assertTrue(rows["mac"].rstrip().endswith("behind"))
        self.assertIn("no version reported", rows["old"])
        self.assertIn("npm update -g @mu-crew/museum && museum init", out.stdout)

    def indexed_turns(self, term: str) -> int:
        db = sqlite3.connect(self.dir / search.TURNS)
        try:
            sql = "SELECT count(*) FROM fts WHERE fts MATCH ?"
            return db.execute(sql, (f'"{term}"',)).fetchone()[0]
        finally:
            db.close()

    def indexed(self) -> int:
        db = sqlite3.connect(self.dir / search.TURNS)
        try:
            return db.execute("SELECT count(*) FROM file").fetchone()[0]
        finally:
            db.close()

    def test_find_skips_the_tool_tier_when_conversation_fills_the_listing(self) -> None:
        out = self.run_search("find", "stalwart", "--limit", "1").stdout
        tally = "tool output not searched" if self.index else "1 only in tool output"
        self.assertIn(f'"stalwart": 3 sessions in conversation, {tally}.', out)

    def test_index_command_builds_what_find_reads(self) -> None:
        r = self.run_search("index")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual((self.dir / search.TURNS).exists(), self.index)
        if self.index:
            self.assertEqual(self.indexed_turns("stalwart"), 4)  # a, b, c and SELF

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


@unittest.skipUnless(shutil.which("rg"), "rg not installed")
class StoreWithoutIndex(Store):
    index = False


del Store  # only the concrete variants run


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
        self.assertEqual(search.forward(ns), ["find", "--limit", "3", "--skip", "me", "--", "t"])
        self.assertEqual(search.forward(search.parse(["show", "f", "t"])), ["show", "--", "f", "t"])
        ns = search.parse(["find", "t", "--newest", "--skip", ""])
        self.assertIn("--newest", search.forward(ns))
        words = search.forward(search.parse(["show", "f", "--users"]))
        self.assertEqual(words, ["show", "--users", "--", "f"])
        self.assertEqual(search.forward(search.parse(["index"])), ["index"])
        self.assertEqual(search.forward(search.parse(["machines"])), ["machines"])

    def test_forward_puts_dashed_positionals_after_double_dash(self) -> None:
        """claude's and codex's project folders start with -: -home reads as -h."""
        for argv in (
            ["find", "--", "-t", "-home-u-proj"],
            ["stats", "--json", "--", "-home-u-proj"],
            ["calls", "--group-by", "role", "--", "-home-u-proj"],
            ["show", "--", "-f.jsonl", "-t"],
        ):
            with self.subTest(argv=argv):
                ns = search.parse(argv)
                again = search.parse(search.forward(ns))
                self.assertEqual(vars(again), vars(ns))

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
        # A POSIX shell parses it back to python3 -c SRC --store ... find ... -- it's.
        words = subprocess.run(
            [
                "sh",
                "-c",
                'eval "set -- $1"; a=$1; shift 3; printf "%s\\n" "$a" "$@"',
                "sh",
                r.stdout,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        self.assertEqual([words[0], words[1], words[3]], ["python3", "--store", "find"])
        self.assertEqual(words[-2:], ["--", "it's"])


if __name__ == "__main__":
    unittest.main()
