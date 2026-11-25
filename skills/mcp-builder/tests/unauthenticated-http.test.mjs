/**
 * Spec for the streamable HTTP examples in
 * skills/mcp-builder/reference/node_mcp_server.md.
 *
 * These tests fail while a copy of those examples still accepts an MCP
 * tools/call from a client that presents no credential and reaches the
 * process on a non-loopback address. They pass once each example either
 * refuses that request before the tool handler runs (for example with
 * 401 or 403, including a disallowed Origin) or does not accept the TCP
 * connection on a non-loopback address.
 *
 * The Python remote snippet is not covered here. On mcp 1.30.0,
 * FastMCP.run() rejects the snippet's arguments before it listens.
 *
 * Versions are the ones npm installs from the guide's ranges today:
 * @modelcontextprotocol/sdk ^1.6.1 -> 1.32.2, axios ^1.7.9 -> 1.21.1,
 * zod ^3.23.8 -> 3.25.76, typescript ^5.7.2 -> 5.9.3. The HTTP samples
 * import express without a pin; 5.3.0 is the version npm installs.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const testsDir = path.dirname(fileURLToPath(import.meta.url));
const guidePath = path.join(testsDir, "../reference/node_mcp_server.md");
const guide = fs.readFileSync(guidePath, "utf8");
const tsc = path.join(testsDir, "node_modules/typescript/lib/tsc.js");

const ATTACKER_ORIGIN = "http://attacker.example";

let exclusive = Promise.resolve();
function withLock(fn) {
  const run = exclusive.then(fn, fn);
  exclusive = run.then(
    () => {},
    () => {}
  );
  return run;
}

function extractFenceContaining(source, needle) {
  const re = /```typescript\r?\n([\s\S]*?)```/g;
  for (const match of source.matchAll(re)) {
    if (match[1].includes(needle)) return match[1];
  }
  throw new Error(`No typescript fence contains ${needle}`);
}

function extractBlock(source, startToken) {
  const start = source.indexOf(startToken);
  if (start < 0) throw new Error(`Guide text is missing ${startToken}`);
  const open = source.indexOf("{", start);
  let depth = 0;
  let quote = null;
  for (let i = open; i < source.length; i++) {
    const c = source[i];
    if (quote) {
      if (c === "\\") {
        i++;
        continue;
      }
      if (c === quote) quote = null;
      continue;
    }
    if (c === "/" && source[i + 1] === "/") {
      const nl = source.indexOf("\n", i);
      i = nl < 0 ? source.length : nl;
      continue;
    }
    if (c === "/" && source[i + 1] === "*") {
      const end = source.indexOf("*/", i + 2);
      i = end < 0 ? source.length : end + 1;
      continue;
    }
    if (c === "'" || c === '"' || c === "`") {
      quote = c;
      continue;
    }
    if (c === "{") depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`Unbalanced block for ${startToken}`);
}

function nonLoopbackIPv4() {
  for (const addresses of Object.values(os.networkInterfaces())) {
    for (const address of addresses ?? []) {
      if (address.family === "IPv4" && !address.internal) return address.address;
    }
  }
  throw new Error("This host has no non-loopback IPv4 address to connect to");
}

function decodeProcAddress(hex) {
  if (hex.length === 8) {
    const value = Number.parseInt(hex, 16);
    return [value & 0xff, (value >> 8) & 0xff, (value >> 16) & 0xff, (value >> 24) & 0xff].join(".");
  }
  const parts = [];
  for (let i = 0; i < hex.length; i += 8) {
    const word = Number.parseInt(hex.slice(i, i + 8), 16);
    const left = ((word & 0xff) << 8) | ((word >> 8) & 0xff);
    const right = (((word >> 16) & 0xff) << 8) | ((word >> 24) & 0xff);
    parts.push(left.toString(16).padStart(4, "0"), right.toString(16).padStart(4, "0"));
  }
  return parts.join(":");
}

function listeningAddresses(port) {
  const found = [];
  for (const file of ["/proc/net/tcp", "/proc/net/tcp6"]) {
    const text = fs.readFileSync(file, "utf8");
    for (const line of text.trim().split("\n").slice(1)) {
      const columns = line.trim().split(/\s+/);
      const [ip, hexPort] = columns[1].split(":");
      const state = columns[3];
      if (state === "0A" && Number.parseInt(hexPort, 16) === port) {
        found.push(decodeProcAddress(ip));
      }
    }
  }
  return found;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function startStub() {
  const hits = [];
  const stub = http.createServer((req, res) => {
    const names = Object.keys(req.headers).sort();
    hits.push({ method: req.method, url: req.url, headerNames: names });
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ users: [{ id: "U1", name: "Ada", email: "ada@example.com", active: true }], total: 1 }));
  });
  return new Promise((resolve, reject) => {
    stub.once("error", reject);
    stub.listen(0, "127.0.0.1", () => {
      resolve({
        hits,
        base: `http://127.0.0.1:${stub.address().port}/v1`,
        close: () =>
          new Promise((done) => {
            stub.close(() => done());
          })
      });
    });
  });
}

function compileServer(source) {
  const root = path.join(testsDir, ".generated", `${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  const srcDir = path.join(root, "src");
  fs.mkdirSync(srcDir, { recursive: true });
  fs.writeFileSync(path.join(srcDir, "server.ts"), source);
  const outDir = path.join(root, "dist");
  const compiled = spawn(
    process.execPath,
    [
      tsc,
      "--pretty",
      "false",
      "--target",
      "ES2022",
      "--module",
      "Node16",
      "--moduleResolution",
      "Node16",
      "--strict",
      "--skipLibCheck",
      "--esModuleInterop",
      "--outDir",
      outDir,
      "--rootDir",
      srcDir,
      path.join(srcDir, "server.ts")
    ],
    { cwd: testsDir, stdio: ["ignore", "pipe", "pipe"] }
  );
  let stderr = "";
  compiled.stdout.on("data", (chunk) => {
    stderr += chunk;
  });
  compiled.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  return new Promise((resolve, reject) => {
    compiled.on("exit", (code) => {
      if (code !== 0) {
        reject(new Error(`tsc exited ${code}\n${stderr}`));
        return;
      }
      resolve({ entry: path.join(outDir, "server.js"), cleanup: () => fs.rmSync(root, { recursive: true, force: true }) });
    });
  });
}

const HARNESS_SPECIFIERS = [
  "@modelcontextprotocol/sdk/server/mcp.js",
  "@modelcontextprotocol/sdk/server/streamableHttp.js",
  "express",
  "zod",
  "axios"
];

function extraImports(source) {
  const imports = [];
  for (const match of source.matchAll(/^import\s[\s\S]*?from\s+["'][^"']+["'];?/gm)) {
    const line = match[0];
    const provided = HARNESS_SPECIFIERS.some(
      (specifier) => line.includes(`"${specifier}"`) || line.includes(`'${specifier}'`)
    );
    if (!provided) imports.push(line);
  }
  return imports.join("\n");
}

function harnessSource({ httpSource, callsRunHTTP, importsFrom }) {
  const complete = extractFenceContaining(guide, "async function runHTTP");
  const makeApiRequest = extractBlock(complete, "async function makeApiRequest");
  const added = extraImports(importsFrom ?? "");
  return `${added ? `${added}\n` : ""}import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import express from "express";
import { z } from "zod";
import axios from "axios";

const API_BASE_URL = process.env.STUB_API_BASE as string;

${makeApiRequest}

const server = new McpServer({
  name: "example-mcp",
  version: "1.0.0"
});

server.registerTool(
  "example_search_users",
  {
    title: "Search Example Users",
    description: "Search for users in the Example system by name, email, or team.",
    inputSchema: z.object({
      query: z.string()
    }).strict()
  },
  async (params: { query: string }) => {
    const data = await makeApiRequest<any>(
      "users/search",
      "GET",
      undefined,
      { q: params.query }
    );
    return {
      content: [{ type: "text" as const, text: JSON.stringify(data) }]
    };
  }
);

${httpSource}
${callsRunHTTP ? "void runHTTP();" : ""}
`;
}

async function startExample(httpSource, callsRunHTTP, port, importsFrom) {
  const compiled = await compileServer(harnessSource({ httpSource, callsRunHTTP, importsFrom }));
  const child = spawn(process.execPath, [compiled.entry], {
    cwd: testsDir,
    env: {
      ...process.env,
      EXAMPLE_API_KEY: "placeholder-not-a-secret",
      STUB_API_BASE: process.env.STUB_API_BASE,
      PORT: String(port)
    },
    stdio: ["ignore", "pipe", "pipe"]
  });
  let logs = "";
  child.stdout.on("data", (chunk) => {
    logs += chunk;
  });
  child.stderr.on("data", (chunk) => {
    logs += chunk;
  });
  const ready = await waitForListen(child, port, () => logs);
  return {
    ready,
    logs: () => logs,
    async stop() {
      if (child.exitCode === null && child.signalCode === null) {
        child.kill("SIGTERM");
        await new Promise((resolve) => {
          const timer = setTimeout(() => {
            child.kill("SIGKILL");
            resolve(undefined);
          }, 2000);
          child.once("exit", () => {
            clearTimeout(timer);
            resolve(undefined);
          });
        });
      }
      compiled.cleanup();
    }
  };
}

function waitForListen(child, port, logs) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + 10000;
    const timer = setInterval(() => {
      if (child.exitCode !== null) {
        clearInterval(timer);
        reject(new Error(`Example process exited ${child.exitCode} before listening\n${logs()}`));
        return;
      }
      const probe = net.connect({ host: "127.0.0.1", port });
      probe.once("connect", () => {
        probe.end();
        clearInterval(timer);
        resolve(undefined);
      });
      probe.once("error", () => {
        probe.destroy();
        if (Date.now() > deadline) {
          clearInterval(timer);
          reject(new Error(`Example did not listen on 127.0.0.1:${port}\n${logs()}`));
        }
      });
    }, 50);
  });
}

function postToolCall(host, port, origin) {
  const body = JSON.stringify({
    jsonrpc: "2.0",
    id: 1,
    method: "tools/call",
    params: {
      name: "example_search_users",
      arguments: { query: "ab" }
    }
  });
  const headers = {
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
    "Content-Length": Buffer.byteLength(body)
  };
  if (origin) headers.Origin = origin;
  return new Promise((resolve) => {
    const req = http.request(
      { host, port, path: "/mcp", method: "POST", headers, timeout: 5000 },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => {
          resolve({
            refused: false,
            status: res.statusCode ?? 0,
            body: Buffer.concat(chunks).toString("utf8")
          });
        });
      }
    );
    req.on("timeout", () => req.destroy(Object.assign(new Error("timeout"), { code: "ETIMEDOUT" })));
    req.on("error", (error) => {
      const code = error && error.code;
      resolve({
        refused: code === "ECONNREFUSED" || code === "EHOSTUNREACH" || code === "ENETUNREACH",
        status: 0,
        body: "",
        code
      });
    });
    req.end(body);
  });
}

test(
  "runHTTP rejects an unauthenticated tools/call from a non-loopback client",
  { timeout: 60000 },
  () =>
    withLock(async () => {
      const fence = extractFenceContaining(guide, "async function runHTTP");
      const runHTTP = extractBlock(fence, "async function runHTTP");
      await exercise(runHTTP, true, undefined, fence);
    })
);

test(
  "streamable HTTP remote sample rejects an unauthenticated tools/call from a non-loopback client",
  { timeout: 60000 },
  () =>
    withLock(async () => {
      const sample = extractFenceContaining(guide, "app.listen(3000)");
      const withoutImports = sample
        .split("\n")
        .filter((line) => !line.startsWith("import "))
        .join("\n");
      await exercise(withoutImports, false, 3000, sample);
    })
);

async function exercise(httpSource, callsRunHTTP, fixedPort, importsFrom) {
  const stub = await startStub();
  const previous = process.env.STUB_API_BASE;
  process.env.STUB_API_BASE = stub.base;
  try {
    const failures = [];
    for (const origin of [undefined, ATTACKER_ORIGIN]) {
      stub.hits.length = 0;
      const port = fixedPort ?? (await freePort());
      const clientHost = nonLoopbackIPv4();
      const example = await startExample(httpSource, callsRunHTTP, port, importsFrom);
      try {
        const addresses = listeningAddresses(port);
        const outcome = await postToolCall(clientHost, port, origin);
        const blocked =
          stub.hits.length === 0 &&
          (outcome.refused || outcome.status === 401 || outcome.status === 403);
        if (!blocked) {
          failures.push(
            [
              `client=${clientHost}:${port}`,
              `origin=${origin ?? "(absent)"}`,
              `listen=${addresses.join(",") || "(none)"}`,
              `http=${outcome.refused ? `refused:${outcome.code}` : outcome.status}`,
              `toolOutboundCalls=${stub.hits.length}`,
              `outboundHeaderNames=${stub.hits.map((hit) => hit.headerNames.join("|")).join(";")}`,
              `response=${outcome.body.slice(0, 300)}`
            ].join(" ")
          );
        }
      } finally {
        await example.stop();
      }
    }
    assert.deepEqual(failures, [], `Unauthenticated tools/call reached the example.\n${failures.join("\n")}`);
  } finally {
    process.env.STUB_API_BASE = previous;
    await stub.close();
  }
}
