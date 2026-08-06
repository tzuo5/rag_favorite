import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("./index.js", import.meta.url), "utf8");

test("delivers deterministic acknowledgements before suppressing the model reply", () => {
  assert.match(source, /"start-prompt"/);
  assert.match(source, /namespace:\s*"vks"/);
  assert.match(source, /pendingStartRequests/);
  assert.match(source, /"telegram-notify"/);
  assert.match(source, /"destination-prompt"/);
  assert.match(
    source,
    /notificationDelivered:\s*notification\.ok === true/,
  );
  assert.match(
    source,
    /if \(outcome\.notificationDelivered\) \{\s*return \{ handled: true, reason: "video-ingestion-awaiting-start" \};/s,
  );
});

test("exposes QR login for both authenticated platforms", () => {
  assert.match(source, /name:\s*"video_login"/);
  assert.match(source, /"session-login", "--platform", platform/);
  assert.match(source, /xhs:\s*"xiaohongshu"/);
  assert.match(source, /bili:\s*"bilibili"/);
});

test("routes unified work status and controls for every supported workflow", () => {
  assert.match(source, /\[\s*"work-status", "--user-id"/);
  assert.match(source, /workCommand\("video_pause"/);
  assert.match(source, /workCommand\("video_resume"/);
  assert.match(source, /workCommand\("video_cancel"/);
  assert.match(source, /namespace:\s*"vkw"/);
  assert.match(source, /"discovery-prompt"/);
});
