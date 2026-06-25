# -*- coding: utf-8 -*-
"""LLM Function Calling 模式 — 自然语言对话 + 工具调用

用户消息 → LLM 理解意图 → 调用工具 → 返回结果
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# ── Tool definitions ─────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "analyze_stock",
            "description": "对指定股票进行多 Agent 协作分析，生成投资建议。当用户想要分析某只股票、询问投资建议、或想知道某只股票的走势时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "股票代码或中文名称，如 'AAPL'、'腾讯'、'00700.HK'、'NVDA'"
                    },
                    "horizon": {
                        "type": "string",
                        "enum": ["short", "medium", "long"],
                        "description": "分析周期：short=短期(日线)、medium=中期(周线)、long=长期(月线)"
                    },
                    "question": {
                        "type": "string",
                        "description": "用户的具体问题，如 '能不能买'、'目标价多少'、'风险大吗'"
                    }
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_quote",
            "description": "查询股票的实时行情数据，包括最新价、涨跌幅、成交量等。当用户询问某只股票的当前价格或行情时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "股票代码或中文名称"
                    }
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_help",
            "description": "返回系统帮助信息，包括可用功能和使用方法。当用户询问如何使用系统、有什么功能、或需要帮助时调用。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]

SYSTEM_PROMPT = """你是 TradingAgents 智能分析助手。你可以：

1. **分析股票** — 使用 analyze_stock 工具，支持港股、美股、A股
2. **查询行情** — 使用 get_quote 工具，获取实时价格
3. **系统帮助** — 使用 get_help 工具

用户可以用自然语言与你对话，例如：
- "帮我看看腾讯最近怎么样"
- "NVDA能买吗"
- "对比一下苹果和微软"
- "今天大盘怎么样"

请根据用户的意图选择合适的工具。如果用户没有指定分析周期，默认使用 short（短期）。
如果用户的问题不需要调用工具（如闲聊），直接用自然语言回复。
"""

HELP_TEXT = """📊 **TradingAgents 智能分析助手**

**可用功能：**
- 🔍 **股票分析** — 输入股票代码或名称，如 "分析AAPL"、"帮我看看腾讯"
- 📈 **实时行情** — 查询当前价格，如 "NVDA多少钱"
- 💬 **自然对话** — 用自然语言提问，如 "最近有什么好股票"

**支持的市场：**
- 🇺🇸 美股：AAPL, NVDA, TSLA, SPY...
- 🇭🇰 港股：00700.HK, 09988.HK...
- 🇨🇳 A股：600519.SH, 000858.SZ...

**分析周期：**
- 短期 (short) — 日线级别
- 中期 (medium) — 周线级别  
- 长期 (long) — 月线级别
"""


class FunctionCallingHandler:
    """LLM Function Calling 处理器"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
    ):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    async def chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """Send a chat completion request with optional tools."""
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 2000,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await self.client.chat.completions.create(**kwargs)
        return response

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream a chat completion request with optional tools."""
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 2000,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        stream = await self.client.chat.completions.create(**kwargs)
        async for chunk in stream:
            yield chunk


def get_llm_config_from_db() -> Dict[str, str]:
    """Get LLM config from database."""
    try:
        from api.database import SessionLocal, UserLLMConfigDB
        db = SessionLocal()
        try:
            row = db.query(UserLLMConfigDB).first()
            if row:
                return {
                    "api_key": row.api_key_encrypted or "",
                    "base_url": row.backend_url or "https://api.openai.com/v1",
                    "model": row.quick_think_llm or "gpt-4o-mini",
                }
        finally:
            db.close()
    except Exception as e:
        logger.error("Failed to get LLM config: %s", e)
    return {}


def create_handler_from_db() -> Optional[FunctionCallingHandler]:
    """Create a FunctionCallingHandler from database config."""
    config = get_llm_config_from_db()
    if not config.get("api_key"):
        logger.warning("No API key found in DB")
        return None
    return FunctionCallingHandler(
        api_key=config["api_key"],
        base_url=config["base_url"],
        model=config["model"],
    )
