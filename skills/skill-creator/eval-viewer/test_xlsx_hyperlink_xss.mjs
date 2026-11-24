/**
 * Spec for spreadsheet rendering in the eval review page.
 *
 * These tests fail while a spreadsheet placed in a run outputs directory can
 * run script in the review page. They pass once cell text is rendered as text
 * and any hyperlink that is shown is only an http or https URL created
 * without assigning sheet HTML to innerHTML.
 *
 * Run:
 *   node skills/skill-creator/eval-viewer/test_xlsx_hyperlink_xss.mjs
 *
 * Needs python3 and google-chrome. The page loads SheetJS the same way the
 * viewer does (cdn.sheetjs.com, version pinned in viewer.html).
 */

import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import net from "node:net";
import { fileURLToPath } from "node:url";

const viewerDir = path.dirname(fileURLToPath(import.meta.url));
const generator = path.join(viewerDir, "generate_review.py");
const PROMPT = "eval-prompt-marker";
const HIDDEN_PROMPT = "hidden-run-prompt-marker";
const MARKUP_FLAG = "markup-cell-ran";

const failures = [];

function fail(name, detail) {
  failures.push({ name, detail });
  console.error(`FAIL ${name}\n${detail}\n`);
}

function pass(name) {
  console.log(`PASS ${name}`);
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

function run(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"], ...opts });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`${cmd} ${args.join(" ")} exited ${code}\n${stderr}\n${stdout}`));
        return;
      }
      resolve({ stdout, stderr });
    });
  });
}

/**
 * Minimal xlsx. Hyperlink targets are written into the worksheet relationship
 * part the way SheetJS reads them (parse_rels -> parse_ws_xml_hlinks -> cell.l).
 * Rich text is an inline string run, which sheet_to_html inserts from cell.h.
 */
function writeWorkbookSync(file, sheets) {
  const payload = JSON.stringify({ file, sheets });
  const py = `
import json, sys, zipfile
spec = json.loads(sys.stdin.read())
ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
      '<Default Extension="xml" ContentType="application/xml"/>',
      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
wb_sheets = []
wb_rels = []
parts = {}
for i, sheet in enumerate(spec["sheets"], start=1):
    ct.append('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % i)
    wb_sheets.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (sheet["name"], i, i))
    wb_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i, i))
    rows = []
    links = []
    rels = []
    rid = 1
    for row_i, cell in enumerate(sheet["cells"], start=1):
        addr = "A%d" % row_i
        kind = cell["kind"]
        if kind == "rich":
            rows.append('<row r="%d"><c r="%s" t="inlineStr"><is><r><t>%s</t></r></is></c></row>' % (row_i, addr, cell["xml_text"]))
        elif kind == "plain":
            rows.append('<row r="%d"><c r="%s" t="inlineStr"><is><t>%s</t></is></c></row>' % (row_i, addr, cell["xml_text"]))
        else:
            rows.append('<row r="%d"><c r="%s" t="str"><v>%s</v></c></row>' % (row_i, addr, cell["text"]))
            if "target" in cell:
                # Double-quoted attribute. SheetJS's tag regex ends a tag at a raw ">".
                # Quotes inside the target are XML character references; unescapexml
                # restores them after the attribute is read.
                target = cell["target"].replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
                target = target.replace("&amp;#", "&#")
                rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="%s" TargetMode="External"/>' % (rid, target))
                links.append('<hyperlink ref="%s" r:id="rId%d" display="%s"/>' % (addr, rid, cell["text"]))
                rid += 1
    link_xml = ("<hyperlinks>" + "".join(links) + "</hyperlinks>") if links else ""
    parts["xl/worksheets/sheet%d.xml" % i] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<dimension ref="A1:A%d"/>' % max(1, len(sheet["cells"]))
        + "<sheetData>" + "".join(rows) + "</sheetData>" + link_xml + "</worksheet>"
    )
    if rels:
        parts["xl/worksheets/_rels/sheet%d.xml.rels" % i] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rels) + "</Relationships>"
        )
ct.append("</Types>")
parts["[Content_Types].xml"] = "".join(ct)
parts["_rels/.rels"] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
parts["xl/workbook.xml"] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    "<sheets>" + "".join(wb_sheets) + "</sheets></workbook>"
)
parts["xl/_rels/workbook.xml.rels"] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    + "".join(wb_rels) + "</Relationships>"
)
with zipfile.ZipFile(spec["file"], "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for name, body in parts.items():
        zf.writestr(name, body)
`;
  return new Promise((resolve, reject) => {
    const child = spawn("python3", ["-c", py], { stdio: ["pipe", "pipe", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) reject(new Error(`xlsx writer exited ${code}\n${stderr}`));
      else resolve();
    });
    child.stdin.write(payload);
    child.stdin.end();
  });
}

function linkCell(text, target) {
  return { kind: "link", text, target };
}

function pushTarget(label) {
  // void() keeps the javascript: URL from replacing the review document
  // when the assignment's result is a string.
  return `javascript:void(window.__hit_${label}=1)`;
}

function buildSheets() {
  return [
    {
      name: "Data",
      cells: [
        linkCell(
          "plain-link",
          'javascript:void(window.__hit_plain=document.getElementById("prompt-text").textContent)',
        ),
        linkCell("mixed-case-link", "JavaScript:" + pushTarget("mixed").slice("javascript:".length)),
        linkCell("leading-space-link", " " + pushTarget("space")),
        linkCell("embedded-tab-link", "java\tscript:" + pushTarget("tab").slice("javascript:".length)),
        linkCell("entity-encoded-link", "&#106;avascript:" + pushTarget("entity").slice("javascript:".length)),
        linkCell(
          "hidden-run-link",
          `javascript:void(window.__hit_hidden=document.documentElement.textContent.indexOf("${HIDDEN_PROMPT}"))`,
        ),
        linkCell("data-scheme-link", "data:text/html,not-the-review-page"),
        linkCell("vbscript-link", "vbscript:msgbox(1)"),
        linkCell("https-link", "https://example.com/review-safe"),
        linkCell("http-link", "http://example.com/review-safe"),
        {
          kind: "rich",
          xml_text: `before &lt;img src=x onerror=&quot;window.__markup=&apos;${MARKUP_FLAG}&apos;&quot;&gt; after`,
        },
        {
          kind: "plain",
          xml_text: "&lt;img src=x onerror=&quot;window.__plain=1&quot;&gt;",
        },
      ],
    },
    {
      name: "Other",
      cells: [linkCell("other-sheet-link", pushTarget("other"))],
    },
  ];
}

class Cdp {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl);
    this.id = 0;
    this.pending = new Map();
    this.ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      }
    });
  }

  ready() {
    return new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve);
      this.ws.addEventListener("error", reject);
    });
  }

  send(method, params = {}, sessionId) {
    const id = ++this.id;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    this.ws.send(JSON.stringify(payload));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`timeout ${method}`));
        }
      }, 20000);
      this.pending.set(id, {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: (err) => {
          clearTimeout(timer);
          reject(err);
        },
      });
    });
  }

  close() {
    this.ws.close();
  }
}

async function waitForChrome(port) {
  const deadline = Date.now() + 15000;
  let last = "";
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (resp.ok) return await resp.json();
      last = `status ${resp.status}`;
    } catch (err) {
      last = err.message;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`Chrome DevTools did not come up: ${last}`);
}

async function evaluate(cdp, sessionId, expression) {
  const result = await cdp.send(
    "Runtime.evaluate",
    { expression, returnByValue: true, awaitPromise: true },
    sessionId,
  );
  if (result.exceptionDetails) {
    throw new Error(JSON.stringify(result.exceptionDetails));
  }
  return result.result.value;
}

async function trustedClick(cdp, sessionId, text) {
  const box = await evaluate(
    cdp,
    sessionId,
    `(() => {
      const a = [...document.querySelectorAll(".output-file-content a")].find((el) => el.textContent === ${JSON.stringify(text)});
      if (!a) return null;
      a.scrollIntoView({ block: "center", inline: "center" });
      const r = a.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height, href: a.href };
    })()`,
  );
  if (!box || box.w < 1 || box.h < 1) {
    throw new Error(`no clickable box for ${text}: ${JSON.stringify(box)}`);
  }
  await cdp.send(
    "Input.dispatchMouseEvent",
    { type: "mousePressed", x: box.x, y: box.y, button: "left", clickCount: 1 },
    sessionId,
  );
  await cdp.send(
    "Input.dispatchMouseEvent",
    { type: "mouseReleased", x: box.x, y: box.y, button: "left", clickCount: 1 },
    sessionId,
  );
  await new Promise((r) => setTimeout(r, 100));
  return box;
}

function kill(child) {
  if (!child || child.killed || child.exitCode !== null) return;
  child.kill("SIGTERM");
}

async function main() {
  const root = mkdtempSync(path.join(tmpdir(), "eval-xlsx-xss-"));
  const workspace = path.join(root, "iteration");
  const previous = path.join(root, "previous");
  const currentDir = path.join(workspace, "eval-a", "with_skill", "outputs");
  const previousDir = path.join(previous, "eval-a", "with_skill", "outputs");
  const hiddenDir = path.join(workspace, "eval-b", "with_skill", "outputs");
  const reviewPath = path.join(root, "review.html");
  let httpServer;
  let chrome;
  let cdp;

  try {
    await run("mkdir", ["-p", currentDir, previousDir, hiddenDir]);
    writeFileSync(
      path.join(workspace, "eval-a", "eval_metadata.json"),
      JSON.stringify({ eval_id: 0, prompt: PROMPT, assertions: [] }),
    );
    writeFileSync(
      path.join(workspace, "eval-b", "eval_metadata.json"),
      JSON.stringify({ eval_id: 1, prompt: HIDDEN_PROMPT, assertions: [] }),
    );
    writeFileSync(
      path.join(previous, "eval-a", "eval_metadata.json"),
      JSON.stringify({ eval_id: 0, prompt: "previous prompt", assertions: [] }),
    );
    writeFileSync(path.join(hiddenDir, "notes.txt"), "hidden-output-marker\n");

    await writeWorkbookSync(path.join(currentDir, "report.xlsx"), buildSheets());
    await writeWorkbookSync(path.join(previousDir, "old.xlsx"), [
      {
        name: "Data",
        cells: [linkCell("previous-link", pushTarget("previous"))],
      },
    ]);

    await run("python3", [
      generator,
      workspace,
      "--static",
      reviewPath,
      "--previous-workspace",
      previous,
      "--skill-name",
      "xss-spec",
    ]);

    const httpPort = await freePort();
    const chromePort = await freePort();
    httpServer = spawn(
      "python3",
      ["-m", "http.server", String(httpPort), "--bind", "127.0.0.1", "--directory", root],
      { stdio: "ignore" },
    );
    const chromeBin = process.env.CHROME_BIN || "google-chrome";
    chrome = spawn(
      chromeBin,
      [
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        `--remote-debugging-port=${chromePort}`,
        `--user-data-dir=${path.join(root, "chrome-profile")}`,
        "about:blank",
      ],
      { stdio: "ignore" },
    );
    const version = await waitForChrome(chromePort);
    cdp = new Cdp(version.webSocketDebuggerUrl);
    await cdp.ready();
    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
    await cdp.send("Page.enable", {}, sessionId);
    await cdp.send("Runtime.enable", {}, sessionId);
    const pageUrl = `http://127.0.0.1:${httpPort}/review.html`;
    await cdp.send("Page.navigate", { url: pageUrl }, sessionId);

    let snapshot = null;
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 250));
      snapshot = await evaluate(
        cdp,
        sessionId,
        `(() => {
          const body = document.body ? document.body.innerText : "";
          return {
            xlsx: typeof XLSX !== "undefined",
            prompt: document.getElementById("prompt-text") ? document.getElementById("prompt-text").textContent : "",
            text: document.body ? document.body.textContent : "",
            markup: window.__markup || null,
            plain: window.__plain || null,
            activeElements: [...document.querySelectorAll("#outputs-body .output-file-content img, #outputs-body .output-file-content script, #outputs-body .output-file-content iframe, #outputs-body .output-file-content svg, #outputs-body .output-file-content object, #prev-outputs-content .output-file-content img, #prev-outputs-content .output-file-content script")].map((el) => el.tagName),
            anchors: [...document.querySelectorAll("#outputs-body .output-file-content a, #prev-outputs-content .output-file-content a")].map((a) => ({ text: a.textContent, href: a.href, protocol: a.protocol }))
          };
        })()`,
      );
      if (snapshot.xlsx && snapshot.text.includes("plain-link") && snapshot.text.includes("previous-link")) break;
    }

    const loadedName = "review page renders spreadsheet outputs";
    if (!snapshot || !snapshot.xlsx || snapshot.prompt !== PROMPT || !snapshot.text.includes("plain-link")) {
      fail(loadedName, `Review page did not render the spreadsheet through the viewer.\n${JSON.stringify(snapshot, null, 2)}`);
      return;
    }
    pass(loadedName);

    const markupName = "rich-text spreadsheet markup does not run";
    const markupRan = snapshot.markup === MARKUP_FLAG || (snapshot.activeElements || []).length > 0;
    if (markupRan) {
      fail(
        markupName,
        `Cell markup ran while the sheet was inserted into the review page.\nmarkup=${snapshot.markup}\nelements=${JSON.stringify(snapshot.activeElements)}\nThe rich-text cell must show up as text. before/after markers present=${snapshot.text.includes("before") && snapshot.text.includes("after")}`,
      );
    } else if (!snapshot.text.includes("before") || !snapshot.text.includes("after")) {
      fail(markupName, "Rich-text cell text was dropped instead of shown as text.");
    } else {
      pass(markupName);
    }

    const plainName = "plain spreadsheet text is not interpreted as HTML";
    if (snapshot.plain) {
      fail(plainName, "A plain inline string containing a tag ran as HTML.");
    } else if (!snapshot.text.includes("<img")) {
      fail(plainName, "Plain cell text was not visible.");
    } else {
      pass(plainName);
    }

    const allowName = "spreadsheet anchors are only http or https";
    const anchors = snapshot.anchors || [];
    const dangerous = anchors.filter((a) => a.protocol !== "http:" && a.protocol !== "https:");
    const https = anchors.find((a) => a.text === "https-link");
    const http = anchors.find((a) => a.text === "http-link");
    const allowProblems = [];
    if (dangerous.length) {
      allowProblems.push(`non-http(s) anchors:\n${JSON.stringify(dangerous, null, 2)}`);
    }
    if (https && https.href !== "https://example.com/review-safe") {
      allowProblems.push(`https-link href is ${https.href}`);
    }
    if (http && http.href !== "http://example.com/review-safe") {
      allowProblems.push(`http-link href is ${http.href}`);
    }
    for (const label of ["https-link", "http-link", "plain-link", "other-sheet-link", "previous-link"]) {
      if (!snapshot.text.includes(label)) allowProblems.push(`missing cell text ${label}`);
    }
    if (allowProblems.length) fail(allowName, allowProblems.join("\n"));
    else pass(allowName);

    // Open the previous-output section the way a reviewer does, then click
    // every javascript: anchor the sheet renderer created.
    await evaluate(cdp, sessionId, `(() => { const el = document.getElementById("prev-outputs-content"); if (el && !el.classList.contains("open")) togglePrevOutputs(); })()`);
    const jsAnchors = anchors.filter((a) => a.protocol === "javascript:");
    const clickName = "clicking a spreadsheet hyperlink does not run script";
    const clickProblems = [];
    for (const anchor of jsAnchors) {
      try {
        const box = await trustedClick(cdp, sessionId, anchor.text);
        const state = await evaluate(
          cdp,
          sessionId,
          `(() => ({
            href: location.href,
            prompt: document.getElementById("prompt-text") && document.getElementById("prompt-text").textContent,
            hit_plain: window.__hit_plain || null,
            hit_mixed: window.__hit_mixed || null,
            hit_space: window.__hit_space || null,
            hit_tab: window.__hit_tab || null,
            hit_entity: window.__hit_entity || null,
            hit_hidden: window.__hit_hidden,
            hit_other: window.__hit_other || null,
            hit_previous: window.__hit_previous || null
          }))()`,
        );
        if (!String(state.href).startsWith(pageUrl)) {
          clickProblems.push(`${anchor.text} navigated to ${state.href} (from ${box.href})`);
          break;
        }
        const ran = [
          ["plain-link", state.hit_plain],
          ["mixed-case-link", state.hit_mixed],
          ["leading-space-link", state.hit_space],
          ["embedded-tab-link", state.hit_tab],
          ["entity-encoded-link", state.hit_entity],
          ["hidden-run-link", state.hit_hidden],
          ["other-sheet-link", state.hit_other],
          ["previous-link", state.hit_previous],
        ].filter(([, value]) => value !== null && value !== undefined && value !== false);
        if (anchor.text === "plain-link" || anchor.text === "hidden-run-link" || ran.some(([name]) => name === anchor.text)) {
          if (ran.length) {
            clickProblems.push(
              `${anchor.text} ran in the review page (${box.href}). hits=${JSON.stringify(Object.fromEntries(ran))} prompt=${state.prompt}`,
            );
          }
        }
      } catch (err) {
        clickProblems.push(`${anchor.text}: ${err.message}`);
      }
    }
    if (clickProblems.length) fail(clickName, clickProblems.join("\n"));
    else if (jsAnchors.length) {
      fail(clickName, `javascript: anchors were present but clicks did not run their targets:\n${JSON.stringify(jsAnchors, null, 2)}`);
    } else pass(clickName);
  } finally {
    if (cdp) cdp.close();
    kill(chrome);
    kill(httpServer);
    await new Promise((r) => setTimeout(r, 400));
    rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
}

main()
  .then(() => {
    if (failures.length) {
      console.error(`${failures.length} test(s) failed`);
      process.exit(1);
    }
    console.log("all spreadsheet review tests passed");
  })
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
