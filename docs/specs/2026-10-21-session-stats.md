# `museum-search stats` and `calls`: where agent time goes

Status: implemented in `cb222a7`; Sol review findings addressed and release
gate verified. The clarifications below supersede the initial design where
the archive cannot support the proposed measurement.

## Implementation clarifications

- Timing categories are observed gaps between message entries: model before
  assistant messages, tool before tool results, waiting before other messages.
  They are harness wall-time estimates, not provider-only inference latency.
  Model/tool wall spans split across UTC window boundaries. Call rows carry
  `duration_source`: `durationMs` for recorded measurements, `estimated` for
  call-to-result gaps (marked in text too). Calls in one assistant message
  share a request timestamp, not an execution start. Estimates may include
  earlier calls; sums are not tool wall time. No execution sequence, parallelism
  or concurrency can be inferred or is reported.
- `--since`/`--until` select session-start date cohorts by filename, not
  event-time slices. Selected sessions can contribute wall time to later
  windows. Session counts use the first message window; call counts and
  duration summaries stay in the request timestamp window.
- Roles use the leading word of the metadata agent name. A metadata entry with
  no agent is `main`; a session without metadata is `other`.
- Shell matching retains simple bash/sh heredoc bodies (optionally via env),
  suppresses Python/data bodies, and splits separators outside quotes.
  Background is a launch heuristic: unquoted lone `&` plus redirect, unless
  followed by explicit `wait`. Quoted operators do not count. This is not a
  shell parser: wrappers, nested/multiple heredocs, multiline quotes and
  conditional waits can be misclassified. Restrict to bash and inspect
  examples; text matches do not prove execution.
- Median uses `statistics.median`, averaging the middle pair for even counts.
  P90 separately uses sorted index floor(n × 0.9), capped at the last value.
  Empty `stats --json` is `{"roles": {}, "trend": {}, "slow": []}`.
- `--result` filters result text; `--error` counts the tool's `isError` flag.
  Matching `gate: ok` is not a general pass classifier, and absence of the
  marker is not a failure. Group output therefore reports errors, not inferred
  pass/fail labels.
- A session is not a task identifier. Grouping by session is useful for drilling
  down, but repeated/resumed sessions and multiple tasks need separate accounting.
- The original `/tmp` analysis included false-positive gate mentions. Reproduce
  its questions and correct its figures, rather than requiring exact agreement.
Date: 2026-10-21

## Problem

Questions like "why is this crew getting slower?" need numbers that
`find` and `show` cannot give: how long tool calls take, how much time
goes to the model's turns versus tools, and how that changes over days
and by agent role. The swaywardlock gate analysis on 2026-10-21 took
throwaway Python scripts run over ssh on the store host, plus a lot of
guessing an agent's role from its first prompt. The same question about
mu will come up, and so will others.

## Approach

Two subcommands in `skills/museum/scripts/museum-search`, `stats` and
`calls`. They reuse
the config loading and the single-ssh-call remote path that `find` and
`show` already have, so it runs where the files are. Python standard
library only, like the rest of the script.

```
museum-search stats [PROJECT] [--since DATE] [--until DATE]
                    [CALL FILTERS] [--by day|6h|hour] [--slow N] [--json]
museum-search calls [PROJECT] [--since DATE] [--until DATE]
                    [CALL FILTERS] [--group-by session|agent|workstream|role]
                    [--sort secs|time] [--limit N] [--json]
```

`stats` gives the aggregate view, and `calls` drills into the calls
behind it. Both take the same call filters, so a filter tried in `calls`
can be pasted into `stats` unchanged.

- `PROJECT` narrows to store folders whose name contains it, as in
  `find`. The `museum` entry's `project` field also counts, so mu
  worktree folders are included.
- `CALL FILTERS` limit the tool-call figures to matching calls; see
  [Call filters](#call-filters).
- `--by` sets the time window for the trend table.
- `--slow N` lists the N slowest tool calls, with time, role, session
  file and the start of the command.
- `--json` prints the raw rows, so other analysis does not need new
  scripts.

### Call filters

These are the cuts the swaywardlock gate analysis had to write by hand
(`/tmp/gs3.py`). A call must pass every filter given.

| Filter | Selects | Why it is needed |
|---|---|---|
| `--match REGEX` | the command or arguments match | pick the calls in question, e.g. `contrib/gate\|cargo test` |
| `--exclude REGEX` | the command does **not** match | drop calls that only mention the term, e.g. `^\s*(cat\|sed\|grep\|rg\|head\|tail\|wc)\b` for gate scripts being read rather than run |
| `--tool NAME[,NAME]` | the tool name (`bash`, `codemode`, `read`, …) | separate shell work from file reads |
| `--role ROLE`, `--agent GLOB`, `--workstream GLOB` | the session's `museum` fields | e.g. workers only, or `swaywardlock-ut-*` |
| `--min-secs N`, `--max-secs N` | call duration | drop instant calls, or find the polls |
| `--result REGEX` | the tool result text matches | `gate: ok` vs `FAIL\|panicked`, to count passes and failures |
| `--error` | calls whose result is an error | failed tool calls only |
| `--background` / `--foreground` | launch heuristic: unquoted lone `&` plus redirect, with no explicit later `wait` | inspect later polls; not proof of a running background job |

### `calls` output

With no `--group-by`, one row per call: request time, duration and
`duration_source`, tool, role, agent, workstream, the tool's error flag,
a background-launch heuristic flag, the session file, and a command preview. `--sort`
is by duration (the default) or by start time. `--limit` defaults to 50.

`--group-by` adds up the calls per session, agent, workstream or role:
count, total and median duration, error count, and the first user
message of the session (for `session`). Grouping by session shows where matches cluster; inspect its first user
message to identify the task rather than assuming one session equals one task.

Each session row includes the file path, so the next step is
`museum-search show FILE TERM`.

## What it measures

Per session, from the timestamps of its entries:
- **model time:** message gap ending at an assistant message (harness
  estimate, including streaming, not provider-only inference)
- **tool wall time:** message gap ending at a tool result, counted once
- **waiting time:** all other message gaps (excluded from the other two)
- the number of tool calls, and the calls the call filters selected,
  with their durations

## Output

Three plain-text tables:

1. **Totals by role:** sessions, tool calls, model hours, tool hours,
   share of the session that is tool time, and the count and hours of
   the calls the filters selected.
2. **Trend:** one row per window. Sessions, tool calls, matched calls,
   matched median and p90 duration, and the model-time / tool-time
   split.
3. **Slowest calls** (with `--slow`): the top rows of
   `calls --sort secs`, with the same columns, so `--slow` is a preview
   and `calls` does the drilling down.

The trend table does not report concurrency: request timestamps and
durations do not establish reliable execution intervals. Only model/tool
wall spans are split at window boundaries; matched call durations and
percentiles belong to the request window.

## Decisions

- **Role comes from data, not guessing.** Use the `museum` entry's
  `agent` field (worker-N, reviewer-N, fixer-N, delegate-…) and
  `workstream` field. Sessions without one are grouped as `other`. The
  first-prompt heuristics from the throwaway scripts are not carried
  over.
- **Cheap enough for multi-GB stores.** Stream each file line by line.
  Look only at `session`, `custom/museum` and `message` entries, and
  keep only timestamps, roles, tool names and the first 300 characters
  of each command. Select files by folder and by start time (taken from
  the file name) before opening any.
- **A background launch may show up as quick calls.** A gate started with
  `&` without `wait` may return quickly; inspect later calls that poll its
  log. `--match` catches those polls too when the pattern names the log,
  so this is documented rather than worked around.

## Testing

`test/test_search.py` style: small JSONL fixtures with known timestamps.
Check the model / tool / user-wait split, each call filter (including
`--exclude` against a read-only command that mentions the match term,
and `--background`), shell versus data heredocs, quoted operators and
explicit waits, `calls --group-by`, duration sources on shared requests,
absence of concurrency, wall-span window splitting, even-count medians,
pre-3.10 zip compatibility, role fallback, and empty/nonempty JSON reports.

## Out of scope

Charts, an index or cache, and cost or token accounting (sessions do
carry `usage`, so token use could be added later).

## Rollout

Build it, then reproduce the swaywardlock numbers in
`mu/docs/specs/2026-10-21-fast-and-slow-gates.md` with one command per
table. The filter that makes `contrib/gate` count only real runs is long,
so it goes in a variable:

```
G='(distrobox enter|timeout [0-9]+) .*contrib/gate|(^|[;&(]) *contrib/gate'
R='^\s*(cat|sed|grep|rg|head|tail|wc)\b'
S=skills/museum/scripts/museum-search

# full gate runs and how long they take, per 6 hours
$S stats swayward --match "$G" --exclude "$R" --min-secs 30 --by 6h

# full gate runs per task, split by role
$S calls swayward --match "$G" --exclude "$R" --group-by session

# pass and fail counts
$S calls swayward --match "$G" --result 'gate: ok' --group-by role
$S calls swayward --match "$G" --result 'FAIL|panicked' --group-by role

# what workers spend polling background gates
$S calls swayward --role worker --match 'gate.*\.log' --group-by session
``` Investigate mismatches: the archive changes as sessions sync, and the
exploratory scripts counted some command mentions as executions.
Then update the museum SKILL.md with a short "Stats" section.
