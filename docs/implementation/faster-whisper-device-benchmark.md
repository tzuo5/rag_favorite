# Faster Whisper CPU / GPU 本地测速

同一段中文做菜视频，音频 168.44 秒；每组首次转录后预热一次，再测量三次。以下速度使用三次正式测量的中位数，不包含音频解码、模型加载和下载。

| 配置 | 三次耗时（秒） | 中位数（秒） | 范围（秒） | 实时因子 | 加载（秒） | 首次转录（秒） |
|---|---|---:|---|---:|---:|---:|
| medium-cpu (int8) | 60.888, 60.984, 61.246 | 60.984 | 60.888–61.246 | 0.3621 | 1.204 | 62.428 |
| medium-gpu (float16) | 12.699, 12.569, 12.595 | 12.595 | 12.569–12.699 | 0.0748 | 0.851 | 12.653 |
| Large-v3 GPU FP16 | 显存不足，加载失败 | — | — | — | — | — |

GPU Medium 相比 CPU Medium 快 **4.84 倍**，节省转录时间 **79.3%**。这是 CPU INT8（4 线程）与 GPU FP16 的实际配置比较，不能将全部差异归因于硬件。

Large-v3 FP16 在模型加载时 CUDA OOM。用户选择保留 LM Studio 向量模型，不进一步释放显存；因此没有 Large-v3 与 Medium 的速度倍数。

## 配置与限制

- CPU：Intel Core i7-13700H；GPU：RTX 4060 Laptop 8 GB。
- Faster Whisper 1.2.1；CTranslate2 4.8.2；beam_size=5、VAD 开启、词级时间戳开启、自动识别语言、num_workers=1、普通单文件转录。
- 输入转为 16 kHz 单声道 PCM，三组使用同一音频；CPU 和 GPU 成功组的输入哈希与参数已核对一致。
- Medium revision: 08e178d48790749d25932bbc082711ddcfdfbc4f；Large-v3 revision: edaa852ec7e145841d8ffdb056a99866b5f0a478。
- NVIDIA 驱动 595.91.07 与 NVML 库 595.99 不一致；实际 CUDA 转录通过，未提供 GPU 峰值显存。
- ImageBind 和视频 worker 在正式测速期间暂停；其他桌面/GPU 程序未停止。CPU 首次运行曾与 10 秒 GPU 验证重叠，此次首次耗时不用于速度倍数。
- CPU / GPU Medium 分别输出 81 / 110 个片段；没有人工标准稿，不报告准确率或质量排名。

## 复现与原始输出

- 测速脚本：`ingestion/benchmark/compare_devices.py`。
- 完整参数和结果：`docs/implementation/faster-whisper-device-benchmark-results.json`。
- 音频、每次文本及词级时间戳、日志和固定版本 Large-v3：`.runtime/whisper-benchmark-20261011`。
- GPU 调用需要将当前环境 `torch/lib` 加入 `LD_LIBRARY_PATH`；本次未安装依赖或修改生产配置。

## 恢复验证

ImageBind 健康接口返回 `ok=true`；ImageBind 和视频 worker 均为 `active/running`，队列全局 `paused=false`；向量模型全程未卸载。单个任务暂停列表在期间发生外部变化，恢复时保留最新列表。此处确认服务和队列开关恢复，不代表已有任务已完成。
