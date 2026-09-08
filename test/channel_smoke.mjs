#!/usr/bin/env node
// Drive comms_channel.mjs over stdio as an MCP client (no Claude involved):
// expects the tool list, a channel notification for a dropped inbox file, and an outbox
// file after calling comms_reply.  Usage: node test/channel_smoke.mjs
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { z } from "zod";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const spool = fs.mkdtempSync(path.join(os.tmpdir(), "comms-spool-"));
fs.mkdirSync(path.join(spool, "inbox"), { recursive: true });

const transport = new StdioClientTransport({
  command: "node",
  args: [path.join(here, "..", "comms_channel.mjs")],
  env: { ...process.env, COMMS_SPOOL: spool, COMMS_STREAM: "Smoke" },
  stderr: "pipe",
});
const client = new Client({ name: "smoke", version: "0.0.1" }, { capabilities: {} });
const got = [];
client.setNotificationHandler(
  z.object({ method: z.literal("notifications/claude/channel"), params: z.object({ content: z.string(), meta: z.record(z.string()).optional() }) }),
  (n) => got.push(n.params),
);
await client.connect(transport);
transport.stderr?.on("data", (d) => process.stderr.write("  shim: " + d));

const caps = client.getServerCapabilities();
console.log("capabilities:", JSON.stringify(caps));
if (!caps?.experimental?.["claude/channel"]) throw new Error("claude/channel capability missing");
const tools = await client.listTools();
console.log("tools:", tools.tools.map((t) => t.name).join(", "));

const req = { event_id: "evt123", stream: "Smoke", kind: "request", summary: "Say hello", description: "just a test", claimed_at: "now" };
fs.writeFileSync(path.join(spool, "inbox", "evt123.json"), JSON.stringify(req));
const t0 = Date.now();
while (!got.length && Date.now() - t0 < 8000) await new Promise((r) => setTimeout(r, 100));
if (!got.length) throw new Error("no channel notification within 8s");
console.log(`notification after ${Date.now() - t0} ms, meta=${JSON.stringify(got[0].meta)}\n---\n${got[0].content}\n---`);
if (!fs.existsSync(path.join(spool, "inbox", "delivered", "evt123.json"))) throw new Error("inbox file not moved to delivered/");

const res = await client.callTool({ name: "comms_reply", arguments: { event_id: "evt123", status: "done", text: "Hello back." } });
console.log("comms_reply ->", res.content[0].text);
const out = JSON.parse(fs.readFileSync(path.join(spool, "outbox", "evt123.json"), "utf8"));
if (out.status !== "done" || out.text !== "Hello back.") throw new Error("outbox content wrong");
console.log("outbox ok:", JSON.stringify(out));
await client.close();
fs.rmSync(spool, { recursive: true, force: true });
console.log("SMOKE PASS");
