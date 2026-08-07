# Whisper benchmark — 2026-07-23

- Host: AMD EPYC 9754 VM, 2 vCPU, 7.4 GiB RAM, no CUDA GPU
- Audio: 39.814 seconds, synthetic Mandarin + English + Japanese, mono 16 kHz
- Model: Faster-Whisper `base`, CPU, `int8`, 2 CPU threads, 1 worker, beam 1
- Elapsed (cold model load included): 37.087 seconds
- Peak process RSS: 526.4 MiB
- Real-time factor: 0.932

Selection: `base` is retained. It fits comfortably alongside PostgreSQL,
OpenClaw and Ollama while remaining approximately real-time on two vCPUs.
Concurrency remains one and the service memory ceiling is 4 GiB. The synthetic
Mandarin/Japanese voice was not a quality corpus, so this run is used for
resource sizing rather than language-accuracy claims.
