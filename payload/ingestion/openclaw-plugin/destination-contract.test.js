import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("./index.js", import.meta.url), "utf8");

test("offers every writable knowledge destination with bounded callbacks", () => {
  const destinations = [
    ["thought-politics", "tp"],
    ["tech", "te"],
    ["finance", "fi"],
    ["career", "ca"],
    ["social-conduct", "sc"],
    ["literature-culture", "lc"],
    ["general", "ge"],
    ["cooking", "co"],
  ];
  const objectId = "00000000-0000-0000-0000-000000000001";
  for (const [id, code] of destinations) {
    assert.match(source, new RegExp(`id: "${id}", code: "${code}"`));
    assert.ok(`vkb:start:${code}:50:${objectId}`.length <= 64);
    assert.ok(`vkb:start:${code}:all:50:${objectId}`.length <= 64);
    assert.ok(`vki:${code}:${objectId}`.length <= 64);
  }
  assert.match(source, /destination\.id === "main"/);
});

test("preserves all-preview semantics through preview and confirmation", () => {
  assert.match(source, /parts\[offset\] === "all"/);
  assert.match(source, /kind: isAllPreview \? "all_preview" : "latest"/);
  assert.match(source, /--selection-kind", selection\.kind/);
  assert.match(
    source,
    /jsonOutput\(await runJsonAware\(\[\s*"confirm-batch"/s,
  );
  assert.match(source, /下载本次预览中的全部 \$\{limit\} 个视频/);
});
