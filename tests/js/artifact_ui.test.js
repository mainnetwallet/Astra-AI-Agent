/* Regression coverage for generated-artifact rendering.
 *
 * This executes the real renderArtifact() function from static/js/astra.js
 * with the exact object shape returned by Artifact.to_dict(), without
 * booting the rest of the browser application.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.join(__dirname, "..", "..");
// Normalise CRLF (what a Windows checkout with core.autocrlf=true produces)
// so the `\n}\n` function-end search below works on every platform.
const SOURCE = fs.readFileSync(
  path.join(ROOT, "static/js/astra.js"), "utf8").replace(/\r\n/g, "\n");

function loadRenderArtifact() {
  const start = SOURCE.indexOf("function renderArtifact(a) {");
  assert.notStrictEqual(start, -1, "renderArtifact() must exist");
  const end = SOURCE.indexOf("\n}\n", start);
  assert.notStrictEqual(end, -1, "renderArtifact() must have a closing brace");
  const fnSource = SOURCE.slice(start, end + 2);
  return vm.runInNewContext(`(${fnSource})`, {
    document: {
      createElement() {
        return { className: "", innerHTML: "" };
      },
    },
    esc: (value) => String(value == null ? "" : value),
    _fileIcon: () => "🖼️",
    _humanSize: (value) => String(value),
  });
}

test("backend-shaped image artifacts select the image branch and keep actions", () => {
  const renderArtifact = loadRenderArtifact();
  const artifact = {
    artifact_type: "image",
    mime_type: "image/png",
    id: "artifact-123",
    filename: "generated.png",
  };

  const card = renderArtifact(artifact);
  assert.strictEqual(card.className, "artifact-card");
  assert.match(card.innerHTML, /class="artifact-image"/);
  assert.match(card.innerHTML, />Open<\/a>/);
  assert.match(card.innerHTML, />Download<\/a>/);
  assert.match(card.innerHTML, /target="_blank"/);
  assert.match(card.innerHTML, /download="generated\.png"/);
});
