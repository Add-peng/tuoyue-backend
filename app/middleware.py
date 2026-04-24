"""Sensitive word filtering utilities and middleware."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("tuoyue.sensitive_words")


class DFASensitiveFilter:
    """A simple DFA/trie based sensitive word matcher."""

    def __init__(self, words_path: str | None = None) -> None:
        self._root: dict[str, dict[str, Any]] = {}
        self.words_path = Path(words_path) if words_path else Path(__file__).resolve().parent / "data" / "sensitive_words.txt"
        self._load_words()

    def _load_words(self) -> None:
        path = self.words_path
        if not path.exists():
            alt_path = Path(__file__).resolve().parent / "data" / "sensitive_words.json"
            if alt_path.exists():
                path = alt_path
            else:
                logger.warning("Sensitive word file not found: %s", self.words_path)
                return

        try:
            raw_text = path.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except Exception as exc:
            logger.error("Failed to load sensitive word list: %s", exc)
            return

        words = data.get("words", []) if isinstance(data, dict) else []
        if not isinstance(words, list):
            logger.warning("Sensitive word payload is not a list: %s", path)
            return

        for word in words:
            if isinstance(word, str):
                self.add_word(word)

        logger.info("Loaded %d sensitive words from %s", len(words), path)

    def add_word(self, word: str) -> None:
        word = word.strip()
        if not word:
            return

        node = self._root
        for char in word:
            node = node.setdefault(char, {})
        node["_end"] = True

    def contains_sensitive(self, text: str) -> bool:
        if not text:
            return False

        for start in range(len(text)):
            node = self._root
            for char in text[start:]:
                if char not in node:
                    break
                node = node[char]
                if node.get("_end"):
                    return True
        return False


sensitive_filter = DFASensitiveFilter()


class SensitiveWordMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method != "POST" or request.url.path != "/api/generate":
            return await call_next(request)

        body = await request.body()
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return await call_next(request)

        topic = payload.get("topic")
        if isinstance(topic, str) and sensitive_filter.contains_sensitive(topic):
            return JSONResponse(
                status_code=400,
                content={"detail": "输入内容包含敏感词，请修改后重试。"},
            )

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = receive  # type: ignore[attr-defined]
        return await call_next(request)


# Backwards compatible helper names used by existing imports.
init_sensitive_word_matcher = lambda: sensitive_filter
get_matcher = lambda: sensitive_filter
