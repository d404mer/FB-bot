"""Helpers: comment hash, delay, logging, MarkdownV2 escaping."""
import hashlib
import logging
import os
import random
import re
import time

# Characters that must be escaped for Telegram MarkdownV2
MARKDOWN_V2_ESCAPE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")


def comment_hash(author: str, date: str, text: str) -> str:
    """Stable hash for a comment (author + date + text)."""
    raw = f"{author}|{date}|{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_delay(seconds: float, jitter: float = 2.0) -> None:
    """Sleep for seconds with optional random jitter (e.g. REQUEST_DELAY ± jitter)."""
    delay = max(0, seconds + random.uniform(-jitter, jitter))
    time.sleep(delay)


def escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    if not text:
        return ""
    return MARKDOWN_V2_ESCAPE.sub(r"\\\1", text)


def setup_logging(log_file: str | None = None) -> None:
    """Configure root logger: console + optional file, with level and format."""
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_path = os.path.abspath(log_file)
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt=datefmt,
        handlers=handlers,
        force=True,
    )
