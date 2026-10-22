"""Legacy ingestion must hand XHS networking to the managed RAG session."""

from urllib.parse import urlsplit


def require_managed_xhs(url):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if any(host == domain or host.endswith("." + domain)
           for domain in ("xiaohongshu.com", "xhslink.cn", "xhslink.com", "xhscdn.com")):
        raise ValueError("XHS_SESSION_SERVICE_REQUIRED: 请使用 RAG 统一队列导入小红书内容。")
