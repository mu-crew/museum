/**
 * museum: back up this node's pi sessions from inside pi.
 *
 * Sessions only change while pi runs, so pi triggers the backup, in the pane's
 * own environment (ssh agent, PATH).
 *
 *   session_start     catch up on what a crashed or killed pi left, if due
 *   agent_settled     sync if the node's last sync started over 10 minutes ago
 *   session_shutdown  flush: sync this session's final turns, 5s after exit
 *
 * At session start it also records where the session ran as a `museum` custom
 * entry (not sent to the model): the git repo behind the cwd and the mu
 * workstream. A mu worktree's folder name says `worker-1`, not the project, so
 * this is what lets a search group worker sessions under their repo.
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
type Ctx = {
  hasUI: boolean;
  cwd: string;
  ui: { setStatus(key: string, text: string | undefined): void };
  sessionManager: { getEntries(): Array<{ type: string; customType?: string }> };
};
type ExtensionAPI = {
  on(
    event: "session_start" | "agent_settled" | "session_shutdown",
    handler: (event: unknown, ctx: Ctx) => void,
  ): void;
  appendEntry(customType: string, data: unknown): void;
  exec(
    command: string,
    args: string[],
    options?: { cwd?: string; timeout?: number },
  ): Promise<{ stdout: string; code: number }>;
};

/** What `museum-search` shows for a session; every field may be missing. */
export type Meta = {
  project?: string; // main repo's folder name, else the mu workstream
  repo?: string; // main repo path, the same for all of its worktrees
  remote?: string;
  branch?: string;
  commit?: string;
  workstream?: string;
  agent?: string;
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

function run(mode: "--if-due" | "--now", delayS = 0): void {
  // Cheap pre-check, so ten agents settling does not start ten Pythons. The
  // script re-checks under its lock; this is only an optimisation.
  if (mode === "--if-due") {
    const last = Number(read("last-start").trim()) || 0;
    if (Date.now() / 1000 - last < INTERVAL_S) return;
  }
  try {
    const child =
      delayS > 0
        ? spawn("/bin/sh", ["-c", `sleep ${delayS}; exec "$0" "$1"`, syncPath(), mode], {
            detached: true,
            stdio: "ignore",
          })
        : spawn(syncPath(), [mode], { detached: true, stdio: "ignore" });
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

/**
 * `git rev-parse --path-format=absolute --git-common-dir HEAD --abbrev-ref HEAD`
 * output and the origin URL, plus mu's pane variables, as one Meta.
 */
export function meta(revParse: string, remote: string, env: Record<string, string | undefined>): Meta {
  // --abbrev-ref applies to every argument after it, so the commit comes first.
  const [common, commit, branch] = revParse.trim().split("\n");
  // The common dir is <repo>/.git for a plain repo and for every worktree of it.
  const repo = common?.endsWith("/.git") ? dirname(common) : undefined;
  const m: Meta = {
    project: repo ? repo.split("/").pop() : env.MU_WORKSTREAM,
    repo,
    remote: remote.trim() || undefined,
    branch: branch && branch !== "HEAD" ? branch : undefined,
    commit: commit || undefined,
    workstream: env.MU_WORKSTREAM || undefined,
    agent: env.MU_AGENT_NAME || undefined,
  };
  return Object.fromEntries(Object.entries(m).filter(([, v]) => v)) as Meta;
}

async function record(pi: ExtensionAPI, ctx: Ctx): Promise<void> {
  // A resumed or reloaded session already has its entry.
  if (ctx.sessionManager.getEntries().some((e) => e.type === "custom" && e.customType === "museum")) return;
  const git = (args: string[]) =>
    pi.exec("git", args, { cwd: ctx.cwd, timeout: 3000 }).then(
      (r) => (r.code === 0 ? r.stdout : ""),
      () => "",
    );
  const [revParse, remote] = await Promise.all([
    git(["rev-parse", "--path-format=absolute", "--git-common-dir", "HEAD", "--abbrev-ref", "HEAD"]),
    git(["config", "--get", "remote.origin.url"]),
  ]);
  const m = meta(revParse, remote, process.env);
  if (Object.keys(m).length > 0) pi.appendEntry("museum", m);
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
    // Not awaited: git must never delay pi's startup.
    record(pi, ctx).catch(() => {});
  });
  pi.on("agent_settled", (_event, ctx) => {
    show(ctx);
    run("--if-due");
  });
  // Delayed: other extensions still write to the session after this handler
  // (a session name, for one), and a sync that starts now would miss them
  // until the next one. The child is detached, so pi does not wait.
  pi.on("session_shutdown", () => run("--now", 5));
}
