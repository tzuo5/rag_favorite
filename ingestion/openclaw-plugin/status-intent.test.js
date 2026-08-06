import assert from "node:assert/strict";
import test from "node:test";

import { isStatusIntent } from "./status-intent.js";

test("recognizes terse follow-up questions from the real Telegram chat", () => {
  for (const text of [
    "好了吗",
    "处理好了吗",
    "完成了吗？",
    "视频处理好了吗",
    "刚才的任务怎么样了",
    "归档进度",
    "查看状态！",
    "将视屏链接归档的那个",
  ]) {
    assert.equal(isStatusIntent(text), true, text);
  }
});

test("does not claim unrelated conversation", () => {
  for (const text of [
    "",
    "你好吗",
    "这个视频讲了什么",
    "帮我归档这个视频",
    "系统健康吗",
    "今天天气怎么样",
  ]) {
    assert.equal(isStatusIntent(text), false, text);
  }
});
