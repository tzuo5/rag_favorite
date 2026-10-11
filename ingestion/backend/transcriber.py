import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class Transcriber:
    """音频转录器，使用Faster-Whisper进行语音转文字"""
    
    def __init__(
        self,
        model_size: Optional[str] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
        cpu_threads: Optional[int] = None,
        num_workers: Optional[int] = None,
        beam_size: Optional[int] = None,
    ):
        """
        初始化转录器
        
        Args:
            model_size: Whisper模型大小 (tiny, base, small, medium, large)
        """
        self.model_size = model_size or os.getenv("WHISPER_MODEL_SIZE", "medium")
        self.device = device or os.getenv("WHISPER_DEVICE", "cpu")
        self.compute_type = compute_type or os.getenv("WHISPER_COMPUTE_TYPE", "int8")
        self.cpu_threads = cpu_threads or int(os.getenv("WHISPER_CPU_THREADS", "2"))
        self.num_workers = num_workers or int(os.getenv("WHISPER_NUM_WORKERS", "1"))
        self.beam_size = beam_size or int(os.getenv("WHISPER_BEAM_SIZE", "1"))
        self.model = None
        self.last_detected_language = None
        
    def _load_model(self):
        """延迟加载模型"""
        if self.model is None:
            from faster_whisper import WhisperModel
            logger.info(f"正在加载Whisper模型: {self.model_size}")
            try:
                self.model = WhisperModel(
                    self.model_size,
                    device=self.device,
                    compute_type=self.compute_type,
                    cpu_threads=self.cpu_threads,
                    num_workers=self.num_workers,
                )
                logger.info("模型加载完成")
            except Exception as e:
                logger.error(f"模型加载失败: {str(e)}")
                raise Exception(f"模型加载失败: {str(e)}")
    
    async def transcribe(self, audio_path: str, language: Optional[str] = None) -> str:
        return (await self.transcribe_result(audio_path, language)).to_markdown()

    async def transcribe_result(self, audio_path: str, language: Optional[str] = None):
        """
        转录音频文件
        
        Args:
            audio_path: 音频文件路径
            language: 指定语言（可选，如果不指定则自动检测）
            
        Returns:
            转录文本（Markdown格式）
        """
        try:
            # 检查文件是否存在
            if not os.path.exists(audio_path):
                raise Exception(f"音频文件不存在: {audio_path}")
            
            # 加载模型
            import asyncio
            await asyncio.to_thread(self._load_model)
            
            logger.info(f"开始转录音频: {audio_path}")
            
            # 直接调用会阻塞事件循环；放入线程避免阻塞
            from backend.ingestion.temporal_models import TranscriptResult, TranscriptSegment, segment_id
            def _do_transcribe():
                segments, info = self.model.transcribe(
                    audio_path,
                    language=language,
                    beam_size=self.beam_size,
                    best_of=max(1, self.beam_size),
                    temperature=[0.0, 0.2, 0.4],  # 使用温度递增策略
                    # 更稳健：开启VAD与阈值，降低静音/噪音导致的重复
                    vad_filter=True,
                    vad_parameters={
                        "min_silence_duration_ms": 900,  # 静音检测时长
                        "speech_pad_ms": 300  # 语音填充
                    },
                    no_speech_threshold=0.7,  # 无语音阈值
                    compression_ratio_threshold=2.3,  # 压缩比阈值，检测重复
                    log_prob_threshold=-1.0,  # 日志概率阈值
                    # 避免错误累积导致的连环重复
                    condition_on_previous_text=False
                )
                return list(segments), info
            segments, info = await asyncio.to_thread(_do_transcribe)
            
            detected_language = info.language
            self.last_detected_language = detected_language  # 保存检测到的语言
            logger.info(f"检测到的语言: {detected_language}")
            logger.info(f"语言检测概率: {info.language_probability:.2f}")
            
            # 组装转录结果
            transcript_text = TranscriptResult(language=detected_language, language_probability=info.language_probability, timing_precision="segment",
                segments=[TranscriptSegment(id=segment_id(i, s.text.strip(), s.start, s.end),
                    text=s.text.strip(), start=s.start, end=s.end) for i, s in enumerate(segments)])
            logger.info("转录完成")
            
            return transcript_text
            
        except Exception as e:
            logger.error(f"转录失败: {str(e)}")
            raise Exception(f"转录失败: {str(e)}")
    
    def _format_time(self, seconds: float) -> str:
        """
        将秒数转换为时分秒格式
        
        Args:
            seconds: 秒数
            
        Returns:
            格式化的时间字符串
        """
        from backend.ingestion.temporal_models import format_time
        return format_time(seconds)
    
    def get_supported_languages(self) -> list:
        """
        获取支持的语言列表
        """
        return [
            "zh", "en", "ja", "ko", "es", "fr", "de", "it", "pt", "ru",
            "ar", "hi", "th", "vi", "tr", "pl", "nl", "sv", "da", "no"
        ]
    
    def get_detected_language(self, transcript_text: Optional[str] = None) -> Optional[str]:
        """
        获取检测到的语言
        
        Args:
            transcript_text: 转录文本（可选，用于从文本中提取语言信息）
            
        Returns:
            检测到的语言代码
        """
        # 如果有保存的语言，直接返回
        if self.last_detected_language:
            return self.last_detected_language
        
        # 如果提供了转录文本，尝试从中提取语言信息
        if transcript_text and "**Detected Language:**" in transcript_text:
            lines = transcript_text.split('\n')
            for line in lines:
                if "**Detected Language:**" in line:
                    lang = line.split(":")[-1].strip()
                    return lang if lang else None
        
        return None
