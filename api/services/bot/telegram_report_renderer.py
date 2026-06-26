"""
Telegram Report Renderer
========================
Converts Markdown analysis reports into Telegram-friendly format:
- ### headers → bold headers
- **bold** → *bold* (Telegram MarkdownV2)
- Tables → aligned plain text
- --- → ━━━━━━━
- Smart segmentation by sections (≤4000 chars per message)
- Preserves emojis
"""

import re
from typing import List

# Telegram max message length
MAX_LEN = 4000

# ── Markdown → Telegram conversion ──────────────────────────────────────────

def _convert_headers(text: str) -> str:
    """### Header → **Header** (bold)"""
    # ### Header → **Header**
    text = re.sub(r'^#{1,4}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    return text


def _convert_bold(text: str) -> str:
    """**bold** → *bold* (Telegram MarkdownV2 uses * for bold)"""
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    return text


def _convert_tables(text: str) -> str:
    """Convert markdown tables to aligned plain text."""
    lines = text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Detect table: starts with | and has at least 2 |
        if line.startswith('|') and line.count('|') >= 3:
            # Check if next line is separator (|---|---|)
            if i + 1 < len(lines) and re.match(r'^[\|\s\-:]+$', lines[i + 1].strip()):
                # Parse header
                headers = [c.strip() for c in line.split('|')[1:-1]]
                # Parse rows
                rows = []
                j = i + 2  # skip header + separator
                while j < len(lines) and lines[j].strip().startswith('|'):
                    cells = [c.strip() for c in lines[j].split('|')[1:-1]]
                    rows.append(cells)
                    j += 1

                # Calculate column widths
                col_widths = [len(h) for h in headers]
                for row in rows:
                    for k, cell in enumerate(row):
                        if k < len(col_widths):
                            col_widths[k] = max(col_widths[k], len(cell))

                # Format as aligned text
                header_line = '  '.join(h.ljust(col_widths[k]) for k, h in enumerate(headers))
                result.append(header_line)
                result.append('─' * len(header_line))
                for row in rows:
                    row_line = '  '.join(
                        (cell.ljust(col_widths[k]) if k < len(col_widths) else cell)
                        for k, cell in enumerate(row)
                    )
                    result.append(row_line)
                result.append('')  # blank line after table
                i = j
                continue
        result.append(lines[i])
        i += 1
    return '\n'.join(result)


def _convert_separators(text: str) -> str:
    """--- → ━━━━━━━━━━━━━━━━━━━━"""
    text = re.sub(r'^-{3,}$', '━' * 22, text, flags=re.MULTILINE)
    text = re.sub(r'^\*{3,}$', '━' * 22, text, flags=re.MULTILINE)
    return text


def _convert_italic(text: str) -> str:
    """*italic* stays as is (already Telegram format)."""
    return text


def _clean_links(text: str) -> str:
    """[text](url) → text (url)"""
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', text)
    return text


def convert_md_to_telegram(text: str) -> str:
    """Full conversion pipeline: Markdown → Telegram-friendly text."""
    text = _convert_headers(text)
    text = _convert_bold(text)
    text = _convert_tables(text)
    text = _convert_separators(text)
    text = _convert_italic(text)
    text = _clean_links(text)
    # Clean up excessive blank lines
    text = re.sub(r'\n{4,}', '\n\n\n', text)
    return text.strip()


# ── Smart segmentation ───────────────────────────────────────────────────────

def _split_by_sections(text: str) -> List[str]:
    """Split text into logical sections by ### headers."""
    # Split on lines that start with * (converted from ###)
    parts = re.split(r'(?=^\*[A-Z\u4e00-\u9fff])', text, flags=re.MULTILINE)
    return [p.strip() for p in parts if p.strip()]


def segment_report(text: str, max_len: int = MAX_LEN) -> List[str]:
    """
    Split a report into Telegram-friendly chunks.
    Each chunk ≤ max_len characters, split at section boundaries.
    """
    sections = _split_by_sections(text)
    
    chunks = []
    current = ""
    
    for section in sections:
        # If single section exceeds max_len, split by paragraphs
        if len(section) > max_len:
            # Flush current
            if current:
                chunks.append(current.strip())
                current = ""
            # Split large section by double newlines
            paragraphs = section.split('\n\n')
            for para in paragraphs:
                if len(current) + len(para) + 2 > max_len:
                    if current:
                        chunks.append(current.strip())
                    # If single paragraph still too long, hard split
                    if len(para) > max_len:
                        for i in range(0, len(para), max_len):
                            chunks.append(para[i:i + max_len])
                        current = ""
                    else:
                        current = para
                else:
                    current = current + '\n\n' + para if current else para
        elif len(current) + len(section) + 2 > max_len:
            # Flush current, start new chunk
            if current:
                chunks.append(current.strip())
            current = section
        else:
            current = current + '\n\n' + section if current else section
    
    if current.strip():
        chunks.append(current.strip())
    
    return chunks if chunks else [text[:max_len]]


# ── Public API ───────────────────────────────────────────────────────────────

def render_report(markdown_text: str) -> List[str]:
    """
    Convert a Markdown report to Telegram-ready message chunks.
    
    Returns a list of strings, each ≤ 4000 chars, ready to send
    with parse_mode="Markdown" (legacy) or as plain text.
    
    Usage:
        chunks = render_report(report_md)
        for chunk in chunks:
            await send_telegram(chat_id, chunk)
    """
    # Step 1: Convert Markdown → Telegram format
    converted = convert_md_to_telegram(markdown_text)
    
    # Step 2: Segment into chunks
    chunks = segment_report(converted)
    
    return chunks


def render_report_plain(markdown_text: str) -> List[str]:
    """
    Same as render_report() but strips all formatting for plain text mode.
    Useful when Telegram Markdown parsing fails.
    """
    converted = convert_md_to_telegram(markdown_text)
    # Strip remaining * bold markers
    converted = converted.replace('*', '')
    chunks = segment_report(converted)
    return chunks
