"""
Telegram Report Renderer v4
============================
Uses telegramify-markdown's synchronous `convert()` API.
Returns plain text + entity dicts — no parse_mode needed, no async issues.
"""

import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

MAX_LEN = 4096


def render_report(markdown_text: str) -> List[Tuple[str, list]]:
    """Convert Markdown to list of (text, entities) tuples.

    Uses convert() synchronous API. Each returned item is (plain_text, entity_dicts).
    Callers send with BotResponse(text=..., entities=...).
    """
    try:
        from telegramify_markdown.converter import convert

        text, entities = convert(markdown_text)
        entity_dicts = [e.to_dict() for e in entities]

        # Split into chunks respecting MAX_LEN
        return _split_with_entities(text, entity_dicts)

    except Exception as exc:
        logger.warning("[telegram_renderer] convert failed: %s", exc)

    # Fallback: plain text
    return [(chunk, []) for chunk in _chunk_plain(markdown_text)]


def _split_with_entities(text: str, entities: list, max_len: int = MAX_LEN) -> List[Tuple[str, list]]:
    """Split text+entities into chunks ≤ max_len at newline boundaries."""
    if len(text) <= max_len:
        return [(text, entities)]

    chunks = []
    pos = 0
    while pos < len(text):
        if pos + max_len >= len(text):
            # Last chunk
            chunk_text = text[pos:]
            chunk_entities = _offset_entities(entities, -pos)
            chunk_entities = [e for e in chunk_entities if e["offset"] >= 0 and e["offset"] + e["length"] <= len(chunk_text)]
            chunks.append((chunk_text, chunk_entities))
            break

        # Find a good split point (newline near max_len)
        split_at = text.rfind("\n", pos, pos + max_len)
        if split_at <= pos:
            split_at = pos + max_len  # No newline found, hard split

        chunk_text = text[pos:split_at]
        chunk_entities = _slice_entities(entities, pos, split_at)
        chunks.append((chunk_text, chunk_entities))
        pos = split_at
        if pos < len(text) and text[pos] == "\n":
            pos += 1  # Skip the newline

    return chunks


def _slice_entities(entities: list, start: int, end: int) -> list:
    """Extract entities within [start, end) and adjust offsets."""
    result = []
    for e in entities:
        e_start = e["offset"]
        e_end = e_start + e["length"]
        # Entity overlaps with chunk
        if e_start < end and e_end > start:
            new_e = dict(e)
            new_e["offset"] = max(0, e_start - start)
            new_e["length"] = min(e_end, end) - max(e_start, start)
            if new_e["length"] > 0:
                result.append(new_e)
    return result


def _offset_entities(entities: list, offset: int) -> list:
    """Shift all entity offsets by offset."""
    result = []
    for e in entities:
        new_e = dict(e)
        new_e["offset"] = e["offset"] + offset
        result.append(new_e)
    return result


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
