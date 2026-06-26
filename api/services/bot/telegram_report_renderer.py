"""
Telegram Report Renderer v2
============================
Uses telegramify-markdown for proper MarkdownV2 conversion.
Falls back to plain text if conversion fails.
"""

import logging
from typing import List

logger = logging.getLogger(__name__)

MAX_LEN = 4096  # Telegram message limit


def render_report(markdown_text: str) -> List[str]:
    """
    Convert a Markdown report to Telegram MarkdownV2 message chunks.

    Uses telegramify-markdown for proper conversion:
    - ### headers → emoji + bold
    - **bold** → _bold_ (MarkdownV2 italic/bold)
    - Tables → code blocks
    - --- → separator lines

    Returns a list of strings, each ≤ 4096 chars, with parse_mode="MarkdownV2".
    """
    try:
        from telegramify_markdown import standardize, markdownify, split_markdownv2

        # Step 1: Standardize markdown
        std = standardize(markdown_text)

        # Step 2: Convert to MarkdownV2
        md2 = markdownify(std)

        # Step 3: Split into chunks
        chunks = split_markdownv2(md2)

        if chunks:
            return chunks

    except Exception as exc:
        logger.warning("[telegram_renderer] telegramify failed, using plain: %s", exc)

    # Fallback: plain text with basic formatting
    return _fallback_chunks(markdown_text)


def render_report_plain(markdown_text: str) -> List[str]:
    """
    Plain text version — strips all markdown formatting.
    Use when Telegram MarkdownV2 parsing keeps failing.
    """
    import re
    text = markdown_text
    # Strip headers
    text = re.sub(r'^#{1,4}\s+', '', text, flags=re.MULTILINE)
    # Strip bold
    text = text.replace('**', '')
    # Strip tables → simple text
    lines = []
    for line in text.split('\n'):
        if line.strip().startswith('|') and '|' in line[1:]:
            cells = [c.strip() for c in line.split('|')[1:-1]]
            if all(set(c) <= set('-: ') for c in cells):
                continue  # skip separator
            lines.append('  '.join(cells))
        else:
            lines.append(line)
    text = '\n'.join(lines)
    # Strip separators
    text = text.replace('---', '━' * 20)
    # Clean excessive blank lines
    text = re.sub(r'\n{4,}', '\n\n\n', text)

    return _chunk_text(text.strip())


def _fallback_chunks(text: str) -> List[str]:
    """Simple chunking for fallback."""
    return _chunk_text(text)


def _chunk_text(text: str, max_len: int = MAX_LEN) -> List[str]:
    """Split text into chunks ≤ max_len at paragraph boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = ""
    for para in text.split('\n\n'):
        if len(current) + len(para) + 2 > max_len:
            if current:
                chunks.append(current.strip())
            # If single paragraph too long, hard split
            if len(para) > max_len:
                for i in range(0, len(para), max_len):
                    chunks.append(para[i:i + max_len])
                current = ""
            else:
                current = para
        else:
            current = current + '\n\n' + para if current else para

    if current.strip():
        chunks.append(current.strip())

    return chunks if chunks else [text[:max_len]]
