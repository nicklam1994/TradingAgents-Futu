# TradingAgents-Futu：港股 + 美股多智能体自主交易系统

基于 [KylinMountain/TradingAgents-AShare](https://github.com/KylinMountain/TradingAgents-AShare) 二次开发，专注 **港股 + 美股**，通过 **Futu OpenD** 接入实时行情与模拟交易，配合 15 Agent 协作分析与自主 OODA 闭环。

<div align="center">
  <img src="assets/web/analysis.png" width="100%" alt="智能分析"/>
  <p><em>15 名智能体实时协作 + Futu OpenD 模拟交易 + 自主选股→分析→回测→下单→反思闭环</em></p>
</div>

---

## 开发进度

| Phase | 功能 | 状态 | Commit |
|-------|------|------|--------|
| 1 | Futu Provider（US/HK 数据源） | ✅ 完成 | `b372819` |
| 2 | 搜索 API 框架（7 引擎）+ 搜索服务配置 UI | ✅ 完成 | `4585fab` |
| 3 | 社交舆情（Reddit/X/Poly）+ 配置 UI | ✅ 完成 | `fa64c69` |
| 4 | 结构化输出 & 风控增强 | ✅ 完成 | `b389e2e` |
| 5 | Futu 模拟交易服务（9 端点 + RSA 加密 + OpenD 配置 UI） | ✅ 完成 | `fc8ed41` |
| 6 | 量化绩效指标（max_drawdown/sharpe/sortino/win_rate/calmar） | 🔄 进行中 | — |
| 7 | 模拟交易 Agent & 反思 | ⏳ 待开始 | — |
| 8 | 自主 Orchestrator（OODA） | ⏳ 待开始 | — |
| 9 | 前端 5 新页面 | ⏳ 待开始 | — |
| 10 | 止损/策略插件 | ⏳ 待开始 | — |
| 11 | 通知系统移植（7 渠道 + Bot 平台） | ⏳ 待开始 | — |

---

## 核心特性

### Futu OpenD 实时行情（港股/美股）✅ Phase 1

| 能力 | 说明 |
|------|------|
| **K 线数据** | Futu `request_history_kline`，日线最高优先级 |
| **实时行情** | Futu `get_market_snapshot`，PE/PB/市值/振幅/52 周高低 |
| **技术指标** | K 线 + stockstats（MA/MACD/RSI/Bollinger/ATR） |
| **模拟交易** | Futu SIMULATE 环境：下单/撤单/持仓/成交查询 |
| **码制转换** | 自动：`AAPL` → `US.AAPL`，`00700.HK` → `HK.00700` |
| **Fallback** | Futu 不可用时自动降级到 yfinance / Alpha Vantage |

### 搜索引擎增强（7 家）✅ Phase 2

Tavily / Brave / SerpAPI / SearXNG / Bocha / Anspire / MiniMax — 多 Key 轮转 + 10min 缓存 + tenacity 重试。

**已验证引擎**（实例化测试通过）：
- ✅ **Tavily** — LLM 优化搜索
- ✅ **SerpAPI** — Google 搜索代理
- ✅ **Bocha** — 中文 AI 搜索

**前端配置**：设置页 → 搜索服务接入 → 填入 API Key → toggle 启用 → 保存配置

### 社交舆情（美股）✅ Phase 3

Reddit / X (Twitter) / Polymarket 情绪数据，通过 `api.adanos.org` 获取情绪分、提及量、趋势。

- **SocialSentimentService**：453 行，支持 fetch_reddit_report / fetch_x_trending / fetch_polymarket_trending
- **LangChain Tool**：`get_social_sentiment`，Social Media Analyst 并行调用
- **前端配置**：设置页 → 社交舆情接入 → API Key + Base URL → 保存配置

### 结构化输出 & 风控增强 ✅ Phase 4

分析师输出结构化 JSON，Risk Judge 强制 7 类风险检查。

- **VERDICT JSON schema**：7 个分析师输出 confidence/signal/key_levels/target_price/risk_flags
- **7 类风险 checklist**：流动性/波动率/集中度/相关性/宏观/事件/技术
- **信号处理**：`extract_verdict_data` + `extract_risk_judge_data` 结构化提取
- **DB 存储**：confidence (0-100) + target_price + risk_flags 聚合

### Futu 模拟交易服务 ✅ Phase 5

9 个 REST 端点覆盖 Futu 交易 API + 高级信号执行。

| 端点 | Futu API | 说明 |
|------|----------|------|
| `GET /v1/sim/account` | `accinfo_query` | 账户资金 |
| `GET /v1/sim/positions` | `position_list_query` | 持仓查询 |
| `POST /v1/sim/order` | `place_order` | 模拟下单 |
| `DELETE /v1/sim/order` | `modify_order` | 撤单 |
| `GET /v1/sim/orders` | `order_list_query` | 当日订单 |
| `GET /v1/sim/acc-list` | `get_acc_list` | 账户列表（HK/US） |
| `GET /v1/sim/trading-info` | `acctradinginfo_query` | 可买/可卖量 |
| `GET /v1/sim/history-orders` | `history_order_list_query` | 历史订单 |
| `POST /v1/sim/signal` | `execute_signal` | 高级信号执行（Kelly/置信度） |

**关键技术**：
- RSA 加密跨网连接（`_need_encrypt()` localhost 不加密，远程自动 RSA）
- 统一 `_get_trade_ctx()` 工厂方法
- `get_acc_list(trd_market)` 支持 HK/US 双市场
- 前端 Futu OpenD 配置 UI（连接测试 + 用户信息 + 行情权限 + 账户列表）

### 自主交易闭环（OODA 循环）⏳ Phase 8

```
选股扫描 → 15 Agent 分析 → 历史回测 → Kelly 仓位分配 → Futu 模拟下单 → 持仓监控 → 反思教训 → 策略优化 → 下一轮
```

用户只需一句话：*"futu 虚拟账户，给你 2w 美金，执行闭环模拟交易"*

### 量化绩效指标 🔄 Phase 6

最大回撤 / 夏普比率 / Sortino 比率 / 胜率 / Calmar 比率 — 纯 Python 实现，`GET /v1/sim/performance` 端点。

### 反思记忆系统 ⏳ Phase 7

交易后 LLM 自动反思，生成教训存入 BM25 记忆，下次类似场景自动调取。5 个独立记忆实例（Bull/Bear/Trader/Judge/RiskJudge）。

---

## 架构总览

> 📊 **[点击查看交互式架构图](https://htmlpreview.github.io/?https://github.com/nicklam1994/TradingAgents-Futu/blob/main/assets/web/architecture.html)**

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
│  Futu(港美股) → yfinance(全球) → Alpha Vantage(备用)            │
│  Search Service (7引擎) ✅ + Social Sentiment (Reddit/X/Poly) ✅ │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│                    模拟交易层 ✅ Phase 5                          │
│  Futu SIMULATE: 下单/撤单/持仓/可买量/历史订单/账户列表           │
│  RSA 加密 + 统一工厂方法 + HK/US 双市场                          │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│                    量化绩效 🔄 Phase 6                           │
│  max_drawdown / sharpe / sortino / win_rate / calmar            │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│                    通知系统（7 渠道 + Bot 平台） ⏳ Phase 11      │
│  Email / 企业微信 / 飞书 / Telegram / Discord / Slack / Webhook  │
│  DingTalk Bot / Feishu Bot / Discord Bot / Telegram Bot          │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│                    执行 & 存储层                                  │
│  Futu SIMULATE 模拟交易 + Backtest Engine + BM25 Memory         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 快速上手

### 前置条件

1. **Futu OpenD**：[下载安装](https://openapi.futunn.com/)，启动后默认监听 `127.0.0.1:11111`
2. **Futu 账户**：注册并开通模拟交易权限（港股/美股）
3. **Python 3.10+** + **Node.js 18+**

### 安装

```bash
git clone https://github.com/nicklam1994/TradingAgents-Futu.git
cd TradingAgents-Futu

# 后端
uv sync
pip install futu-api tavily-python newspaper3k serpapi

# 前端
cd frontend
npm install && npm run build
cd ..
```

### 配置

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

# Futu OpenD
FUTU_OPEND_HOST=127.0.0.1
FUTU_OPEND_PORT=11111

# RSA 密钥（跨网连接需要）
FUTU_RSA_KEY_PATH=config/rsa_key.txt

# 搜索引擎（也可在前端设置页配置）
TAVILY_API_KEY=your-tavily-key

# 社交舆情（也可在前端设置页配置）
SOCIAL_SENTIMENT_API_KEY=your-adanos-key
```

### 启动

```bash
# 确保 Futu OpenD 已运行
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

### 模型 & 配置

| 操作 | 接口 |
|------|------|
| 获取运行时配置 | `GET /v1/config` |
| 更新运行时配置 | `PUT /v1/config` |
| 获取搜索配置 | `GET /v1/config/search` |
| 保存搜索配置 | `PUT /v1/config/search` |
| 获取舆情配置 | `GET /v1/config/social-sentiment` |
| 保存舆情配置 | `PUT /v1/config/social-sentiment` |
| 获取 Futu 配置 | `GET /v1/config/futu-opend` |
| 保存 Futu 配置 | `PUT /v1/config/futu-opend` |
| Futu 连接测试 | `GET /v1/futu/status` |
| 模型列表 | `GET /v1/models` |

### 模拟交易 ✅ Phase 5

| 操作 | 接口 |
|------|------|
| 账户资金 | `GET /v1/sim/account` |
| 持仓查询 | `GET /v1/sim/positions` |
| 模拟下单 | `POST /v1/sim/order` |
| 撤销订单 | `DELETE /v1/sim/order` |
| 订单列表 | `GET /v1/sim/orders` |
| 账户列表 | `GET /v1/sim/acc-list` |
| 可买/可卖量 | `GET /v1/sim/trading-info` |
| 历史订单 | `GET /v1/sim/history-orders` |
| 信号执行 | `POST /v1/sim/signal` |

### 量化绩效 🔄 Phase 6

| 操作 | 接口 |
|------|------|
| 绩效指标 | `GET /v1/sim/performance` |

### 自主交易 ⏳ Phase 8

| 操作 | 接口 |
|------|------|
| 创建任务 | `POST /v1/autonomous/create` |
| 任务状态 | `GET /v1/autonomous/{id}` |
| 暂停任务 | `POST /v1/autonomous/{id}/pause` |
| 停止任务 | `POST /v1/autonomous/{id}/stop` |

认证：在前端"设置 / API Token"生成密钥，通过 `Authorization: Bearer <TOKEN>` 传入。

---

## 数据源覆盖

| 市场 | 行情 | 新闻 | 基本面 | 舆情 |
|------|------|------|--------|------|
| **港股** | Futu OpenD ✅ | yfinance / 7 家搜索 ✅ | Futu Snapshot ✅ | 搜索引擎 ✅ |
| **美股** | Futu OpenD / yfinance ✅ | yfinance / Alpha Vantage / 7 家搜索 ✅ | yfinance / Alpha Vantage ✅ | Reddit / X / Polymarket ✅ |

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
├── orchestrator/              # 🆕 自主编排层（OODA 循环）
├── dataflows/                 # 数据源（Provider Registry）
│   ├── providers/
│   │   ├── futu_provider.py   # ✅ Futu OpenD Provider（Phase 1）
│   │   ├── search_news_provider.py  # ✅ 搜索新闻 Provider（Phase 2）
│   │   ├── cn_akshare_provider.py
│   │   ├── yfinance_provider.py
│   │   └── alpha_vantage_provider.py
│   ├── search_service.py      # ✅ 7 引擎搜索（Phase 2）
│   ├── search_providers/      # ✅ 7 个搜索引擎实现
│   ├── social_sentiment.py    # ✅ Reddit/X/Polymarket（Phase 3）
│   └── quant_metrics.py       # 🔄 量化绩效指标（Phase 6）
├── graph/                     # LangGraph 状态机
│   └── signal_processing.py   # ✅ VERDICT/RISK_JUDGE 结构化提取（Phase 4）
├── prompts/                   # 中英文提示词
└── skills/                    # 🆕 可插拔策略插件（Phase 10）

api/                           # FastAPI 后端
├── main.py                    # API 端点
├── database.py                # ✅ ReportDB 新增 confidence/target_price/risk_flags（Phase 4）
└── services/
    ├── report_service.py      # ✅ 结构化字段解析 + 存储（Phase 4）
    ├── sim_trading_service.py # ✅ Futu 模拟交易（Phase 5）
    └── autonomous_service.py  # 🆕 自主任务管理（Phase 8）

frontend/                      # React + Vite 前端
├── public/
│   └── providers.json         # ✅ API Provider 清单（动态加载）
└── src/
    └── pages/
        └── Settings.tsx       # ✅ 模型接入 + Futu OpenD + 搜索 + 舆情配置
```

---

## 技术栈

| 层 | 技术 |
|----|------|
| **Agent 框架** | LangGraph + LangChain |
| **LLM** | OpenAI / Anthropic / Gemini / DeepSeek / Moonshot / MiMo |
| **后端** | FastAPI + SQLite + SQLAlchemy |
| **前端** | React + TypeScript + Vite + Tailwind |
| **行情/交易** | Futu OpenD（港股 + 美股模拟交易） |
| **全球数据** | yfinance + Alpha Vantage |
| **搜索引擎** | Tavily / Brave / SerpAPI / SearXNG / Bocha / Anspire / MiniMax |
| **社交舆情** | Reddit / X / Polymarket（via api.adanos.org） |
| **记忆** | BM25 词法检索（无需 embedding API） |

---

## 特别鸣谢

- [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) — 核心架构灵感
- [KylinMountain/TradingAgents-AShare](https://github.com/KylinMountain/TradingAgents-AShare) — A 股适配基础

## 许可说明

- 核心逻辑基于 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) (Apache 2.0)
- 新增模块采用 `PolyForm Noncommercial 1.0.0` 协议

## 重要声明

- **仅供学习研究**：不构成投资建议
- **实盘风险**：证券市场有风险，投资需谨慎
- **数据延迟**：数据源可能存在延迟，以交易所实时公告为准
