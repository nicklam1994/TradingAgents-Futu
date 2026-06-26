"""
Telegram Report Renderer v4
============================
Uses telegramify-markdown's synchronous `convert()` API.
Returns plain text + entity dicts — no parse_mode needed, no async issues.
Splits in UTF-16 space to preserve entity boundaries.
"""

import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

MAX_LEN = 4096  # Telegram limit in UTF-16 code units


def render_report(markdown_text: str) -> List[Tuple[str, list]]:
    """Convert Markdown to list of (text, entities) tuples.

    Uses convert() synchronous API. Each returned item is (plain_text, entity_dicts).
    Callers send with BotResponse(text=..., entities=...).
    """
    try:
        from telegramify_markdown.converter import convert

        text, entities = convert(markdown_text)
        entity_dicts = [e.to_dict() for e in entities]

        # Split in UTF-16 space to preserve entity boundaries
        return _split_utf16(text, entity_dicts)

    except Exception as exc:
        logger.warning("[telegram_renderer] convert failed: %s", exc)

    # Fallback: plain text
    return [(chunk, []) for chunk in _chunk_plain(markdown_text)]


def _split_utf16(text: str, entities: list, max_units: int = MAX_LEN) -> List[Tuple[str, list]]:
    """Split text+entities in UTF-16 code-unit space.

    Telegram measures entity offset/length in UTF-16 code units.
    We encode to UTF-16LE (no BOM), split the bytes, then decode back.
    """
    # Encode entire text to UTF-16LE (2 bytes per code unit, no BOM)
    encoded = text.encode("utf-16-le")
    total_units = len(encoded) // 2

    if total_units <= max_units:
        return [(text, entities)]

    chunks = []
    unit_pos = 0

    while unit_pos < total_units:
        end_unit = min(unit_pos + max_units, total_units)

        if end_unit < total_units:
            # Try to split at a newline (UTF-16 code unit for \n = 0x000A)
            # Search backwards for \n in the UTF-16 bytes
            best_split = end_unit
            for probe in range(end_unit - 1, unit_pos, -1):
                byte_offset = probe * 2
                if encoded[byte_offset] == 0x0A and encoded[byte_offset + 1] == 0x00:
                    best_split = probe + 1  # include the newline
                    break
            end_unit = best_split

        # Extract chunk bytes and decode
        chunk_bytes = encoded[unit_pos * 2 : end_unit * 2]
        chunk_text = chunk_bytes.decode("utf-16-le")

        # Extract entities that fall within this chunk
        chunk_entities = []
        for e in entities:
            e_start = e["offset"]
            e_end = e_start + e["length"]
            # Entity overlaps with [unit_pos, end_unit)
            if e_start < end_unit and e_end > unit_pos:
                new_e = dict(e)
                new_e["offset"] = max(0, e_start - unit_pos)
                new_e["length"] = min(e_end, end_unit) - max(e_start, unit_pos)
                if new_e["length"] > 0:
                    chunk_entities.append(new_e)

        chunks.append((chunk_text, chunk_entities))
        unit_pos = end_unit

    return chunks


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
