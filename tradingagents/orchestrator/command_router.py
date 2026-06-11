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
      "action": "select",
      "params": {{
        "count": 5,
        "market": "HK",
        "category": "科技",
        "filter_params": {{
          "filters": [
            {{"field": "MARKET_VAL", "min": 1000000000, "max": null}},
            {{"field": "PE_TTM", "min": 0, "max": 20}}
          ],
          "sort_field": "TURNOVER",
          "sort_dir": "DESC"
        }}
      }},
      "depends_on": []
    }},
    {{
      "id": "t3",
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
2. **金額解析**（必須正確提取！）：
   - 「2w」= 20000，「5k」= 5000，「1m」= 1000000
   - 「兩萬」= 20000，「五萬」= 50000，「十萬」= 100000，「一百萬」= 1000000
   - 「2萬」= 20000，「5萬」= 50000，「10萬」= 100000
   - 「2万」= 20000，「5万」= 50000（簡體）
   - 「1億」= 100000000
   - 數字可帶逗號：「50,000」= 50000
   - 複合寫法：「2w5」= 25000，「1.5w」= 15000
   - 將解析到的金額放在頂層 "budget" 和 allocate 任務的 params.budget 中
3. **幣種識別**（必須正確提取！）：
   - 「美金」/「美元」/「美刀」/「美股」/「USD」/「美」/「usd」→ USD
   - 「港幣」/「港元」/「港紙」/「港股」/「HKD」/「港」/「hkd」→ HKD
   - 「人民幣」/「RMB」/「CNY」/「人民币」/「cny」/「rmb」→ CNY
   - 如果指令中同時出現預算和幣種詞（如「50000港元」），必須同時提取兩者
   - 默認幣種：USD（僅當用戶未提及任何幣種時使用）
   - 將幣種放在頂層 "currency" 和 allocate 任務的 params.currency 中
4. **數量/Top N 提取**（用於 select 操作）：
   - 「選5支」/「選5隻」/「選5只」/「挑5只」/「篩選5只」→ count: 5
   - 「top 3」/「前三」/「前3名」→ count: 3
   - 「選10只港股」→ count: 10，且可在 params 中加入 market: "HK"
   - 「推薦幾只」/「推薦一些」→ 默認 count: 5
   - 如果用戶指定了數量，必須放在 select 任務的 params.count 中
   - 默認 count: 3
5. 股票代碼：自動識別 HK.00700、US.AAPL、SH.600519 等格式
6. 如果未指定股票，action 設為 select（需要 StockSelector 篩選）
7. mode 默認為 simulate（模擬交易），除非用戶明確要求真實交易

## select 操作的 filter_params（結構化篩選參數）

select 任務的 params 中可包含 `filter_params`，用於向富途 `get_stock_filter` API 傳遞精確的量化篩選條件。filter_params 結構如下：

```json
{{
  "filters": [
    {{"field": "STOCK_FIELD", "min": 數值, "max": 數值或null}},
    ...
  ],
  "sort_field": "STOCK_FIELD",
  "sort_dir": "ASC或DESC"
}}
```

### 可用的 StockField 欄位（精選）

**價格/成交量**:
- CUR_PRICE（現價）、CHANGE_RATE（漲跌幅%）、TURNOVER_RATE（換手率%）、AMPLITUDE（振幅%）、VOLUME（成交量）、TURNOVER（成交額）

**估值**:
- PE_TTM（市盈率TTM）、PB_RATE（市淨率）、PS_TTM（市銷率TTM）、PCF_TTM（市現率TTM）

**規模**:
- MARKET_VAL（總市值）、FLOAT_MARKET_VAL（流通市值）、TOTAL_SHARE（總股本）、LOT_PRICE（每手價）

**技術指標**:
- MA5/MA10/MA20/MA60（均線）、RSI（RSI）、MACD（MACD）、MACD_DIFF（DIF）、MACD_DEA（DEA）

**財務指標**:
- NET_PROFIT（淨利潤）、BASIC_EPS（基本EPS）、GROSS_PROFIT_RATE（毛利率%）、RETURN_ON_EQUITY_RATE（ROE%）、EBITDA（EBITDA）

**成長指標**:
- EPS_GROWTH_RATE（EPS增長率%）、NET_PROFIX_GROWTH（淨利潤增長率%）、SUM_OF_BUSINESS_GROWTH（營收增長率%）

**動量指標**:
- CHANGE_RATE_BEGIN_YEAR（年初至今漲幅%）、CUR_PRICE_TO_HIGHEST52_WEEKS_RATIO（距52周高點%）、CUR_PRICE_TO_LOWEST52_WEEKS_RATIO（距52周低點%）

### filter_params 規則
1. **filter_params 是可選的**：如果用戶只說「選5只科技股」沒有具體條件，可以不提供 filter_params，系統會使用默認值
2. **默認篩選**：MARKET_VAL > 1,000,000,000（排除小市值股）、默認排序：TURNOVER DESC（按成交額降序，優先活躍股）
3. **語義推斷**（根據用戶描述推斷合理的 filter_params）：
   - 「低估值」/「便宜」→ PE_TTM max: 15, PB_RATE max: 2
   - 「放量」/「活躍」/「熱門」→ TURNOVER_RATE min: 3%
   - 「強勢」/「漲得好」/「動量強」→ CHANGE_RATE min: 3%
   - 「大盤股」/「藍籌」→ MARKET_VAL min: 50,000,000,000（500億）
   - 「小盤股」/「成長股」→ MARKET_VAL max: 10,000,000,000（100億），EPS_GROWTH_RATE min: 20
   - 「高ROE」→ RETURN_ON_EQUITY_RATE min: 15
   - 「高股息」/「分紅好」→ DIVIDEND_YIELD min: 3（如有）
   - 「接近52周新高」→ CUR_PRICE_TO_HIGHEST52_WEEKS_RATIO min: 90
   - 「RSI超賣」→ RSI max: 30
   - 「年初至今漲幅大」→ CHANGE_RATE_BEGIN_YEAR min: 30
4. **filter 中 min/max 可為 null**：null 表示不限制該方向的邊界
5. **sort_dir**：ASC（升序）或 DESC（降序），默認 DESC

## 示例

### 示例 1：「50000港元閉環模擬交易」
```json
{{
  "intent": "以50000港元進行閉環模擬交易",
  "tasks": [
    {{"id": "t1", "action": "select", "params": {{"market": "HK", "filter_params": {{"filters": [{{"field": "MARKET_VAL", "min": 1000000000, "max": null}}], "sort_field": "TURNOVER", "sort_dir": "DESC"}}}}, "depends_on": []}},
    {{"id": "t2", "action": "analyze", "params": {{"horizon": "short", "analysts": ["market", "fundamentals", "sentiment"]}}, "depends_on": ["t1"]}},
    {{"id": "t3", "action": "allocate", "params": {{"budget": 50000.0, "currency": "HKD"}}, "depends_on": ["t2"]}},
    {{"id": "t4", "action": "execute", "depends_on": ["t3"]}},
    {{"id": "t5", "action": "observe", "depends_on": ["t4"]}}
  ],
  "budget": 50000.0,
  "currency": "HKD",
  "mode": "simulate"
}}
```

### 示例 2：「2萬美金選5只港股閉環」
```json
{{
  "intent": "以2萬美金篩選5只港股並閉環交易",
  "tasks": [
    {{"id": "t1", "action": "select", "params": {{"count": 5, "market": "HK", "filter_params": {{"filters": [{{"field": "MARKET_VAL", "min": 1000000000, "max": null}}], "sort_field": "TURNOVER", "sort_dir": "DESC"}}}}, "depends_on": []}},
    {{"id": "t2", "action": "analyze", "params": {{"horizon": "short", "analysts": ["market", "fundamentals", "sentiment"]}}, "depends_on": ["t1"]}},
    {{"id": "t3", "action": "allocate", "params": {{"budget": 20000.0, "currency": "USD"}}, "depends_on": ["t2"]}},
    {{"id": "t4", "action": "execute", "depends_on": ["t3"]}},
    {{"id": "t5", "action": "observe", "depends_on": ["t4"]}}
  ],
  "budget": 20000.0,
  "currency": "USD",
  "mode": "simulate"
}}
```

### 示例 3：「選3只低估值科技股分析一下」（帶 filter_params）
```json
{{
  "intent": "篩選3只低估值科技股並進行分析",
  "tasks": [
    {{"id": "t1", "action": "select", "params": {{"count": 3, "category": "科技", "filter_params": {{"filters": [{{"field": "MARKET_VAL", "min": 1000000000, "max": null}}, {{"field": "PE_TTM", "min": 0, "max": 15}}, {{"field": "PB_RATE", "min": 0, "max": 2}}], "sort_field": "PE_TTM", "sort_dir": "ASC"}}}}, "depends_on": []}},
    {{"id": "t2", "action": "analyze", "params": {{"horizon": "short", "analysts": ["market", "fundamentals"]}}, "depends_on": ["t1"]}}
  ],
  "currency": "USD",
  "mode": "simulate"
}}
```

### 示例 4：「選5只強勢放量港股」（多條件 filter_params）
```json
{{
  "intent": "篩選5只強勢放量港股",
  "tasks": [
    {{"id": "t1", "action": "select", "params": {{"count": 5, "market": "HK", "filter_params": {{"filters": [{{"field": "MARKET_VAL", "min": 1000000000, "max": null}}, {{"field": "CHANGE_RATE", "min": 3, "max": null}}, {{"field": "TURNOVER_RATE", "min": 3, "max": null}}], "sort_field": "CHANGE_RATE", "sort_dir": "DESC"}}}}, "depends_on": []}}
  ],
  "currency": "HKD",
  "mode": "simulate"
}}
```

### 示例 5：「選5只科技股」（無具體條件，使用默認 filter_params）
```json
{{
  "intent": "篩選5只科技股",
  "tasks": [
    {{"id": "t1", "action": "select", "params": {{"count": 5, "category": "科技", "filter_params": {{"filters": [{{"field": "MARKET_VAL", "min": 1000000000, "max": null}}], "sort_field": "TURNOVER", "sort_dir": "DESC"}}}}, "depends_on": []}}
  ],
  "currency": "USD",
  "mode": "simulate"
}}
```

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
