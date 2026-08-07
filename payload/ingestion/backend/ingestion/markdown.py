from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import yaml

from .models import Enrichment, SourceMetadata, utc_now
from .security import safe_filename


def infer_domain(metadata: SourceMetadata, enrichment: Enrichment) -> str:
    author = (metadata.author or "").casefold()
    if "mr jonathan" in author:
        return "career"
    text = " ".join(filter(None, [metadata.original_title, metadata.description, *enrichment.tags])).lower()
    rules = (
        ("cooking", ("菜谱", "食谱", "烹饪", "料理", "烘焙", "鸡尾酒", "recipe", "cooking")),
        ("finance", ("投资", "基金", "股票", "理财", "资产配置", "investment", "portfolio")),
        ("technology", ("编程", "代码", "服务器", "数据库", "人工智能", "python", "c++", "software", "database", "server")),
        (
            "social-conduct",
            (
                "人情世故", "说话艺术", "沟通分寸", "官场", "体制内",
                "酒局", "饭局", "敬酒", "察言观色", "为人处世",
                "social etiquette", "social conduct",
            ),
        ),
        (
            "career",
            (
                "求职", "招聘", "面试", "终面", "内推", "领英", "岗位",
                "简历", "职场", "老板", "领导", "升职", "不升", "晋升", "绩效", "linkedin",
                "career", "resume", "interview", "promotion",
            ),
        ),
        ("learning", ("教程", "课程", "学习", "研究", "lecture", "tutorial", "course")),
    )
    for domain, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return domain
    return "reference"


def retrieval_aliases(metadata: SourceMetadata, enrichment: Enrichment) -> list[str]:
    values = ["视频归档", "视频转录", f"{metadata.platform.value}视频", enrichment.normalized_title]
    return list(dict.fromkeys(item for item in values if item))


def transcript_body(value: str) -> str:
    value = re.sub(r"^# Video Transcription\s*", "", value.strip())
    value = re.sub(r"\*\*Detected Language:\*\*[^\n]*\n?", "", value)
    value = re.sub(r"\*\*Language Probability:\*\*[^\n]*\n?", "", value)
    value = re.sub(r"^## Transcription Content\s*", "", value.strip())
    return value.strip()


class KnowledgeFileBuilder:
    def build(
        self,
        *,
        document_id: str,
        metadata: SourceMetadata,
        enrichment: Enrichment,
        transcript: str,
        telegram_chat_id: str,
        telegram_message_id: str,
        cover_filename: str | None = None,
    ) -> tuple[str, str]:
        body = transcript_body(transcript)
        checksum = hashlib.sha256(body.replace("\r\n", "\n").encode("utf-8")).hexdigest()
        domain = infer_domain(metadata, enrichment)
        frontmatter = {
            "id": document_id, "title": enrichment.normalized_title,
            "original_title": metadata.original_title,
            "source_platform": metadata.platform.value, "source_url": metadata.canonical_url,
            "source_id": metadata.source_id, "author": metadata.author,
            "published_at": metadata.published_at, "captured_at": utc_now().isoformat(),
            "language": metadata.language or "und", "duration_seconds": metadata.duration_seconds,
            "tags": enrichment.tags, "content_type": "video-transcript",
            "domain": domain, "lifecycle": "curated",
            "retrieval_aliases": retrieval_aliases(metadata, enrichment), "database": None,
            "checksum": checksum, "telegram_chat_id": telegram_chat_id,
            "telegram_message_id": telegram_message_id,
        }
        if cover_filename:
            frontmatter["cover_image"] = cover_filename
        source_url = metadata.canonical_url or "无"
        cover = f"![[{cover_filename}]]\n\n" if cover_filename else ""
        platform_label = (
            "小红书"
            if metadata.platform.value == "xiaohongshu"
            else metadata.platform.value
        )
        source_link = (
            f"[在{platform_label}查看原视频]({source_url})"
            if metadata.canonical_url
            else "无"
        )
        text = (
            "---\n" + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n\n"
            f"# {enrichment.normalized_title}\n\n{cover}"
            f"## 摘要\n\n{enrichment.summary}\n\n"
            "## 关键观点\n\n" + "\n".join(f"- {point}" for point in enrichment.key_points) + "\n\n"
            "## 来源信息\n\n"
            f"- 平台：{metadata.platform.value}\n- 作者：{metadata.author or '未知'}\n"
            f"- 原始链接：{source_link}\n- 发布时间：{metadata.published_at or '未知'}\n"
            f"- 视频时长：{metadata.duration_seconds if metadata.duration_seconds is not None else '未知'}\n\n"
            f"## Transcript\n\n{body}\n"
        )
        return text, checksum

    def atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)

    def staging_path(self, root: Path, job_id: str, title: str) -> Path:
        return root / f"{safe_filename(title)}--{job_id[:8]}.md"


def fallback_enrichment(title: str | None, transcript: str) -> Enrichment:
    body = transcript_body(transcript)
    sentences = [item.strip() for item in re.split(r"(?<=[。！？.!?])\s*|\n+", body) if len(item.strip()) >= 8]
    title = (title or (sentences[0][:60] if sentences else "未命名视频转录")).strip()
    key_points = (sentences[:5] or ["完整内容见 Transcript。"])
    summary = " ".join(sentences[:3])[:1200] or "已保存完整转录，暂无可用摘要。"
    title_tokens = re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9+.-]{2,}", title)
    tokens = re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9+.-]{2,}", body)
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    # A single ASR occurrence is too weak to become a durable tag. Title terms
    # are trusted, while transcript-only terms need repetition.
    tags = list(dict.fromkeys(title_tokens))
    tags.extend(key for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])) if count >= 2 and key not in tags)
    tags = tags[:12]
    for default in ("自动转录", "语音识别", "内容摘要", "时间戳文本", "多语言内容", "全文检索", "来源归档", "知识库索引"):
        if len(tags) >= 8: break
        tags.append(default)
    return Enrichment(normalized_title=title[:300], summary=summary, key_points=key_points, tags=tags[:20])
