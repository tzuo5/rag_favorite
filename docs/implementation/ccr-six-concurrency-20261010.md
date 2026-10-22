# CCR 六并发切换（2026-10-10，America/Chicago）

当前 RAG 的共享 CCR 请求上限已设为 **6**，视频 stage worker 已重载并加载
**6 个 LLM 消费者**。视频提取和历史摘要回填共用这六个请求名额。队列已恢复为
切换前的未暂停状态；原先停用的摘要 worker 保持停用。

配置位于 `.runtime/videorag/video.toml`：`ccr_concurrency = 6`。未写该选项的
其他 profile 保持原来单并发行为。下载、准备和发布各保留一个消费者；ASR、GPU
和 embedding 的资源锁不变。当前最多接纳 10 个活跃任务，仍受 10 GiB 临时存储预算约束。

## 实现

- `video_config.py` 加载并校验整数并发值 1–16。
- `pipeline_fence.py` 用六个 PostgreSQL session advisory lock 作为共享请求名额；
  第七个请求等待，名额释放后恢复，暂停时不再发起请求。第零号保留旧锁的名称。
- `video_pipeline.py` 启动六个独立 LLM 消费者，并调整有限任务接纳量。每个任务
  保持独立 provider、lease、attempt token 和检查点。下载、准备、发布不扩并发。

## 验证

97 项相关回归测试通过，无跳过；新增的活跃任务上限测试另有 1 项通过。
其中实际 PostgreSQL 测试覆盖六请求上限、第七请求等待、释放后续跑、等待时暂停，
以及真实调度器同时处理六个合成 fixture 任务。合成数据库记录使用独立库标识并清理，
未调用模型或修改生产任务。编译、Ruff 和 diff whitespace 检查通过。

随后通过生产使用的 `CCRProvider` 和新限流器发送了 6 条简单算术请求，
实际同时获准的名额峰值为 6，全部 HTTP 200、完整流、有效 JSON、答案正确。
返回模型均为 GPT-6 Luna，reasoning=medium；最长请求 4.05 秒。
测试只使用合成文本，没有发送保留的视频帧或转写。

重载前先暂停新操作并排空在途操作，然后只重载原先运行的视频 worker。
新 worker PID 为 2089947，其 `queue-worker.json` 同时确认
`ccr_concurrency=6` 和 `llm_consumers=6`。切换后 30 秒的数据库只读观察实际看到
六个 LLM 消费者锁；生产 CCR 同时在途峰值为 5，
请求槽占用峰值为 5。这是上限为六的运行模式，
实际在途数量由准备好的任务和请求阶段决定。

本次验证证明配置、调度和限流已经生效，不证明真实八帧的固定提速倍数或长期成功率。
CCR 模型与 reasoning 设置未更改，没有重启 CCR、ASR 或 embedding 服务。

## 本地记录与恢复

`.runtime/ccr-concurrency6-rollout/` 保存切换前源码与 runtime profile、队列/服务
状态、`synthetic-smoke.json`、`deployment.json` 和 `observation.json`。

若需要回到单并发，将 active profile 的 `ccr_concurrency` 改成 1，并使用相同的
暂停、排空、worker 重载、恢复队列流程。不要在新旧 worker 仍有请求在途时切换。
已有源码修改已原样保留在切换前快照，恢复整个文件前需核对后续编辑。

结构化结果：`ccr-six-concurrency-20261010.json`。
