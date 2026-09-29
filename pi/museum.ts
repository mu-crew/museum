/**
 * museum: back up this node's pi sessions from inside pi.
 *
 * Replaces cron/launchd. Sessions only change while pi runs, and pi runs in the
 * pane's own environment (ssh agent, PATH), which a scheduler does not have.
 *
 *   session_start     catch up on what a crashed or killed pi left, if due
 *   agent_settled     sync if the node's last sync started over 10 minutes ago
 *   session_shutdown  flush: sync this session's final turns now
 *
 * All the coordination is in `museum-sync`: one sync per node however many
 * agents call it, and a flush that arrives mid-sync re-runs it. This file only
 * spawns it detached (never blocking a turn, surviving pi's exit) and shows a
 * footer warning when the last backup failed or is old.
 *
 * Installed by museum's install.sh as a symlink to the repo, so MUSEUM_SYNC
 * resolves next to it; override with $MUSEUM_SYNC.
 */
import { spawn } from "node:child_process";
import { readFileSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Declared, not imported, so the extension needs nothing installed beside it.
type Ctx = { hasUI: boolean; ui: { setStatus(key: string, text: string | undefined): void } };
type ExtensionAPI = {
  on(
    event: "session_start" | "agent_settled" | "session_shutdown",
    handler: (event: unknown, ctx: Ctx) => void,
  ): void;
};

const INTERVAL_S = Number(process.env.MUSEUM_INTERVAL) || 600;
const STALE_S = 24 * 60 * 60;
const STATE_DIR = process.env.MUSEUM_STATE_DIR || join(homedir(), ".cache", "museum");

function syncPath(): string {
  if (process.env.MUSEUM_SYNC) return process.env.MUSEUM_SYNC;
  const here = dirname(realpathSync(fileURLToPath(import.meta.url)));
  return join(here, "..", "bin", "museum-sync");
}

function read(name: string): string {
  try {
    return readFileSync(join(STATE_DIR, name), "utf8");
  } catch {
    return "";
  }
}

function run(mode: "--if-due" | "--now"): void {
  // Cheap pre-check, so ten agents settling does not fork ten shells. The
  // script re-checks under its lock; this is only an optimisation.
  if (mode === "--if-due") {
    const last = Number(read("last-start").trim()) || 0;
    if (Date.now() / 1000 - last < INTERVAL_S) return;
  }
  try {
    const child = spawn(syncPath(), [mode], { detached: true, stdio: "ignore" });
    child.on("error", () => {});
    child.unref();
  } catch {
    // A missing script shows up as a stale backup in the footer.
  }
}

/** One footer line when something is wrong, nothing when all is well. */
export function warning(state: string, lastStart: string, now: number): string | undefined {
  const field = (key: string) => state.match(new RegExp(`^${key}=(.*)$`, "m"))?.[1] ?? "";
  const status = field("status");
  const message = field("message");
  const okAt = status === "ok" ? Number(field("time")) : 0;
  if (!status && !lastStart.trim()) return "museum: no backup yet";
  if (status === "error") return `museum: backup failing: ${message}`.slice(0, 120);
  const age = now - okAt;
  if (age > STALE_S) return `museum: no backup for ${Math.floor(age / 86400)}d`;
  return undefined;
}

export default function museum(pi: ExtensionAPI): void {
  const show = (ctx: Ctx) => {
    if (!ctx.hasUI) return;
    ctx.ui.setStatus("museum", warning(read("state"), read("last-start"), Date.now() / 1000));
  };
  // --if-due, not --now: mu starts agents in batches, and each start forcing
  // a sync would turn a crew launch into a sync storm for nothing.
  pi.on("session_start", (_event, ctx) => {
    show(ctx);
    run("--if-due");
  });
  pi.on("agent_settled", (_event, ctx) => {
    show(ctx);
    run("--if-due");
  });
  pi.on("session_shutdown", () => run("--now"));
}
