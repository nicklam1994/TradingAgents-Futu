"""
Telegram Report Renderer v3
============================
Uses telegramify-markdown's async `telegramify()` API.
Returns plain text + entity dicts — no parse_mode needed, avoids all escaping issues.
"""

import asyncio
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

MAX_LEN = 4096


def render_report(markdown_text: str) -> List[Tuple[str, list]]:
    """Convert Markdown to list of (text, entities) tuples.

    Uses telegramify() async API. Each returned item is (plain_text, entity_dicts).
    Callers send with BotResponse(text=..., entities=...).
    """
    try:
        from telegramify_markdown import telegramify, Text

        segments = asyncio.run(
            telegramify(markdown_text, max_message_length=MAX_LEN, render_mermaid=False)
        )

        result: List[Tuple[str, list]] = []
        for seg in segments:
            if isinstance(seg, Text):
                entity_dicts = [e.to_dict() for e in seg.entities]
                result.append((seg.text, entity_dicts))
            elif hasattr(seg, "text"):
                result.append((seg.text, []))
            else:
                result.append((str(seg), []))

        if result:
            return result

    except Exception as exc:
        logger.warning("[telegram_renderer] telegramify failed: %s", exc)

    # Fallback: plain text
    return [(chunk, []) for chunk in _chunk_plain(markdown_text)]


def render_report_plain(markdown_text: str) -> List[Tuple[str, list]]:
    """Plain text version — strips all markdown formatting."""
    import re

    text = markdown_text
    text = re.sub(r"^#{1,4}\s+", "", text, flags=re.MULTILINE)
    text = text.replace("**", "")
    lines = []
    for line in text.split("\n"):
        if line.strip().startswith("|") and "|" in line[1:]:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            lines.append("  ".join(cells))
        else:
            lines.append(line)
    text = "\n".join(lines)
    text = text.replace("---", "━" * 20)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return [(chunk, []) for chunk in _chunk_plain(text.strip())]


def _chunk_plain(text: str, max_len: int = MAX_LEN) -> List[str]:
    """Split text into chunks ≤ max_len at paragraph boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks, current = [], ""
    for para in text.split("\n\n"):
        if len(current) + len(para) + 2 > max_len:
            if current:
                chunks.append(current.strip())
            if len(para) > max_len:
                for i in range(0, len(para), max_len):
                    chunks.append(para[i : i + max_len])
                current = ""
            else:
                current = para
        else:
            current = current + "\n\n" + para if current else para
    if current.strip():
        chunks.append(current.strip())
    return chunks or [text[:max_len]]
