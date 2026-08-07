import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const manifest = JSON.parse(readFileSync(new URL("./openclaw.plugin.json", import.meta.url), "utf8"));
const source = readFileSync(new URL("./index.js", import.meta.url), "utf8");

test("declares and registers the read-only capability tool", () => {
  assert.deepEqual(manifest.contracts.tools, ["video_ingestion_capabilities"]);
  assert.match(source, /name:\s*"video_ingestion_capabilities"/);
  assert.match(source, /\["capabilities"\]/);
  assert.doesNotMatch(source, /video capability lookup failed: \$\{String\(error\)\}/);
});
