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

from helpers import BIN, load

search = load("museum-search")

HAIL = "--home-u-hacking-hail--"
WORKER = "--home-u-.local-state-mu-workspaces-hail-worker-1--"


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

    def run_search(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        if not self.rg:
            # A PATH with python3 but no rg exercises the pure-Python scan.
            env["PATH"] = str(Path(sys.executable).parent)
        return subprocess.run(
            [sys.executable, str(BIN / "museum-search"), "--store", str(self.dir), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_find_ranks_projects_first_then_newest(self) -> None:
        out = self.run_search("find", "STALWART").stdout
        files = [line.split()[1] for line in out.splitlines() if line.strip().startswith("file:")]
        self.assertEqual(
            files,
            [
                f"pc/{HAIL}/2026-06-07T09-42-00-000Z_b.jsonl",
                f"pc/{HAIL}/2026-05-22T16-32-24-344Z_a.jsonl",
                f"pc/{WORKER}/2026-06-08T00-00-00-000Z_c.jsonl",
            ],
        )
        self.assertIn("3 sessions mention", out)

    def test_find_block_prefers_conversation_text_and_metadata(self) -> None:
        out = self.run_search("find", "stalwart", "hail", "--limit", "1").stdout
        self.assertIn("2026-06-07 09:42  pc  hail@main  (3 matching entries)", out)
        self.assertIn("first: look at this repo", out)
        self.assertIn("match: Hail runs on Stalwart + JMAP.", out)
        self.assertNotIn("table", out)
        self.assertIn("showing 1", out)

    def test_find_labels_folders_without_metadata(self) -> None:
        out = self.run_search("find", "fix stalwart").stdout
        self.assertIn("pc  mu:hail-worker-1  (1 matching entries)", out)

    def test_find_matches_json_escaped_and_unicode_terms(self) -> None:
        for term in ['"it\'s done"', "\\ café", "CAFÉ"]:
            with self.subTest(term=term):
                self.assertIn("1 sessions mention", self.run_search("find", term).stdout)

    def test_find_misses(self) -> None:
        self.assertIn("no session mentions", self.run_search("find", "zzqq").stdout)
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

    def test_forward_carries_the_limit(self) -> None:
        ns = search.parse(["find", "t", "--limit", "3"])
        self.assertEqual(search.forward(ns), ["find", "t", "--limit", "3"])
        self.assertEqual(search.forward(search.parse(["show", "f", "t"])), ["show", "f", "t"])

    def test_remote_command_is_one_shell_command(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as conf:
            conf.write("STORE=box:/srv/museum\n")
            conf.flush()
            r = subprocess.run(
                [sys.executable, str(BIN / "museum-search"), "--remote-command", "find", "it's"],
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
