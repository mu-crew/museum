"""museum-search stats and calls against a fixture store with known timings."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from helpers import SCRIPTS, load
from test_search import write_session

search = load("museum-search", SCRIPTS)

WS = "--home-u-.local-state-mu-workspaces-lock-ut-p3-worker-1--"


def at(sec: int) -> str:
    return f"2026-10-20T10:{sec // 60:02d}:{sec % 60:02d}.000Z"


def museum(agent: str | None) -> dict[str, Any]:
    data = {"project": "lock", "workstream": "lock-ut-p3"}
    return {"type": "custom", "customType": "museum", "data": {**data, "agent": agent}}


def say(role: str, sec: int, content: Any, **extra: Any) -> dict[str, Any]:
    return {
        "type": "message",
        "timestamp": at(sec),
        "message": {"role": role, "content": content, **extra},
    }


def call(sec: int, cid: str, command: str, name: str = "bash") -> dict[str, Any]:
    block = {"type": "toolCall", "id": cid, "name": name, "arguments": {"command": command}}
    return say("assistant", sec, [block])


def result(
    sec: int, cid: str, out: str, error: bool = False, ms: int | None = None
) -> dict[str, Any]:
    extra: dict[str, Any] = {"toolCallId": cid, "isError": error}
    if ms is not None:
        extra["durationMs"] = ms
    return say("toolResult", sec, [{"type": "text", "text": out}], **extra)


class Stats(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        # A worker: 10 s of model time, then a 300 s gate that passes, 5 s of
        # model time, a read of the gate script (mentions it, does not run it),
        # a backgrounded gate, and a gate run after a heredoc whose body
        # mentions the gate. durationMs, where recorded, beats the gap.
        write_session(
            self.dir,
            f"pc/{WS}/2026-10-20T10-00-00-000Z_w.jsonl",
            [
                {"type": "session", "cwd": "/w"},
                museum("worker-1"),
                say("user", 0, "Task t1 in workstream lock-ut-p3. Go."),
                call(10, "a", "cd /w && contrib/gate"),
                result(310, "a", "gate: ok"),
                call(315, "b", "cat contrib/gate"),
                result(316, "b", "#!/bin/sh"),
                call(320, "c", "(contrib/gate > /tmp/g.log 2>&1 &); echo started"),
                result(321, "c", "started"),
                call(330, "d", "python3 - <<'EOF'\np='contrib/gate'\nEOF\ncontrib/e2e-sway t"),
                result(400, "d", "FAIL preview", error=True, ms=60000),
                say("assistant", 410, [{"type": "text", "text": "done"}]),
                say("user", 470, "thanks"),
            ],
        )
        # A reviewer, at the same time: its gate overlaps the worker's. Its
        # folder does not say "lock"; its museum entry's project does.
        write_session(
            self.dir,
            "pc/--home-u-wm--/2026-10-20T10-00-05-000Z_r.jsonl",
            [
                museum("reviewer-2"),
                say("user", 5, "You are reviewing task t1 in workstream lock-ut-p3."),
                call(20, "a", "timeout 1800 distrobox enter dev -- bash -c 'cd x && contrib/gate'"),
                result(120, "a", "gate: ok"),
            ],
        )
        # No museum entry, another project: outside "lock" by folder, kept by role.
        write_session(
            self.dir,
            "pc/--home-u-notes--/2026-10-21T08-00-00-000Z_n.jsonl",
            [say("user", 0, "hi"), call(1, "a", "ls", name="read"), result(2, "a", "x")],
        )

    def run_search(self, *args: str) -> str:
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "museum-search"), "--store", str(self.dir), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def calls(self, *args: str) -> list[dict[str, Any]]:
        return json.loads(self.run_search("calls", "lock", "--json", *args))

    def test_time_split(self) -> None:
        roles = json.loads(self.run_search("stats", "lock", "--json"))["roles"]
        w = roles["worker"]
        # model: 10 + 5 + 4 + 9 + 10; tool: 300 + 1 + 1 + 70; wait: the user's 60 s.
        self.assertEqual((w["model"], w["tool"], w["wait"]), (38, 372, 60))
        self.assertEqual((w["sessions"], w["calls"]), (1, 4))
        self.assertEqual(roles["reviewer"]["tool"], 100)

    def test_project_keeps_workspace_sessions_and_drops_others(self) -> None:
        roles = json.loads(self.run_search("stats", "lock", "--json"))["roles"]
        self.assertEqual(sorted(roles), ["reviewer", "worker"])
        everything = json.loads(self.run_search("stats", "--json"))["roles"]
        self.assertEqual(everything["other"]["sessions"], 1)

    def test_match_tests_segments_and_skips_heredoc_bodies(self) -> None:
        got = self.calls("--match", "contrib/gate", "--exclude", r"^\s*cat\b", "--sort", "time")
        self.assertEqual(
            [c["command"][:12] for c in got], ["cd /w && con", "timeout 1800", "(contrib/gat"]
        )
        # The heredoc body mentions the gate; only the e2e line after it runs.
        self.assertEqual(self.calls("--match", "contrib/gate", "--agent", "worker-*", "--min-secs", "50"),
                         [self.calls("--match", "contrib/gate", "--min-secs", "200")[0]])  # fmt: skip
        self.assertEqual(len(self.calls("--match", "e2e-sway")), 1)

    def test_shell_heredoc_bodies_remain_matchable(self) -> None:
        for n, command in enumerate(
            [
                "bash <<'EOF'\ncontrib/gate\nEOF",
                "sh -s <<EOF\ncd /w && contrib/gate\nEOF",
                "env bash <<-EOF\n\tcontrib/gate\nEOF",
            ]
        ):
            write_session(
                self.dir,
                f"pc/--lock--/2026-10-20T10-00-00-000Z_shell{n}.jsonl",
                [museum("shell-1"), call(10, "a", command), result(20, "a", "ok")],
            )
        self.assertEqual(len(self.calls("--agent", "shell-1", "--match", r"^\s*contrib/gate")), 3)
        self.assertEqual(self.calls("--agent", "worker-1", "--match", r"^p="), [])

    def test_duration_prefers_duration_ms(self) -> None:
        (e2e,) = self.calls("--match", "e2e-sway")
        self.assertEqual((e2e["secs"], e2e["error"]), (60, True))

    def test_shared_request_timestamp_durations_are_labelled_not_sequenced(self) -> None:
        request = call(10, "a", "first")
        request["message"]["content"] += call(10, "b", "second")["message"]["content"]
        request["message"]["content"] += call(10, "c", "third")["message"]["content"]
        write_session(
            self.dir,
            "pc/--lock--/2026-10-20T10-00-00-000Z_shared.jsonl",
            [
                museum("shared-1"),
                say("user", 0, "go"),
                request,
                result(20, "a", "ok", ms=10000),
                result(30, "b", "ok", ms=10000),
                result(40, "c", "ok"),
            ],
        )
        rows = self.calls("--agent", "shared-1", "--sort", "time")
        self.assertEqual([r["secs"] for r in rows], [10, 10, 30])
        self.assertEqual(
            [r.get("duration_source") for r in rows], ["durationMs", "durationMs", "estimated"]
        )
        self.assertEqual({r["start"] for r in rows}, {"2026-10-20T10:00:10"})

    def test_standalone_exclude_ignores_empty_shell_segments(self) -> None:
        ns = search.parse(["calls", "--exclude", r"^\s*cat\b"])
        import re

        flt = search.Filters(
            None, re.compile(ns.exclude), set(), None, None, None, False, None, None, None, None
        )
        for command in (
            "cat contrib/gate",
            "cat contrib/gate\n",
            "cat contrib/gate;",
            "cat contrib/gate;  \n",
        ):
            with self.subTest(command=command):
                self.assertFalse(flt.command("bash", command))
        self.assertTrue(flt.command("bash", "cat contrib/gate; contrib/gate"))

    def test_outcome_filters(self) -> None:
        self.assertEqual(len(self.calls("--result", "gate: ok")), 2)
        self.assertEqual(len(self.calls("--error")), 1)
        (bg,) = self.calls("--background")
        self.assertTrue(bg["command"].startswith("(contrib/gate"))
        self.assertEqual(len(self.calls("--foreground")), 4)
        self.assertEqual(len(self.calls("--max-secs", "2")), 2)
        self.assertEqual({c["role"] for c in self.calls("--role", "rev*")}, {"reviewer"})
        self.assertEqual(self.calls("--tool", "read"), [])

    def test_background_launch_heuristic_respects_quotes_and_wait(self) -> None:
        commands = [
            ('echo "& >"', False),
            ("echo '&' > /tmp/log", False),
            ('echo ">" &', False),
            (r"echo \& \>", False),
            ("gate > /tmp/log 2>&1 & wait", False),
            ("gate > /tmp/log &\nwait $!", False),
            ("gate > /tmp/log & echo 'wait'", True),
            ("gate > /tmp/log &", True),
            ("gate > /tmp/log && echo done", False),
            ("gate &> /tmp/log", False),
        ]
        for n, (command, _) in enumerate(commands):
            write_session(
                self.dir,
                f"pc/--lock--/2026-10-20T10-00-00-000Z_bg{n}.jsonl",
                [museum("launch-1"), call(n, "a", command), result(n + 1, "a", "ok")],
            )
        rows = self.calls("--agent", "launch-1", "--sort", "time")
        self.assertEqual([r["background"] for r in rows], [want for _, want in commands])
        self.assertEqual(len(self.calls("--agent", "launch-1", "--foreground")), 8)

    def test_group_by(self) -> None:
        roles = {g["key"]: g for g in self.calls("--match", "contrib/gate", "--group-by", "role")}
        self.assertEqual((roles["worker"]["calls"], roles["worker"]["background"]), (3, 1))
        (session,) = self.calls("--agent", "reviewer-2", "--group-by", "session")
        self.assertIn("You are reviewing task t1", session["first"])
        self.assertTrue(session["key"].endswith("_r.jsonl"))

    def test_trend_does_not_report_unreliable_concurrency(self) -> None:
        trend = json.loads(self.run_search("stats", "lock", "--min-secs", "60", "--json"))["trend"]
        (day,) = trend.values()
        self.assertEqual(day["matched"], 3)
        self.assertNotIn("concurrent", day)
        self.assertNotIn("concurrent", self.run_search("stats", "lock"))

    def test_wall_spans_split_across_window_boundaries(self) -> None:
        for by, before, after, left, right in (
            (
                "hour",
                "2026-10-20T10:59:50",
                "2026-10-20T11:00:00",
                "2026-10-20 10h",
                "2026-10-20 11h",
            ),
            (
                "6h",
                "2026-10-20T11:59:50",
                "2026-10-20T12:00:00",
                "2026-10-20 06h",
                "2026-10-20 12h",
            ),
            ("day", "2026-10-20T23:59:50", "2026-10-21T00:00:00", "2026-10-20", "2026-10-21"),
        ):
            with self.subTest(by=by):
                messages = [say("user", 0, "go"), call(10, "a", "gate"), result(20, "a", "ok")]
                messages[0]["timestamp"] = before + "Z"
                messages[1]["timestamp"] = after[:17] + "10Z"
                messages[2]["timestamp"] = after[:17] + "30Z"
                write_session(
                    self.dir,
                    "pc/--lock--/2026-10-20T10-00-00-000Z_boundary.jsonl",
                    [museum("boundary-1"), *messages],
                )
                report = json.loads(
                    self.run_search("stats", "lock", "--agent", "boundary-1", "--by", by, "--json")
                )
                trend = report["trend"]
                self.assertIn(right, trend)
                self.assertEqual((trend[left]["model"], trend[right]["model"]), (10, 10))
                self.assertEqual((trend[left]["tool"], trend[right]["tool"]), (0, 20))
                self.assertEqual((trend[left]["sessions"], trend[right]["sessions"]), (1, 0))
                self.assertEqual((trend[left]["matched"], trend[right]["matched"]), (0, 1))
                self.assertEqual(report["roles"]["boundary"]["model"], 20)
                # Date filters select session-start cohorts, not event-time slices.
                cohort = json.loads(
                    self.run_search(
                        "stats",
                        "lock",
                        "--agent",
                        "boundary-1",
                        "--until",
                        "2026-10-20",
                        "--by",
                        by,
                        "--json",
                    )
                )
                self.assertEqual(cohort, report)

    def test_tool_wall_span_split_across_midnight(self) -> None:
        messages = [say("user", 0, "go"), call(10, "a", "gate"), result(20, "a", "ok")]
        for message, ts in zip(
            messages,
            ["2026-10-20T23:59:40", "2026-10-20T23:59:50", "2026-10-21T00:00:10"],
            strict=True,
        ):
            message["timestamp"] = ts + "Z"
        write_session(
            self.dir,
            "pc/--lock--/2026-10-20T10-00-00-000Z_toolboundary.jsonl",
            [museum("boundary-1"), *messages],
        )
        trend = json.loads(self.run_search("stats", "lock", "--agent", "boundary-1", "--json"))[
            "trend"
        ]
        self.assertEqual([v["tool"] for v in trend.values()], [10, 10])
        self.assertEqual([v["matched_secs"] for v in trend.values()], [20, 0])

    def test_even_median_is_average_of_middle_values(self) -> None:
        report = json.loads(self.run_search("stats", "lock", "--agent", "worker-1", "--json"))
        (day,) = report["trend"].values()
        # Durations sorted: 1, 1, 60, 300. Median = (1 + 60) / 2.
        self.assertEqual(day["median_secs"], 30.5)
        self.assertEqual(day["p90_secs"], 300)
        (group,) = self.calls("--agent", "worker-1", "--group-by", "role")
        self.assertEqual(group["median_secs"], 30.5)
        # Nine values distinguish the p90 upper-index convention from nearest-rank.
        self.assertEqual(search.quantile([1, 2, 3, 4, 5, 6, 7, 8, 9], 0.9), 9)

    def test_empty_stats_json_keeps_report_schema(self) -> None:
        out = self.run_search("stats", "absent-project", "--json")
        try:
            report = json.loads(out)
        except ValueError:
            self.fail(f"empty stats did not emit JSON: {out!r}")
        self.assertEqual(report, {"roles": {}, "trend": {}, "slow": []})
        self.assertIn("no sessions match", self.run_search("stats", "absent-project"))

    def test_empty_group_is_a_valid_report(self) -> None:
        out = self.run_search("calls", "lock", "--match", "absent", "--group-by", "role")
        self.assertIn("0 calls in 0 groups", out)

    def test_bad_regex_is_a_cli_error(self) -> None:
        r = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "museum-search"),
                "--store",
                str(self.dir),
                "stats",
                "--match",
                "[",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--match", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_text_output_and_slow(self) -> None:
        out = self.run_search("stats", "lock", "--slow", "1")
        self.assertIn("role", out)
        self.assertIn("slowest 1 matched calls", out)
        self.assertIn("300s  bash  worker-1", out)

    def test_since_until(self) -> None:
        self.assertEqual(json.loads(self.run_search("stats", "--since", "2026-10-21", "--json"))["roles"].keys(),
                         {"other"})  # fmt: skip
        self.assertNotIn(
            "other",
            json.loads(self.run_search("stats", "--until", "2026-10-20", "--json"))["roles"],
        )


class Units(unittest.TestCase):
    def test_segments(self) -> None:
        self.assertEqual(
            search.segments('a && b; c || d\ne "x && y"'), ["a ", " b", " c ", " d", 'e "x && y"']
        )
        self.assertEqual(search.segments("p <<'E'\nbody; x\nE\nq"), ["p <<'E'", "q"])

    def test_text_table_runs_with_pre310_zip(self) -> None:
        def old_zip(*iterables: Any) -> Any:
            return zip(*iterables)  # noqa: B905 - emulate pre-3.10 zip

        with (
            patch.object(search, "zip", old_zip, create=True),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            try:
                search.print_table(["role", "calls"], [["worker", 2]])
            except TypeError as exc:
                self.fail(f"table requires Python 3.10 zip: {exc}")
        self.assertIn("worker", output.getvalue())
        self.assertIn("2", output.getvalue())

    def test_role(self) -> None:
        self.assertEqual(search.role_of({"agent": "delegate-ref-x"}), "delegate")
        self.assertEqual(search.role_of({"agent": "worker-12"}), "worker")
        self.assertEqual(search.role_of({"project": "p"}), "main")
        self.assertEqual(search.role_of({}), "other")

    def test_forward_round_trips(self) -> None:
        argv = [
            "calls",
            "lock",
            "--match",
            "a b",
            "--min-secs",
            "3",
            "--background",
            "--group-by",
            "role",
        ]
        words = search.forward(search.parse(argv))
        self.assertEqual(words[:6], ["calls", "lock", "--match", "a b", "--min-secs", "3.0"])
        self.assertIn("--background", words)
        self.assertEqual(search.forward(search.parse(words)), words)
        stats = search.forward(search.parse(["stats", "--by", "6h", "--error"]))
        self.assertEqual(search.parse(stats).by, "6h")
        self.assertTrue(search.parse(stats).error)


if __name__ == "__main__":
    unittest.main()
