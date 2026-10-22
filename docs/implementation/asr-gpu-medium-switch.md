# 本地转录切换为 GPU Medium

当前 `.runtime/videorag/video.toml` 使用已有 Medium，设置 `asr_device="cuda"`、`asr_compute_type="float16"`。CPU 旧配置的默认行为保持不变。

RTX 4060 Laptop 的现有显存无法容纳 ImageBind 和 Medium 同时常驻，因此 GPU ASR 与视觉推理共享锁；转录期间 ImageBind 暂存 CPU，完成后 Whisper 权重暂存 CPU并恢复 ImageBind 到 GPU。该方式保留模型内存副本，避免每次从磁盘重新加载。异常路径也会尝试恢复视觉模型，不回退 CPU 转录。

转录缓存指纹加入设备和计算精度，切换后不复用旧 CPU ASR 切片缓存；已有知识库数据无需重新生成，视觉向量空间未改变。

实际服务验收：30 秒中文音频连续两次返回 `local-faster-whisper-medium-cuda-float16`，耗时 4.721 秒、3.389 秒（包括接口与模型搬移开销）；每次转录后文本向量为 1024 维，随后真实视频向量验收通过（0.908 秒）。75 项相关测试通过，静态检查通过。

ImageBind 和视频 worker 已恢复为 active，队列全局暂停解除，LM Studio 向量模型未卸载。完整接口结果见 `asr-gpu-medium-switch-results.json`；原始文本和配置备份位于 `.runtime/asr-gpu-medium-switch/`。

回退方式：将配置中的 ASR 设备/精度改回 `cpu` / `int8`，在暂停队列后重启 ImageBind 和视频 worker，再恢复原队列开关。
