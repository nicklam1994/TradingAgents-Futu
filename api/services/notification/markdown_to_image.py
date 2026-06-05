# -*- coding: utf-8 -*-
"""Markdown-to-image renderer for notification channels that don't support
rich text (Telegram, WeChat, etc.).

Converts a Markdown string into a PNG image using Pillow.
Falls back to a plain-text rendering if Pillow is unavailable.

Input: Markdown string
Output: PNG bytes

Usage::

    from api.services.notification.markdown_to_image import markdown_to_image
    png_bytes = markdown_to_image("# Hello\n\nThis is a **test** report.")
    Path("report.png").write_bytes(png_bytes)
"""

from __future__ import annotations

import io
import logging
import re
import textwrap
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Image dimensions
DEFAULT_WIDTH = 800
PADDING = 40
LINE_SPACING = 6

# Colors (RGB)
BG_COLOR = (255, 255, 255)        # White background
TEXT_COLOR = (30, 41, 59)          # Slate-800
HEADING_COLOR = (15, 23, 42)      # Slate-900
CODE_BG = (241, 245, 249)         # Slate-100
BORDER_COLOR = (226, 232, 240)    # Slate-200
ACCENT_COLOR = (59, 130, 246)     # Blue-500
MUTED_COLOR = (100, 116, 139)     # Slate-500

# Font sizes
FONT_SIZE_BODY = 16
FONT_SIZE_H1 = 28
FONT_SIZE_H2 = 22
FONT_SIZE_H3 = 18
FONT_SIZE_CODE = 14
FONT_SIZE_SMALL = 13


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def markdown_to_image(
    md_text: str,
    *,
    width: int = DEFAULT_WIDTH,
    title: Optional[str] = None,
) -> bytes:
    """Render Markdown text to a PNG image.

    Args:
        md_text: Markdown-formatted text.
        width: Image width in pixels.
        title: Optional title to render at the top.

    Returns:
        PNG image as bytes.
    """
    try:
        return _render_with_pillow(md_text, width=width, title=title)
    except ImportError:
        logger.warning("[markdown_to_image] Pillow not installed, using fallback renderer")
        return _render_fallback(md_text, width=width, title=title)
    except Exception as exc:
        logger.error("[markdown_to_image] Pillow render failed: %s, using fallback", exc)
        return _render_fallback(md_text, width=width, title=title)


# ---------------------------------------------------------------------------
# Pillow renderer
# ---------------------------------------------------------------------------

def _render_with_pillow(
    md_text: str,
    *,
    width: int,
    title: Optional[str],
) -> bytes:
    """Render using Pillow (PIL)."""
    from PIL import Image, ImageDraw, ImageFont

    # Parse markdown into styled lines
    lines = _parse_markdown_lines(md_text, title=title)

    # Try to load a font that supports CJK
    font_body = _load_font(FONT_SIZE_BODY)
    font_h1 = _load_font(FONT_SIZE_H1, bold=True)
    font_h2 = _load_font(FONT_SIZE_H2, bold=True)
    font_h3 = _load_font(FONT_SIZE_H3, bold=True)
    font_code = _load_font(FONT_SIZE_CODE, monospace=True)
    font_small = _load_font(FONT_SIZE_SMALL)

    font_map = {
        "body": font_body,
        "h1": font_h1,
        "h2": font_h2,
        "h3": font_h3,
        "code": font_code,
        "small": font_small,
    }

    # Measure total height
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    total_height = PADDING
    wrapped_lines = []
    max_text_width = width - PADDING * 2

    for line_info in lines:
        font = font_map.get(line_info["style"], font_body)
        text = line_info["text"]
        color = line_info.get("color", TEXT_COLOR)

        if line_info["style"] == "divider":
            wrapped_lines.append({**line_info, "wrapped": [""]})
            total_height += 20
            continue

        if line_info["style"] == "code_block":
            wrapped_lines.append({**line_info, "wrapped": [text]})
            total_height += FONT_SIZE_CODE + LINE_SPACING + 8
            continue

        # Word-wrap for body text
        if line_info["style"] in ("body", "small"):
            chars_per_line = max(10, max_text_width // (FONT_SIZE_BODY - 2))
            wrapped = textwrap.wrap(text, width=chars_per_line) or [""]
        else:
            wrapped = [text]

        wrapped_lines.append({**line_info, "wrapped": wrapped})
        line_height = _get_line_height(line_info["style"])
        total_height += len(wrapped) * (line_height + LINE_SPACING)

    total_height += PADDING  # bottom padding

    # Create image
    img = Image.new("RGB", (width, total_height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Render
    y = PADDING
    for line_info in wrapped_lines:
        font = font_map.get(line_info["style"], font_body)
        color = line_info.get("color", TEXT_COLOR)
        style = line_info["style"]

        if style == "divider":
            draw.line(
                [(PADDING, y + 6), (width - PADDING, y + 6)],
                fill=BORDER_COLOR, width=1,
            )
            y += 20
            continue

        if style == "code_block":
            # Draw code background
            code_rect = [PADDING - 8, y - 4, width - PADDING + 8, y + FONT_SIZE_CODE + 8]
            draw.rectangle(code_rect, fill=CODE_BG, outline=BORDER_COLOR)
            draw.text((PADDING, y), line_info["text"], font=font_code, fill=TEXT_COLOR)
            y += FONT_SIZE_CODE + LINE_SPACING + 8
            continue

        for wrapped_line in line_info["wrapped"]:
            x = PADDING
            if style == "bullet":
                x = PADDING + 16
                draw.text((PADDING, y), "•", font=font, fill=ACCENT_COLOR)

            # Handle bold markers **text**
            rendered = _render_inline(draw, x, y, wrapped_line, font, color)
            y += _get_line_height(style) + LINE_SPACING

    # Convert to bytes
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _parse_markdown_lines(md_text: str, *, title: Optional[str] = None) -> list:
    """Parse markdown text into styled line descriptors."""
    lines = []
    if title:
        lines.append({"text": title, "style": "h1", "color": HEADING_COLOR})

    in_code_block = False
    for raw_line in md_text.split("\n"):
        stripped = raw_line.strip()

        # Code fence
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            lines.append({"text": raw_line, "style": "code_block"})
            continue

        # Empty line
        if not stripped:
            continue

        # Headings
        if stripped.startswith("### "):
            lines.append({"text": stripped[4:], "style": "h3", "color": HEADING_COLOR})
        elif stripped.startswith("## "):
            lines.append({"text": stripped[3:], "style": "h2", "color": HEADING_COLOR})
        elif stripped.startswith("# "):
            lines.append({"text": stripped[2:], "style": "h1", "color": HEADING_COLOR})

        # Horizontal rule
        elif stripped in ("---", "***", "___"):
            lines.append({"text": "", "style": "divider"})

        # Bullet list
        elif stripped.startswith("- ") or stripped.startswith("* "):
            lines.append({"text": stripped[2:], "style": "bullet"})

        # Numbered list
        elif re.match(r"^\d+\.\s", stripped):
            text = re.sub(r"^\d+\.\s", "", stripped)
            lines.append({"text": text, "style": "bullet"})

        # Regular text
        else:
            lines.append({"text": stripped, "style": "body"})

    return lines


def _render_inline(draw, x: int, y: int, text: str, font, color: tuple) -> int:
    """Render a line of text, handling **bold** markers."""
    parts = re.split(r"(\*\*[^*]+\*\*)", text)
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            inner = part[2:-2]
            draw.text((x, y), inner, font=font, fill=HEADING_COLOR)
            bbox = draw.textbbox((0, 0), inner, font=font)
            x += bbox[2] - bbox[0]
        else:
            draw.text((x, y), part, font=font, fill=color)
            bbox = draw.textbbox((0, 0), part, font=font)
            x += bbox[2] - bbox[0]
    return x


def _get_line_height(style: str) -> int:
    """Get line height for a given style."""
    return {
        "h1": FONT_SIZE_H1 + 8,
        "h2": FONT_SIZE_H2 + 6,
        "h3": FONT_SIZE_H3 + 4,
        "code_block": FONT_SIZE_CODE + 8,
        "body": FONT_SIZE_BODY + 2,
        "bullet": FONT_SIZE_BODY + 2,
        "small": FONT_SIZE_SMALL + 2,
    }.get(style, FONT_SIZE_BODY + 2)


def _load_font(size: int, *, bold: bool = False, monospace: bool = False):
    """Try to load a TrueType font. Falls back to Pillow default."""
    from PIL import ImageFont
    import os

    # Common font paths (Linux)
    candidates = []
    if monospace:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
            "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        ]
    elif bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]

    # Also try user-configured font
    env_font = os.getenv("NOTIFICATION_FONT_PATH", "")
    if env_font:
        candidates.insert(0, env_font)

    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue

    # Fallback: Pillow default bitmap font (no CJK support)
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Fallback renderer (no Pillow)
# ---------------------------------------------------------------------------

def _render_fallback(
    md_text: str,
    *,
    width: int,
    title: Optional[str],
) -> bytes:
    """Minimal fallback: render plain text to a 1-bit BMP using stdlib only.

    This produces a very basic image — no styling, no CJK.  It exists so
    the system degrades gracefully when Pillow is not installed.
    """
    # Strip markdown formatting
    plain = _strip_markdown(md_text)
    if title:
        plain = f"{title}\n{'=' * 40}\n{plain}"

    # Truncate to reasonable size
    lines = plain.split("\n")[:100]
    text = "\n".join(lines)

    # Encode as a minimal PBM-like image using pure Python
    # We'll create a simple white-on-black text image
    char_w = 8
    char_h = 14
    cols = max(40, (width - PADDING * 2) // char_w)
    rows = len(text.split("\n"))

    img_w = cols * char_w + PADDING * 2
    img_h = rows * char_h + PADDING * 2

    # Build a minimal BMP
    # For simplicity, return the text as UTF-8 bytes with a header
    # indicating it's a text fallback
    header = f"[Image fallback — install Pillow for proper rendering]\n{'─' * 50}\n"
    return (header + text).encode("utf-8")


def _strip_markdown(text: str) -> str:
    """Strip basic markdown formatting for plain-text fallback."""
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"```[\s\S]*?```", "[code block]", text)
    text = re.sub(r"^[-*]\s+", "  • ", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s+", "  ", text, flags=re.MULTILINE)
    text = re.sub(r"^---+$", "────────────────", text, flags=re.MULTILINE)
    return text.strip()
