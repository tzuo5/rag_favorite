from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import psutil
from faster_whisper import WhisperModel


audio = Path(__file__).with_name("mixed-zh-en-ja.wav")
process = psutil.Process()
peak_rss = process.memory_info().rss
running = True


def monitor() -> None:
    global peak_rss
    while running:
        peak_rss = max(peak_rss, process.memory_info().rss)
        time.sleep(0.02)


thread = threading.Thread(target=monitor, daemon=True)
thread.start()
started = time.perf_counter()
model = WhisperModel(
    os.getenv("WHISPER_MODEL_SIZE", "base"),
    device=os.getenv("WHISPER_DEVICE", "cpu"),
    compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
    cpu_threads=int(os.getenv("WHISPER_CPU_THREADS", "2")),
    num_workers=int(os.getenv("WHISPER_NUM_WORKERS", "1")),
)
segments, info = model.transcribe(
    str(audio), beam_size=int(os.getenv("WHISPER_BEAM_SIZE", "1")),
    vad_filter=True, condition_on_previous_text=False,
)
texts = [segment.text.strip() for segment in segments]
elapsed = time.perf_counter() - started
running = False
thread.join(timeout=1)
duration = float(info.duration)
print(json.dumps({
    "audio_seconds": round(duration, 3),
    "elapsed_seconds_including_load": round(elapsed, 3),
    "peak_rss_mib": round(peak_rss / 1024 / 1024, 1),
    "real_time_factor": round(elapsed / duration, 3),
    "model": os.getenv("WHISPER_MODEL_SIZE", "base"),
    "device": os.getenv("WHISPER_DEVICE", "cpu"),
    "compute_type": os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
    "cpu_threads": int(os.getenv("WHISPER_CPU_THREADS", "2")),
    "num_workers": int(os.getenv("WHISPER_NUM_WORKERS", "1")),
    "beam_size": int(os.getenv("WHISPER_BEAM_SIZE", "1")),
    "detected_language": info.language,
    "text": " ".join(texts),
}, ensure_ascii=False))
