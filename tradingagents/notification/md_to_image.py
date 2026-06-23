# -*- coding: utf-8 -*-
"""
Markdown 转图片工具

将 Markdown 转为 PNG 图片，用于不支持 Markdown 的通知渠道（如微信）。

支持两种引擎：
- imgkit (wkhtmltoimage): 默认，需要安装 wkhtmltoimage
- markdown-to-file (m2f): emoji 支持更好，需要 npm install -g markdown-to-file

当转换失败或依赖不可用时返回 None，调用方可回退为纯文本发送。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _markdown_to_html_document(markdown_text: str) -> str:
    """将 Markdown 转为完整 HTML 文档（内联样式）。

    这是一个基础实现，适用于图片渲染场景。
    """
    import re

    body_lines: list[str] = []
    for line in markdown_text.split("\n"):
        # Headings
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            sizes = {1: 28, 2: 24, 3: 20, 4: 18, 5: 16, 6: 14}
            size = sizes.get(level, 14)
            body_lines.append(
                f'<h{level} style="font-size:{size}px;margin:12px 0 6px;">{m.group(2)}</h{level}>'
            )
            continue
        # Horizontal rule
        if line.strip() in ("---", "***", "___"):
            body_lines.append('<hr style="border:1px solid #ddd;margin:16px 0;">')
            continue
        # Bold
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        # Italic
        line = re.sub(r"\*(.+?)\*", r"<em>\1</em>", line)
        # Inline code
        line = re.sub(r"`(.+?)`", r"<code style='background:#f0f0f0;padding:2px 4px;border-radius:3px;'>\1</code>", line)
        if line.strip():
            body_lines.append(f'<p style="margin:4px 0;">{line}</p>')
        else:
            body_lines.append("")

    body = "\n".join(body_lines)
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    font-size: 14px;
    line-height: 1.6;
    color: #333;
    max-width: 800px;
    margin: 0 auto;
    padding: 20px;
    background: #fff;
}}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
th {{ background: #f5f5f5; font-weight: bold; }}
code {{ font-family: "SF Mono", Monaco, Consolas, monospace; }}
</style>
</head>
<body>{body}</body>
</html>"""


def _markdown_to_image_m2f(markdown_text: str) -> Optional[bytes]:
    """通过 m2f (markdown-to-file) CLI 将 Markdown 转为 PNG。"""
    if shutil.which("m2f") is None:
        logger.warning("m2f not found in PATH. Install with: npm i -g markdown-to-file")
        return None

    temp_dir: Optional[str] = None
    try:
        temp_dir = tempfile.mkdtemp()
        md_path = os.path.join(temp_dir, "report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown_text)

        result = subprocess.run(
            ["m2f", md_path, "png", f"outputDirectory={temp_dir}"],
            capture_output=True,
            timeout=60,
            check=False,
        )
        png_path = os.path.join(temp_dir, "report.png")
        if result.returncode != 0 or not os.path.isfile(png_path):
            logger.warning(
                "m2f conversion failed: returncode=%s, stderr=%s",
                result.returncode,
                (result.stderr or b"").decode("utf-8", errors="replace")[:200],
            )
            return None

        with open(png_path, "rb") as f:
            return f.read()
    except subprocess.TimeoutExpired:
        logger.warning("m2f conversion timed out (60s)")
        return None
    except Exception as exc:
        logger.warning("markdown_to_image (m2f) failed: %s", exc)
        return None
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except OSError as exc:
                logger.debug("Failed to remove temp dir %s: %s", temp_dir, exc)


def _markdown_to_image_wkhtml(markdown_text: str) -> Optional[bytes]:
    """通过 imgkit/wkhtmltoimage 将 Markdown 转为 PNG。"""
    try:
        import imgkit  # type: ignore
    except ImportError:
        logger.debug("imgkit not installed, markdown_to_image unavailable")
        return None

    html = _markdown_to_html_document(markdown_text)
    try:
        options = {
            "format": "png",
            "encoding": "UTF-8",
            "quiet": "",
        }
        out = imgkit.from_string(html, False, options=options)
        if out and isinstance(out, bytes) and len(out) > 0:
            return out
        logger.warning("imgkit.from_string returned empty or invalid result")
        return None
    except OSError as exc:
        if "wkhtmltoimage" in str(exc).lower() or "wkhtmltopdf" in str(exc).lower():
            logger.debug("wkhtmltopdf/wkhtmltoimage not found: %s", exc)
        else:
            logger.warning("imgkit/wkhtmltoimage error: %s", exc)
        return None
    except Exception as exc:
        logger.warning("markdown_to_image conversion failed: %s", exc)
        return None


def markdown_to_image(
    markdown_text: str,
    max_chars: int = 15000,
    engine: str = "wkhtmltoimage",
) -> Optional[bytes]:
    """将 Markdown 转为 PNG 图片字节。

    Args:
        markdown_text: 原始 Markdown 内容。
        max_chars: 超过此长度跳过转换（避免超大图片）。默认 15000。
        engine: 转换引擎，"wkhtmltoimage" 或 "markdown-to-file"。

    Returns:
        PNG 字节，转换失败或依赖不可用时返回 None。
    """
    if len(markdown_text) > max_chars:
        logger.warning(
            "Markdown content (%d chars) exceeds max_chars (%d), skipping image conversion",
            len(markdown_text), max_chars,
        )
        return None

    if engine == "markdown-to-file":
        return _markdown_to_image_m2f(markdown_text)
    return _markdown_to_image_wkhtml(markdown_text)
