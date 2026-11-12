/**
 * museum: pi's harness hook, and the backup warning in pi's footer.
 *
 * pi has no command hooks, only extensions, so this is pi's `museum hook`: at
 * session start it runs `museum hook pi` with the payload the other harnesses
 * send (hook_event_name, session_id, cwd). museum-sync writes the meta record,
 * the session's repo and mu identity, as it does for claude, codex and
 * cursor-agent, from inside this pi's process tree, which is the only place
 * mu's anchor and environment can be read.
 *
 * Nothing here syncs. `museum tick`, from the timer or tmux, syncs whatever
 * changed, outside every sandbox. So the footer is the one place a backup
 * that stopped shows, and it says so in two cases:
 *
 *   failing  the last sync failed: state.json's `warning`, written by
 *            museum-sync, the one place that rule lives
 *   stale    a change to this session has waited over 30 minutes for a sync
 *            that worked: the timer is not running, or syncs die before they
 *            record anything. Decided here, because then no sync runs to say
 *            it.
 *
 * Every pi on the node shows the node's one state.json, re-read every few
 * seconds, so a recovery any sync sees clears every footer.
 *
 * The hook is the museum-sync beside the `museum` on PATH, the one `museum
 * init` pointed the timer and the other hooks at, so one copy serves the node
 * even when pi's package copy is another version. Without one on PATH, it is
 * ../bin/museum-sync beside this file. $MUSEUM_SYNC overrides both.
 */

import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Declared, not imported, so the extension needs nothing installed beside it.
type Ctx = {
  hasUI: boolean;
  cwd: string;
  ui: { setStatus(key: string, text: string | undefined): void };
  sessionManager: { getSessionId(): string; getSessionFile(): string | undefined };
};
type ExtensionAPI = {
  on(event: "session_start" | "session_shutdown", handler: (event: unknown, ctx: Ctx) => void): void;
};

const POLL_MS = 5000;
export const STALE_S = 30 * 60;
// A working timer syncs a change within MUSEUM_INTERVAL plus its 5-minute
// period; past that and STALE_S, it is stale. museum-sync doctor uses the same.
const interval = Number(process.env.MUSEUM_INTERVAL);
const STALE_AFTER = Math.max(STALE_S, (Number.isFinite(interval) && interval > 0 ? interval : 600) + 600);
const STATE_DIR =
  process.env.MUSEUM_STATE_DIR || join(process.env.XDG_STATE_HOME || join(homedir(), ".local", "state"), "museum");
// 126/127: the shell or env could not run it (no python3 on PATH, say).
const NOT_RUN = new Set([126, 127]);

/** What `museum sync-path` prints: the museum-sync that `museum` runs. */
function askMuseum(museum: string): string | undefined {
  try {
    const r = spawnSync(museum, ["sync-path"], { encoding: "utf8", timeout: 3000 });
    const sync = r.status === 0 ? r.stdout.trim() : "";
    return sync && existsSync(sync) ? sync : undefined;
  } catch {
    return undefined;
  }
}

/**
 * The museum-sync of the first `museum` on PATH: beside it, through its
 * symlinks, or, for a version manager's shim (asdf, volta), whatever the shim
 * runs says. undefined without a `museum` on PATH.
 */
export function besidePathMuseum(
  path: string | undefined = process.env.PATH,
  ask: (museum: string) => string | undefined = askMuseum,
): string | undefined {
  for (const dir of (path ?? "").split(delimiter)) {
    const museum = dir && join(dir, "museum");
    if (!museum || !existsSync(museum)) continue;
    try {
      const sync = join(dirname(realpathSync(museum)), "museum-sync");
      if (existsSync(sync)) return sync;
    } catch {
      // unreadable: ask it
    }
    return ask(museum);
  }
  return undefined;
}

let resolved: string | undefined;

function syncPath(): string {
  if (process.env.MUSEUM_SYNC) return process.env.MUSEUM_SYNC;
  resolved ??=
    besidePathMuseum() ?? join(dirname(realpathSync(fileURLToPath(import.meta.url))), "..", "bin", "museum-sync");
  return resolved;
}

// Set when this pi could not run museum-sync. Cleared by the next run that works.
let notRun: string | undefined;

/** `museum hook pi`, detached, with the payload on stdin: never blocks pi. */
function hook(ctx: Ctx): void {
  const payload = JSON.stringify({
    hook_event_name: "SessionStart",
    session_id: ctx.sessionManager.getSessionId(),
    cwd: ctx.cwd,
  });
  try {
    const child = spawn(syncPath(), ["--hook", "pi"], { detached: true, stdio: ["pipe", "ignore", "ignore"] });
    child.on("error", (e) => (notRun = `museum: cannot run museum-sync: ${e.message}`));
    child.on("exit", (code) => {
      notRun = code !== null && NOT_RUN.has(code) ? `museum: cannot run museum-sync (exit ${code})` : undefined;
    });
    child.stdin?.on("error", () => {});
    child.stdin?.end(payload);
    child.unref();
  } catch (e) {
    notRun = `museum: cannot run museum-sync: ${(e as Error).message}`;
  }
}

/** What `museum-sync` records after each sync, in state.json. */
export type State = {
  status: "ok" | "error";
  time: number; // epoch seconds
  message: string; // the error, empty when ok
  warning: string; // the failing label, empty when ok
  last_ok?: number; // epoch seconds: when the last sync that worked started
};

/** 2m, 3h, 4d. */
export function ago(seconds: number): string {
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  return m < 48 * 60 ? `${Math.floor(m / 60)}h` : `${Math.floor(m / 1440)}d`;
}

/** state.json's fields; undefined when there is none, null when it is not a JSON object. */
function parse(file: string | undefined): State | undefined | null {
  if (file === undefined) return undefined;
  try {
    const state: unknown = JSON.parse(file);
    return state && typeof state === "object" && !Array.isArray(state) ? (state as State) : null;
  } catch {
    return null;
  }
}

/** When the last sync that worked started, 0 for never. */
function lastOkOf(state: State | undefined | null): number {
  // state.json from before last_ok: an ok sync's time is when it finished.
  return state?.last_ok ?? (state?.status === "ok" ? state.time : 0);
}

/**
 * When this pi first saw this session's file changed after the last sync that
 * worked, carried from poll to poll: `since` is the previous answer.
 * undefined while nothing is unsynced.
 */
export function unsyncedSince(
  file: string | undefined,
  changed: number | undefined,
  since: number | undefined,
  now: number = Date.now() / 1000,
): number | undefined {
  const lastOk = lastOkOf(parse(file));
  if (changed === undefined || !lastOk || changed <= lastOk) return undefined;
  return since ?? now;
}

/**
 * The footer label, undefined while backups work. `file` is state.json, which
 * museum-sync writes atomically, so one that does not parse is not a
 * half-written one. `changed` is when this session's file last changed, in
 * epoch seconds, undefined before pi has written it; `since` is
 * unsyncedSince's answer.
 *
 * Stale means a change has waited longer than a working timer lets it, not
 * that the last sync is old: after a break the last sync is hours old and the
 * first new turn is seconds old. The change waited at least since the earlier
 * of its file's mtime and when this pi first saw it unsynced, which also
 * catches a session that writes on and so keeps its mtime fresh.
 */
export function warning(
  file: string | undefined,
  notRun: string | undefined,
  changed: number | undefined,
  now: number = Date.now() / 1000,
  since: number | undefined = undefined,
  staleAfter: number = STALE_S,
): string | undefined {
  if (notRun) return notRun.slice(0, 120);
  const state = parse(file);
  if (state === null) return "museum: state.json does not parse · museum doctor";
  if (state?.warning) return state.warning;
  const lastOk = lastOkOf(state);
  if (!lastOk) return "museum: no backup yet · museum doctor";
  if (changed === undefined || changed <= lastOk) return undefined;
  const waited = now - Math.min(changed, since ?? changed);
  if (waited <= staleAfter) return undefined;
  return `museum: backup stale (last ok ${ago(now - lastOk)} ago) · museum doctor`;
}

function read(name: string): string | undefined {
  try {
    return readFileSync(join(STATE_DIR, name), "utf8");
  } catch {
    return undefined;
  }
}

function mtime(file: string | undefined): number | undefined {
  try {
    return file ? statSync(file).mtimeMs / 1000 : undefined;
  } catch {
    return undefined;
  }
}

export default function museum(pi: ExtensionAPI): void {
  let timer: ReturnType<typeof setInterval> | undefined;
  // null: nothing set yet, so the first show() always sets the status.
  let shown: string | undefined | null = null;
  let since: number | undefined;
  const show = (ctx: Ctx) => {
    const file = read("state.json");
    const changed = mtime(ctx.sessionManager.getSessionFile());
    const now = Date.now() / 1000;
    since = unsyncedSince(file, changed, since, now);
    const text = warning(file, notRun, changed, now, since, STALE_AFTER);
    if (text !== shown) ctx.ui.setStatus("museum", text);
    shown = text;
  };
  const stop = () => {
    clearInterval(timer);
    timer = undefined;
  };
  pi.on("session_start", (_event, ctx) => {
    if (ctx.hasUI) {
      stop();
      shown = null;
      since = undefined;
      show(ctx);
      timer = setInterval(() => show(ctx), POLL_MS);
      timer.unref?.();
    }
    hook(ctx);
  });
  pi.on("session_shutdown", () => stop());
}
