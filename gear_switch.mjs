#!/usr/bin/env node
// Switch a running Claude Code session's model + effort for THIS SESSION ONLY by driving the
// /model picker in its tmux pane.  Shared by comms_channel.mjs (the sequencer) and
// bin/gear-switch-test.  Parse-and-verify, never blind keystrokes: the picker's row list
// changes with the current model (5 or 6 rows seen on 2.1.259) and wraps at both ends.
//
//   node gear_switch.mjs <tmux-target> <model> <effort>     e.g. comms-inbox haiku low
//   models: default opus-1m fable sonnet haiku opus   effort: low medium high xhigh max
// Exit 0 = switched (or already there), 1 = could not verify (the pane is left as found).
import { spawnSync } from "node:child_process";

const EFFORT_LADDER = ["low", "medium", "high", "xhigh", "max"];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function tmux(target, ...args) {
  const r = spawnSync("tmux", args.length ? [args[0], "-t", target, ...args.slice(1)] : ["ls"], { encoding: "utf8" });
  return r.status === 0 ? r.stdout : null;
}
const pane = (t) => tmux(t, "capture-pane", "-p") || "";
async function key(t, k, n = 1) {
  for (let i = 0; i < n; i++) {
    tmux(t, "send-keys", k);
    await sleep(120);
  }
}

export function paneIdle(t) {
  const tail = pane(t).split("\n").filter((l) => l.trim()).slice(-8).join("\n");
  return tail.includes("❯") && !/esc to interrupt|thinking with|ing… \(/i.test(tail);
}
export async function waitIdle(t, maxMs = 20000) {
  const t0 = Date.now();
  while (Date.now() - t0 < maxMs) {
    if (paneIdle(t)) return true;
    await sleep(1000);
  }
  return false;
}

// Parse the picker: rows like "  ❯ 5. Haiku   Haiku 4.5 · …" and the effort line "◐ Medium effort ←/→".
function parsePicker(text) {
  const rows = [];
  for (const line of text.split("\n")) {
    const m = line.match(/^\s*(❯)?\s*(\d)\.\s+(Default|Opus \(1M context\)|Opus|Fable|Sonnet|Haiku)\b/);
    if (m) {
      const name = m[3] === "Opus (1M context)" ? "opus-1m" : m[3].toLowerCase();
      rows.push({ n: Number(m[2]), name, cursor: !!m[1] });
    }
  }
  const e = text.match(/([A-Za-z]+) effort\s*(?:\(default\))?\s*←\/→/);
  return { rows, effort: e ? e[1].toLowerCase() : null, open: rows.length > 0 && /Select model/.test(text) };
}

export async function driveGearSwitch(target, model, effort, log = () => {}) {
  if (!EFFORT_LADDER.includes(effort)) throw new Error(`unknown effort ${effort}`);
  if (!(await waitIdle(target))) return { ok: false, reason: "pane not idle" };
  await key(target, "/model");
  await key(target, "Enter");
  await sleep(1500);
  let p = parsePicker(pane(target));
  if (!p.open) {
    await key(target, "Escape");
    return { ok: false, reason: "picker did not open" };
  }
  const want = p.rows.find((r) => r.name === model);
  if (!want) {
    await key(target, "Escape");
    return { ok: false, reason: `no picker row for ${model}; rows: ${p.rows.map((r) => r.name).join(",")}` };
  }
  // Model row: move by the difference, re-read, repeat (bounded).
  for (let i = 0; i < 4; i++) {
    const cur = p.rows.find((r) => r.cursor);
    if (cur && cur.name === model) break;
    const delta = want.n - (cur ? cur.n : want.n);
    await key(target, delta > 0 ? "Down" : "Up", Math.abs(delta));
    await sleep(300);
    p = parsePicker(pane(target));
  }
  if (p.rows.find((r) => r.cursor)?.name !== model) {
    await key(target, "Escape");
    return { ok: false, reason: "could not land on the model row" };
  }
  // Effort: step one at a time, verifying the label after each key (the ladder may not wrap).
  // Some rows (Haiku) show no effort control at all: nothing to set, confirm as is.
  if (p.effort == null) log(`picker shows no effort control for ${model}; model only`);
  for (let i = 0; i < 10 && p.effort != null && p.effort !== effort; i++) {
    const ci = EFFORT_LADDER.indexOf(p.effort), wi = EFFORT_LADDER.indexOf(effort);
    if (ci < 0) break;
    await key(target, wi > ci ? "Right" : "Left");
    await sleep(300);
    p = parsePicker(pane(target));
  }
  if (p.effort != null && p.effort !== effort) {
    await key(target, "Escape");
    return { ok: false, reason: `effort stuck at ${p.effort}` };
  }
  await key(target, "s"); // this session only — never "Enter", which saves the user's default
  await sleep(1200);
  let after = pane(target);
  if (/Switch model\?|Change effort level\?/.test(after)) {
    await key(target, "Enter"); // "1. Yes, switch" — confirms the cache re-read
    await sleep(1200);
    after = pane(target);
  }
  const line = after.split("\n").reverse().find((l) => /Set model to|Kept model|Set effort/.test(l));
  const ok = !!line && /session only/.test(line);
  log(`gear switch → ${model}/${effort}: ${(line || "no confirmation line").trim()}`);
  return { ok, reason: line ? line.trim() : "no confirmation line" };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const [target, model, effort] = process.argv.slice(2);
  if (!target || !model || !effort) {
    console.error("usage: gear_switch.mjs <tmux-target> <model> <effort>");
    process.exit(2);
  }
  const r = await driveGearSwitch(target, model, effort, (m) => console.error(m));
  console.log(r.ok ? "OK" : "FAILED", r.reason);
  process.exit(r.ok ? 0 : 1);
}
