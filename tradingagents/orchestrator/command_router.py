"""CommandRouter — LLM-based natural language intent decomposition.

Parses natural language trading commands (e.g., "2w美金閉環模擬交易")
into a structured task DAG with dependencies.

Usage:
    router = CommandRouter(llm_provider="openai", llm_model="gpt-4o")
    dag = router.route("2w美金閉環模擬交易 HK.00700")
    # dag = {
    #     "tasks": [
    #         {"id": "t1", "action": "analyze", "symbol": "HK.00700", "params": {...}},
    #         {"id": "t2", "action": "allocate", "depends_on": ["t1"], ...},
    #         {"id": "t3", "action": "execute", "depends_on": ["t2"], ...},
    #     ],
    #     "budget": 20000.0,
    #     "currency": "USD",
    # }
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# ── Intent schema ─────────────────────────────────────────────────────────

INTENT_PROMPT_ZH = """你是一個量化交易系統的指令路由器。用戶會用自然語言描述交易意圖，你需要將其分解為結構化的任務 DAG。

## 支持的操作
- analyze: 分析股票（動量/基本面/情緒/技術面）
- select: 從候選池中篩選 Top N 股票
- allocate: 基於 Kelly 公式分配資金
- execute: 執行模擬交易
- observe: 監控持倉（止損/止盈）
- reflect: 交易後反思

## 輸出格式（JSON）
{{
  "intent": "用戶意圖摘要",
  "tasks": [
    {{
      "id": "t1",
      "action": "analyze",
      "symbol": "HK.00700",
      "params": {{"horizon": "short", "analysts": ["market", "fundamentals"]}},
      "depends_on": []
    }},
    {{
      "id": "t2",
      "action": "allocate",
      "params": {{"budget": 20000.0, "currency": "USD"}},
      "depends_on": ["t1"]
    }}
  ],
  "budget": 20000.0,
  "currency": "USD",
  "mode": "simulate"
}}

## 規則
1. 如果用戶提到「閉環」或「end-to-end」，生成 analyze → allocate → execute → observe 完整鏈
2. 金額解析：「2w」= 20000，「5k」= 5000，「1m」= 1000000
3. 幣種默認 USD，「港幣」/「HKD」→ HKD，「人民幣」/「RMB」/「CNY」→ CNY
4. 股票代碼：自動識別 HK.00700、US.AAPL、SH.600519 等格式
5. 如果未指定股票，action 設為 select（需要 StockSelector 篩選）
6. mode 默認為 simulate（模擬交易），除非用戶明確要求真實交易

用戶指令：{command}
"""

INTENT_PROMPT_EN = """You are a command router for a quantitative trading system.
Parse the user's natural language trading command into a structured task DAG.

## Supported actions
- analyze: Analyze stocks (momentum/fundamental/sentiment/technical)
- select: Screen Top N stocks from a candidate pool
- allocate: Allocate capital using Kelly criterion
- execute: Execute simulated trades
- observe: Monitor positions (stop-loss/take-profit)
- reflect: Post-trade reflection

## Output format (JSON)
{{
  "intent": "User intent summary",
  "tasks": [
    {{
      "id": "t1",
      "action": "analyze",
      "symbol": "US.AAPL",
      "params": {{"horizon": "short", "analysts": ["market", "fundamentals"]}},
      "depends_on": []
    }}
  ],
  "budget": 20000.0,
  "currency": "USD",
  "mode": "simulate"
}}

## Rules
1. If user mentions "closed loop" or "end-to-end", generate full chain: analyze → allocate → execute → observe
2. Amount parsing: "2w" = 20000, "5k" = 5000, "1m" = 1000000
3. Default currency: USD. "HKD"/"港幣" → HKD, "CNY"/"RMB"/"人民幣" → CNY
4. Stock codes: detect HK.00700, US.AAPL, SH.600519 formats
5. If no stock specified, set action to select (needs StockSelector screening)
6. Default mode is simulate unless user explicitly requests real trading

User command: {command}
"""


@dataclass
class TaskNode:
    """A single task in the DAG."""
    id: str
    action: str
    symbol: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)


@dataclass
class CommandDAG:
    """Parsed command as a directed acyclic graph of tasks."""
    intent: str
    tasks: List[TaskNode]
    budget: Optional[float] = None
    currency: str = "USD"
    mode: str = "simulate"
    raw_response: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for API/DB storage."""
        return {
            "intent": self.intent,
            "tasks": [
                {
                    "id": t.id,
                    "action": t.action,
                    "symbol": t.symbol,
                    "params": t.params,
                    "depends_on": t.depends_on,
                }
                for t in self.tasks
            ],
            "budget": self.budget,
            "currency": self.currency,
            "mode": self.mode,
        }


class CommandRouter:
    """LLM-based natural language command parser.

    Decomposes trading instructions into structured task DAGs.
    Supports Chinese and English commands.

    Dependencies:
        - tradingagents.llm_clients.factory for LLM calls
    """

    def __init__(
        self,
        llm_provider: Optional[str] = None,
        llm_model: Optional[str] = None,
        language: str = "zh",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        strategy_instructions: Optional[str] = None,
    ):
        """Initialize the command router.

        Args:
            llm_provider: LLM provider name (default from env TA_LLM_PROVIDER or "openai")
            llm_model: Model name (default from env TA_LLM_MODEL or "gpt-4o")
            language: Prompt language — "zh" for Chinese, "en" for English
            api_key: LLM API key (default from env OPENAI_API_KEY)
            base_url: LLM base URL (default from env TA_LLM_BASE_URL)
            strategy_instructions: YAML strategy instructions to inject into prompt
        """
        self._provider = llm_provider or os.getenv("TA_LLM_PROVIDER", "openai")
        self._model = llm_model or os.getenv("TA_LLM_MODEL", "gpt-4o")
        self._language = language
        self._api_key = api_key or os.getenv("TA_API_KEY") or os.getenv("OPENAI_API_KEY")
        self._base_url = base_url or os.getenv("TA_LLM_BASE_URL")
        self._strategy_instructions = strategy_instructions or ""
        self._llm = None  # Lazy init

    def _get_llm(self):
        """Lazy-init LLM client.

        Returns:
            LangChain ChatOpenAI (or equivalent) instance via get_llm()
        """
        if self._llm is None:
            from tradingagents.llm_clients.factory import create_llm_client

            kwargs: dict = {}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            if self._api_key:
                kwargs["api_key"] = self._api_key
            client = create_llm_client(
                self._provider, self._model, **kwargs
            )
            self._llm = client.get_llm()  # Returns LangChain LLM instance
        return self._llm

    def route(self, command: str) -> CommandDAG:
        """Parse a natural language command into a task DAG.

        Args:
            command: User's trading instruction (e.g., "2w美金閉環模擬交易 HK.00700")

        Returns:
            CommandDAG with parsed tasks and metadata

        Raises:
            ValueError: If the LLM response cannot be parsed
        """
        prompt = self._build_prompt(command)
        logger.info("Routing command: %s", command[:100])

        try:
            from langchain_core.messages import SystemMessage, HumanMessage

            llm = self._get_llm()
            messages = [
                SystemMessage(content=prompt),
                HumanMessage(content=command),
            ]
            result = llm.invoke(messages)
            response = result.content if hasattr(result, "content") else str(result)
            return self._parse_response(response, command)
        except Exception as e:
            logger.error("Command routing failed: %s", e, exc_info=True)
            # Fallback: create a simple analyze task
            return self._fallback_dag(command)

    def _build_prompt(self, command: str) -> str:
        """Build the appropriate language prompt with strategy context."""
        base = INTENT_PROMPT_ZH.format(command=command) if self._language == "zh" else INTENT_PROMPT_EN.format(command=command)
        if self._strategy_instructions:
            base += f"\n\n## 当前交易策略指引\n\n{self._strategy_instructions}\n\n请根据上述策略指引来理解和分解用户的交易指令。"
        return base

    def _parse_response(self, response: str, original_command: str) -> CommandDAG:
        """Parse LLM JSON response into CommandDAG.

        Args:
            response: Raw LLM response (expected JSON)
            original_command: Original user command for fallback

        Returns:
            Parsed CommandDAG

        Raises:
            ValueError: If JSON parsing fails
        """
        # Extract JSON from response (handle markdown code blocks)
        json_str = response.strip()
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0].strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse LLM response as JSON: %s", e)
            raise ValueError(f"Invalid LLM response format: {e}")

        # Build task nodes
        tasks = []
        for t in data.get("tasks", []):
            tasks.append(TaskNode(
                id=t.get("id", f"t_{uuid4().hex[:6]}"),
                action=t.get("action", "analyze"),
                symbol=t.get("symbol"),
                params=t.get("params", {}),
                depends_on=t.get("depends_on", []),
            ))

        return CommandDAG(
            intent=data.get("intent", original_command),
            tasks=tasks,
            budget=data.get("budget"),
            currency=data.get("currency", "USD"),
            mode=data.get("mode", "simulate"),
            raw_response=response,
        )

    def _fallback_dag(self, command: str) -> CommandDAG:
        """Create a simple fallback DAG when LLM parsing fails.

        Generates a single analyze task as the minimal safe action.
        """
        # Try to extract a stock symbol from the command
        import re
        symbol_match = re.search(
            r'((?:HK|US|SH|SZ|CN)\.\w+)', command, re.IGNORECASE
        )
        symbol = symbol_match.group(1).upper() if symbol_match else None

        task_id = f"t_{uuid4().hex[:6]}"
        task = TaskNode(
            id=task_id,
            action="analyze" if symbol else "select",
            symbol=symbol,
            params={"horizon": "short", "fallback": True},
        )

        logger.warning("Using fallback DAG for command: %s", command[:80])
        return CommandDAG(
            intent=f"Fallback analysis: {command}",
            tasks=[task],
            mode="simulate",
        )
