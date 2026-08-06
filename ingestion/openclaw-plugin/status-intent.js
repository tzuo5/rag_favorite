const TRAILING_PUNCTUATION_RE = /[\s？?!！。，、~～]+$/u;

const SHORT_STATUS_PHRASES = new Set([
  "好了吗",
  "好了没",
  "处理好了吗",
  "录入好了吗",
  "归档好了吗",
  "完成了吗",
  "搞定了吗",
  "怎么样了",
  "现在怎么样了",
  "到哪了",
  "进度",
  "查看进度",
  "处理进度",
  "录入进度",
  "归档进度",
  "状态",
  "查看状态",
  "处理状态",
  "录入状态",
  "归档状态",
]);

const STATUS_QUESTION_RE =
  /^(?:(?:这个|那个|刚才(?:的)?|之前(?:的)?|上一个)?(?:视频|视屏|音频|链接|任务|批次|录入|归档)?(?:处理|录入|归档)?)?(?:好了吗|好了没|完成了吗|搞定了吗|怎么样了|到哪了)$/u;

const CLARIFIED_VIDEO_TASK_RE =
  /^(?:刚才|之前|上次)?(?:你)?(?:帮我)?(?:将|把)?(?:这个|那个)?(?:视频|视屏)(?:链接)?(?:归档|录入|处理)(?:的)?(?:这个|那个|任务)?$/u;

export function isStatusIntent(value) {
  const text = String(value || "").trim().replace(TRAILING_PUNCTUATION_RE, "");
  if (!text) return false;
  return (
    SHORT_STATUS_PHRASES.has(text)
    || STATUS_QUESTION_RE.test(text)
    || CLARIFIED_VIDEO_TASK_RE.test(text)
  );
}
