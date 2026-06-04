# TradingAgents-Futu：A 股 + 港股 + 美股多智能体自主交易系统

基于 [KylinMountain/TradingAgents-AShare](https://github.com/KylinMountain/TradingAgents-AShare) 二次开发，新增 **Futu OpenD 实时行情**、**模拟交易闭环**、**自主 OODA 循环**和**多市场覆盖**（US/HK/CN）。

<div align="center">
  <img src="assets/web/analysis.png" width="100%" alt="智能分析"/>
  <p><em>15 名智能体实时协作 + Futu OpenD 模拟交易 + 自主选股→分析→回测→下单→反思闭环</em></p>
</div>

---

## 核心特性（相比上游新增）

### 🆕 Futu OpenD 接入（US/HK）

| 能力 | 说明 |
|------|------|
| **K 线数据** | Futu `request_history_kline`，US/HK 日线最高优先级 |
| **实时行情** | Futu `get_market_snapshot`，PE/PB/市值/振幅/52 周高低 |
| **技术指标** | K 线 + stockstats（MA/MACD/RSI/Bollinger/ATR） |
| **模拟交易** | Futu SIMULATE 环境：下单/撤单/持仓/成交查询 |
| **码制转换** | 自动：`AAPL` → `US.AAPL`，`00700.HK` → `HK.00700` |
| **Fallback** | Futu 不可用时自动降级到 yfinance/Alpha Vantage |

### 🆕 搜索引擎增强（7 家）

Tavily / Brave / SerpAPI / SearXNG / Bocha / Anspire / MiniMax — 多 Key 轮转 + 10min 缓存 + tenacity 重试。

### 🆕 社交舆情（US 市场）

Reddit / X (Twitter) / Polymarket 情绪数据，通过 `api.adanos.org` 获取情绪分、提及量、趋势。

### 🆕 自主交易闭环（OODA 循环）

```
选股扫描 → 15 Agent 分析 → 历史回测 → Kelly 仓位分配 → Futu 模拟下单 → 持仓监控 → 反思教训 → 策略优化 → 下一轮
```

用户只需一句话：*"futu 虚拟账户，给你 2w 美金，执行闭环模拟交易"*

### 🆕 量化绩效指标

最大回撤 / 夏普比率 / Sortino 比率 / 胜率 / Calmar 比率 — 全自动计算。

### 🆕 反思记忆系统

交易后 LLM 自动反思，生成教训存入 BM25 记忆，下次类似场景自动调取。5 个独立记忆实例（Bull/Bear/Trader/Judge/RiskJudge）。

---

## 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                    Orchestrator（自主循环编排器）                  │
│  Command Router → Stock Selector → Portfolio Allocator → Observer│
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│              LangGraph 状态机（6 层，15 Agent）                   │
│  Layer 1: 7 分析师并行 → Layer 2: 多空辩论 → Layer 3: 裁决       │
│  Layer 4: Trader → Layer 5: 风控三方辩论 → Layer 6: Risk Judge    │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│                    数据源层（Provider Registry + Fallback）       │
│  Futu(US/HK) → Akshare(CN) → yfinance(全球) → Alpha Vantage    │
│  Search Service (7引擎) + Social Sentiment (Reddit/X/Poly)      │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│                    执行 & 存储层                                  │
│  Futu SIMULATE 模拟交易 + Backtest Engine + BM25 Memory         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 快速上手

### 源码安装

```bash
git clone https://github.com/nicklam1994/TradingAgents-Futu.git
cd TradingAgents-Futu

# 后端（Python 3.10+）
uv sync
pip install futu-api tavily-python newspaper3k

# 前端（Node.js 18+）
cd frontend
npm install
npm run build
cd ..
```

### 环境变量

```bash
cp .env.example .env
```

编辑 `.env`：

```bash
# LLM（必填）
TA_API_KEY=your-api-key
TA_BASE_URL=https://api.openai.com/v1
TA_LLM_QUICK=gpt-4o-mini
TA_LLM_DEEP=gpt-4o

# Futu OpenD（US/HK 数据源）
FUTU_OPEND_HOST=127.0.0.1
FUTU_OPEND_PORT=11111

# 搜索引擎（至少一个）
TAVILY_API_KEY=your-tavily-key

# 社交舆情（可选）
SOCIAL_SENTIMENT_API_KEY=your-adanos-key
```

### 启动

```bash
# 启动 Futu OpenD（需单独安装）
# 下载: https://openapi.futunn.com/

# 启动后端
uv run python -m uvicorn api.main:app --port 8000
```

访问 `http://localhost:8000`。

---

## API 端点

### 分析与报告

| 操作 | 接口 |
|------|------|
| 自然语言分析 | `POST /v1/chat/completions` |
| 直接分析 | `POST /v1/analyze` |
| 任务状态 | `GET /v1/jobs/{id}` |
| SSE 事件流 | `GET /v1/jobs/{id}/events` |
| 历史报告 | `GET /v1/reports` |

### 模拟交易（新增）

| 操作 | 接口 |
|------|------|
| 账户资金 | `GET /v1/sim/account` |
| 持仓查询 | `GET /v1/sim/positions` |
| 模拟下单 | `POST /v1/sim/order` |
| 撤销订单 | `DELETE /v1/sim/order/{id}` |
| 订单列表 | `GET /v1/sim/orders` |
| 成交记录 | `GET /v1/sim/deals` |
| 量化绩效 | `GET /v1/sim/performance` |
| 触发反思 | `POST /v1/sim/reflect` |

### 自主交易（新增）

| 操作 | 接口 |
|------|------|
| 创建任务 | `POST /v1/autonomous/create` |
| 任务状态 | `GET /v1/autonomous/{id}` |
| 暂停任务 | `POST /v1/autonomous/{id}/pause` |
| 停止任务 | `POST /v1/autonomous/{id}/stop` |

---

## 数据源覆盖

| 市场 | 行情数据 | 新闻 | 基本面 | 舆情 |
|------|---------|------|--------|------|
| **A 股** | akshare / baostock | akshare 东方财富 | akshare 新浪/同花顺 | 雪球热搜 |
| **港股** | Futu OpenD | yfinance / 搜索引擎 | Futu Snapshot | 搜索引擎 |
| **美股** | Futu OpenD / yfinance | yfinance / Alpha Vantage / 7 家搜索 | yfinance / Alpha Vantage | Reddit / X / Polymarket |

---

## 项目结构

```
tradingagents/
├── agents/                    # 15 个 Agent 角色
│   ├── analysts/              # 7 分析师（Market/Social/News/Fund/Macro/SmartMoney/VP）
│   ├── researchers/           # Bull/Bear 多空辩论
│   ├── managers/              # Research Manager + Risk Manager
│   ├── risk_mgmt/             # 激进/稳健/中性风控辩论
│   ├── trader/                # Trader + SimExecutor + ExitStrategy
│   └── utils/                 # 记忆、校准、工具
├── orchestrator/              # 🆕 自主编排层
├── dataflows/                 # 数据源（Provider Registry）
│   ├── providers/             # Futu / Akshare / yfinance / Alpha Vantage
│   ├── search_service.py      # 🆕 7 引擎搜索
│   ├── social_sentiment.py    # 🆕 Reddit/X/Polymarket
│   └── quant_metrics.py       # 🆕 量化绩效指标
├── graph/                     # LangGraph 状态机
├── prompts/                   # 中英文提示词
└── skills/                    # 🆕 可插拔策略插件

api/                           # FastAPI 后端
├── main.py                    # API 端点
└── services/
    ├── sim_trading_service.py # 🆕 Futu 模拟交易
    └── autonomous_service.py  # 🆕 自主任务管理

frontend/                      # React + Vite 前端
```

---

## 技术栈

| 层 | 技术 |
|----|------|
| **Agent 框架** | LangGraph + LangChain |
| **LLM** | OpenAI / Anthropic / Gemini / DeepSeek / Moonshot |
| **后端** | FastAPI + SQLite + SQLAlchemy |
| **前端** | React + TypeScript + Vite + Tailwind |
| **数据源** | Futu OpenD + akshare + yfinance + Alpha Vantage |
| **搜索引擎** | Tavily / Brave / SerpAPI / SearXNG / Bocha / Anspire / MiniMax |
| **记忆** | BM25 词法检索（无需 embedding API） |
| **交易** | Futu OpenD SIMULATE 环境 |

---

## 特别鸣谢

- [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) — 核心架构灵感
- [KylinMountain/TradingAgents-AShare](https://github.com/KylinMountain/TradingAgents-AShare) — A 股适配

## 许可说明

- 核心逻辑基于 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) (Apache 2.0)
- 新增模块采用 `PolyForm Noncommercial 1.0.0` 协议

## 重要声明

- **仅供学习研究**：不构成投资建议
- **实盘风险**：证券市场有风险，投资需谨慎
- **数据延迟**：数据源可能存在延迟，以交易所实时公告为准
