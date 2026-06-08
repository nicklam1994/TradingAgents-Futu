from __future__ import annotations

import asyncio
import json
import os
import re
import traceback
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Lock
from fastapi import Body
from typing import Any, Dict, List, Literal, Optional, Tuple
from uuid import uuid4

import logging
import time

# Configure standard logging to include timestamps
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, HTTPException, Depends, Query, Request, UploadFile, status, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_serializer
from sqlalchemy.orm import Session
import pandas as pd

from api.database import UserDB, UserLLMConfigDB, VersionStatsDB, ReportDB, ImportedPortfolioPositionDB, FeedbackDB, SponsorDB, init_db, get_db, get_db_ctx
from api.job_store import get_job_store as _new_job_store
from api.services import auth_service, portfolio_import_service, report_service, token_service, watchlist_service, scheduled_service, tracking_board_service, watchlist_board_service, feedback_service, sponsor_service
from api.services.quote_ws_manager import quote_ws_manager
from api.services.bot import BotManager, DingTalkBot, FeishuBot, DiscordBot, TelegramBot, BotCommandHandler

def _get_real_ip(request: Request) -> Optional[str]:
    """Extract real client IP, preferring Cloudflare/proxy headers."""
    if request is None:
        return None
    # Cloudflare Tunnel injects the real client IP here
    ip = request.headers.get("CF-Connecting-IP")
    if ip:
        return ip.strip()
    # Standard proxy header fallback
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.graph.data_collector import DataCollector

# 全局共享 DataCollector：同一 ticker+date 的数据只拉一次，所有 job 复用缓存
_shared_data_collector = DataCollector()
from tradingagents.dataflows.market_calendar import today_str as market_today_str
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.quant_metrics import QuantMetrics
from tradingagents.graph.intent_parser import parse_intent as _parse_intent
from tradingagents.agents.utils.context_utils import USER_CONTEXT_KEYS, normalize_user_context
from tradingagents.agents.utils.agent_states import current_tracker_var


def _cors_allow_origins() -> list[str]:
    raw = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    default_origins = [
        "http://127.0.0.1:5174",
        "http://localhost:5174",
        "http://127.0.0.1:5175",
        "http://localhost:5175",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]
    if not raw:
        return default_origins
    return [item.strip() for item in raw.split(",") if item.strip()]


def _cors_allow_origin_regex() -> str | None:
    raw = os.getenv("CORS_ALLOW_ORIGIN_REGEX", "").strip()
    return raw or None


def _report_version_stats() -> None:
    """Report anonymous version stats to the official site."""
    import threading, uuid

    def _send():
        try:
            requests.post(
                "https://app.510168.xyz/api/version-stats",
                json={"v": APP_VERSION, "nonce": uuid.uuid4().hex},
                timeout=30,
            )
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def _resolve_scheduled_trade_date(trade_date: str) -> str:
    """Use the requested trading day, or fall back to the latest HK trading day."""
    from tradingagents.dataflows.market_calendar import is_trading_day, previous_trading_day

    return trade_date if is_trading_day(trade_date, "HK") else previous_trading_day(trade_date, "HK")


def _build_scheduled_analyze_request(
    db: Session,
    user_id: str,
    symbol: str,
    horizon: str,
    trade_date: str,
    scheduled_user_context: Optional[Dict[str, Any]] = None,
) -> "AnalyzeRequest":
    scheduled_user_context = scheduled_user_context or _build_imported_user_context(db, user_id, symbol)
    # Read user's saved analyst selection from DB
    user_cfg = auth_service.get_user_llm_config(db, user_id)
    selected = None
    if user_cfg and user_cfg.default_analysts:
        try:
            selected = json.loads(user_cfg.default_analysts)
        except Exception:
            pass
    req = AnalyzeRequest(
        symbol=symbol,
        trade_date=trade_date,
        horizons=[horizon],
        query=f"定时分析 {symbol}",
        user_intent={
            "ticker": symbol,
            "horizons": [horizon],
            "focus_areas": [],
            "specific_questions": [],
            "user_context": scheduled_user_context,
        },
        objective=scheduled_user_context.get("objective"),
        current_position=scheduled_user_context.get("current_position"),
        current_position_pct=scheduled_user_context.get("current_position_pct"),
        average_cost=scheduled_user_context.get("average_cost"),
        user_notes=scheduled_user_context.get("user_notes"),
    )
    if selected:
        req.selected_analysts = selected
    return req


async def _run_manual_trigger(
    task: dict,
    requested_trade_date: str,
    job_id: str,
) -> None:
    """Execute a manual-trigger analysis (no scheduler concurrency control).

    Used by the /v1/scheduled/{id}/trigger and /v1/scheduled/batch/trigger
    endpoints. Calls _run_job directly then records the test result.
    """
    task_id = task["id"]
    user_id = task["user_id"]
    symbol = task["symbol"]
    horizon = task.get("horizon") or "short"

    actual_trade_date = _resolve_scheduled_trade_date(requested_trade_date)
    _log(f"[Manual Trigger] {symbol} trade_date={actual_trade_date} (requested={requested_trade_date})")

    try:
        with get_db_ctx() as db:
            scheduled_user_context = task.get("manual_user_context") or _build_imported_user_context(
                db, user_id, symbol
            )
            req = _build_scheduled_analyze_request(
                db=db,
                user_id=user_id,
                symbol=symbol,
                horizon=horizon,
                trade_date=actual_trade_date,
                scheduled_user_context=scheduled_user_context,
            )

        await _run_job(job_id, req, False, True, user_id, "scheduled_manual")
        job_state = _get_job(job_id)
        if job_state.get("status") == "failed":
            raise RuntimeError(job_state.get("error") or f"manual trigger job {job_id} failed")
        with get_db_ctx() as db:
            scheduled_service.record_manual_test_result(db, task_id, "success", report_id=job_id)
        _log(f"[Manual Trigger] Completed {symbol}")
    except Exception as e:
        logger.error(f"[Manual Trigger] Failed {symbol}: {e}\n{traceback.format_exc()}")
        with get_db_ctx() as db:
            scheduled_service.record_manual_test_result(db, task_id, "failed")


# ── Bot platform manager ─────────────────────────────────────────────────────
_bot_manager: Optional[BotManager] = None


def _bot_analyze_fn_factory():
    """Create the analyze coroutine for the bot command handler.

    Returns an async function that triggers a TradingAgents analysis job
    and waits for the result.
    """
    async def _analyze(symbol: str, **kwargs: Any) -> str:
        """Trigger a TradingAgents analysis and return the result text."""
        horizon = kwargs.get("horizon", "short")
        from uuid import uuid4

        trade_date = market_today_str("HK")
        job_id = f"bot-{uuid4().hex[:8]}"

        req = AnalyzeRequest(
            symbol=symbol,
            trade_date=trade_date,
            horizons=[horizon],
            query=f"Bot analysis {symbol}",
            user_intent={
                "ticker": symbol,
                "horizons": [horizon],
                "focus_areas": [],
                "specific_questions": [],
                "user_context": {},
            },
        )

        store = get_job_store()
        store.create(job_id, {"status": "running", "symbol": symbol, "source": "bot"})

        await _run_job(job_id, req, user_id="bot", request_source="bot")

        # Wait for job completion (poll)
        for _ in range(600):  # 5 minutes max
            state = store.get(job_id)
            if state and state.get("status") in ("completed", "failed"):
                break
            await asyncio.sleep(0.5)

        state = store.get(job_id) or {}
        if state.get("status") == "completed":
            report = state.get("report") or state.get("result", {})
            # Extract the key parts of the analysis
            parts = []
            if isinstance(report, dict):
                if report.get("decision"):
                    parts.append(f"决策: {report['decision']}")
                if report.get("direction"):
                    parts.append(f"方向: {report['direction']}")
                if report.get("confidence") is not None:
                    parts.append(f"置信度: {report['confidence']}%")
                summary = (
                    report.get("final_trade_decision")
                    or report.get("trader_investment_plan")
                    or report.get("investment_plan")
                    or ""
                )
                if summary:
                    # Clip to reasonable length
                    parts.append(f"\n{summary[:1500]}")
            return "\n".join(parts) if parts else f"分析完成，但未生成摘要。Job ID: {job_id}"
        else:
            error = state.get("error", "unknown error")
            return f"❌ 分析失败: {error}"

    return _analyze


def _init_bot_manager() -> BotManager:
    """Initialize and configure the bot manager with all enabled platforms."""
    manager = BotManager()

    # Create the command handler
    handler = BotCommandHandler(
        analyze_fn=_bot_analyze_fn_factory(),
    )
    manager.on_message(handler.handle)

    # Register platforms based on available credentials
    if os.getenv("DINGTALK_APP_KEY") and os.getenv("DINGTALK_APP_SECRET"):
        manager.register(DingTalkBot())
        _log("DingTalk bot registered.")

    if os.getenv("FEISHU_APP_ID") and os.getenv("FEISHU_APP_SECRET"):
        manager.register(FeishuBot())
        _log("Feishu bot registered.")

    if os.getenv("DISCORD_BOT_TOKEN"):
        manager.register(DiscordBot())
        _log("Discord bot registered.")

    if os.getenv("TELEGRAM_BOT_TOKEN"):
        manager.register(TelegramBot())
        _log("Telegram bot registered.")

    return manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup and cleanup on shutdown."""
    # Raise the AnyIO thread limiter ceiling so frequent sync endpoints
    # (tracking-board polling, /v1/jobs/{id} polling, akshare-backed
    # market endpoints) cannot starve each other when the event loop is
    # also running long-lived `_run_job` tasks.
    try:
        from anyio import to_thread as _anyio_to_thread

        limiter = _anyio_to_thread.current_default_thread_limiter()
        desired = int(os.getenv("ANYIO_THREAD_LIMIT", "120"))
        if limiter.total_tokens < desired:
            limiter.total_tokens = desired
            _log(f"AnyIO thread limiter raised to {desired}.")
    except Exception as exc:
        _log(f"Could not raise AnyIO thread limiter: {exc}")

    # Default asyncio executor is used by `asyncio.to_thread`. The CPython
    # default is `min(32, cpu_count + 4)`, which is too small when many
    # `_run_job_inner` coroutines fan out concurrent `to_thread` calls for
    # DB writes, LLM extraction, and akshare data collection.
    new_default_executor: Optional[ThreadPoolExecutor] = None
    try:
        loop = asyncio.get_running_loop()
        executor_workers = int(os.getenv("ASYNCIO_DEFAULT_EXECUTOR_WORKERS", "64"))
        new_default_executor = ThreadPoolExecutor(
            max_workers=executor_workers,
            thread_name_prefix="ta-asyncio",
        )
        loop.set_default_executor(new_default_executor)
        _log(f"Default asyncio executor set to {executor_workers} workers.")
    except Exception as exc:
        _log(f"Could not configure default asyncio executor: {exc}")

    init_db()
    _log("Database initialized.")
    store = get_job_store()
    store.clear()
    _background_tasks.clear()

    # Security: warn loudly if using default secret key
    if not os.getenv("TA_APP_SECRET_KEY"):
        _log("=" * 70)
        _log("WARNING: TA_APP_SECRET_KEY is not set!")
        _log("Using hardcoded default key. ALL encryption and JWT signing")
        _log("is INSECURE. Set TA_APP_SECRET_KEY env var before production use.")
        _log("=" * 70)

    _report_version_stats()

    # ── Inject API keys from DB into os.environ ────────────────────────
    await asyncio.to_thread(_inject_search_config_to_env)

    # Trade calendar: using Futu OpenD on-demand (no pre-load needed)
    _log("Trade calendar: using Futu OpenD on-demand.")
    # Pre-load stock resolver universe (US/ETF/HK, 35k+ entries)
    await asyncio.to_thread(_load_stock_resolver)
    _log("Stock map pre-loaded on startup.")

    # ── Bot platforms ────────────────────────────────────────────────────
    global _bot_manager
    _bot_manager = _init_bot_manager()
    if _bot_manager.bots:
        _log(f"Starting {len(_bot_manager.bots)} bot platforms...")
        await _bot_manager.start_all()
        _log(f"Bot platforms active: {', '.join(_bot_manager.list_active())}")
    else:
        _log("No bot platforms configured (set DINGTALK_APP_KEY / FEISHU_APP_ID / DISCORD_BOT_TOKEN / TELEGRAM_BOT_TOKEN to enable).")

    # ── WebSocket Quote Manager (Futu real-time push) ──────────────
    await quote_ws_manager.start()
    _log("WebSocket Quote Manager started.")

    yield
    _log("Shutting down: Cleaning up resources...")
    await quote_ws_manager.stop()
    _log("WebSocket Quote Manager stopped.")
    if _bot_manager and _bot_manager.bots:
        _log("Stopping bot platforms...")
        await _bot_manager.stop_all()
    _executor.shutdown(wait=True)
    if new_default_executor is not None:
        new_default_executor.shutdown(wait=False)
    _log("Executor shutdown complete.")


_is_prod = os.getenv("ENV", "").lower() == "prod"


def _get_version() -> str:
    """Get app version: APP_VERSION env > package metadata > 'dev'."""
    v = os.getenv("APP_VERSION")
    if v:
        return v
    try:
        from importlib.metadata import version as pkg_version
        return pkg_version("tradingagents")
    except Exception:
        return "dev"


APP_VERSION = _get_version()

app = FastAPI(
    title="TradingAgents-AShare API",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_origin_regex=_cors_allow_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_executor = ThreadPoolExecutor(max_workers=int(os.getenv("TA_MAX_WORKERS", "2")))

# ── Singleton job store (in-memory or Redis depending on REDIS_URL) ─────────
_job_store_instance: Optional[Any] = None

def get_job_store():
    global _job_store_instance
    if _job_store_instance is None:
        _job_store_instance = _new_job_store()
    return _job_store_instance

# Runtime config overrides via PATCH /v1/config
_global_config_overrides: Dict[str, Any] = {}

# Allowlist for config_overrides from client requests.
# Security: prevents injection of api_key, backend_url, or other sensitive keys.
_CONFIG_OVERRIDES_ALLOWLIST = {
    "llm_provider", "deep_think_llm", "quick_think_llm",
    "max_debate_rounds", "max_risk_discuss_rounds",
    "prompt_language",
}
# Hold references to fire-and-forget tasks so they are not garbage collected
_background_tasks: set = set()

# ── Stock name/code resolver (US/HK/ETF via stock_resolver) ───────────────────
# Replaces deprecated _cn_stock_map (A-share via akshare).


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_JOB_TIMEOUT = int(os.getenv("TA_JOB_TIMEOUT", "1800"))  # seconds (默认 30 分钟，适配多 Agent 长流程分析)
def _create_tracked_task(coro, *, label: str = "Background task") -> asyncio.Task:
    """Create an asyncio task and keep a reference to prevent GC.
    Also logs unhandled exceptions via a done callback."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task):
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception():
            logger.error("%s failed: %s", label, t.exception())

    task.add_done_callback(_on_done)
    return task


def _log(msg: str):
    """Helper to log with timestamp via standard logging."""
    logger.info(msg)


def _serialize_datetime_utc(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _force_update_env(key: str, value: str):
    """Force-update an env var (used after PUT endpoints to take effect immediately)."""
    if value:
        os.environ[key] = value
    elif key in os.environ:
        del os.environ[key]


def _inject_search_config_to_env():
    """Read search_config from DB and inject API keys into os.environ.

    This ensures SearchService, SocialSentimentService, and data source
    providers can read their keys via os.getenv() at runtime.
    """
    try:
        from api.database import SessionLocal
        from api.services import auth_service

        db = SessionLocal()
        try:
            # Find any user with search_config
            from api.database import UserLLMConfigDB
            user_cfg = db.query(UserLLMConfigDB).filter(
                UserLLMConfigDB.search_config.isnot(None),
                UserLLMConfigDB.search_config != "",
            ).first()
            if not user_cfg or not user_cfg.search_config:
                _log("[EnvInject] No search_config found in DB.")
                return

            import json
            cfg = json.loads(user_cfg.search_config)

            # Search provider keys
            _SEARCH_ENV = {
                "tavily": "TAVILY_API_KEYS",
                "brave": "BRAVE_API_KEYS",
                "serpapi": "SERPAPI_API_KEYS",
                "bocha": "BOCHA_API_KEYS",
                "anspire": "ANSPIRE_API_KEYS",
                "minimax": "MINIMAX_API_KEYS",
                "searxng": "SEARXNG_BASE_URLS",
            }
            injected = []
            for name, env_var in _SEARCH_ENV.items():
                key = cfg.get(name, {}).get("api_key", "").strip()
                if key and not os.environ.get(env_var):
                    os.environ[env_var] = key
                    injected.append(name)

            # Data source keys
            _DS_ENV = {
                "alpha_vantage": "ALPHA_VANTAGE_API_KEY",
                "finnhub": "FINNHUB_API_KEY",
                "tiingo": "TIINGO_API_KEY",
                "aitick": "AITICK_API_KEY",
                "itick": "ITICK_API_KEY",
            }
            for name, env_var in _DS_ENV.items():
                key = cfg.get("data_sources", {}).get(name, {}).get("api_key", "").strip()
                if key and not os.environ.get(env_var):
                    os.environ[env_var] = key
                    injected.append(name)

            # Social sentiment
            ss = cfg.get("social_sentiment", {})
            ss_key = ss.get("api_key", "").strip()
            if ss_key and not os.environ.get("SOCIAL_SENTIMENT_API_KEY"):
                os.environ["SOCIAL_SENTIMENT_API_KEY"] = ss_key
                injected.append("social_sentiment")
            ss_url = ss.get("base_url", "").strip()
            if ss_url and not os.environ.get("SOCIAL_SENTIMENT_BASE_URL"):
                os.environ["SOCIAL_SENTIMENT_BASE_URL"] = ss_url

            # Cache TTL
            cache_ttl = cfg.get("cache_ttl", 1800)
            os.environ["SEARCH_CACHE_TTL"] = str(cache_ttl)

            if injected:
                _log(f"[EnvInject] Injected {len(injected)} API keys from DB: {', '.join(injected)}")
            else:
                _log("[EnvInject] No API keys to inject (all empty or already set).")
            _log(f"[EnvInject] Search cache TTL: {cache_ttl}s")
        finally:
            db.close()
    except Exception as e:
        _log(f"[EnvInject] Failed: {e}")


def _load_stock_resolver():
    """Pre-load the stock_resolver universe (US/ETF/HK, 35k+ entries)."""
    try:
        from tradingagents.dataflows.stock_resolver import _load
        _load()
    except Exception as e:
        _log(f"[StockResolver] Pre-load failed: {e}")


def _get_reverse_stock_map() -> Dict[str, str]:
    """Return code→name mapping from stock_resolver (US/ETF/HK)."""
    try:
        from tradingagents.dataflows.stock_resolver import _get_by_upper
        universe = _get_by_upper()
        return {entry["code"]: entry["name"] for entry in universe.values()}
    except Exception:
        return {}


def _get_reverse_stock_map_cached_only() -> Dict[str, str]:
    """Return code→name mapping only from already-warmed cache."""
    return _get_reverse_stock_map()


def _search_stock_by_name(query: str) -> Optional[str]:
    """Look up stock code by name/ticker using stock_resolver (US/ETF/HK)."""
    query = query.strip()
    if not query:
        return None
    try:
        from tradingagents.dataflows.stock_resolver import resolve_ticker
        result = resolve_ticker(query)
        if result:
            return result["code"]
    except Exception:
        pass
    return None


def _split_watchlist_batch_text(text: str) -> List[str]:
    return [token.strip() for token in re.split(r"[\s,，、；;]+", text.strip()) if token.strip()]


def _resolve_watchlist_identifier(
    raw: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    token = raw.strip()
    if not token:
        return None, None, "输入为空"
    # Try stock_resolver first
    try:
        from tradingagents.dataflows.stock_resolver import resolve_ticker, to_display, is_known_ticker
        resolved = resolve_ticker(token)
        if resolved:
            return resolved["code"], to_display(resolved["code"]), None
    except Exception:
        pass
    # Fallback to normalize
    symbol = _normalize_symbol(token)
    if symbol and symbol != token:
        return symbol, symbol, None
    return None, None, f"未识别的股票代码或名称: {token}"


_auth_scheme = HTTPBearer(auto_error=False)

FIXED_TEAMS = {
    "Analyst Team": [
        "Market Analyst",
        "Social Analyst",
        "News Analyst",
        "Fundamentals Analyst",
        "Macro Analyst",
        "Smart Money Analyst",
        "Volume Price Analyst",
    ],
    "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
    "Trading Team": ["Trader"],
    "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
    "Portfolio Management": ["Portfolio Manager"],
}
ANALYST_ORDER = ["market", "social", "news", "fundamentals", "macro", "smart_money", "volume_price"]
ANALYST_AGENT_NAMES = {
    "market": "Market Analyst",
    "social": "Social Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
    "macro": "Macro Analyst",
    "volume_price": "Volume Price Analyst",
    "smart_money": "Smart Money Analyst",
    "bull": "Bull Researcher",
    "bear": "Bear Researcher",
    "Bull_Initial": "Bull Researcher",
    "Bear_Initial": "Bear Researcher",
    "Bull_Rebuttal": "Bull Researcher",
    "Bear_Rebuttal": "Bear Researcher",
    "research_manager": "Research Manager",
    "trader": "Trader",
    "aggressive": "Aggressive Analyst",
    "neutral": "Neutral Analyst",
    "conservative": "Conservative Analyst",
    "portfolio_manager": "Portfolio Manager",
}
ANALYST_REPORT_MAP = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
    "macro": "macro_report",
    "smart_money": "smart_money_report",
    "volume_price": "volume_price_report",
}

# All analysts always run — each uses its own natural time window
# (technical/funds → short, fundamentals/macro → medium)
def _get_horizon_analysts(horizon: str, available: List[str]) -> List[str]:
    """Return all available analysts regardless of horizon."""
    return list(available)


def _announcements_file() -> Path:
    return Path(__file__).resolve().parent / "announcements.json"


def _load_latest_announcement() -> Optional[Dict[str, Any]]:
    path = _announcements_file()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log(f"[Announcements] Failed to read {path.name}: {exc}")
        return None

    announcements = raw.get("announcements") if isinstance(raw, dict) else raw
    if not isinstance(announcements, list):
        return None

    for item in announcements:
        if not isinstance(item, dict):
            continue
        if item.get("active", True) is False:
            continue
        return item
    return None


class UserContextInput(BaseModel):
    objective: Optional[str] = Field(None, description="用户目标动作，如建仓/加仓/减仓/止损/观察")
    risk_profile: Optional[str] = Field(None, description="风险偏好，如保守/平衡/激进")
    investment_horizon: Optional[str] = Field(None, description="持有周期，如短线/波段/中线")
    cash_available: Optional[float] = Field(None, description="可用资金")
    current_position: Optional[float] = Field(None, description="当前持仓数量")
    current_position_pct: Optional[float] = Field(None, description="当前仓位占比")
    average_cost: Optional[float] = Field(None, description="当前持仓成本")
    max_loss_pct: Optional[float] = Field(None, description="最大容忍亏损百分比")
    constraints: List[str] = Field(default_factory=list, description="用户的硬约束列表")
    user_notes: Optional[str] = Field(None, description="用户补充说明")


class AnalyzeRequest(UserContextInput):
    symbol: str = Field(default="", description="股票代码，如 600519.SH（当 query 包含代码时可省略）")
    trade_date: str = Field(default_factory=lambda: market_today_str("HK"), description="交易日期 YYYY-MM-DD")
    selected_analysts: List[str] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals", "macro", "smart_money", "volume_price"]
    )
    config_overrides: Dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    # When set, triggers intent-driven analysis via streaming dual-horizon path
    query: Optional[str] = Field(default=None, description="自然语言查询，如：分析贵州茅台短线机会")
    horizons: List[str] = Field(default_factory=lambda: ["short"], description="分析周期列表，如 ['short'] 或 ['short','medium']")
    # Pre-parsed intent from _ai_extract_symbol_and_date (avoids second LLM call in _run_job)
    user_intent: Optional[Dict[str, Any]] = Field(default=None, description="预解析的用户意图，由 chat_completions 传入")


class AnalyzeResponse(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    created_at: str


class BatchScheduledTriggerJob(BaseModel):
    item_id: str
    job_id: str
    symbol: str
    name: str
    status: Literal["pending", "running", "completed", "failed"]
    created_at: str
    current_position: Optional[float] = None
    average_cost: Optional[float] = None
    waiting_ahead_count: Optional[int] = None
    scheduled_running_count: Optional[int] = None
    scheduled_concurrency_limit: Optional[int] = None


class BatchScheduledTriggerResponse(BaseModel):
    summary: Dict[str, int]
    jobs: List[BatchScheduledTriggerJob]


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    symbol: str
    trade_date: str
    error: Optional[str] = None
    waiting_ahead_count: Optional[int] = None
    scheduled_running_count: Optional[int] = None
    scheduled_concurrency_limit: Optional[int] = None


class ChatMessage(BaseModel):
    role: str
    content: Any


class ChatCompletionRequest(UserContextInput):
    model: Optional[str] = "tradingagents-ashare"
    messages: List[ChatMessage]
    stream: bool = True
    selected_analysts: List[str] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals", "macro", "smart_money", "volume_price"]
    )
    config_overrides: Dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    resolved_symbol: Optional[str] = None  # User-selected symbol from disambiguation


class KlineResponse(BaseModel):
    symbol: str
    start_date: str
    end_date: str
    candles: List[Dict[str, Any]]


# Report API Models
class ReportCreateRequest(BaseModel):
    symbol: str = Field(..., description="股票代码")
    trade_date: str = Field(..., description="交易日期 YYYY-MM-DD")
    decision: Optional[str] = Field(None, description="交易决策")
    result_data: Optional[Dict[str, Any]] = Field(None, description="完整分析结果")


class ReportResponse(BaseModel):
    id: str
    user_id: Optional[str]
    symbol: str
    name: Optional[str] = None
    trade_date: str
    status: Literal["pending", "running", "completed", "failed"] = "completed"
    error: Optional[str] = None
    decision: Optional[str]
    direction: Optional[str]
    confidence: Optional[int]
    target_price: Optional[float]
    stop_loss_price: Optional[float]
    risk_items: Optional[List[Dict[str, Any]]] = None
    key_metrics: Optional[List[Dict[str, Any]]] = None
    analyst_traces: Optional[List[Dict[str, Any]]] = None
    risk_flags: Optional[List[str]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    waiting_ahead_count: Optional[int] = None
    scheduled_running_count: Optional[int] = None
    scheduled_concurrency_limit: Optional[int] = None

    model_config = {"from_attributes": True}

    @field_serializer("created_at", "updated_at", when_used="json")
    def serialize_report_datetimes(self, value: Optional[datetime]) -> Optional[str]:
        return _serialize_datetime_utc(value)


class ReportDetailResponse(ReportResponse):
    market_report: Optional[str]
    sentiment_report: Optional[str]
    news_report: Optional[str]
    fundamentals_report: Optional[str]
    macro_report: Optional[str]
    smart_money_report: Optional[str]
    volume_price_report: Optional[str]
    game_theory_report: Optional[str]
    investment_plan: Optional[str]
    trader_investment_plan: Optional[str]
    final_trade_decision: Optional[str]
    result_data: Optional[Dict[str, Any]]


class ReportListResponse(BaseModel):
    total: int
    reports: List[ReportResponse]


class ReportBatchDeleteRequest(BaseModel):
    report_ids: List[str] = Field(default_factory=list)


class ReportBatchDeleteResponse(BaseModel):
    deleted_ids: List[str]
    missing_ids: List[str]


class LatestReportsBySymbolsRequest(BaseModel):
    symbols: List[str] = Field(default_factory=list)


class LatestReportsBySymbolsResponse(BaseModel):
    reports: List[ReportResponse]


class PortfolioOverviewResponse(BaseModel):
    watchlist: List[dict]
    scheduled: List[dict]
    latest_reports: List[ReportResponse]
    portfolio_import: Optional[dict] = None


class WatchlistAddRequest(BaseModel):
    text: Optional[str] = None
    symbol: Optional[str] = None


class ScheduledBatchIdsRequest(BaseModel):
    item_ids: List[str] = Field(default_factory=list)


class ScheduledBatchUpdateRequest(BaseModel):
    item_ids: List[str] = Field(default_factory=list)
    is_active: Optional[bool] = None
    horizon: Optional[str] = None
    trigger_time: Optional[str] = None


class AnnouncementItemResponse(BaseModel):
    title: str
    detail: str


class AnnouncementResponse(BaseModel):
    id: str
    tag: Optional[str] = None
    title: str
    summary: Optional[str] = None
    published_at: str
    items: List[AnnouncementItemResponse]
    cta_label: Optional[str] = None
    cta_path: Optional[str] = None


class LatestAnnouncementResponse(BaseModel):
    announcement: Optional[AnnouncementResponse] = None


class UserResponse(BaseModel):
    id: str
    email: str
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
    email_report_enabled: bool = True

    model_config = {"from_attributes": True}

    @field_serializer("created_at", "last_login_at", when_used="json")
    def serialize_user_datetimes(self, value: Optional[datetime]) -> Optional[str]:
        return _serialize_datetime_utc(value)


class AuthRequestCodeRequest(BaseModel):
    email: str


class AuthVerifyCodeRequest(BaseModel):
    email: str
    code: str


class AuthVerifyCodeResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class UserRuntimeConfigResponse(BaseModel):
    llm_provider: str
    deep_think_llm: str
    quick_think_llm: str
    backend_url: str
    max_debate_rounds: int
    max_risk_discuss_rounds: int
    has_api_key: bool = False
    api_key: Optional[str] = None
    has_wecom_webhook: bool = False
    wecom_webhook_display: Optional[str] = None
    server_fallback_enabled: bool = True
    email_report_enabled: bool = True
    wecom_report_enabled: bool = True
    default_analysts: List[str] = Field(default_factory=lambda: ["market", "social", "news", "fundamentals", "macro", "smart_money", "volume_price"])


class UserRuntimeConfigUpdateRequest(BaseModel):
    llm_provider: Optional[str] = None
    deep_think_llm: Optional[str] = None
    quick_think_llm: Optional[str] = None
    backend_url: Optional[str] = None
    max_debate_rounds: Optional[int] = None
    max_risk_discuss_rounds: Optional[int] = None
    email_report_enabled: Optional[bool] = None
    wecom_report_enabled: Optional[bool] = None
    api_key: Optional[str] = None
    wecom_webhook_url: Optional[str] = None
    clear_api_key: bool = False
    clear_wecom_webhook: bool = False
    warmup: bool = True
    force_warmup: bool = False
    default_analysts: Optional[List[str]] = None


class SearchProviderConfig(BaseModel):
    name: str
    label: str
    env_key: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    enabled: bool = True


class SearchConfigResponse(BaseModel):
    providers: List[SearchProviderConfig]
    has_any_key: bool = False
    cache_ttl: int = 1800


class SearchConfigUpdateRequest(BaseModel):
    providers: List[SearchProviderConfig]
    cache_ttl: Optional[int] = None


# ── Data source provider config (行情数据源) ─────────────────────────────────

class DataSourceProviderConfig(BaseModel):
    name: str
    label: str
    env_key: str
    market: str  # "US" | "HK" | "both"
    api_key: Optional[str] = None
    enabled: bool = True


class DataSourceConfigResponse(BaseModel):
    providers: List[DataSourceProviderConfig]
    has_any_key: bool = False


class DataSourceConfigUpdateRequest(BaseModel):
    providers: List[DataSourceProviderConfig]


class SocialSentimentConfigResponse(BaseModel):
    api_key: str = ""
    base_url: str = "https://api.adanos.org"
    has_key: bool = False

class SocialSentimentConfigUpdateRequest(BaseModel):
    api_key: str = ""
    base_url: str = "https://api.adanos.org"


class UserRuntimeWarmupRequest(UserRuntimeConfigUpdateRequest):
    prompt: str = "你好"


class RuntimeWarmupResult(BaseModel):
    model: str
    targets: List[str] = Field(default_factory=list)
    content: Optional[str] = None
    error: Optional[str] = None


class UserRuntimeWarmupResponse(BaseModel):
    prompt: str
    results: List[RuntimeWarmupResult]


class WecomWebhookWarmupRequest(BaseModel):
    wecom_webhook_url: Optional[str] = None
    content: Optional[str] = None


class WecomWebhookWarmupResponse(BaseModel):
    sent: bool = True
    message: str
    webhook_display: Optional[str] = None


class PortfolioPositionItem(BaseModel):
    symbol: str = Field(..., description="股票代码，如 600519.SH 或 600519")
    name: Optional[str] = Field(None, description="股票名称")
    current_position: Optional[float] = Field(None, description="持仓数量")
    available_position: Optional[float] = Field(None, description="可用数量")
    average_cost: Optional[float] = Field(None, description="成本价")
    market_value: Optional[float] = Field(None, description="市值")
    current_position_pct: Optional[float] = Field(None, description="仓位占比 %")


class PortfolioImportSyncRequest(BaseModel):
    positions: List[PortfolioPositionItem] = Field(..., description="持仓列表")
    source: str = Field("manual", description="持仓来源标识")
    auto_apply_scheduled: bool = Field(True, description="是否自动将持仓股票加入定时任务")


class UserTokenResponse(BaseModel):
    id: str
    name: str
    token: str
    token_hint: Optional[str] = None
    last_used_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("created_at", "last_used_at", when_used="json")
    def serialize_token_datetimes(self, value: Optional[datetime]) -> Optional[str]:
        return _serialize_datetime_utc(value)


class UserTokenListItem(BaseModel):
    """Token info for list endpoint — never exposes the full token."""
    id: str
    name: str
    token_hint: Optional[str] = None
    last_used_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("created_at", "last_used_at", when_used="json")
    def serialize_token_datetimes(self, value: Optional[datetime]) -> Optional[str]:
        return _serialize_datetime_utc(value)


class UserTokenCreateRequest(BaseModel):
    name: str


def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _user_config_overrides(user_id: Optional[str], db: Optional[Session] = None) -> Dict[str, Any]:
    if not user_id:
        return {}

    def _query(sess: Session) -> Dict[str, Any]:
        user_cfg = auth_service.get_user_llm_config(sess, user_id)
        if not user_cfg:
            return {}
        result: Dict[str, Any] = {}
        for key in (
            "llm_provider",
            "backend_url",
            "quick_think_llm",
            "deep_think_llm",
            "max_debate_rounds",
            "max_risk_discuss_rounds",
        ):
            value = getattr(user_cfg, key, None)
            if value is not None:
                result[key] = value
        api_key = auth_service.decrypt_secret(user_cfg.api_key_encrypted)
        if api_key:
            result["api_key"] = api_key
        return result

    if db is not None:
        return _query(db)
    with get_db_ctx() as own_db:
        return _query(own_db)


def _build_runtime_config(overrides: Dict[str, Any], user_id: Optional[str] = None, db: Optional[Session] = None) -> Dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)
    server_fallback_enabled = os.getenv("ALLOW_SERVER_LLM_FALLBACK", "1").strip().lower() in ("1", "true", "yes", "on")
    config["server_fallback_enabled"] = server_fallback_enabled

    # Security: filter request overrides to allowlist only
    overrides = {k: v for k, v in overrides.items() if k in _CONFIG_OVERRIDES_ALLOWLIST}

    # Apply global config overrides (from PATCH /v1/config)
    if _global_config_overrides:
        config = _deep_merge(config, dict(_global_config_overrides))
    
    # Fetch user specific overrides from DB (pass db to reuse caller's session)
    user_overrides = _user_config_overrides(user_id, db=db)

    # ── Critical: Filter out empty strings before merging ──
    # This prevents an empty DB field from wiping out an Env Var default.
    filtered_user_overrides = {k: v for k, v in user_overrides.items() if v not in (None, "", [])}
    filtered_request_overrides = {k: v for k, v in overrides.items() if v not in (None, "", [])}

    if filtered_user_overrides:
        config = _deep_merge(config, filtered_user_overrides)
    if filtered_request_overrides:
        config = _deep_merge(config, filtered_request_overrides)

    # ── Intelligent fallback between models ──
    # If one is provided but the other is missing (even after env var merge), cross-fill.
    quick = config.get("quick_think_llm")
    deep = config.get("deep_think_llm")

    if not deep and quick:
        config["deep_think_llm"] = quick
    if not quick and deep:
        config["quick_think_llm"] = deep

    return config


class RequireUser:
    def __init__(self, allow_api_token: bool = True):
        self.allow_api_token = allow_api_token

    def __call__(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_auth_scheme),
    ) -> UserDB:
        if not credentials:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")

        token = credentials.credentials

        with get_db_ctx() as db:
            # 1. 优先尝试 JWT (网页登录)
            try:
                payload = auth_service.decode_access_token(token)
                user_id = str(payload.get("sub") or "")
                user = auth_service.get_user_by_id(db, user_id)
                if user and user.is_active:
                    # expunge 使 ORM 对象脱离 session，close 后仍可访问属性
                    db.expunge(user)
                    return user
            except Exception:
                # 不是有效的 JWT 或已过期，尝试 API Token
                pass

            # 2. 尝试 API Token (仅在允许时)
            if self.allow_api_token and token.startswith(token_service.TOKEN_PREFIX):
                user = token_service.verify_token(db, token)
                if user and user.is_active:
                    db.expunge(user)
                    return user

        detail = "身份验证失败或该接口不支持 API Token 访问" if self.allow_api_token else "该接口仅限网页端登录访问"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


# 快捷依赖定义
_require_api_user = RequireUser(allow_api_token=True)    # 允许 API Token
_require_web_user = RequireUser(allow_api_token=False)   # 仅限网页登录


def _optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_auth_scheme),
) -> Optional[UserDB]:
    if not credentials:
        return None
    try:
        payload = auth_service.decode_access_token(credentials.credentials)
    except Exception:
        return None
    user_id = str(payload.get("sub") or "")
    if not user_id:
        return None
    with get_db_ctx() as db:
        user = auth_service.get_user_by_id(db, user_id)
        if user:
            db.expunge(user)
        return user


def _set_job(job_key: str, **kwargs) -> None:
    # Callers may pass job_id=<value> as a stored field.  Since
    # store.set_job()'s first positional param is also called job_id,
    # we must strip it from kwargs to avoid a "got multiple values" TypeError.
    # _get_job() always injects job_id back into the returned dict.
    kwargs.pop("job_id", None)
    get_job_store().set_job(job_key, **kwargs)


def _get_job(job_key: str) -> Dict[str, Any]:
    d = get_job_store().get_job(job_key)
    if d:
        d.setdefault("job_id", job_key)
    return d


def _emit_job_event(job_id: str, event: str, data: Dict[str, Any]) -> None:
    get_job_store().emit_event(job_id, event, data)


def _attach_job_runtime_state(target: Any, job_id: Optional[str]) -> Any:
    if not job_id:
        return target
    job = _get_job(job_id)
    if not job:
        return target

    for field in ("waiting_ahead_count", "scheduled_running_count", "scheduled_concurrency_limit"):
        value = job.get(field)
        if value is not None or hasattr(target, field):
            setattr(target, field, value)
    return target


def _extract_request_user_context(request: UserContextInput) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key in USER_CONTEXT_KEYS:
        value = getattr(request, key, None)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if key == "constraints" and not value:
            continue
        payload[key] = value
    return payload


def _merge_user_context_payload(
    explicit_context: Dict[str, Any],
    inferred_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    merged = normalize_user_context(inferred_context or {})
    merged.update(normalize_user_context(explicit_context or {}))
    return merged


def _compose_analysis_user_context(
    db: Session,
    user_id: str,
    symbol: str,
    *,
    explicit_context: Optional[Dict[str, Any]] = None,
    inferred_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    imported_context = _build_manual_imported_user_context(db, user_id, symbol)
    merged_with_imported = _merge_user_context_payload(inferred_context or {}, imported_context)
    return _merge_user_context_payload(explicit_context or {}, merged_with_imported)


def _apply_user_context_to_request(request: "AnalyzeRequest", user_context: Dict[str, Any]) -> "AnalyzeRequest":
    request.objective = user_context.get("objective")
    request.risk_profile = user_context.get("risk_profile")
    request.investment_horizon = user_context.get("investment_horizon")
    request.cash_available = user_context.get("cash_available")
    request.current_position = user_context.get("current_position")
    request.current_position_pct = user_context.get("current_position_pct")
    request.average_cost = user_context.get("average_cost")
    request.max_loss_pct = user_context.get("max_loss_pct")
    request.constraints = user_context.get("constraints", [])
    request.user_notes = user_context.get("user_notes")
    return request


def _build_result_payload(final_state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": final_state.get("company_of_interest"),
        "trade_date": final_state.get("trade_date"),
        "direction": None,
        "instrument_context": final_state.get("instrument_context"),
        "market_context": final_state.get("market_context"),
        "user_context": final_state.get("user_context"),
        "workflow_context": final_state.get("workflow_context"),
        "market_report": final_state.get("market_report"),
        "sentiment_report": final_state.get("sentiment_report"),
        "news_report": final_state.get("news_report"),
        "fundamentals_report": final_state.get("fundamentals_report"),
        "macro_report": final_state.get("macro_report"),
        "smart_money_report": final_state.get("smart_money_report"),
        "volume_price_report": final_state.get("volume_price_report"),
        "game_theory_report": final_state.get("game_theory_report"),
        "game_theory_signals": final_state.get("game_theory_signals"),
        "analyst_traces": final_state.get("analyst_traces"),
        "investment_plan": final_state.get("investment_plan"),
        "trader_investment_plan": final_state.get("trader_investment_plan"),
        "risk_feedback_state": final_state.get("risk_feedback_state"),
        "final_trade_decision": final_state.get("final_trade_decision"),
    }


class AgentProgressTracker:
    # 阶段标题映射
    STAGE_TITLES = {
        "market_analysis": "市场分析完成",
        "sentiment_analysis": "舆情分析完成",
        "news_analysis": "新闻分析完成",
        "fundamentals_analysis": "基本面分析完成",
        "research_decision": "研究团队决策",
        "trader_plan": "交易计划制定",
        "risk_assessment": "风险评估完成",
        "final_decision": "最终决策",
    }
    
    def __init__(self, selected_analysts: List[str], job_id: str, horizon: Optional[str] = None):
        self.job_id = job_id
        self.horizon = horizon
        self.selected_analysts = [a.lower() for a in selected_analysts]
        self.status: Dict[str, str] = {}
        self.start_times: Dict[str, float] = {}  # 记录每个 agent 开始时间
        self.report_sections: Dict[str, Optional[str]] = {
            "market_report": None,
            "sentiment_report": None,
            "news_report": None,
            "fundamentals_report": None,
            "macro_report": None,
            "smart_money_report": None,
            "volume_price_report": None,
            "game_theory_report": None,
            "investment_plan": None,
            "trader_investment_plan": None,
            "final_trade_decision": None,
        }
        # 跟踪已完成的阶段，避免重复发送里程碑
        self._completed_stages: set = set()
        # 跟踪已发送的 writing 状态，避免重复发送
        self._writing_status_sent: set = set()
        
        for team_agents in FIXED_TEAMS.values():
            for agent in team_agents:
                self.status[agent] = "pending"

        # 未选中的分析师标记为 skipped（仍展示，便于固定 12-agent 看板）
        for key in ANALYST_ORDER:
            agent = ANALYST_AGENT_NAMES[key]
            if key not in self.selected_analysts:
                self.status[agent] = "skipped"

    def _emit_milestone(self, stage: str, summary: str = "") -> None:
        """发送用户可见的里程碑事件"""
        if stage in self._completed_stages:
            return
        self._completed_stages.add(stage)
        
        title = self.STAGE_TITLES.get(stage, stage)
        _emit_job_event(
            self.job_id,
            "agent.milestone",
            {
                "stage": stage,
                "title": title,
                "summary": summary,
                "timestamp": _utcnow_iso(),
                "horizon": self.horizon,
            },
        )
        _log(f"[Milestone] {title}: {summary[:100]}...")

    def _emit_report_chunked(self, job_id: str, section: str, content: str) -> None:
        """将报告内容分片发送，直接透传不做人工延迟
        
        按较大块分片（如按段落），让前端自然渲染
        """
        # 按段落分割，保持Markdown结构
        paragraphs = content.split('\n\n')
        
        for i, para in enumerate(paragraphs):
            if not para.strip():
                continue
                
            _emit_job_event(
                job_id,
                "agent.report.chunk",
                {
                    "section": section,
                    "chunk": para + '\n\n',
                    "index": i,
                    "is_complete": False,
                    "horizon": self.horizon,
                },
            )
        
        # 发送完成标记
        _emit_job_event(
            job_id,
            "agent.report.chunk",
            {
                "section": section,
                "chunk": "",
                "index": -1,
                "is_complete": True,
                "horizon": self.horizon,
            },
        )

    def snapshot(self) -> Dict[str, Any]:
        agents = []
        for team, members in FIXED_TEAMS.items():
            for m in members:
                agents.append({"team": team, "agent": m, "status": self.status.get(m, "pending")})
        return {"agents": agents, "horizon": self.horizon}

    def _set_status(self, agent: str, status: str) -> None:
        prev = self.status.get(agent)
        if prev == status:
            return
        self.status[agent] = status
        
        # 记录时间
        if status == "in_progress":
            self.start_times[agent] = time.time()
        elif status == "completed" and agent in self.start_times:
            duration = time.time() - self.start_times[agent]
            _log(f"[Timer] Agent {agent} ({self.horizon or 'main'}) finished in {duration:.2f}s")

        _emit_job_event(
            self.job_id,
            "agent.status",
            {"agent": agent, "status": status, "previous_status": prev, "horizon": self.horizon},
        )

    def _update_research_team_status(self, status: str) -> None:
        for agent in ["Bull Researcher", "Bear Researcher", "Research Manager"]:
            self._set_status(agent, status)

    def _generate_stage_summary(self, stage: str, chunk: Dict[str, Any]) -> str:
        """根据阶段生成简要总结"""
        if stage == "market_analysis":
            report = chunk.get("market_report", "")
            # 提取关键信息
            if "支撑" in report or "压力" in report:
                return "技术面关键位已识别"
            return "技术面分析完成"
        elif stage == "sentiment_analysis":
            return "舆情数据已收集"
        elif stage == "news_analysis":
            return "新闻影响已评估"
        elif stage == "fundamentals_analysis":
            return "基本面指标已计算"
        elif stage == "research_decision":
            return "多空观点已形成"
        elif stage == "trader_plan":
            return "交易策略已制定"
        elif stage == "risk_assessment":
            return "风险水平已评估"
        elif stage == "final_decision":
            decision = chunk.get("final_trade_decision", "")
            return f"最终建议: {decision[:50]}..." if len(decision) > 50 else f"最终建议: {decision}"
        return ""

    def _emit_writing_status(self, agent_name: str, report_type: str) -> None:
        """发送正在编写报告的状态（每个agent只发送一次）"""
        # 检查是否已经发送过
        status_key = f"{agent_name}:{report_type}"
        if status_key in self._writing_status_sent:
            return
        self._writing_status_sent.add(status_key)
        
        report_names = {
            "market_report": "市场分析",
            "sentiment_report": "舆情分析",
            "news_report": "新闻分析",
            "fundamentals_report": "基本面分析",
            "investment_plan": "投资计划",
            "trader_investment_plan": "交易计划",
            "final_trade_decision": "最终交易决策",
        }
        _emit_job_event(
            self.job_id,
            "agent.writing",
            {
                "agent": agent_name,
                "report": report_type,
                "report_name": report_names.get(report_type, report_type),
                "status": "writing",
                "horizon": self.horizon,
            },
        )

    def _emit_token(self, agent_name: str, report_type: str, token: str) -> None:
        """推送 Token 级别的流式内容（跳过空 token，避免思维模型推理阶段刷屏）"""
        if not token:
            return
        _emit_job_event(
            self.job_id,
            "agent.token",
            {
                "agent": agent_name,
                "report": report_type,
                "token": token,
                "horizon": self.horizon,
            },
        )

    def emit_debate_token(
        self, debate: str, agent: str, round_num: int, token: str,
    ) -> None:
        """推送辩论 token（流式输出，每个 chunk 调用一次）"""
        if not token:
            return
        try:
            _emit_job_event(
                self.job_id,
                "agent.debate.token",
                {
                    "debate": debate,
                    "agent": agent,
                    "round": round_num,
                    "token": token,
                    "horizon": self.horizon,
                },
            )
        except Exception:
            pass

    def emit_debate_message(
        self, debate: str, agent: str, round_num: int,
        content: str, is_verdict: bool = False,
    ) -> None:
        """推送辩论消息（每个 agent 每轮完成后调用一次）"""
        if not content:
            return
        try:
            _emit_job_event(
                self.job_id,
                "agent.debate",
                {
                    "debate": debate,
                    "agent": agent,
                    "round": round_num,
                    "content": content,
                    "is_verdict": is_verdict,
                    "horizon": self.horizon,
                },
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to emit debate message for %s in %s", agent, debate, exc_info=True,
            )

    def apply_chunk(self, chunk: Dict[str, Any]) -> None:
        # 分析师阶段状态推进
        found_active = False
        for analyst_key in ANALYST_ORDER:
            if analyst_key not in self.selected_analysts:
                continue

            agent_name = ANALYST_AGENT_NAMES[analyst_key]
            report_key = ANALYST_REPORT_MAP[analyst_key]
            has_report = bool(chunk.get(report_key))

            if has_report:
                if self.status.get(agent_name) != "completed":
                    self._set_status(agent_name, "completed")
                    self.report_sections[report_key] = chunk.get(report_key)
            elif not found_active:
                # 只在状态从 pending 变为 in_progress 时发送 writing 状态
                prev_status = self.status.get(agent_name)
                if prev_status != "in_progress":
                    self._set_status(agent_name, "in_progress")
                    # 发送正在分析的状态（只发送一次）
                    self._emit_writing_status(agent_name, report_key)
                found_active = True
            else:
                self._set_status(agent_name, "pending")

        # 分析师全部完成后，启动 Bull Researcher
        if not found_active and self.selected_analysts:
            if self.status.get("Bull Researcher") == "pending":
                self._set_status("Bull Researcher", "in_progress")

        # 研究团队状态更新
        debate_state = chunk.get("investment_debate_state") or {}
        bull_hist = str(debate_state.get("bull_history", "")).strip()
        bear_hist = str(debate_state.get("bear_history", "")).strip()
        judge = str(debate_state.get("judge_decision", "")).strip()
        if bull_hist or bear_hist:
            self._update_research_team_status("in_progress")
        if judge:
            self._update_research_team_status("completed")
            if self.status.get("Trader") != "in_progress":
                self._set_status("Trader", "in_progress")
                self._emit_writing_status("Trader", "trader_investment_plan")

        # 交易团队
        if chunk.get("trader_investment_plan"):
            if self.status.get("Trader") != "completed":
                self._set_status("Trader", "completed")
                self._set_status("Aggressive Analyst", "in_progress")

        # 风控与组合团队（发送最终决策）
        risk_state = chunk.get("risk_debate_state") or {}
        risk_judge = str(risk_state.get("judge_decision", "")).strip()

        if risk_judge:
            if self.status.get("Portfolio Manager") != "completed":
                self._set_status("Portfolio Manager", "in_progress")
                self._set_status("Aggressive Analyst", "completed")
                self._set_status("Conservative Analyst", "completed")
                self._set_status("Neutral Analyst", "completed")
                self._set_status("Portfolio Manager", "completed")
                final_summary = self._generate_stage_summary("final_decision", chunk)
                self._emit_milestone("final_decision", final_summary)


def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts).strip()
    return str(content)


def _generate_tool_description(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """生成工具调用的可读描述"""
    if tool_name == "get_indicators":
        indicator = tool_args.get("indicator")
        if isinstance(indicator, str) and indicator:
            indicator_map = {
                "close_50_sma": "50日均线",
                "close_200_sma": "200日均线",
                "close_10_ema": "10日EMA",
                "close_20_ema": "20日EMA",
                "rsi": "RSI",
                "macd": "MACD",
                "boll": "布林中轨",
                "boll_ub": "布林上轨",
                "boll_lb": "布林下轨",
                "atr": "ATR波动率",
                "vwma": "VWMA量价均线",
                "obv": "OBV能量潮",
            }
            return f"计算 {indicator_map.get(indicator, indicator)}"
        return "获取技术指标"
    elif tool_name == "get_stock_data":
        return "获取股票历史数据"
    elif tool_name == "get_fundamentals":
        metrics = tool_args.get("metrics", [])
        if metrics:
            return f"获取 {', '.join(metrics[:2])}{' 等' if len(metrics) > 2 else ''} 基本面数据"
        return "获取基本面数据"
    elif tool_name == "get_income_statement":
        return "获取利润表"
    elif tool_name == "get_balance_sheet":
        return "获取资产负债表"
    elif tool_name == "get_cash_flow":
        return "获取现金流量表"
    elif tool_name == "get_news":
        return "获取相关新闻"
    elif tool_name == "get_social_sentiment":
        return "获取舆情数据"
    return f"调用 {tool_name}"


async def _run_job(
    job_id: str,
    request: AnalyzeRequest,
    stream_events: bool = False,
    save_report: bool = True,
    user_id: Optional[str] = None,
    request_source: str = "api",
) -> None:
    # 用 asyncio.Task + sleep 竞速代替 wait_for，避免 cancel 卡在 to_thread 导致
    # semaphore 永远不释放的问题。超时后标记失败但不 cancel 内部协程（让线程自然结束）。
    inner_task = asyncio.create_task(
        _run_job_inner(job_id, request, stream_events, save_report, user_id, request_source)
    )
    done, _ = await asyncio.wait({inner_task}, timeout=_JOB_TIMEOUT)
    if inner_task in done:
        # 正常完成（可能成功也可能异常）
        if not inner_task.cancelled() and inner_task.exception():
            _log(f"[Job {job_id}] failed: {inner_task.exception()}")
        return
    # 超时：标记失败，但不 cancel 内部 task（避免 cancel 卡住）
    err_msg = f"任务超时（超过 {_JOB_TIMEOUT} 秒），已自动终止"
    _log(f"[Job {job_id}] {err_msg}")
    _set_job(job_id, status="failed", error=err_msg, finished_at=_utcnow_iso())
    # 注意：不能用 asyncio.to_thread 写 DB，因为线程池可能被僵尸任务占满导致死锁。
    # 用同步方式直接写，SQLite 的写入足够快不会阻塞事件循环。
    try:
        with get_db_ctx() as db:
            report_service.mark_report_failed(db, job_id, err_msg)
    except Exception:
        pass
    _emit_job_event(job_id, "job.failed", {"job_id": job_id, "error": err_msg})


async def _run_job_inner(
    job_id: str,
    request: AnalyzeRequest,
    stream_events: bool = False,
    save_report: bool = True,
    user_id: Optional[str] = None,
    request_source: str = "api",
) -> None:
    job_start_t = time.time()
    # Normalize for logic but keep original for display
    display_name = request.symbol
    normalized_symbol = _normalize_symbol(request.symbol)

    # ── Step 0: Initialize report in DB (short-lived session) ──
    def _init_and_configure():
        with get_db_ctx() as db:
            try:
                report_service.init_report(
                    db=db,
                    report_id=job_id,
                    symbol=normalized_symbol,
                    trade_date=request.trade_date,
                    user_id=user_id,
                )
                report_service.update_report_partial(db, job_id, status="running")
                db.commit()
            except Exception as e:
                _log(f"CRITICAL: Failed to initialize report in DB: {e}")
        return _build_runtime_config(request.config_overrides, user_id=user_id)

    config = await asyncio.to_thread(_init_and_configure)

    _set_job(job_id, status="running", started_at=_utcnow_iso(), symbol=normalized_symbol)

    _emit_job_event(
        job_id,
        "job.running",
        {
            "job_id": job_id,
            "symbol": normalized_symbol,
            "display_name": display_name,
            "trade_date": request.trade_date
        },
    )
    # Ensure request object uses the normalized symbol for internal logic
    request.symbol = normalized_symbol
    user_context_payload = _extract_request_user_context(request)
    tracker = AgentProgressTracker(request.selected_analysts, job_id)
    _emit_job_event(job_id, "agent.snapshot", tracker.snapshot())

    try:
        if request.dry_run:
            result = {
                "mode": "dry_run",
                "symbol": request.symbol,
                "trade_date": request.trade_date,
                "selected_analysts": request.selected_analysts,
                "user_context": user_context_payload,
                "llm_provider": config.get("llm_provider"),
                "data_vendors": config.get("data_vendors"),
            }
            _set_job(
                job_id,
                status="completed",
                result=result,
                decision="DRY_RUN",
                finished_at=_utcnow_iso(),
            )
            _emit_job_event(
                job_id,
                "job.completed",
                {"job_id": job_id, "decision": "DRY_RUN", "result": result},
            )
            return

        _shared_data_collector.ref(request.symbol, request.trade_date)
        graph = TradingAgentsGraph(
            selected_analysts=request.selected_analysts,
            debug=False,
            config=config,
            data_collector=_shared_data_collector,
        )
        final_state: Optional[Dict[str, Any]] = None

        # 强制单周期：多个 horizon 时只取第一个，避免 dual-horizon 双倍开销
        if not request.horizons:
            request.horizons = ["short"]
        elif len(request.horizons) > 1:
            request.horizons = [request.horizons[0]]

        # ── Dual-horizon intent-driven path ──────────────────────────────────
        if request.query:
            # 1. 组装用户意图
            intent_start_t = time.time()
            ticker = request.symbol or display_name

            # 优先使用已由 chat_completions 预解析的 intent（单次 LLM），避免二次调用
            if request.user_intent:
                user_intent = dict(request.user_intent)
                user_intent["ticker"] = ticker
                user_intent["horizons"] = request.horizons
            else:
                # 直接 POST /v1/analyze 时的兜底（无预解析 intent）
                user_intent = await asyncio.to_thread(_parse_intent, request.query, graph.quick_thinking_llm, fallback_ticker=ticker)
                if not request.horizons:
                    request.horizons = user_intent["horizons"]
                user_intent["horizons"] = request.horizons
            _log(f"[Timer] Intent Parsing took {time.time() - intent_start_t:.2f}s")

            inferred_user_context = user_intent.get("user_context") or {}
            user_context_payload = _merge_user_context_payload(
                user_context_payload,
                inferred_user_context,
            )
            user_intent["user_context"] = user_context_payload

            # Use normalized ticker from intent parser if available
            ticker = user_intent.get("ticker") or ticker

            # 2. 一次性采集数据，短线/中线共用缓存
            lookback_label = "14天关键行情" if request.horizons == ["short"] else "90天全量行情、财务、新闻、资金"
            _emit_job_event(job_id, "agent.tool_call", {
                "agent": "数据采集", "tool": "data_collector",
                "description": f"预加载 {ticker} 近{lookback_label}数据…",
            })
            _log(f"[DualHorizon] Collecting data for {ticker} {request.trade_date} (horizons={request.horizons})…")
            collect_start_t = time.time()
            await asyncio.to_thread(graph.data_collector.collect, ticker, request.trade_date, horizons=request.horizons)
            _log(f"[Timer] Data Collection step in _run_job took {time.time() - collect_start_t:.2f}s")

            _emit_job_event(job_id, "agent.tool_call", {
                "agent": "数据采集", "tool": "data_collector",
                "description": "数据采集完成，开始多维度分析",
            })

            report_keys = (
                "market_report", "sentiment_report", "news_report", "fundamentals_report",
                "macro_report", "smart_money_report", "volume_price_report",
                "investment_plan", "trader_investment_plan", "final_trade_decision",
            )

            horizon_states: Dict[str, Any] = {}

            async def _process_horizon(horizon: str):
                """Async helper to run analysis for a single horizon."""
                # 根据周期过滤 analyst，共享已采集的数据缓存
                horizon_analysts = _get_horizon_analysts(horizon, request.selected_analysts)
                horizon_graph = TradingAgentsGraph(
                    selected_analysts=horizon_analysts,
                    debug=False,
                    config=config,
                    data_collector=graph.data_collector,
                )

                horizon_label = "短线" if horizon == "short" else "中线"
                _emit_job_event(job_id, "agent.horizon_start", {
                    "horizon": horizon, "label": horizon_label,
                })
                # 每轮重置 tracker，前端进度条重新走一遍
                h_tracker = AgentProgressTracker(horizon_analysts, job_id, horizon=horizon)
                _emit_job_event(job_id, "agent.snapshot", h_tracker.snapshot())
                # 告知前端本轮参与的 analyst 即将开始
                for analyst_key in ANALYST_ORDER:
                    if analyst_key in horizon_analysts:
                        aname = ANALYST_AGENT_NAMES[analyst_key]
                        h_tracker._set_status(aname, "in_progress")
                        h_tracker._emit_writing_status(aname, ANALYST_REPORT_MAP[analyst_key])

                h_args = horizon_graph.propagator.get_graph_args()

                # Use thread_id for LangGraph checkpointer persistence
                if "config" not in h_args:
                    h_args["config"] = {}
                h_args["config"]["configurable"] = {"thread_id": f"{job_id}_{horizon}"}

                init_state = horizon_graph.propagator.create_initial_state(
                    ticker, request.trade_date,
                    user_context=user_context_payload,
                    selected_analysts=horizon_analysts,
                    request_source=request_source,
                    user_intent=user_intent, horizon=horizon,
                )
                last_report: Dict[str, str] = {}
                seen: Dict[str, bool] = {}   # 追踪哪些字段已出现过，避免重复事件
                horizon_final = None

                # DB 更新使用短生命周期 session，避免长期占用连接池
                def _horizon_partial_update(updates: dict):
                    with get_db_ctx() as _hdb:
                        report_service.update_report_partial(_hdb, job_id, **updates)

                # 通过 ContextVar 将 tracker 传入 async 节点（LangGraph 不传递 schema 外的字段）
                _tracker_token = current_tracker_var.set(h_tracker)
                try:
                    async for chunk in horizon_graph.graph.astream(init_state, **h_args):
                        horizon_final = chunk

                        # ── 并行感知的状态推进 ──────────────────
                        # 1. 每个 analyst 报告首次出现 → completed
                        for analyst_key in ANALYST_ORDER:
                            if analyst_key not in horizon_analysts:
                                continue
                            rkey = ANALYST_REPORT_MAP[analyst_key]
                            aname = ANALYST_AGENT_NAMES[analyst_key]
                            if chunk.get(rkey) and not seen.get(rkey):
                                seen[rkey] = True
                                h_tracker._set_status(aname, "completed")

                        # 2. 分析师全部完成后 → Bull/Bear/ResearchManager 开始
                        all_analysts_done = all(
                            seen.get(ANALYST_REPORT_MAP.get(a, "")) for a in h_tracker.selected_analysts
                        )
                        if all_analysts_done and not seen.get("_research_started"):
                            seen["_research_started"] = True
                            h_tracker._set_status(ANALYST_AGENT_NAMES["bull"], "in_progress")
                            h_tracker._set_status(ANALYST_AGENT_NAMES["bear"], "in_progress")
                            h_tracker._set_status(ANALYST_AGENT_NAMES["research_manager"], "in_progress")

                        # 3. research judge → 研究团队完成, Trader 开始
                        debate = chunk.get("investment_debate_state") or {}
                        if debate.get("judge_decision") and not seen.get("judge_decision"):
                            seen["judge_decision"] = True
                            for r_key in ["bull", "bear", "research_manager"]:
                                h_tracker._set_status(ANALYST_AGENT_NAMES[r_key], "completed")
                            h_tracker._set_status(ANALYST_AGENT_NAMES["trader"], "in_progress")
                            h_tracker._emit_writing_status(ANALYST_AGENT_NAMES["trader"], "trader_investment_plan")

                        # 4. trader plan → Trader completed, 风控开始
                        if chunk.get("trader_investment_plan") and not seen.get("trader_investment_plan"):
                            seen["trader_investment_plan"] = True
                            h_tracker._set_status(ANALYST_AGENT_NAMES["trader"], "completed")
                            h_tracker._set_status(ANALYST_AGENT_NAMES["aggressive"], "in_progress")

                        # 5. risk judge → 风控全部完成
                        risk = chunk.get("risk_debate_state") or {}
                        if risk.get("judge_decision") and not seen.get("risk_judge_decision"):
                            seen["risk_judge_decision"] = True
                            for r_key in ["aggressive", "neutral", "conservative", "portfolio_manager"]:
                                h_tracker._set_status(ANALYST_AGENT_NAMES[r_key], "completed")
                        # ── end 并行感知 ────────────────────────────────────────────

                        # 报告分片推送与数据库即时更新
                        db_updates = {}
                        for key in report_keys:
                            value = chunk.get(key)
                            if value and value != last_report.get(key):
                                last_report[key] = value
                                db_updates[key] = str(value)
                                h_tracker._emit_report_chunked(job_id, key, str(value))

                        if db_updates:
                            await asyncio.to_thread(_horizon_partial_update, db_updates)
                except Exception as e:
                    _log(
                        f"Error during horizon streaming ({horizon}): {e!r}\n"
                        f"{traceback.format_exc()}"
                    )
                    raise
                finally:
                    current_tracker_var.reset(_tracker_token)

                horizon_states[horizon] = horizon_final
                for agent, st in h_tracker.status.items():
                    if st not in ("completed", "skipped"):
                        h_tracker._set_status(agent, "completed")
                _emit_job_event(job_id, "agent.horizon_done", {"horizon": horizon})

            # 3. 按解析出的 horizons 并行运行 astream()，事件实时推给前端
            results = await asyncio.gather(
                *[_process_horizon(h) for h in request.horizons],
                return_exceptions=True,
            )
            horizon_errors = []
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    tb = "".join(traceback.format_exception(type(r), r, r.__traceback__))
                    _log(f"Horizon '{request.horizons[i]}' failed: {r!r}\n{tb}")
                    horizon_errors.append(f"{request.horizons[i]}: {r}")
            if horizon_errors:
                raise RuntimeError(f"Horizon analysis failed: {'; '.join(horizon_errors)}")

            short_r = graph._build_horizon_result("short", horizon_states.get("short") or {})
            medium_r = graph._build_horizon_result("medium", horizon_states.get("medium") or {})
            primary_r = short_r if horizon_states.get("short") else medium_r
            decision = graph.process_signal(primary_r.get("final_trade_decision", "")) or "UNKNOWN"
            result = {
                "symbol": ticker,
                "trade_date": request.trade_date,
                "mode": "dual_horizon",
                "user_intent": user_intent,
                "short_term": short_r,
                "medium_term": medium_r,
                "decision": decision,
                # Hoist primary horizon's report fields to top level so that
                # resolve_report_fields / create_report can find them directly.
                "final_trade_decision": primary_r.get("final_trade_decision", ""),
                "investment_plan": primary_r.get("investment_plan", ""),
                "trader_investment_plan": primary_r.get("trader_investment_plan", ""),
                "market_report": primary_r.get("market_report", ""),
                "sentiment_report": primary_r.get("sentiment_report", ""),
                "news_report": primary_r.get("news_report", ""),
                "fundamentals_report": primary_r.get("fundamentals_report", ""),
                "macro_report": primary_r.get("macro_report", ""),
                "smart_money_report": primary_r.get("smart_money_report", ""),
                "volume_price_report": primary_r.get("volume_price_report", ""),
                "analyst_traces": (
                    short_r.get("analyst_traces", []) + medium_r.get("analyst_traces", [])
                ),
            }
            # LLM 结构化提取（目标价、止损、信心、风险、关键指标）
            # 注意：必须在 _set_job(status="completed") 之前完成，否则 SSE 超时
            # 会因为看到 status="completed" 而提前关闭流，导致 job.completed 事件丢失。
            structured = None
            try:
                structured = await asyncio.to_thread(
                    report_service.extract_structured_data,
                    final_trade_decision=primary_r.get("final_trade_decision", ""),
                    fundamentals_report=primary_r.get("fundamentals_report", ""),
                    config=config,
                )
            except Exception as e:
                _log(f"Structured extraction failed (non-fatal): {e}")

            resolved = await asyncio.to_thread(
                report_service.resolve_report_fields,
                result_data=result,
                confidence_override=structured.confidence if structured else None,
                target_price_override=structured.target_price if structured else None,
                stop_loss_override=structured.stop_loss_price if structured else None,
            )
            result.update({
                "direction": resolved["direction"],
                "confidence": resolved["confidence"],
                "target_price": resolved["target_price"],
                "stop_loss_price": resolved["stop_loss_price"],
                "risk_flags": resolved.get("risk_flags", []),
            })

            # 自动保存报告到数据库
            if save_report:
                def _save_report_sync():
                    with get_db_ctx() as save_db:
                        report_service.create_report(
                            db=save_db,
                            symbol=request.symbol,
                            trade_date=request.trade_date,
                            decision=decision,
                            result_data=result,
                            user_id=user_id,
                            risk_items=([r.model_dump() for r in structured.risks] if structured else None),
                            key_metrics=([m.model_dump() for m in structured.key_metrics] if structured else None),
                            confidence_override=result["confidence"],
                            target_price_override=result["target_price"],
                            stop_loss_override=result["stop_loss_price"],
                            report_id=job_id,
                            analyst_traces=result.get("analyst_traces"),
                        )
                        save_db.commit()

                try:
                    await asyncio.to_thread(_save_report_sync)
                except Exception as e:
                    _log(f"Failed to save report: {e}")

            # 所有后处理完成后再标记 completed，防止 SSE 超时提前关闭流
            _set_job(job_id, status="completed", result=result,
                     decision=decision, finished_at=_utcnow_iso())
            _emit_job_event(job_id, "job.completed", {
                "job_id": job_id, "decision": decision,
                "direction": result["direction"],
                "result": result, "mode": "dual_horizon",
                "risk_items": [r.model_dump() for r in structured.risks] if structured else [],
                "key_metrics": [m.model_dump() for m in structured.key_metrics] if structured else [],
                "confidence": result["confidence"],
                "target_price": result["target_price"],
                "stop_loss_price": result["stop_loss_price"],
                "risk_flags": result.get("risk_flags", []),
            })
            _log(f"Job completed successfully: {job_id}")
            _log(f"[Timer] TOTAL Job execution (dual_horizon) took {time.time() - job_start_t:.2f}s")
            return
        # ── End dual-horizon path ─────────────────────────────────────────────

        if stream_events:
            init_state = graph.propagator.create_initial_state(
                request.symbol,
                request.trade_date,
                user_context=user_context_payload,
                selected_analysts=request.selected_analysts,
                request_source=request_source,
            )
            args = graph.propagator.get_graph_args()
            
            # Pass job_id as thread_id for LangGraph checkpointer persistence
            if "config" not in args:
                args["config"] = {}
            args["config"]["configurable"] = {"thread_id": job_id}

            report_keys = (
                "market_report",
                "sentiment_report",
                "news_report",
                "fundamentals_report",
                "macro_report",
                "smart_money_report",
                "volume_price_report",
                "investment_plan",
                "trader_investment_plan",
                "final_trade_decision",
            )
            last_report: Dict[str, str] = {}
            seen: Dict[str, bool] = {}

            _tracker_token = current_tracker_var.set(tracker)
            try:
                async for chunk in graph.graph.astream(init_state, **args):
                    final_state = chunk
                    # ── 并行感知的状态推进 ──────────────────
                    # 1. 每个 analyst 报告首次出现 → completed
                    for analyst_key in ANALYST_ORDER:
                        if analyst_key not in request.selected_analysts:
                            continue
                        rkey = ANALYST_REPORT_MAP[analyst_key]
                        aname = ANALYST_AGENT_NAMES[analyst_key]
                        if chunk.get(rkey) and not seen.get(rkey):
                            seen[rkey] = True
                            tracker._set_status(aname, "completed")

                    # 2. 分析师全部完成 → 研究团队开始
                    all_analysts_done = all(
                        seen.get(ANALYST_REPORT_MAP.get(a, "")) for a in tracker.selected_analysts
                    )
                    if all_analysts_done and not seen.get("_research_started"):
                        seen["_research_started"] = True
                        tracker._set_status(ANALYST_AGENT_NAMES["bull"], "in_progress")
                        tracker._set_status(ANALYST_AGENT_NAMES["bear"], "in_progress")
                        tracker._set_status(ANALYST_AGENT_NAMES["research_manager"], "in_progress")

                    debate = chunk.get("investment_debate_state") or {}
                    if debate.get("judge_decision") and not seen.get("judge_decision"):
                        seen["judge_decision"] = True
                        for r_key in ["bull", "bear", "research_manager"]:
                            tracker._set_status(ANALYST_AGENT_NAMES[r_key], "completed")
                        tracker._set_status(ANALYST_AGENT_NAMES["trader"], "in_progress")

                    if chunk.get("trader_investment_plan") and not seen.get("trader_investment_plan"):
                        seen["trader_investment_plan"] = True
                        tracker._set_status(ANALYST_AGENT_NAMES["trader"], "completed")
                        tracker._set_status(ANALYST_AGENT_NAMES["aggressive"], "in_progress")

                    risk = chunk.get("risk_debate_state") or {}
                    if risk.get("judge_decision") and not seen.get("risk_judge_decision"):
                        seen["risk_judge_decision"] = True
                        for r_key in ["aggressive", "neutral", "conservative", "portfolio_manager"]:
                            tracker._set_status(ANALYST_AGENT_NAMES[r_key], "completed")
                    # ────────────────────────────────────────────

                    # ── Partial DB Persistence & UI Streaming ──
                    db_updates = {}
                    for key in report_keys:
                        value = chunk.get(key)
                        if value and value != last_report.get(key):
                            last_report[key] = value
                            db_updates[key] = str(value)
                            # 立即推送报告分片，前端即可“即产即看”
                            tracker._emit_report_chunked(job_id, key, str(value))
                    
                    if db_updates:
                        def _partial_update(updates=db_updates):
                            with get_db_ctx() as _db:
                                report_service.update_report_partial(_db, job_id, **updates)
                        await asyncio.to_thread(_partial_update)
                    
                    # ── Message & Tool Call Handling ──
                    messages = chunk.get("messages", [])
                    if messages:
                        msg = messages[-1]
                        content = _extract_message_text(getattr(msg, "content", ""))
                        agent_name = getattr(msg, "name", None)
                        msg_type = getattr(msg, "type", "unknown")  # human/system/ai/tool

                        if content:
                            if agent_name:
                                _log(f"[Agent Message] {agent_name}: {content[:200]}...")
                            elif msg_type in ("human", "system"):
                                # Graph 入口的初始 prompt，不是 agent 产出，跳过
                                pass
                            else:
                                _log(f"[Agent Message] {msg_type}: {content[:200]}...")

                        for tool_call in getattr(msg, "tool_calls", []) or []:
                            tool_name = tool_call.get("name", "unknown") if isinstance(tool_call, dict) else getattr(tool_call, "name", "unknown")
                            tool_args = tool_call.get("args", {}) if isinstance(tool_call, dict) else getattr(tool_call, "args", {})
                            _log(f"[Tool Call] {agent_name or msg_type}: {tool_name}")

                            agent_display = agent_name
                            if not agent_display:
                                tool_to_agent = {
                                    "get_stock_data": "数据获取",
                                    "get_indicators": "技术分析师",
                                    "get_fundamentals": "基本面分析师",
                                    "get_income_statement": "基本面分析师",
                                    "get_balance_sheet": "基本面分析师",
                                    "get_cash_flow": "基本面分析师",
                                    "get_news": "新闻分析师",
                                    "get_social_sentiment": "舆情分析师",
                                }
                                agent_display = tool_to_agent.get(tool_name, "系统")

                            tool_description = _generate_tool_description(tool_name, tool_args)
                            _emit_job_event(
                                job_id,
                                "agent.tool_call",
                                {
                                    "agent": agent_display,
                                    "tool": tool_name,
                                    "description": tool_description,
                                },
                            )
                
            except Exception as e:
                _log(f"Error during default streaming: {e}")
            finally:
                current_tracker_var.reset(_tracker_token)
        else:
            final_state, _ = await asyncio.to_thread(
                graph.propagate,
                request.symbol,
                request.trade_date,
                user_context=user_context_payload,
                selected_analysts=request.selected_analysts,
                request_source=request_source,
                thread_id=job_id,
            )

        if not final_state:
            raise RuntimeError("graph returned empty final state")

        decision = graph.process_signal(final_state["final_trade_decision"]) or "UNKNOWN"
        result = _build_result_payload(final_state)
        result["decision"] = decision

        # 全量收口为 completed/skipped
        for agent, status in tracker.status.items():
            if status not in ("completed", "skipped"):
                tracker._set_status(agent, "completed")

        # LLM 结构化提取（非阻塞，失败不影响主流程）
        # 注意：_set_job(status="completed") 必须在此之后调用，否则 SSE 超时会提前关闭流
        structured = None
        try:
            structured = await asyncio.to_thread(
                report_service.extract_structured_data,
                final_trade_decision=result.get("final_trade_decision", ""),
                fundamentals_report=result.get("fundamentals_report", ""),
                config=config,
            )
        except Exception as e:
            _log(f"Structured extraction failed (non-fatal): {e}")

        # 一次性解析所有字段（方向、信心、目标价等）
        resolved = await asyncio.to_thread(
            report_service.resolve_report_fields,
            result_data=result,
            confidence_override=structured.confidence if structured else None,
            target_price_override=structured.target_price if structured else None,
            stop_loss_override=structured.stop_loss_price if structured else None,
        )

        # 注入结果字典以便通知和保存使用
        result.update({
            "direction": resolved["direction"],
            "confidence": resolved["confidence"],
            "target_price": resolved["target_price"],
            "stop_loss_price": resolved["stop_loss_price"],
            "risk_flags": resolved.get("risk_flags", []),
        })

        # 自动保存/收口报告到数据库
        if save_report:
            def _save_report_final_sync():
                with get_db_ctx() as save_db:
                    report_service.create_report(
                        db=save_db,
                        symbol=request.symbol,
                        trade_date=request.trade_date,
                        decision=decision,
                        result_data=result,
                        user_id=user_id,
                        risk_items=([r.model_dump() for r in structured.risks] if structured else None),
                        key_metrics=([m.model_dump() for m in structured.key_metrics] if structured else None),
                        confidence_override=result["confidence"],
                        target_price_override=result["target_price"],
                        stop_loss_override=result["stop_loss_price"],
                        report_id=job_id,
                        analyst_traces=result.get("analyst_traces"),
                    )
                    save_db.commit()

            try:
                await asyncio.to_thread(_save_report_final_sync)
            except Exception as e:
                _log(f"Failed to finalize report: {e}")
        # 所有后处理完成后再标记 completed，防止 SSE 超时提前关闭流
        _set_job(
            job_id,
            status="completed",
            result=result,
            decision=decision,
            finished_at=_utcnow_iso(),
        )
        _emit_job_event(
            job_id,
            "job.completed",
            {
                "job_id": job_id,
                "decision": decision,
                "direction": result["direction"],
                "result": result,
                "risk_items": [r.model_dump() for r in structured.risks] if structured else [],
                "key_metrics": [m.model_dump() for m in structured.key_metrics] if structured else [],
                "confidence": result["confidence"],
                "target_price": result["target_price"],
                "stop_loss_price": result["stop_loss_price"],
                "risk_flags": result.get("risk_flags", []),
            },
        )
        _log(f"Job completed successfully: {job_id}")
        _log(f"[Timer] TOTAL Job execution (single_horizon) took {time.time() - job_start_t:.2f}s")
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        _set_job(
            job_id,
            status="failed",
            error=err_msg,
            traceback=traceback.format_exc(),
            finished_at=_utcnow_iso(),
        )
        
        # ── Persistent failure recording (short-lived session) ──
        try:
            def _record_failure():
                with get_db_ctx() as err_db:
                    report_service.mark_report_failed(err_db, job_id, f"{err_msg}\n\n{traceback.format_exc()}")
            await asyncio.to_thread(_record_failure)
        except Exception as db_exc:
            _log(f"Failed to record failure in DB: {db_exc}")

        _emit_job_event(
            job_id,
            "job.failed",
            {"job_id": job_id, "error": err_msg},
        )
    finally:
        _shared_data_collector.evict(request.symbol, request.trade_date)


def _regex_extract_ticker(text: str) -> Optional[str]:
    """Extract an obvious stock/ETF ticker from natural language via regex.

    Catches patterns the LLM might miss (e.g. obscure ETFs like SPCX, SOXL).
    Returns a normalized symbol or None.

    Strategy: regex candidate → stopwords filter → stock_universe validation.
    Universe match = guaranteed valid.  Non-match = still returned (may be
    obscure ticker not in our 35k universe).
    """
    # Lazy import to avoid circular deps and keep cold-start fast
    try:
        from tradingagents.dataflows.stock_resolver import is_known_ticker
    except ImportError:
        is_known_ticker = None  # type: ignore[assignment]

    _STOPWORDS = {
        'THE', 'AND', 'FOR', 'BUT', 'NOT', 'YOU', 'ALL', 'CAN', 'HER',
        'WAS', 'ONE', 'OUR', 'OUT', 'HAS', 'HIS', 'HOW', 'MAN', 'NEW',
        'NOW', 'OLD', 'SEE', 'WAY', 'WHO', 'BOY', 'DID', 'GET', 'HIM',
        'LET', 'SAY', 'SHE', 'TOO', 'USE', 'DAD', 'MOM', 'MAY', 'TRY',
        'ASK', 'MEN', 'RAN', 'SIX', 'TEN', 'TOP', 'YES', 'YET', 'ACE',
        'ADD', 'AGE', 'AGO', 'AID', 'AIM', 'AIR', 'ART', 'BAD', 'BAG',
        'BAR', 'BED', 'BIG', 'BIT', 'BOW', 'BOX', 'BUS', 'BUY', 'CAP',
        'CAR', 'COP', 'CUP', 'CUT', 'DIE', 'DOG', 'DRY', 'EAR', 'EAT',
        'END', 'EYE', 'FAN', 'FAR', 'FAT', 'FEW', 'FIT', 'FLY', 'FOG',
        'FUN', 'FUR', 'GAS', 'GOD', 'GOT', 'GUN', 'GUT', 'GUY', 'HAD',
        'HAT', 'HIT', 'HOT', 'HUGE', 'HUT', 'ICE', 'ILL', 'ITS', 'JET',
        'JOB', 'JOY', 'KEY', 'KID', 'LAW', 'LAY', 'LED', 'LIE', 'LIP',
        'LOG', 'LOT', 'LOW', 'MAP', 'MET', 'MIX', 'MOB', 'MUD', 'NET',
        'ODD', 'OIL', 'OWN', 'PAD', 'PAN', 'PAY', 'PEN', 'PET', 'PIE',
        'PIN', 'PIT', 'POT', 'PUT', 'RAN', 'RAW', 'RED', 'RIB', 'RID',
        'ROW', 'RUB', 'RUG', 'SAD', 'SAT', 'SET', 'SIT', 'SKI', 'SKY',
        'SON', 'SPA', 'SUM', 'SUN', 'TAB', 'TAN', 'TAP', 'TAX',
        'TIE', 'TIN', 'TIP', 'TOE', 'TON', 'TOY', 'TUB', 'TWO', 'VAN',
        'WAR', 'WET', 'WHY', 'WIN', 'WON', 'ZOO', 'TALK', 'LOOK',
        'GOOD', 'BEST', 'THAT', 'THIS', 'WITH', 'FROM', 'HAVE', 'WILL',
        'BEEN', 'CALL', 'COME', 'EACH', 'FIND', 'GIVE', 'GOES', 'HELD',
        'HELP', 'HERE', 'HIGH', 'HOME', 'HOPE', 'IDEA', 'INTO', 'JUST',
        'KEEP', 'LAST', 'LATE', 'LIKE', 'LINE', 'LONG', 'MADE', 'MAKE',
        'MORE', 'MOST', 'MUCH', 'MUST', 'NEED', 'NEXT', 'ONLY', 'OVER',
        'PART', 'READ', 'REAL', 'SAID', 'SAME', 'SEEM', 'SHOW', 'SIDE',
        'SOME', 'SURE', 'TAKE', 'TELL', 'THAN', 'THEM', 'THEN', 'THEY',
        'TIME', 'TURN', 'USED', 'VERY', 'WANT', 'WELL', 'WENT', 'WERE',
        'WHAT', 'WHEN', 'WHOM', 'WORK', 'YEAR', 'YOUR', 'VIEW', 'OPEN',
        'FEAR', 'GOLD', 'LOVE', 'LIFE', 'CARE', 'COST', 'DEAL', 'EARN',
        'FAIL', 'FALL', 'FEAR', 'FILE', 'FIRM', 'FUND', 'GAIN', 'HOLD',
        'MOVE', 'NOTE', 'OFFER', 'PLAN', 'RATE', 'RISK', 'ROLE', 'RULE',
        'SELL', 'SIGN', 'SIZE', 'STEP', 'STOP', 'TEST', 'TURN', 'TYPE',
        'UNIT', 'VEST', 'VOTE', 'WAGE', 'ZERO',
        'ABOUT', 'AFTER', 'AGAIN', 'ALONG', 'ALSO', 'BACK', 'BEEN',
        'BEING', 'BELOW', 'BETWEEN', 'BOTH', 'CAME', 'CASE', 'COME',
        'COULD', 'DOES', 'DONE', 'DOWN', 'EVEN', 'EVER', 'FAR', 'FEEL',
        'FEW', 'FIVE', 'FOUR', 'FREE', 'GAVE', 'GIVE', 'GOES', 'GONE',
        'GREAT', 'GREW', 'GROW', 'HALF', 'HARD', 'HAVE', 'HEAD', 'HEAR',
        'HELD', 'HOURS', 'HUNDRED', 'INTO', 'JUST', 'KNOW', 'LAND',
        'LARGE', 'LATER', 'LEAST', 'LEAVE', 'LEFT', 'LESS', 'LEVEL',
        'LIGHT', 'LIVES', 'LOOK', 'MADE', 'MAIN', 'MAKE', 'MANY',
        'MEAN', 'MIGHT', 'MONEY', 'MONTHS', 'MORNING', 'MOTHER',
        'NAMES', 'NEVER', 'NIGHT', 'NORTH', 'OFTEN', 'ORDER', 'OTHER',
        'PIECE', 'PLACE', 'POINT', 'POWER', 'PRESS', 'QUICK', 'QUITE',
        'REACH', 'RIGHT', 'ROUND', 'SENSE', 'SEVERAL', 'SHORT',
        'SHOWN', 'SINCE', 'SMALL', 'SOUND', 'SOUTH', 'STAND', 'START',
        'STATE', 'STILL', 'STOOD', 'STREET', 'STRONG', 'SYSTEM',
        'TABLE', 'THINK', 'THIRD', 'THOUGH', 'THREE', 'TIMES', 'TOTAL',
        'TOWER', 'TRACK', 'TRADE', 'TREAT', 'TRIED', 'TRULY', 'UNDER',
        'UNTIL', 'UPON', 'USING', 'USUAL', 'VALUE', 'WATER', 'WEIGHT',
        'WHOLE', 'WHOSE', 'WOMEN', 'WORLD', 'WOULD', 'WRITE', 'YOUNG',
        'USD', 'HKD', 'CNY', 'RMB', 'ETF', 'IPO', 'CEO', 'CFO', 'CTO',
        'API', 'LLM', 'GPT', 'FAQ', 'PDF', 'URL', 'XML', 'CSS', 'HTML',
        'HTTP', 'HTTPS', 'SSH', 'FTP', 'SMTP', 'JSON', 'YAML',
    }
    # Prefer longer tickers first (5>4>3>2) to reduce false positives like "AI" vs "OPEN"
    for length in (5, 4, 3, 2):
        for m in re.finditer(
            r'(?<![A-Za-z])([A-Z]{' + str(length) + r'})(?:\.(NYSE|NASDAQ|AMEX))?(?![A-Za-z])',
            text.upper(),
        ):
            ticker = m.group(1)
            if ticker in _STOPWORDS:
                continue
            canonical = _normalize_symbol(ticker)
            # Universe validation: confirmed ticker → return immediately
            if is_known_ticker and is_known_ticker(canonical):
                _log(f"[RegexExtract] Universe-confirmed: '{ticker}' → {canonical}")
                return canonical
            # Not in universe but passed stopwords → still return (may be obscure)
            _log(f"[RegexExtract] Regex-only match (not in universe): '{ticker}' → {canonical}")
            return canonical
    return None


def _normalize_symbol(raw: str) -> str:
    """Normalize stock symbol to canonical format: US=AAPL, HK=00020.HK, ETF=QQQ."""
    s = raw.strip().upper()
    # Priority 1: stock_resolver by code (covers US/HK/ETF)
    from tradingagents.dataflows.stock_resolver import resolve_ticker as _resolve
    resolved = _resolve(s)
    if resolved:
        return resolved["code"]
    # Priority 2: Chinese name lookup (e.g. 商汤 → 00020.HK)
    from tradingagents.dataflows.stock_resolver import search_by_name as _search_name
    resolved = _search_name(raw.strip())
    if resolved:
        return resolved["code"]
    # Priority 3: US ticker (1-6 letters, optional .XX suffix like BRK.B)
    m = re.search(r"(\^?[A-Z]{1,6}(?:\.[A-Z]{1,3})?)\b", s)
    if m:
        return m.group(1)
    return s


def _extract_chat_text(messages: List[ChatMessage]) -> str:
    if not messages:
        return ""
    last = messages[-1]
    return _extract_message_text(last.content)


def _extract_symbol_and_date(text: str) -> tuple[Optional[str], Optional[str]]:
    """Extract ticker and date from natural language. US/HK/ETF only."""
    # Date extraction
    date_match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    date = date_match.group(0) if date_match else None

    # Priority 1: HK stock code (e.g. 00020.HK, HK.00020, 港股00700)
    hk_match = re.search(r"(\d{1,5})\.(HK)\b", text, re.IGNORECASE)
    if hk_match:
        code = hk_match.group(1).zfill(5)
        return f"{code}.HK", date
    hk_match2 = re.search(r"(?:HK|港股)\.?(\d{1,5})", text, re.IGNORECASE)
    if hk_match2:
        code = hk_match2.group(1).zfill(5)
        return f"{code}.HK", date

    # Priority 2: Chinese stock name embedded in text (e.g. "分析商汤", "帮我看看百度")
    try:
        from tradingagents.dataflows.stock_resolver import search_by_name
        # Extract continuous Chinese segments, then try all 2-4 char substrings
        cn_segments = re.findall(r"[\u4e00-\u9fff]+", text)
        best_match = None
        best_len = 0
        for seg in cn_segments:
            for n in range(min(4, len(seg)), 1, -1):  # try 4, 3, 2 char substrings
                for i in range(len(seg) - n + 1):
                    part = seg[i : i + n]
                    resolved = search_by_name(part)
                    if resolved and n > best_len:
                        best_match = resolved
                        best_len = n
        if best_match:
            return best_match["code"], date
    except Exception:
        pass

    # Priority 3: US ticker or ETF (1-6 letters, works with Chinese adjacency)
    us_match = re.search(r"(?:^|[^A-Z])([A-Z]{1,6}(?:\.[A-Z]{1,3})?)(?:[^A-Z]|$)", text.upper())
    if us_match:
        ticker = us_match.group(1)
        # Validate it's not a common word
        _STOPWORDS = {'THE', 'AND', 'FOR', 'BUT', 'NOT', 'YOU', 'ALL', 'CAN', 'HER', 'WAS', 'ONE', 'OUR', 'OUT', 'HAS', 'HIS', 'HOW', 'MAN', 'NEW', 'NOW', 'OLD', 'SEE', 'WAY', 'WHO', 'BOY', 'DID', 'GET', 'HIM'}
        if ticker not in _STOPWORDS:
            return ticker, date

    # Priority 4: stock_resolver on full text
    try:
        from tradingagents.dataflows.stock_resolver import resolve_ticker as _rt
        resolved = _rt(text.strip())
        if resolved:
            return resolved["code"], date
    except Exception:
        pass

    return None, date


def _check_disambiguation(text: str, resolved_symbol: str) -> List[Dict[str, str]]:
    """Check if the resolved symbol has multiple candidates requiring disambiguation.

    Returns a list of candidate dicts [{code, name, market}, ...] if ambiguous,
    or empty list if the match is unambiguous.
    Only triggers when the query contains Chinese stock names (not codes).
    """
    # Skip if the query already contains a stock code (e.g. "港股00700", "00020.HK")
    if re.search(r"\d{4,6}(?:\.(?:HK|SH|SZ))?", text):
        return []
    # Skip if query is purely English (ticker-based)
    if not re.search(r"[\u4e00-\u9fff]{2,}", text):
        return []

    # Generic market terms that should not trigger disambiguation
    _MARKET_TERMS = {"港股", "美股", "大盘", "指数", "市场", "行情", "走势", "分析", "看看"}

    try:
        from tradingagents.dataflows.stock_resolver import search_by_name_multi
        cn_segments = re.findall(r"[\u4e00-\u9fff]+", text)
        for seg in cn_segments:
            if seg in _MARKET_TERMS:
                continue
            for n in range(min(4, len(seg)), 1, -1):
                for i in range(len(seg) - n + 1):
                    part = seg[i : i + n]
                    if part in _MARKET_TERMS:
                        continue
                    results = search_by_name_multi(part)
                    unique_codes = {r["code"] for r in results}
                    if len(unique_codes) > 1:
                        seen = set()
                        candidates = []
                        for r in results:
                            if r["code"] not in seen:
                                seen.add(r["code"])
                                candidates.append({
                                    "code": r["code"],
                                    "name": r["name"],
                                    "market": r["market"],
                                })
                        return candidates[:5]
    except Exception:
        pass
    return []


def _sse_pack(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _parse_stock_csv(raw: str) -> List[Dict[str, Any]]:
    if not raw:
        return []
    lines = [ln for ln in raw.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        return []

    try:
        df = pd.read_csv(StringIO("\n".join(lines)))
    except Exception:
        return []

    if "Date" not in df.columns:
        return []

    rename_map = {k: k.strip() for k in df.columns}
    df = df.rename(columns=rename_map)
    required = ["Date", "Open", "High", "Low", "Close"]
    for col in required:
        if col not in df.columns:
            return []

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date", "Open", "High", "Low", "Close"]).sort_values("Date")
    if df.empty:
        return []

    candles: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        candles.append(
            {
                "date": row["Date"].strftime("%Y-%m-%d"),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]) if "Volume" in df.columns and pd.notna(row.get("Volume")) else None,
            }
        )
    return candles





def _normalize_kline_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    col_map = {
        "日期": "Date",
        "date": "Date",
        "Date": "Date",
        "开盘": "Open",
        "open": "Open",
        "Open": "Open",
        "最高": "High",
        "high": "High",
        "High": "High",
        "最低": "Low",
        "low": "Low",
        "Low": "Low",
        "收盘": "Close",
        "close": "Close",
        "Close": "Close",
        "成交量": "Volume",
        "volume": "Volume",
        "Volume": "Volume",
        "成交额": "Amount",
        "amount": "Amount",
        "Amount": "Amount",
        "涨跌幅": "ChangePercent",
        "涨跌额": "Change",
        "换手率": "TurnoverRate",
    }
    out = df.rename(columns=col_map).copy()
    required = ["Date", "Open", "High", "Low", "Close"]
    if any(col not in out.columns for col in required):
        return pd.DataFrame()

    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
    out = out.dropna(subset=["Date"]).sort_values("Date")
    for col in ["Open", "High", "Low", "Close", "Volume", "Amount", "ChangePercent", "Change", "TurnoverRate"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    return out.reset_index(drop=True)



    yyyymmdd_start = start_date.replace("-", "")
    yyyymmdd_end = end_date.replace("-", "")
    last_exc: Exception | None = None

    for fetcher in (
        lambda: ak.stock_zh_index_daily_em(
            symbol=vendor_symbol,
            start_date=yyyymmdd_start,
            end_date=yyyymmdd_end,
        ),
        lambda: ak.stock_zh_index_daily(symbol=vendor_symbol),
        lambda: ak.index_zh_a_hist(
            symbol=symbol_key.split(".")[0],
            period="daily",
            start_date=yyyymmdd_start,
            end_date=yyyymmdd_end,
        ),
    ):
        try:
            raw_df = fetcher()
            df = _normalize_kline_df(raw_df)
            if df.empty:
                continue
            df = df[(df["Date"] >= pd.to_datetime(start_date)) & (df["Date"] <= pd.to_datetime(end_date))]
            if df.empty:
                continue
            candles: List[Dict[str, Any]] = []
            prev_close: float | None = None
            for _, row in df.iterrows():
                close = float(row["Close"])
                change = float(row["Change"]) if "Change" in df.columns and pd.notna(row.get("Change")) else (close - prev_close if prev_close is not None else None)
                change_pct = (
                    float(row["ChangePercent"])
                    if "ChangePercent" in df.columns and pd.notna(row.get("ChangePercent"))
                    else ((change / prev_close) * 100 if prev_close not in (None, 0) and change is not None else None)
                )
                candles.append(
                    {
                        "date": row["Date"].strftime("%Y-%m-%d"),
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": close,
                        "volume": float(row["Volume"]) if "Volume" in df.columns and pd.notna(row.get("Volume")) else None,
                        "amount": float(row["Amount"]) if "Amount" in df.columns and pd.notna(row.get("Amount")) else None,
                        "change": change,
                        "change_percent": change_pct,
                        "turnover_rate": float(row["TurnoverRate"]) if "TurnoverRate" in df.columns and pd.notna(row.get("TurnoverRate")) else None,
                    }
                )
                prev_close = close
            return candles
        except Exception as exc:
            last_exc = exc
            continue

    if last_exc:
        _log(f"[kline] index fetch failed for {symbol}: {type(last_exc).__name__}: {last_exc}")
    return []


async def _stream_job_events(job_id: str):
    store = get_job_store()
    yield _sse_pack("job.ready", {"job_id": job_id})
    async for event in store.subscribe(job_id):
        evt_name = event["event"]
        yield _sse_pack(evt_name, event["data"])
        if evt_name in ("job.completed", "job.failed"):
            yield "event: done\ndata: [DONE]\n\n"
            return


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


# Simple in-memory rate limiter for version stats: {ip: last_timestamp}
_vs_rate_limit: Dict[str, float] = {}
_VS_RATE_INTERVAL = 3600  # at most once per hour per IP


@app.post("/api/version-stats")
def version_stats(payload: Dict[str, Any] = Body(...), request: Request = None, db: Session = Depends(get_db)):
    """Collect anonymous version statistics from deployed instances."""
    remote_ip = _get_real_ip(request)

    # Rate limit by IP
    now = time.time()
    if remote_ip:
        last = _vs_rate_limit.get(remote_ip, 0)
        if now - last < _VS_RATE_INTERVAL:
            return {"status": "ok"}
        _vs_rate_limit[remote_ip] = now

    record = VersionStatsDB(
        version=str(payload.get("v", ""))[:50],
        nonce=str(payload.get("nonce", ""))[:64],
        remote_ip=remote_ip,
    )
    db.add(record)
    db.commit()
    return {"status": "ok"}


_RESOLVABLE_SYMBOL_RE = re.compile(
    r"^("
    r"\d{6}\.(SH|SZ|BJ)"          # A 股 / 北交所
    r"|\d{4,5}\.HK"                # 港股
    r"|[A-Z\^][A-Z0-9.\^\-]{0,10}" # 美股 / 通用 ticker / yfinance 指数
    r")$"
)


@app.get("/v1/market/kline", response_model=KlineResponse)
def get_kline(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> KlineResponse:
    end = end_date or market_today_str("HK")
    if start_date:
        start = start_date
    else:
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=120)).strftime("%Y-%m-%d")

    # Normalize symbol
    original = symbol
    symbol = _normalize_symbol(symbol)
    # HK index symbols → yfinance format (^HSI, ^HSCE)
    _HK_INDEX_MAP = {"800000": "^HSI", "800100": "^HSCE", "^HSI": "^HSI", "^HSCE": "^HSCE"}
    yf_symbol = _HK_INDEX_MAP.get(symbol.upper())
    if yf_symbol:
        symbol = yf_symbol
    # US index symbols → yfinance format (^DJI, ^IXIC, ^GSPC)
    _US_INDEX_MAP = {".DJI": "^DJI", ".IXIC": "^IXIC", ".SPX": "^GSPC", "^DJI": "^DJI", "^IXIC": "^IXIC", "^GSPC": "^GSPC"}
    yf_symbol = _US_INDEX_MAP.get(symbol.upper())
    if yf_symbol:
        symbol = yf_symbol
    if not _RESOLVABLE_SYMBOL_RE.match(symbol):
        raise HTTPException(
            status_code=400,
            detail=(
                f"unrecognized symbol {original!r} (normalized to {symbol!r}); "
                f"expected formats: 'AAPL' / '00700.HK'"
            ),
        )
    config = _build_runtime_config({})
    set_config(config)
    # Index symbols (^HSI, ^DJI etc.) → use yfinance directly, skip Futu
    if symbol.startswith("^"):
        from tradingagents.dataflows.providers.yfinance_provider import YFinanceProvider
        raw = YFinanceProvider().get_stock_data(symbol, start, end)
    else:
        raw = route_to_vendor("get_stock_data", symbol, start, end)
    candles = _parse_stock_csv(raw)
    if not candles:
        raise HTTPException(status_code=404, detail="no kline data")
    return KlineResponse(
        symbol=symbol,
        start_date=start,
        end_date=end,
        candles=candles,
    )




@app.get("/v1/market/hot-stocks")
def get_hot_stocks(source: str = "em", limit: int = 30) -> Dict:
    """Return hot stocks from available sources.

    Note: A-share hot stock sources (EastMoney/Xueqiu/THS) removed.
    This endpoint now returns an empty list. Use stock-search instead.
    """
    return {"stocks": [], "total": 0, "source": "none", "fallback": False, "message": "Hot stocks endpoint deprecated. Use /v1/market/stock-search instead."}


@app.post("/v1/analyze", response_model=AnalyzeResponse)
async def analyze(
    request: AnalyzeRequest,
    current_user: UserDB = Depends(_require_api_user),
) -> AnalyzeResponse:
    explicit_context = _extract_request_user_context(request)

    def _load_user_context() -> Dict[str, Any]:
        with get_db_ctx() as db:
            return _compose_analysis_user_context(
                db,
                current_user.id,
                request.symbol,
                explicit_context=explicit_context,
            )

    # Don't block the event loop on a sync SQLite read while the scheduler
    # process may be holding write locks.
    merged_user_context = await asyncio.to_thread(_load_user_context)
    _apply_user_context_to_request(request, merged_user_context)

    job_id = uuid4().hex
    now = _utcnow_iso()
    _set_job(
        job_id,
        job_id=job_id,
        user_id=current_user.id,
        status="pending",
        created_at=now,
        started_at=None,
        finished_at=None,
        symbol=request.symbol,
        trade_date=request.trade_date,
        error=None,
        result=None,
        decision=None,
    )
    _emit_job_event(
        job_id,
        "job.created",
        {"job_id": job_id, "symbol": request.symbol, "trade_date": request.trade_date},
    )
    if request.dry_run:
        await _run_job(job_id, request, True, True, current_user.id, "api")
        final_status = _get_job(job_id).get("status", "completed")
        return AnalyzeResponse(job_id=job_id, status=final_status, created_at=now)
    _create_tracked_task(_run_job(job_id, request, True, True, current_user.id, "api"))
    return AnalyzeResponse(job_id=job_id, status="pending", created_at=now)


def _require_job_owner(job_id: str, current_user: UserDB) -> Dict[str, Any]:
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    owner_id = job.get("user_id")
    if owner_id and owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/v1/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str, current_user: UserDB = Depends(_require_api_user)) -> JobStatusResponse:
    job = _require_job_owner(job_id, current_user)
    return JobStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        created_at=job["created_at"],
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
        symbol=job["symbol"],
        trade_date=job["trade_date"],
        error=job.get("error"),
        waiting_ahead_count=job.get("waiting_ahead_count"),
        scheduled_running_count=job.get("scheduled_running_count"),
        scheduled_concurrency_limit=job.get("scheduled_concurrency_limit"),
    )


@app.get("/v1/jobs/{job_id}/result")
def get_job_result(job_id: str, current_user: UserDB = Depends(_require_api_user)) -> Dict[str, Any]:
    job = _require_job_owner(job_id, current_user)
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail=f"job status is {job['status']}")
    return {
        "job_id": job_id,
        "status": job["status"],
        "decision": job.get("decision"),
        "result": job.get("result"),
        "finished_at": job.get("finished_at"),
    }


@app.get("/v1/jobs/{job_id}/events")
def stream_job_events(job_id: str, current_user: UserDB = Depends(_require_api_user)):
    _require_job_owner(job_id, current_user)
    return StreamingResponse(
        _stream_job_events(job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


async def _ai_extract_symbol_and_date_streaming(
    text: str, config: Dict[str, Any], job_id: str
) -> tuple[Optional[str], Optional[str], List[str], List[str], List[str], Dict[str, Any]]:
    """
    Async streaming version of _ai_extract_symbol_and_date.
    Emits agent.token events so the frontend can show streaming output during extraction.
    """
    from tradingagents.llm_clients.factory import create_llm_client
    import json as _json

    today = datetime.now().strftime("%Y-%m-%d")
    llm_name: Optional[str] = None
    llm_date: Optional[str] = None
    llm_horizons: List[str] = ["short"]
    llm_focus_areas: List[str] = []
    llm_specific_questions: List[str] = []
    llm_user_context: Dict[str, Any] = {}

    try:
        client = create_llm_client(
            provider=config.get("llm_provider", "openai"),
            model=config.get("quick_think_llm"),
            base_url=config.get("backend_url"),
            api_key=config.get("api_key"),
        )
        prompt = f"""你是金融数据助手。从用户消息中提取以下字段并以 JSON 输出。

字段说明：
- stock_name：用户提到的公司名称或股票代码原文（如"华盛天成"、"贵州茅台"、"600519"、"AAPL"）；美股直接填 ticker。
- date：YYYY-MM-DD 格式。今天是 {today}，如未提及则填今天。
- horizons：分析周期，只能选一个：
  * 用户明确提到"中线/中期/几个月/季度/长期/趋势投资"→ ["medium"]
  * 其他所有情况（含未提及）→ ["short"]
- focus_areas：用户关注的分析维度关键词列表，如 ["技术面", "资金面", "业绩"]，未提及则 []。
- specific_questions：用户提出的具体问题列表，如 ["近期有无催化剂？", "主力是否出货？"]，未提及则 []。
- user_context：从自然语言中提取的账户与约束对象。若未提及返回 {{}}。可包含：
  * objective：建仓 / 加仓 / 减仓 / 止损 / 观察 / 持有处理
  * risk_profile：保守 / 平衡 / 激进
  * investment_horizon：短线 / 波段 / 中线 / 长期
  * cash_available / current_position / current_position_pct / average_cost / max_loss_pct：数字
  * constraints：字符串数组
  * user_notes：仅保留重要但未能结构化归类的信息

仅输出 JSON，不要任何其他文字：
{{"stock_name": "...", "date": "YYYY-MM-DD", "horizons": ["short"], "focus_areas": [], "specific_questions": [], "user_context": {{}}}}

如果无法识别股票标的：{{"stock_name": null, "date": null, "horizons": ["short"], "focus_areas": [], "specific_questions": [], "user_context": {{}}}}

用户消息："{text}"
"""
        llm = client.get_llm()
        _log(f"[LLM Debug] Streaming StockExtract with model: {getattr(llm, 'model_name', 'unknown')}")

        full_content = ""
        async for chunk in llm.astream(prompt):
            token = chunk.content if hasattr(chunk, "content") else str(chunk)
            full_content += token
            if token:
                _emit_job_event(job_id, "agent.token", {
                    "agent": "意图解析",
                    "report": "stock_extract",
                    "token": token,
                })

        _log(f"[LLM Debug] StockExtract response: {full_content[:200]}")
        m = re.search(r"\{.*\}", full_content, re.DOTALL)
        if m:
            data = _json.loads(m.group(0))
            llm_name = (data.get("stock_name") or "").strip() or None
            llm_date = data.get("date") or today
            llm_horizons = data.get("horizons") or ["short"]
            llm_focus_areas = data.get("focus_areas") or []
            llm_specific_questions = data.get("specific_questions") or []
            llm_user_context = normalize_user_context(data.get("user_context") or {})
    except Exception as e:
        _log(f"[StockExtract streaming] LLM failed: {e}")

    # ── Step 1b: If LLM returned no stock_name, try regex fallback ─────────────
    if not llm_name:
        regex_symbol = _regex_extract_ticker(text)
        if regex_symbol:
            _log(f"[StockExtract streaming] LLM returned null, regex fallback matched: '{regex_symbol}'")
            return regex_symbol, llm_date or today, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context
        return None, None, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    _log(f"[StockExtract] extracted name='{llm_name}', date={llm_date}, horizons={llm_horizons}")
    if re.match(r"^\d{6}$", llm_name) or re.match(r"^[A-Za-z]{1,6}(\.[A-Za-z]+)?$", llm_name):
        symbol = _normalize_symbol(llm_name)
        return symbol or None, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    local_code = await asyncio.to_thread(_search_stock_by_name, llm_name)
    if local_code:
        return local_code, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    fallback = _normalize_symbol(llm_name)
    if fallback:
        return fallback, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    # ── Step 5: Final regex fallback on original text ─────────────────────────
    regex_symbol = _regex_extract_ticker(text)
    if regex_symbol:
        _log(f"[StockExtract streaming] Final regex fallback matched: '{regex_symbol}'")
        return regex_symbol, llm_date or today, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    return None, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context


def _ai_extract_symbol_and_date(
    text: str, config: Dict[str, Any]
) -> tuple[Optional[str], Optional[str], List[str], List[str], List[str], Dict[str, Any]]:
    """
    Single-LLM extraction: stock name, date, horizons, focus_areas, specific_questions.
    Then resolves the stock name to an authoritative code via akshare.
    Returns (symbol, date, horizons, focus_areas, specific_questions, inferred_user_context).
    """
    from tradingagents.llm_clients.factory import create_llm_client
    import json as _json

    today = datetime.now().strftime("%Y-%m-%d")

    llm_name: Optional[str] = None
    llm_date: Optional[str] = None
    llm_horizons: List[str] = ["short"]
    llm_focus_areas: List[str] = []
    llm_specific_questions: List[str] = []
    llm_user_context: Dict[str, Any] = {}
    try:
        client = create_llm_client(
            provider=config.get("llm_provider", "openai"),
            model=config.get("quick_think_llm"),
            base_url=config.get("backend_url"),
            api_key=config.get("api_key"),
        )
        prompt = f"""你是金融数据助手。从用户消息中提取以下字段并以 JSON 输出。

字段说明：
- stock_name：用户提到的公司名称或股票代码原文（如"华盛天成"、"贵州茅台"、"600519"、"AAPL"）；美股直接填 ticker。
- date：YYYY-MM-DD 格式。今天是 {today}，如未提及则填今天。
- horizons：分析周期，只能选一个：
  * 用户明确提到"中线/中期/几个月/季度/长期/趋势投资"→ ["medium"]
  * 其他所有情况（含未提及）→ ["short"]
- focus_areas：用户关注的分析维度关键词列表，如 ["技术面", "资金面", "业绩"]，未提及则 []。
- specific_questions：用户提出的具体问题列表，如 ["近期有无催化剂？", "主力是否出货？"]，未提及则 []。
- user_context：从自然语言中提取的账户与约束对象。若未提及返回 {{}}。可包含：
  * objective：建仓 / 加仓 / 减仓 / 止损 / 观察 / 持有处理
  * risk_profile：保守 / 平衡 / 激进
  * investment_horizon：短线 / 波段 / 中线 / 长期
  * cash_available / current_position / current_position_pct / average_cost / max_loss_pct：数字
  * constraints：字符串数组
  * user_notes：仅保留重要但未能结构化归类的信息

仅输出 JSON，不要任何其他文字：
{{"stock_name": "...", "date": "YYYY-MM-DD", "horizons": ["short"], "focus_areas": [], "specific_questions": [], "user_context": {{}}}}

如果无法识别股票标的：{{"stock_name": null, "date": null, "horizons": ["short"], "focus_areas": [], "specific_questions": [], "user_context": {{}}}}

用户消息："{text}"
"""
        llm = client.get_llm()
        
        # 调试日志：打印请求参数
        target_url = getattr(llm, 'openai_api_base', 'default')
        _log(f"[LLM Debug] Requesting StockExtract with model: {getattr(llm, 'model_name', 'unknown')} at {target_url}")
        _log(f"[LLM Debug] Prompt: {prompt[:500]}...")

        response = llm.invoke(prompt)
        raw = response if isinstance(response, str) else getattr(response, "content", str(response))
        
        # 调试日志：打印原始响应
        _log(f"[LLM Debug] Raw Response: {raw}")

        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            data = _json.loads(m.group(0))
            llm_name = (data.get("stock_name") or "").strip() or None
            llm_date = data.get("date") or today
            llm_horizons = data.get("horizons") or ["short"]
            llm_focus_areas = data.get("focus_areas") or []
            llm_specific_questions = data.get("specific_questions") or []
            llm_user_context = normalize_user_context(data.get("user_context") or {})
    except Exception as e:
        _log(f"[StockExtract] LLM failed: {e}")

    # ── Step 1b: If LLM returned no stock_name, try regex fallback ─────────────
    if not llm_name:
        regex_symbol = _regex_extract_ticker(text)
        if regex_symbol:
            _log(f"[StockExtract] LLM returned null, regex fallback matched: '{regex_symbol}'")
            return regex_symbol, llm_date or today, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context
        _log(f"[StockExtract] LLM returned no stock name for: '{text[:40]}'")
        return None, None, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    _log(f"[StockExtract] LLM extracted name='{llm_name}', date={llm_date}, horizons={llm_horizons}")

    # ── Step 2: If looks like a direct code (digits / letters), normalize it ──
    if re.match(r"^\d{6}$", llm_name) or re.match(r"^[A-Za-z]{1,6}(\.[A-Za-z]+)?$", llm_name):
        symbol = _normalize_symbol(llm_name)
        _log(f"[StockExtract] Direct code: {symbol}")
        return symbol or None, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    # ── Step 3: Search stock resolver (US/ETF/HK) ─────────────────────────────
    local_code = _search_stock_by_name(llm_name)
    if local_code:
        _log(f"[StockExtract] resolver match: '{llm_name}' → {local_code}")
        return local_code, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    # ── Step 4: Last resort — treat LLM name as a raw code ────────────────────
    fallback = _normalize_symbol(llm_name)
    if fallback:
        _log(f"[StockExtract] Fallback normalize: '{llm_name}' → {fallback}")
        return fallback, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    # ── Step 5: Final regex fallback on original text ─────────────────────────
    regex_symbol = _regex_extract_ticker(text)
    if regex_symbol:
        _log(f"[StockExtract] Final regex fallback matched: '{regex_symbol}'")
        return regex_symbol, llm_date or today, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

    _log(f"[StockExtract] Could not resolve '{llm_name}' to a stock code")
    return None, llm_date, llm_horizons, llm_focus_areas, llm_specific_questions, llm_user_context

@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    current_user: UserDB = Depends(_require_api_user),
):
    text = _extract_chat_text(request.messages)
    config = await asyncio.to_thread(_build_runtime_config, request.config_overrides, user_id=current_user.id)

    # ── 流式模式：立刻返回 SSE 流，在后台异步提取意图再启动任务 ──────────────────
    # 这样用户提交查询后立刻收到 job.ready，不用等待 thinking 模型的 StockExtract。
    if request.stream:
        job_id = uuid4().hex

        async def _extract_and_run():
            try:
                # If user already selected from disambiguation, skip extraction
                if request.resolved_symbol:
                    symbol = _normalize_symbol(request.resolved_symbol)
                    trade_date = market_today_str("HK")
                    horizons = ["short"]
                    focus_areas = []
                    specific_questions = []
                    inferred_user_context = {}
                else:
                    symbol, trade_date, horizons, focus_areas, specific_questions, inferred_user_context = \
                        await _ai_extract_symbol_and_date_streaming(text, config, job_id)

                if not symbol:
                    _emit_job_event(job_id, "job.failed", {
                        "error": "抱歉，我没能从您的消息中识别出股票标的。请输入代码（如 000700.HK 或 AAPL）或可识别的公司名称。"
                    })
                    return

                # ── Disambiguation check ──
                # If the symbol came from Chinese name matching and there are
                # multiple candidates, ask the user to clarify before proceeding.
                if not request.resolved_symbol:
                    candidates = _check_disambiguation(text, symbol)
                    if candidates:
                        _emit_job_event(job_id, "job.disambiguation", {
                            "query": text,
                            "candidates": candidates,
                        })
                        return

                pre_intent = {
                    "raw_query": text,
                    "ticker": symbol,
                    "horizons": horizons,
                    "focus_areas": focus_areas,
                    "specific_questions": specific_questions,
                }
                explicit_context = _extract_request_user_context(request)

                def _load_user_context() -> Dict[str, Any]:
                    with get_db_ctx() as db:
                        return _compose_analysis_user_context(
                            db,
                            current_user.id,
                            symbol,
                            explicit_context=explicit_context,
                            inferred_context=inferred_user_context,
                        )

                merged_user_context = await asyncio.to_thread(_load_user_context)
                pre_intent["user_context"] = merged_user_context
                analyze_req = AnalyzeRequest(
                    symbol=symbol,
                    trade_date=trade_date or market_today_str("HK"),
                    selected_analysts=request.selected_analysts,
                    config_overrides=request.config_overrides,
                    dry_run=request.dry_run,
                    query=text,
                    horizons=horizons,
                    user_intent=pre_intent,
                    objective=merged_user_context.get("objective"),
                    risk_profile=merged_user_context.get("risk_profile"),
                    investment_horizon=merged_user_context.get("investment_horizon"),
                    cash_available=merged_user_context.get("cash_available"),
                    current_position=merged_user_context.get("current_position"),
                    current_position_pct=merged_user_context.get("current_position_pct"),
                    average_cost=merged_user_context.get("average_cost"),
                    max_loss_pct=merged_user_context.get("max_loss_pct"),
                    constraints=merged_user_context.get("constraints", []),
                    user_notes=merged_user_context.get("user_notes"),
                )
                now = _utcnow_iso()
                _set_job(
                    job_id,
                    job_id=job_id,
                    user_id=current_user.id,
                    status="pending",
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                    symbol=analyze_req.symbol,
                    trade_date=analyze_req.trade_date,
                    error=None,
                    result=None,
                    decision=None,
                )
                _emit_job_event(
                    job_id,
                    "job.created",
                    {"job_id": job_id, "symbol": analyze_req.symbol, "trade_date": analyze_req.trade_date},
                )
                await _run_job(job_id, analyze_req, True, True, current_user.id, "chat")
            except Exception as exc:
                _log(f"[chat] _extract_and_run failed: {exc}")
                _emit_job_event(job_id, "job.failed", {"error": str(exc)})

        _create_tracked_task(_extract_and_run())
        return StreamingResponse(
            _stream_job_events(job_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # ── 非流式模式：保持原有阻塞行为 ─────────────────────────────────────────────
    symbol, trade_date, horizons, focus_areas, specific_questions, inferred_user_context = \
        await asyncio.to_thread(_ai_extract_symbol_and_date, text, config)

    if not symbol:
        raise HTTPException(status_code=400, detail="抱歉，我没能从您的消息中识别出股票标的。请输入代码（如 600519.SH）或可识别的公司名称。")

    pre_intent = {
        "raw_query": text,
        "ticker": symbol,
        "horizons": horizons,
        "focus_areas": focus_areas,
        "specific_questions": specific_questions,
    }
    explicit_context = _extract_request_user_context(request)

    def _load_user_context_nonstream() -> Dict[str, Any]:
        with get_db_ctx() as db:
            return _compose_analysis_user_context(
                db,
                current_user.id,
                symbol,
                explicit_context=explicit_context,
                inferred_context=inferred_user_context,
            )

    merged_user_context = await asyncio.to_thread(_load_user_context_nonstream)
    pre_intent["user_context"] = merged_user_context
    analyze_req = AnalyzeRequest(
        symbol=symbol,
        trade_date=trade_date or market_today_str("HK"),
        selected_analysts=request.selected_analysts,
        config_overrides=request.config_overrides,
        dry_run=request.dry_run,
        query=text,
        horizons=horizons,
        user_intent=pre_intent,
        objective=merged_user_context.get("objective"),
        risk_profile=merged_user_context.get("risk_profile"),
        investment_horizon=merged_user_context.get("investment_horizon"),
        cash_available=merged_user_context.get("cash_available"),
        current_position=merged_user_context.get("current_position"),
        current_position_pct=merged_user_context.get("current_position_pct"),
        average_cost=merged_user_context.get("average_cost"),
        max_loss_pct=merged_user_context.get("max_loss_pct"),
        constraints=merged_user_context.get("constraints", []),
        user_notes=merged_user_context.get("user_notes"),
    )
    job_id = uuid4().hex
    now = _utcnow_iso()
    _set_job(
        job_id,
        job_id=job_id,
        user_id=current_user.id,
        status="pending",
        created_at=now,
        started_at=None,
        finished_at=None,
        symbol=analyze_req.symbol,
        trade_date=analyze_req.trade_date,
        error=None,
        result=None,
        decision=None,
    )
    _emit_job_event(
        job_id,
        "job.created",
        {"job_id": job_id, "symbol": analyze_req.symbol, "trade_date": analyze_req.trade_date},
    )
    if request.dry_run:
        await _run_job(job_id, analyze_req, True, True, current_user.id, "chat")
        status_text = _get_job(job_id).get("status", "completed")
        decision_text = _get_job(job_id).get("decision", "DRY_RUN")
        return {
            "id": f"chatcmpl-{job_id}",
            "object": "chat.completion",
            "created": int(datetime.now().timestamp()),
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": (
                            f"已完成分析任务：{job_id}\n"
                            f"symbol={analyze_req.symbol}, trade_date={analyze_req.trade_date}\n"
                            f"status={status_text}, decision={decision_text}"
                        ),
                    },
                }
            ],
        }
    _create_tracked_task(_run_job(job_id, analyze_req, True, True, current_user.id, "chat"))
    return {
        "id": f"chatcmpl-{job_id}",
        "object": "chat.completion",
        "created": int(datetime.now().timestamp()),
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": (
                        f"已启动分析任务：{job_id}\n"
                        f"symbol={analyze_req.symbol}, trade_date={analyze_req.trade_date}\n"
                        f"可通过 /v1/jobs/{job_id} 与 /v1/jobs/{job_id}/result 查询结果。"
                    ),
                },
            }
        ],
    }


# Report API Endpoints
@app.post("/v1/reports", response_model=ReportResponse)
def create_report_endpoint(
    request: ReportCreateRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    """手动创建报告（通常由系统自动调用）."""
    report = report_service.create_report(
        db=db,
        symbol=request.symbol,
        trade_date=request.trade_date,
        decision=request.decision,
        result_data=request.result_data,
        user_id=current_user.id,
    )
    return report


@app.get("/v1/announcements/latest", response_model=LatestAnnouncementResponse)
def get_latest_announcement():
    return {"announcement": _load_latest_announcement()}


@app.get("/v1/reports", response_model=ReportListResponse)
def list_reports(
    symbol: Optional[str] = Query(None, description="按股票代码筛选"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    """获取报告列表."""
    total = report_service.count_reports(db=db, user_id=current_user.id, symbol=symbol)
    reports = report_service.get_reports_by_user(
        db=db,
        user_id=current_user.id,
        symbol=symbol,
        skip=skip,
        limit=limit,
    )
    code_to_name = _get_reverse_stock_map_cached_only()
    for r in reports:
        r.name = code_to_name.get(r.symbol, r.symbol)
        _attach_job_runtime_state(r, str(getattr(r, "id", "")))
    return {"total": total, "reports": reports}


@app.post("/v1/reports/latest-by-symbols", response_model=LatestReportsBySymbolsResponse)
def list_latest_reports_by_symbols(
    body: LatestReportsBySymbolsRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    reports = report_service.get_latest_reports_by_symbols(
        db=db,
        user_id=current_user.id,
        symbols=body.symbols,
    )
    return {"reports": reports}


@app.get("/v1/reports/{report_id}", response_model=ReportDetailResponse)
def get_report_endpoint(
    report_id: str,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    """获取报告详情."""
    report = report_service.get_report(db, report_id, user_id=current_user.id)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    if str(report.status or "") in report_service.ACTIVE_REPORT_STATUSES and not _get_job(report_id):
        report = report_service.finalize_orphan_report(db, report)
    code_to_name = _get_reverse_stock_map()
    report.name = code_to_name.get(report.symbol, report.symbol)
    _attach_job_runtime_state(report, report_id)
    return report


@app.delete("/v1/reports/{report_id}")
def delete_report_endpoint(
    report_id: str,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    """删除报告."""
    success = report_service.delete_report(db, report_id, user_id=current_user.id)
    if not success:
        raise HTTPException(status_code=404, detail="报告不存在")
    return {"message": "报告已删除"}


@app.post("/v1/reports/batch/delete", response_model=ReportBatchDeleteResponse)
def batch_delete_reports_endpoint(
    body: ReportBatchDeleteRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
):
    try:
        return report_service.batch_delete_reports(db, body.report_ids, user_id=current_user.id)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ─── API Token Endpoints ────────────────────────────────────────────────────

@app.get("/v1/tokens", response_model=List[UserTokenListItem])
def list_tokens(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    """获取当前用户的所有 API Token（不返回完整 token）。"""
    return token_service.list_user_tokens(db, current_user.id)


@app.post("/v1/tokens", response_model=UserTokenResponse)
def create_token(
    request: UserTokenCreateRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    """创建一个新的 API Token。完整 token 仅在此接口返回一次。"""
    try:
        return token_service.create_token(db, current_user.id, request.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/v1/tokens/{token_id}")
def delete_token(
    token_id: str,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    """吊销并删除一个 API Token。"""
    success = token_service.delete_token(db, current_user.id, token_id)
    if not success:
        raise HTTPException(status_code=404, detail="Token 不存在")
    return {"message": "Token 已吊销"}


# ─── Backtest Endpoints ───────────────────────────────────────────────────────

from api.services import backtest_service as _bt


class BacktestRequest(BaseModel):
    symbol: str
    start_date: str
    end_date: str
    selected_analysts: List[str] = ["market", "news", "fundamentals", "sentiment"]
    hold_days: int = 5
    sample_interval: int = 7
    config_overrides: Optional[Dict[str, Any]] = None


@app.post("/v1/backtest")
def submit_backtest(
    request: BacktestRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict:
    """提交历史回测任务，返回 job_id."""
    config = _build_runtime_config(request.config_overrides or {}, user_id=current_user.id, db=db)
    job_id = _bt.submit(
        symbol=request.symbol,
        start_date=request.start_date,
        end_date=request.end_date,
        selected_analysts=request.selected_analysts,
        hold_days=request.hold_days,
        sample_interval=request.sample_interval,
        config=config,
    )
    return {"job_id": job_id, "status": "pending"}


@app.get("/v1/backtest")
def list_backtests() -> Dict:
    """列出所有回测任务."""
    jobs = _bt.list_jobs()
    return {"jobs": jobs, "total": len(jobs)}


@app.get("/v1/backtest/{job_id}")
def get_backtest(job_id: str) -> Dict:
    """获取回测任务状态和结果."""
    job = _bt.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="回测任务不存在")
    return job


@app.delete("/v1/backtest/{job_id}")
def delete_backtest(job_id: str) -> Dict:
    """删除回测任务."""
    if not _bt.delete_job(job_id):
        raise HTTPException(status_code=404, detail="回测任务不存在")
    return {"message": "已删除"}


# ─── Runtime Config Endpoints ────────────────────────────────────────────────

_CONFIG_ALLOWED_KEYS = {
    "llm_provider", "deep_think_llm", "quick_think_llm",
    "backend_url", "max_debate_rounds", "max_risk_discuss_rounds",
}
_CONFIG_PREFERENCE_KEYS = {"email_report_enabled", "wecom_report_enabled"}
_CONFIG_MODEL_KEYS = ("llm_provider", "backend_url", "quick_think_llm", "deep_think_llm")
_CONFIG_MODEL_LABELS = {
    "quick_think_llm": "常规模型",
    "deep_think_llm": "推理模型",
}
_CONFIG_PROBE_TIMEOUT_SECONDS = 12.0
_CONFIG_PROBE_PROMPT = "Reply with the single word OK."
_CONFIG_WARMUP_TIMEOUT_SECONDS = 20.0
_CONFIG_WARMUP_PROMPT = "Reply with the single word OK."


def _mask_secret_value(value: Optional[str], *, head: int = 4, tail: int = 4) -> Optional[str]:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if len(normalized) <= head + tail:
        return "*" * max(6, len(normalized))
    return f"{normalized[:head]}{'*' * max(6, len(normalized) - head - tail)}{normalized[-tail:]}"


def _mask_wecom_webhook(webhook_url: Optional[str]) -> Optional[str]:
    normalized = str(webhook_url or "").strip()
    if not normalized:
        return None
    prefix = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
    if normalized.startswith(prefix):
        masked_key = _mask_secret_value(normalized[len(prefix):])
        return f"{prefix}{masked_key}"
    if normalized.startswith("http"):
        if "key=" in normalized:
            base, key = normalized.rsplit("key=", 1)
            return f"{base}key={_mask_secret_value(key)}"
        return _mask_secret_value(normalized, head=18, tail=8)
    return _mask_secret_value(normalized)


def _warmup_model_names(config: Dict[str, Any]) -> List[str]:
    seen: set[str] = set()
    models: List[str] = []
    for key in ("quick_think_llm", "deep_think_llm"):
        value = str(config.get(key) or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        models.append(value)
    return models


def _warmup_model_targets(config: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
    targets: Dict[str, List[str]] = {}
    for key in ("quick_think_llm", "deep_think_llm"):
        model = str(config.get(key) or "").strip()
        if not model:
            continue
        labels = targets.setdefault(model, [])
        label = _CONFIG_MODEL_LABELS.get(key, key)
        if label not in labels:
            labels.append(label)
    return [(model, labels) for model, labels in targets.items()]


def _should_trigger_config_warmup(
    before_cfg: UserRuntimeConfigResponse,
    after_cfg: UserRuntimeConfigResponse,
    updates: UserRuntimeConfigUpdateRequest,
) -> bool:
    if not updates.warmup:
        return False
    if updates.force_warmup:
        return True
    if updates.api_key:
        return True
    before = before_cfg.model_dump()
    after = after_cfg.model_dump()
    return any(before.get(key) != after.get(key) for key in _CONFIG_MODEL_KEYS)


def _build_pending_runtime_config(
    updates: UserRuntimeConfigUpdateRequest,
    user_id: str,
    db: Session,
) -> Dict[str, Any]:
    config = _build_runtime_config({}, user_id=user_id, db=db)
    for key in _CONFIG_ALLOWED_KEYS:
        value = getattr(updates, key, None)
        if value is not None:
            config[key] = value

    if updates.clear_api_key:
        config["api_key"] = ""
    elif updates.api_key:
        config["api_key"] = updates.api_key

    quick = config.get("quick_think_llm")
    deep = config.get("deep_think_llm")
    if not deep and quick:
        config["deep_think_llm"] = quick
    if not quick and deep:
        config["quick_think_llm"] = deep
    return config


def _should_probe_runtime_config(
    before_cfg: UserRuntimeConfigResponse,
    pending_cfg: Dict[str, Any],
    updates: UserRuntimeConfigUpdateRequest,
) -> bool:
    del before_cfg, pending_cfg
    if updates.clear_api_key:
        return False
    return bool(updates.api_key)


def _probe_runtime_config(config: Dict[str, Any]) -> Dict[str, str]:
    from tradingagents.llm_clients.factory import create_llm_client

    provider = str(config.get("llm_provider") or "openai")
    base_url = config.get("backend_url")
    api_key = str(config.get("api_key") or "").strip()
    model = str(config.get("quick_think_llm") or config.get("deep_think_llm") or "").strip()

    if not model or not api_key:
        return {"status": "skipped", "reason": "missing_model_or_key"}

    try:
        client = create_llm_client(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=_CONFIG_PROBE_TIMEOUT_SECONDS,
            max_retries=0,
        )
        llm = client.get_llm()
        response = llm.invoke(_CONFIG_PROBE_PROMPT)
        raw = response if isinstance(response, str) else getattr(response, "content", str(response))
        preview = str(raw).strip().replace("\n", " ")[:80] or "<empty>"
        return {"status": "ok", "model": model, "preview": preview}
    except Exception as exc:
        detail = str(exc).strip()
        lowered = detail.lower()
        if "401" in lowered or "invalid authentication" in lowered or "authenticationerror" in lowered:
            raise HTTPException(
                status_code=400,
                detail="模型 Key 验证失败：上游返回 401 Invalid Authentication，请检查 API Key 是否正确。",
            ) from exc
        raise HTTPException(
            status_code=400,
            detail=f"模型连接验证失败：{detail[:200] or 'unknown error'}",
        ) from exc


def _invoke_runtime_warmup(
    config: Dict[str, Any],
    prompt: str,
    user_id: str,
    timeout: float = _CONFIG_WARMUP_TIMEOUT_SECONDS,
) -> List[Dict[str, Any]]:
    from tradingagents.llm_clients.factory import create_llm_client

    provider = str(config.get("llm_provider") or "openai")
    base_url = config.get("backend_url")
    api_key = config.get("api_key")
    targets = _warmup_model_targets(config)

    if not targets:
        raise HTTPException(status_code=400, detail="请先配置至少一个可用模型。")

    _log(
        f"[LLM Warmup] user={user_id} invoking provider={provider} "
        f"models={[model for model, _ in targets]} base_url={base_url or 'default'}"
    )

    results: List[Dict[str, Any]] = []
    errors: List[str] = []
    for model, labels in targets:
        try:
            client = create_llm_client(
                provider=provider,
                model=model,
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                max_retries=0,
            )
            llm = client.get_llm()
            response = llm.invoke(prompt)
            raw = response if isinstance(response, str) else getattr(response, "content", str(response))
            content = str(raw).strip() or "<empty>"
            preview = content.replace("\n", " ")[:80]
            _log(f"[LLM Warmup] user={user_id} model={model} success response={preview}")
            results.append({
                "model": model,
                "targets": labels,
                "content": content,
                "error": None,
            })
        except Exception as exc:
            detail = str(exc).strip() or "unknown error"
            errors.append(f"{model}: {detail}")
            logger.warning(
                "[LLM Warmup] user=%s model=%s failed: %s",
                user_id,
                model,
                exc,
            )
            results.append({
                "model": model,
                "targets": labels,
                "content": None,
                "error": detail[:200],
            })

    if not any(item.get("content") for item in results):
        raise HTTPException(
            status_code=400,
            detail=f"模型 warmup 失败：{'; '.join(errors)[:300]}",
        )

    return results


def _run_config_warmup(config: Dict[str, Any], user_id: str) -> None:
    models = _warmup_model_names(config)
    if not models:
        _log(f"[LLM Warmup] user={user_id} skipped: no models configured")
        return
    try:
        _invoke_runtime_warmup(config, _CONFIG_WARMUP_PROMPT, user_id, timeout=_CONFIG_WARMUP_TIMEOUT_SECONDS)
    except HTTPException as exc:
        logger.warning("[LLM Warmup] user=%s failed: %s", user_id, exc.detail)


def _config_response_for_user(user: Optional[UserDB], db: Session) -> UserRuntimeConfigResponse:
    cfg = _build_runtime_config({}, user_id=user.id if user else None, db=db)
    user_cfg = auth_service.get_user_llm_config(db, user.id) if user else None
    webhook_url = auth_service.decrypt_secret(getattr(user_cfg, "wecom_webhook_encrypted", None))
    return UserRuntimeConfigResponse(
        llm_provider=cfg["llm_provider"],
        deep_think_llm=cfg["deep_think_llm"],
        quick_think_llm=cfg["quick_think_llm"],
        backend_url=cfg["backend_url"],
        max_debate_rounds=cfg["max_debate_rounds"],
        max_risk_discuss_rounds=cfg["max_risk_discuss_rounds"],
        has_api_key=bool(user_cfg and user_cfg.api_key_encrypted),
        api_key=auth_service.decrypt_secret(user_cfg.api_key_encrypted) if user_cfg and user_cfg.api_key_encrypted else None,
        has_wecom_webhook=bool(webhook_url),
        wecom_webhook_display=_mask_wecom_webhook(webhook_url),
        server_fallback_enabled=bool(cfg.get("server_fallback_enabled", True)),
        email_report_enabled=user.email_report_enabled if user and hasattr(user, 'email_report_enabled') else True,
        wecom_report_enabled=user.wecom_report_enabled if user and hasattr(user, "wecom_report_enabled") else True,
        default_analysts=json.loads(user_cfg.default_analysts) if user_cfg and user_cfg.default_analysts else ["market", "social", "news", "fundamentals", "macro", "smart_money", "volume_price"],
    )


@app.post("/v1/auth/request-code")
def request_login_code(request: AuthRequestCodeRequest):
    email = auth_service.normalize_email(request.email)
    if not re.match(r"^[^@\s]+@[^@\s.]+\.[^@\s.]+$", email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    with get_db_ctx() as db:
        code = auth_service.upsert_login_code(db, email)
    # DB session 已释放，SMTP 不会阻塞连接池
    dev_code = auth_service.send_login_code(email, code)
    response = {"message": "验证码已发送"}
    if dev_code:
        response["dev_code"] = dev_code
    return response


@app.post("/v1/auth/verify-code", response_model=AuthVerifyCodeResponse)
def verify_login_code(body: AuthVerifyCodeRequest, request: Request, db: Session = Depends(get_db)):
    user = auth_service.verify_login_code(db, body.email, body.code, client_ip=_get_real_ip(request))
    if not user:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    access_token = auth_service.create_access_token(user)
    return AuthVerifyCodeResponse(access_token=access_token, user=user)


@app.get("/v1/auth/me", response_model=UserResponse)
def get_me(current_user: UserDB = Depends(_require_web_user)):
    return current_user


@app.get("/v1/config", response_model=UserRuntimeConfigResponse)
def get_runtime_config(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    """获取当前用户运行时配置。"""
    return _config_response_for_user(current_user, db)


@app.patch("/v1/config")
def update_runtime_config(
    updates: UserRuntimeConfigUpdateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    """更新当前用户运行时配置，下次分析时生效。"""
    normalized_wecom_webhook = None
    if updates.wecom_webhook_url:
        from api.services.wecom_notification_service import normalize_webhook_url

        try:
            normalized_wecom_webhook = normalize_webhook_url(updates.wecom_webhook_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    persistent_user = db.query(UserDB).filter(UserDB.id == current_user.id).first() or current_user
    before_cfg = _config_response_for_user(persistent_user, db)
    pending_cfg = _build_pending_runtime_config(updates, persistent_user.id, db)
    if _should_probe_runtime_config(before_cfg, pending_cfg, updates):
        probe = _probe_runtime_config(pending_cfg)
        _log(
            f"[LLM Probe] user={persistent_user.id} provider={pending_cfg.get('llm_provider')} "
            f"model={probe.get('model', '')} status={probe.get('status')}"
        )
    row = auth_service.upsert_user_llm_config(
        db,
        persistent_user.id,
        llm_provider=updates.llm_provider,
        deep_think_llm=updates.deep_think_llm,
        quick_think_llm=updates.quick_think_llm,
        backend_url=updates.backend_url,
        max_debate_rounds=updates.max_debate_rounds,
        max_risk_discuss_rounds=updates.max_risk_discuss_rounds,
        api_key=updates.api_key,
        wecom_webhook_url=normalized_wecom_webhook,
        clear_api_key=updates.clear_api_key,
        clear_wecom_webhook=updates.clear_wecom_webhook,
        default_analysts=updates.default_analysts,
    )
    user_pref_updated = False
    if updates.email_report_enabled is not None:
        persistent_user.email_report_enabled = updates.email_report_enabled
        user_pref_updated = True
    if updates.wecom_report_enabled is not None:
        persistent_user.wecom_report_enabled = updates.wecom_report_enabled
        user_pref_updated = True
    if user_pref_updated:
        db.commit()
    current_cfg = _config_response_for_user(persistent_user, db)
    warmup_models = _warmup_model_names(current_cfg.model_dump())
    should_warmup = _should_trigger_config_warmup(before_cfg, current_cfg, updates)
    warmup_payload: Dict[str, Any]
    if should_warmup and warmup_models:
        warmup_payload = {
            "requested": True,
            "triggered": True,
            "status": "scheduled",
            "models": warmup_models,
            "message": f"模型配置已保存，后台正在预热 {len(warmup_models)} 个模型。",
        }
        background_tasks.add_task(
            _run_config_warmup,
            _build_runtime_config({}, user_id=persistent_user.id, db=db),
            persistent_user.id,
        )
    elif updates.warmup:
        warmup_payload = {
            "requested": True,
            "triggered": False,
            "status": "skipped",
            "models": warmup_models,
            "message": "模型配置已保存，本次未触发 warmup。",
        }
    else:
        warmup_payload = {
            "requested": False,
            "triggered": False,
            "status": "disabled",
            "models": [],
            "message": "模型配置已保存。",
        }
    filtered = {
        k: v
        for k, v in updates.model_dump().items()
        if v is not None
        and k not in {"api_key", "wecom_webhook_url", "warmup", "force_warmup"}
        and (
            k in _CONFIG_ALLOWED_KEYS
            or k in _CONFIG_PREFERENCE_KEYS
            or (k in {"clear_api_key", "clear_wecom_webhook"} and bool(v))
        )
    }
    return {
        "message": "用户配置已更新",
        "applied": filtered,
        "has_api_key": bool(row.api_key_encrypted),
        "current": current_cfg,
        "warmup": warmup_payload,
    }


@app.get("/v1/config/search", response_model=SearchConfigResponse)
def get_search_config(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    user_cfg = auth_service.get_user_llm_config(db, current_user.id)
    search_config = {}
    if user_cfg and hasattr(user_cfg, 'search_config') and user_cfg.search_config:
        try:
            search_config = json.loads(user_cfg.search_config)
        except Exception:
            pass

    default_providers = [
        {"name": "tavily", "label": "Tavily", "env_key": "TAVILY_API_KEY", "api_key": "", "enabled": True},
        {"name": "brave", "label": "Brave Search", "env_key": "BRAVE_API_KEY", "api_key": "", "enabled": True},
        {"name": "serpapi", "label": "SerpAPI", "env_key": "SERPAPI_API_KEY", "api_key": "", "enabled": True},
        {"name": "bocha", "label": "Bocha", "env_key": "BOCHA_API_KEY", "api_key": "", "enabled": True},
        {"name": "anspire", "label": "Anspire", "env_key": "ANSPIRE_API_KEY", "api_key": "", "enabled": True},
        {"name": "minimax", "label": "MiniMax", "env_key": "MINIMAX_API_KEY", "api_key": "", "enabled": True},
        {"name": "searxng", "label": "SearXNG", "env_key": "SEARXNG_BASE_URL", "api_key": "", "base_url": "", "enabled": True},
    ]

    providers = []
    for dp in default_providers:
        saved = search_config.get(dp["name"], {})
        key = saved.get("api_key", "")
        providers.append(SearchProviderConfig(
            name=dp["name"],
            label=dp["label"],
            env_key=dp["env_key"],
            api_key=key,
            base_url=saved.get("base_url", dp.get("base_url", "")),
            enabled=saved.get("enabled", True),
        ))

    has_any = any(p.api_key for p in providers)
    cache_ttl = search_config.get("cache_ttl", 1800)
    return SearchConfigResponse(providers=providers, has_any_key=has_any, cache_ttl=cache_ttl)


@app.put("/v1/config/search")
def update_search_config(
    request: SearchConfigUpdateRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    from api.services import auth_service
    # Read existing config to preserve keys when masked value is sent back
    user_cfg = auth_service.get_user_llm_config(db, current_user.id)
    existing = {}
    if user_cfg and user_cfg.search_config:
        try:
            existing = json.loads(user_cfg.search_config)
        except Exception:
            pass

    config_data = {}
    for p in request.providers:
        new_key = (p.api_key or "").strip()
        old_key = existing.get(p.name, {}).get("api_key", "")
        # If the incoming key looks masked (contains **), keep the existing key
        if new_key and "**" in new_key:
            new_key = old_key
        config_data[p.name] = {
            "api_key": new_key,
            "base_url": p.base_url if p.base_url else "",
            "enabled": p.enabled,
        }

    if not user_cfg:
        user_cfg = UserLLMConfigDB(user_id=current_user.id)
        db.add(user_cfg)
        db.flush()
    # Merge: preserve data_sources and social_sentiment from existing config
    existing["tavily"] = config_data.get("tavily", existing.get("tavily", {}))
    existing["brave"] = config_data.get("brave", existing.get("brave", {}))
    existing["serpapi"] = config_data.get("serpapi", existing.get("serpapi", {}))
    existing["bocha"] = config_data.get("bocha", existing.get("bocha", {}))
    existing["anspire"] = config_data.get("anspire", existing.get("anspire", {}))
    existing["minimax"] = config_data.get("minimax", existing.get("minimax", {}))
    existing["searxng"] = config_data.get("searxng", existing.get("searxng", {}))
    if request.cache_ttl is not None:
        existing["cache_ttl"] = request.cache_ttl
    user_cfg.search_config = json.dumps(existing)
    db.commit()

    # Immediately update os.environ so services pick up new keys
    _SEARCH_ENV = {
        "tavily": "TAVILY_API_KEYS", "brave": "BRAVE_API_KEYS",
        "serpapi": "SERPAPI_API_KEYS", "bocha": "BOCHA_API_KEYS",
        "anspire": "ANSPIRE_API_KEYS", "minimax": "MINIMAX_API_KEYS",
        "searxng": "SEARXNG_BASE_URLS",
    }
    for name, env_var in _SEARCH_ENV.items():
        _force_update_env(env_var, config_data.get(name, {}).get("api_key", ""))

    return {"message": "搜索服务配置已保存", "providers_count": len(config_data)}


# ── Data source config endpoints ──────────────────────────────────────────────

_DEFAULT_DATA_SOURCES = [
    {"name": "alpha_vantage", "label": "Alpha Vantage", "env_key": "ALPHA_VANTAGE_API_KEY", "market": "US"},
    {"name": "finnhub", "label": "Finnhub.io", "env_key": "FINNHUB_API_KEY", "market": "US"},
    {"name": "tiingo", "label": "Tiingo", "env_key": "TIINGO_API_KEY", "market": "US"},
    {"name": "aitick", "label": "AiTick", "env_key": "AITICK_API_KEY", "market": "HK"},
    {"name": "itick", "label": "iTick", "env_key": "ITICK_API_KEY", "market": "HK"},
]


@app.get("/v1/config/data-sources", response_model=DataSourceConfigResponse)
def get_data_source_config(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    user_cfg = auth_service.get_user_llm_config(db, current_user.id)
    ds_config = {}
    if user_cfg and hasattr(user_cfg, 'search_config') and user_cfg.search_config:
        try:
            all_cfg = json.loads(user_cfg.search_config)
            ds_config = all_cfg.get("data_sources", {})
        except Exception:
            pass

    providers = []
    for dp in _DEFAULT_DATA_SOURCES:
        saved = ds_config.get(dp["name"], {})
        key = saved.get("api_key", "")
        providers.append(DataSourceProviderConfig(
            name=dp["name"],
            label=dp["label"],
            env_key=dp["env_key"],
            market=dp["market"],
            api_key=key,
            enabled=saved.get("enabled", True),
        ))

    has_any = any(p.api_key for p in providers)
    return DataSourceConfigResponse(providers=providers, has_any_key=has_any)


@app.put("/v1/config/data-sources")
def update_data_source_config(
    request: DataSourceConfigUpdateRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    user_cfg = auth_service.get_user_llm_config(db, current_user.id)
    if not user_cfg:
        user_cfg = UserLLMConfigDB(user_id=current_user.id)
        db.add(user_cfg)
        db.flush()

    # Read existing to preserve keys when masked value is sent back
    existing_all = {}
    if user_cfg.search_config:
        try:
            existing_all = json.loads(user_cfg.search_config)
        except Exception:
            pass
    existing_ds = existing_all.get("data_sources", {})

    config_data = {}
    for p in request.providers:
        new_key = (p.api_key or "").strip()
        old_key = existing_ds.get(p.name, {}).get("api_key", "")
        if new_key and "**" in new_key:
            new_key = old_key
        config_data[p.name] = {
            "api_key": new_key,
            "enabled": p.enabled,
        }

    existing_all["data_sources"] = config_data
    user_cfg.search_config = json.dumps(existing_all)
    db.commit()

    # Immediately update os.environ
    _DS_ENV = {
        "alpha_vantage": "ALPHA_VANTAGE_API_KEY", "finnhub": "FINNHUB_API_KEY",
        "tiingo": "TIINGO_API_KEY", "aitick": "AITICK_API_KEY", "itick": "ITICK_API_KEY",
    }
    for name, env_var in _DS_ENV.items():
        _force_update_env(env_var, config_data.get(name, {}).get("api_key", ""))

    return {"message": "数据源配置已保存", "providers_count": len(config_data)}


@app.get("/v1/config/social-sentiment", response_model=SocialSentimentConfigResponse)
def get_social_sentiment_config(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    user_cfg = auth_service.get_user_llm_config(db, current_user.id)
    search_config = {}
    if user_cfg and hasattr(user_cfg, 'search_config') and user_cfg.search_config:
        try:
            search_config = json.loads(user_cfg.search_config)
        except Exception:
            pass

    ss = search_config.get("social_sentiment", {})
    api_key = ss.get("api_key", "")
    base_url = ss.get("base_url", "https://api.adanos.org")
    return SocialSentimentConfigResponse(
        api_key=api_key,
        base_url=base_url,
        has_key=bool(api_key),
    )


@app.put("/v1/config/social-sentiment")
def update_social_sentiment_config(
    request: SocialSentimentConfigUpdateRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    user_cfg = auth_service.get_user_llm_config(db, current_user.id)
    if not user_cfg:
        user_cfg = UserLLMConfigDB(user_id=current_user.id)
        db.add(user_cfg)
        db.flush()

    # Merge into existing search_config JSON
    search_config = {}
    if user_cfg.search_config:
        try:
            search_config = json.loads(user_cfg.search_config)
        except Exception:
            pass

    # Only update if API key is not masked (preserve existing key)
    existing = search_config.get("social_sentiment", {})
    new_key = request.api_key
    if new_key and "***" in new_key:
        # Masked value — keep existing key
        new_key = existing.get("api_key", "")

    search_config["social_sentiment"] = {
        "api_key": new_key,
        "base_url": request.base_url or "https://api.adanos.org",
    }
    user_cfg.search_config = json.dumps(search_config)
    db.commit()

    # Immediately update os.environ
    _force_update_env("SOCIAL_SENTIMENT_API_KEY", new_key or "")
    _force_update_env("SOCIAL_SENTIMENT_BASE_URL", request.base_url or "https://api.adanos.org")

    return {"message": "社交舆情配置已保存"}


# ---------------------------------------------------------------------------
# Futu OpenD Configuration
# ---------------------------------------------------------------------------

class FutuOpendConfigResponse(BaseModel):
    host: str = "127.0.0.1"
    port: int = 11111
    rsa_key: str = ""


@app.get("/v1/config/futu-opend", response_model=FutuOpendConfigResponse)
def get_futu_opend_config(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    from dotenv import dotenv_values
    env_vals = dotenv_values(".env")
    host = env_vals.get("FUTU_OPEND_HOST", "127.0.0.1")
    port = int(env_vals.get("FUTU_OPEND_PORT", "11111"))
    has_rsa = bool(env_vals.get("FUTU_RSA_KEY_PATH", "")) and os.path.exists(env_vals.get("FUTU_RSA_KEY_PATH", ""))
    return FutuOpendConfigResponse(host=host, port=port, rsa_key="[SET]" if has_rsa else "")


@app.put("/v1/config/futu-opend")
def update_futu_opend_config(
    request: FutuOpendConfigResponse,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    import re
    env_path = ".env"
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
    keys_to_update = {"FUTU_OPEND_HOST", "FUTU_OPEND_PORT"}
    new_vals = {
        "FUTU_OPEND_HOST": request.host,
        "FUTU_OPEND_PORT": str(request.port),
    }
    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in keys_to_update:
                new_lines.append(f"{key}={new_vals[key]}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line)
    for key in keys_to_update - updated_keys:
        new_lines.append(f"{key}={new_vals[key]}\n")
    with open(env_path, "w") as f:
        f.writelines(new_lines)
    os.environ["FUTU_OPEND_HOST"] = request.host
    os.environ["FUTU_OPEND_PORT"] = str(request.port)
    
    # Save RSA key if provided
    if request.rsa_key and "PRIVATE KEY" in request.rsa_key:
        rsa_path = "config/rsa_key.txt"
        os.makedirs("config", exist_ok=True)
        with open(rsa_path, "w") as f:
            f.write(request.rsa_key.strip() + chr(10))
        # Update .env RSA path
        rsa_lines = []
        found = False
        for line in new_lines:
            if line.startswith("FUTU_RSA_KEY_PATH="):
                rsa_lines.append(f"FUTU_RSA_KEY_PATH={rsa_path}" + chr(10))
                found = True
            else:
                rsa_lines.append(line)
        if not found:
            rsa_lines.append(f"FUTU_RSA_KEY_PATH={rsa_path}" + chr(10))
        with open(env_path, "w") as f:
            f.writelines(rsa_lines)
        os.environ["FUTU_RSA_KEY_PATH"] = rsa_path
    
    return {"message": "Futu OpenD 配置已保存"}


@app.get("/v1/models")
def list_models(
    base_url: str = "",
    api_key: str = "",
    current_user: UserDB = Depends(_require_web_user),
):
    """Proxy /v1/models from any OpenAI-compatible provider."""
    import httpx
    if not base_url or not api_key:
        raise HTTPException(400, "base_url and api_key required")
    url = base_url.rstrip("/") + "/models"
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
            if r.status_code != 200:
                raise HTTPException(r.status_code, r.text[:500])
            data = r.json()
            models = [m["id"] for m in (data.get("data") or []) if m.get("id")]
            return {"models": sorted(models)}
    except httpx.RequestError as e:
        raise HTTPException(502, f"Connection failed: {e}")


@app.get("/v1/futu/status")
def get_futu_status(
    current_user: UserDB = Depends(_require_web_user),
):
    """Test Futu OpenD connectivity and return account info."""
    try:
        from futu import OpenQuoteContext, SysConfig, SecurityFirm
        from dotenv import dotenv_values
        env_vals = dotenv_values(".env")
        host = env_vals.get("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(env_vals.get("FUTU_OPEND_PORT", "11111"))
        
        # Enable RSA for remote connections
        rsa_path = env_vals.get("FUTU_RSA_KEY_PATH", "")
        need_encrypt = host not in ("127.0.0.1", "localhost")
        
        if need_encrypt and rsa_path:
            SysConfig.enable_proto_encrypt(True)
            SysConfig.set_init_rsa_file(rsa_path)
        
        # Enable RSA for remote connections
        rsa_path = env_vals.get("FUTU_RSA_KEY_PATH", "")
        need_encrypt = host not in ("127.0.0.1", "localhost")
        
        if need_encrypt and rsa_path:
            SysConfig.enable_proto_encrypt(True)
            SysConfig.set_init_rsa_file(rsa_path)
        
        # Test quote connection + get user info (must be before close)
        quote_ctx = OpenQuoteContext(host=host, port=port)
        ret, state = quote_ctx.get_global_state()
        if ret != 0:
            quote_ctx.close()
            return {"connected": False, "error": str(state)}
        
        ret2, user_info = quote_ctx.get_user_info()
        server_ver = str(state.get("server_ver", "")) if isinstance(state, dict) else ""
        quote_ctx.close()
        
        # Get trade accounts (HK + US)
        from futu import OpenSecTradeContext, TrdEnv, TrdMarket
        acc_info = []
        seen_ids = set()
        
        for market in [TrdMarket.HK, TrdMarket.US]:
            trade_ctx = OpenSecTradeContext(host=host, port=port, filter_trdmarket=market, security_firm=SecurityFirm.FUTUSECURITIES)
            ret3, acc_data = trade_ctx.get_acc_list()
            trade_ctx.close()
            if ret3 == 0 and not acc_data.empty:
                for _, row in acc_data.iterrows():
                    aid = int(row.get("acc_id", 0))
                    if aid in seen_ids:
                        # Update market auth for duplicate accounts
                        for a in acc_info:
                            if a["acc_id"] == aid:
                                auth = str(row.get("trdmarket_auth", ""))
                                if auth and market == TrdMarket.US and "US" in auth:
                                    a["markets"] = list(set(a.get("markets", []) + ["US"]))
                        continue
                    seen_ids.add(aid)
                    auth = str(row.get("trdmarket_auth", ""))
                    markets = []
                    if "HK" in auth: markets.append("HK")
                    if "US" in auth: markets.append("US")
                    acc_info.append({
                        "acc_id": aid,
                        "acc_type": str(row.get("acc_type", "")),
                        "trd_env": str(row.get("trd_env", "")),
                        "sim_acc_type": str(row.get("sim_acc_type", "")),
                        "acc_status": str(row.get("acc_status", "")),
                        "markets": markets or [str(market).split(".")[-1]],
                    })
        
        user_detail = {}
        if ret2 == 0 and isinstance(user_info, dict):
            user_detail = {
                "user_id": user_info.get("user_id", ""),
                "nick_name": user_info.get("nick_name", ""),
                "avatar_url": user_info.get("avatar_url", ""),
                "sub_quota": user_info.get("sub_quota", 0),
                "history_kl_quota": user_info.get("history_kl_quota", 0),
                "hk_qot_right": user_info.get("hk_qot_right", "N/A"),
                "hk_option_qot_right": user_info.get("hk_option_qot_right", "N/A"),
                "hk_future_qot_right": user_info.get("hk_future_qot_right", "N/A"),
                "us_qot_right": user_info.get("us_qot_right", "N/A"),
                "us_option_qot_right": user_info.get("us_option_qot_right", "N/A"),
                "us_future_qot_right": user_info.get("us_future_qot_right", "N/A"),
            }
        
        return {
            "connected": True,
            "host": host,
            "port": port,
            "server_ver": server_ver,
            "user": user_detail,
            "accounts": acc_info,
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}


@app.post("/v1/config/warmup", response_model=UserRuntimeWarmupResponse)
def warmup_runtime_config(
    request: UserRuntimeWarmupRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    pending_cfg = _build_pending_runtime_config(request, current_user.id, db)
    prompt = (request.prompt or "").strip() or "你好"
    results = _invoke_runtime_warmup(pending_cfg, prompt, current_user.id)
    return {
        "prompt": prompt,
        "results": results,
    }


@app.post("/v1/config/wecom/warmup", response_model=WecomWebhookWarmupResponse)
async def warmup_wecom_webhook(
    request: WecomWebhookWarmupRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(_require_web_user),
):
    from api.services.wecom_notification_service import build_test_message, normalize_webhook_url, send_message

    webhook_url = (request.wecom_webhook_url or "").strip()
    if not webhook_url:
        user_cfg = auth_service.get_user_llm_config(db, current_user.id)
        webhook_url = auth_service.decrypt_secret(getattr(user_cfg, "wecom_webhook_encrypted", None)) or ""
    if not webhook_url:
        raise HTTPException(status_code=400, detail="请先填写或保存企业微信 Webhook")
    try:
        webhook_url = normalize_webhook_url(webhook_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        sent = await asyncio.to_thread(send_message, build_test_message(request.content), webhook_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Webhook 测试发送失败：{exc}") from exc
    if not sent:
        raise HTTPException(status_code=400, detail="Webhook 测试发送失败，请检查地址或机器人状态")

    return {
        "sent": True,
        "message": "Webhook 测试发送成功",
        "webhook_display": _mask_wecom_webhook(webhook_url),
    }


# ── Stock Search ──────────────────────────────────────────────────────────────

@app.get("/v1/market/stock-search")
def search_stocks(
    q: str = Query("", min_length=1, max_length=20),
    current_user: UserDB = Depends(_require_api_user),
):
    """Search stocks by code prefix or name substring.

    Searches the stock universe (US/ETF/HK, 35k+ entries).
    """
    q = q.strip()
    if not q:
        return {"results": []}

    results = []
    seen_symbols: set[str] = set()
    q_upper = q.upper()

    # ── Search stock universe (US/ETF/HK) ──────────────────────────────────
    try:
        from tradingagents.dataflows.stock_resolver import _get_by_upper, _load
        _load()
        universe = _get_by_upper()
        # Search by code prefix
        for key, entry in universe.items():
            if len(results) >= 20:
                break
            code = entry["code"]
            name = entry["name"]
            if code.upper().startswith(q_upper) or q_upper in key:
                if code not in seen_symbols:
                    results.append({"symbol": code, "name": name})
                    seen_symbols.add(code)
        # Search by name substring
        if len(results) < 20:
            for key, entry in universe.items():
                if len(results) >= 20:
                    break
                code = entry["code"]
                name = entry["name"]
                if q in name and code not in seen_symbols:
                    results.append({"symbol": code, "name": name})
                    seen_symbols.add(code)
    except Exception:
        pass

    return {"results": results}


def _annotate_scheduled_with_imported_context(items: List[dict], db: Session, user_id: str) -> List[dict]:
    imported_map: Dict[str, Dict[str, Any]] = {}
    for item in portfolio_import_service.list_imported_positions(db, user_id):
        imported_map[item["symbol"]] = item
    for item in items:
        imported = imported_map.get(item["symbol"])
        item["has_imported_context"] = imported is not None
        item["imported_current_position"] = imported.get("current_position") if imported else None
        item["imported_average_cost"] = imported.get("average_cost") if imported else None
        item["imported_trade_points_count"] = imported.get("trade_points_count") if imported else 0
    return items


def _merge_imported_user_context(*contexts: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    note_parts: List[str] = []
    for ctx in contexts:
        if not ctx:
            continue
        for key, value in ctx.items():
            if key == "user_notes":
                if value:
                    note_parts.append(str(value).strip())
                continue
            if value is not None:
                merged[key] = value
    if note_parts:
        merged["user_notes"] = "\n\n".join(part for part in note_parts if part)
    return normalize_user_context(merged)


def _build_imported_user_context(db: Session, user_id: str, symbol: str) -> Dict[str, Any]:
    context = portfolio_import_service.build_scheduled_user_context(db, user_id, symbol)
    return _merge_imported_user_context(context)


def _build_manual_imported_user_context(db: Session, user_id: str, symbol: str) -> Dict[str, Any]:
    """Build imported position context for manual/ad-hoc analysis runs."""
    return _build_imported_user_context(db, user_id, symbol)


def _attach_stock_names(items: List[dict], code_to_name: Dict[str, str]) -> List[dict]:
    for item in items:
        symbol = str(item.get("symbol") or "").upper()
        item["name"] = code_to_name.get(symbol, symbol or item.get("name") or "")
    return items


@app.get("/v1/portfolio/imports")
def get_portfolio_import_state(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    return portfolio_import_service.get_import_state(db, current_user.id)


@app.post("/v1/portfolio/imports")
def sync_portfolio_import(
    body: PortfolioImportSyncRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    try:
        return portfolio_import_service.sync_positions(
            db=db,
            user_id=current_user.id,
            positions=[p.model_dump() for p in body.positions],
            source=body.source,
            auto_apply_scheduled=body.auto_apply_scheduled,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/v1/portfolio/imports", status_code=204)
def clear_portfolio_import_state(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    portfolio_import_service.clear_imported_portfolio(db, current_user.id)


@app.post("/v1/portfolio/parse-image")
async def parse_position_image_endpoint(
    file: UploadFile = File(...),
    current_user: UserDB = Depends(_require_api_user),
):
    """Parse a broker position screenshot using server-side VLM."""
    from api.services.vlm_position_parser import parse_position_image

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "只支持图片文件")

    image_bytes = await file.read()
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(400, "图片不能超过 10MB")

    try:
        positions = await asyncio.to_thread(parse_position_image, image_bytes, file.content_type)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.warning("[parse-image] VLM parsing failed: %s", exc)
        raise HTTPException(500, "图片解析失败，请稍后重试") from exc

    return {"positions": positions}


@app.get("/v1/dashboard/tracking-board")
def get_dashboard_tracking_board(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    return tracking_board_service.get_tracking_board(db, current_user.id)


# ── Watchlist ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/quotes")
async def websocket_quotes(ws: WebSocket, token: str = Query("")):
    """WebSocket endpoint for real-time quote streaming.

    Connect: ws://host:8088/ws/quotes?token=<jwt>
    Receives: {"type":"quotes","data":{symbol:{price,...}},"states":{symbol:state},"ts":epoch}
    Sends:   {"type":"subscribe","symbols":["AAPL","00700.HK"]}
    """
    # Authenticate via query param token
    from api.database import SessionLocal, UserDB
    from api.services import auth_service
    db = SessionLocal()
    try:
        payload = auth_service.decode_access_token(token)
        if not payload:
            await ws.close(code=4001, reason="Invalid token")
            return
        user_id = payload.get("sub", "")
        user = db.query(UserDB).filter(UserDB.id == user_id).first()
        if not user:
            await ws.close(code=4001, reason="User not found")
            return
    finally:
        db.close()

    await quote_ws_manager.connect(user_id, ws)
    try:
        while True:
            msg = await ws.receive_json()
            logger.info("[quote-ws] received from %s: %s", user_id[:8], msg.get("type", "?"))
            if msg.get("type") == "subscribe":
                symbols = msg.get("symbols", [])
                quote_ws_manager.update_symbols(symbols)
    except WebSocketDisconnect:
        quote_ws_manager.disconnect(user_id)
    except Exception as exc:
        logger.warning("[quote-ws] error for %s: %s", user_id[:8], exc)
        quote_ws_manager.disconnect(user_id)


@app.get("/v1/watchlist")
def list_watchlist(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    items = watchlist_service.list_watchlist(db, current_user.id)
    _attach_stock_names(items, _get_reverse_stock_map())
    return {"items": items}


@app.get("/v1/dashboard/watchlist-board")
def get_dashboard_watchlist_board(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    return watchlist_board_service.get_watchlist_board(db, current_user.id)


@app.post("/v1/watchlist")
def add_to_watchlist(
    body: WatchlistAddRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    text = str(body.text or body.symbol or "").strip()
    if not text:
        raise HTTPException(400, "text or symbol is required")

    tokens = _split_watchlist_batch_text(text)
    if not tokens:
        raise HTTPException(400, "至少提供一个股票代码或名称")

    resolved_entries: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    for idx, token in enumerate(tokens):
        symbol, name, error = _resolve_watchlist_identifier(token)
        if error:
            results.append({
                "_order": idx,
                "input": token,
                "status": "invalid",
                "message": error,
            })
            continue
        resolved_entries.append({
            "_order": idx,
            "input": token,
            "symbol": symbol,
            "name": name,
        })

    add_results = watchlist_service.add_watchlist_items(
        db,
        current_user.id,
        [entry["symbol"] for entry in resolved_entries],
    )
    for entry, result in zip(resolved_entries, add_results):
        item = result.get("item")
        if item:
            item["name"] = entry["name"]
            item["has_scheduled"] = False
        results.append({
            "_order": entry["_order"],
            "input": entry["input"],
            "symbol": entry["symbol"],
            "name": entry["name"],
            "status": result["status"],
            "message": result["message"],
            "item": item,
        })

    results.sort(key=lambda row: row["_order"])
    for row in results:
        row.pop("_order", None)
    summary = {
        "total": len(tokens),
        "added": sum(1 for row in results if row["status"] == "added"),
        "duplicate": sum(1 for row in results if row["status"] == "duplicate"),
        "failed": sum(1 for row in results if row["status"] in {"invalid", "failed"}),
    }
    message_parts = [f"共处理 {summary['total']} 项"]
    if summary["added"]:
        message_parts.append(f"新增 {summary['added']} 项")
    if summary["duplicate"]:
        message_parts.append(f"重复 {summary['duplicate']} 项")
    if summary["failed"]:
        message_parts.append(f"失败 {summary['failed']} 项")
    return {
        "message": "，".join(message_parts),
        "summary": summary,
        "results": results,
    }


@app.delete("/v1/watchlist/{item_id}", status_code=204)
def delete_from_watchlist(
    item_id: str,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    if not watchlist_service.delete_watchlist_item(db, current_user.id, item_id):
        raise HTTPException(404, "未找到该自选股")


# ── Scheduled Analysis ────────────────────────────────────────────────────────

@app.get("/v1/scheduled")
def list_scheduled_analyses(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    items = scheduled_service.list_scheduled(db, current_user.id)
    _attach_stock_names(items, _get_reverse_stock_map_cached_only())
    return {"items": _annotate_scheduled_with_imported_context(items, db, current_user.id)}


@app.get("/v1/portfolio/overview", response_model=PortfolioOverviewResponse)
def get_portfolio_overview(
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    code_to_name = _get_reverse_stock_map_cached_only()

    watchlist_items = watchlist_service.list_watchlist(db, current_user.id)
    _attach_stock_names(watchlist_items, code_to_name)

    scheduled_items = scheduled_service.list_scheduled(db, current_user.id)
    _attach_stock_names(scheduled_items, code_to_name)
    scheduled_items = _annotate_scheduled_with_imported_context(scheduled_items, db, current_user.id)

    latest_reports = report_service.get_latest_reports_by_symbols(
        db=db,
        user_id=current_user.id,
        symbols=[item["symbol"] for item in watchlist_items],
    )
    for report in latest_reports:
        report.name = code_to_name.get(report.symbol, report.symbol)

    portfolio_import = portfolio_import_service.get_import_state(db, current_user.id)

    return {
        "watchlist": watchlist_items,
        "scheduled": scheduled_items,
        "latest_reports": latest_reports,
        "portfolio_import": portfolio_import,
    }


@app.post("/v1/scheduled", status_code=201)
def create_scheduled_analysis(
    body: dict,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    symbol = body.get("symbol", "").strip().upper()
    horizon = body.get("horizon", "short")
    trigger_time = body.get("trigger_time", "20:00")
    if not symbol:
        raise HTTPException(400, "symbol is required")
    code_to_name = _get_reverse_stock_map()
    if symbol not in code_to_name:
        raise HTTPException(400, f"未知的股票代码: {symbol}")
    try:
        item = scheduled_service.create_scheduled(db, current_user.id, symbol, horizon, trigger_time)
        item["name"] = code_to_name.get(symbol, symbol)
        _annotate_scheduled_with_imported_context([item], db, current_user.id)
        return item
    except ValueError as e:
        raise HTTPException(400, str(e))


def _extract_scheduled_update_kwargs(body: dict) -> dict:
    kwargs = {}
    if "is_active" in body:
        kwargs["is_active"] = bool(body["is_active"])
    if "horizon" in body:
        kwargs["horizon"] = body["horizon"]
    if "trigger_time" in body:
        kwargs["trigger_time"] = body["trigger_time"]
    return kwargs


@app.patch("/v1/scheduled/batch")
def batch_update_scheduled_analyses(
    body: ScheduledBatchUpdateRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    kwargs = _extract_scheduled_update_kwargs(body.model_dump(exclude_unset=True))
    if not kwargs:
        raise HTTPException(400, "至少提供一个更新字段")
    try:
        items = scheduled_service.batch_update_scheduled(
            db,
            current_user.id,
            body.item_ids,
            **kwargs,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    code_to_name = _get_reverse_stock_map()
    for item in items:
        item["name"] = code_to_name.get(item["symbol"], item["symbol"])
    return {"items": _annotate_scheduled_with_imported_context(items, db, current_user.id)}


@app.post("/v1/scheduled/batch/delete")
def batch_delete_scheduled_analyses(
    body: ScheduledBatchIdsRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    try:
        return scheduled_service.batch_delete_scheduled(db, current_user.id, body.item_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/v1/scheduled/batch/trigger", response_model=BatchScheduledTriggerResponse)
async def trigger_scheduled_analyses_batch(
    body: ScheduledBatchIdsRequest,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    if not body.item_ids:
        raise HTTPException(400, "请至少选择 1 个定时任务")

    requested_trade_date = market_today_str("HK")
    actual_trade_date = _resolve_scheduled_trade_date(requested_trade_date)
    code_to_name = _get_reverse_stock_map()
    jobs: List[Dict[str, Any]] = []
    with_position_context = 0
    available_tasks = {
        task["id"]: task
        for task in scheduled_service.list_scheduled(db, current_user.id)
    }
    valid_item_ids = []
    missing_item_ids = []
    for raw_item_id in body.item_ids:
        item_id = str(raw_item_id or "").strip()
        if not item_id:
            continue
        if item_id in available_tasks:
            valid_item_ids.append(item_id)
        else:
            missing_item_ids.append(item_id)

    if not valid_item_ids:
        raise HTTPException(400, "选中的定时任务已失效，请刷新页面后重试")

    if missing_item_ids:
        _log(
            f"[Scheduled Batch Trigger] user={current_user.id} skipped missing item_ids={missing_item_ids}"
        )

    for item_id in valid_item_ids:
        task = available_tasks[item_id]

        task_snapshot = dict(task)
        task_snapshot["user_id"] = current_user.id
        task_snapshot["manual_user_context"] = _build_manual_imported_user_context(db, current_user.id, task["symbol"])

        scheduled_user_context = task_snapshot["manual_user_context"]
        if scheduled_user_context.get("current_position") is not None:
            with_position_context += 1

        now = _utcnow_iso()
        job_id = uuid4().hex
        _set_job(
            job_id,
            job_id=job_id,
            status="pending",
            created_at=now,
            symbol=task["symbol"],
            trade_date=actual_trade_date,
            user_id=current_user.id,
            request_source="scheduled_manual_batch",
        )
        _emit_job_event(
            job_id,
            "job.queued",
            {"job_id": job_id, "symbol": task["symbol"], "trade_date": actual_trade_date},
        )
        _create_tracked_task(
            _run_manual_trigger(
                task_snapshot,
                requested_trade_date,
                job_id,
            )
        )

        jobs.append({
            "item_id": task["id"],
            "job_id": job_id,
            "symbol": task["symbol"],
            "name": code_to_name.get(task["symbol"], task["symbol"]),
            "status": "pending",
            "created_at": now,
            "current_position": scheduled_user_context.get("current_position"),
            "average_cost": scheduled_user_context.get("average_cost"),
        })

    return {
        "summary": {
            "total": len(jobs),
            "with_position_context": with_position_context,
        },
        "jobs": jobs,
    }


@app.post("/v1/scheduled/{item_id}/trigger", response_model=AnalyzeResponse)
async def trigger_scheduled_analysis_once(
    item_id: str,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    task = scheduled_service.get_scheduled(db, current_user.id, item_id)
    if task is None:
        raise HTTPException(404, "未找到该定时任务")

    requested_trade_date = market_today_str("HK")
    actual_trade_date = _resolve_scheduled_trade_date(requested_trade_date)
    now = _utcnow_iso()
    job_id = uuid4().hex

    task_snapshot = dict(task)
    task_snapshot["user_id"] = current_user.id
    task_snapshot["manual_user_context"] = _build_manual_imported_user_context(db, current_user.id, task["symbol"])

    _set_job(
        job_id,
        job_id=job_id,
        status="pending",
        created_at=now,
        symbol=task["symbol"],
        trade_date=actual_trade_date,
        user_id=current_user.id,
        request_source="scheduled_manual",
    )
    _emit_job_event(
        job_id,
        "job.queued",
        {"job_id": job_id, "symbol": task["symbol"], "trade_date": actual_trade_date},
    )
    _create_tracked_task(
        _run_manual_trigger(
            task_snapshot,
            requested_trade_date,
            job_id,
        )
    )
    return AnalyzeResponse(job_id=job_id, status="pending", created_at=now)


@app.patch("/v1/scheduled/{item_id}")
def update_scheduled_analysis(
    item_id: str,
    body: dict,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    kwargs = _extract_scheduled_update_kwargs(body)
    try:
        result = scheduled_service.update_scheduled(db, current_user.id, item_id, **kwargs)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if result is None:
        raise HTTPException(404, "未找到该定时任务")
    code_to_name = _get_reverse_stock_map()
    result["name"] = code_to_name.get(result["symbol"], result["symbol"])
    _annotate_scheduled_with_imported_context([result], db, current_user.id)
    return result


@app.delete("/v1/scheduled/{item_id}", status_code=204)
def delete_scheduled_analysis(
    item_id: str,
    current_user: UserDB = Depends(_require_api_user),
    db: Session = Depends(get_db),
):
    if not scheduled_service.delete_scheduled(db, current_user.id, item_id):
        raise HTTPException(404, "未找到该定时任务")


# ─── Sponsor endpoints (public, no auth) ────────────────────────────────────


class SponsorItem(BaseModel):
    id: str
    sponsor_type: str
    name: str
    github: Optional[str] = None
    avatar: Optional[str] = None
    email: Optional[str] = None
    provider: Optional[str] = None
    date: str
    # NOTE: amount is intentionally excluded from the public API


class SponsorsResponse(BaseModel):
    money: List[SponsorItem]
    token: List[SponsorItem]


def _sponsor_to_item(s: SponsorDB) -> SponsorItem:
    return SponsorItem(
        id=s.id,
        sponsor_type=s.sponsor_type,
        name=s.name,
        github=s.github,
        avatar=s.avatar,
        email=s.email,
        provider=s.provider,
        date=s.date,
    )


@app.get("/v1/sponsors", response_model=SponsorsResponse)
def list_sponsors(db: Session = Depends(get_db)):
    """Public endpoint: list all visible sponsors grouped by type."""
    all_sponsors = sponsor_service.list_sponsors(db)
    money = [_sponsor_to_item(s) for s in all_sponsors if s.sponsor_type == "money"]
    token = [_sponsor_to_item(s) for s in all_sponsors if s.sponsor_type == "token"]
    return SponsorsResponse(money=money, token=token)


# ─── Feedback endpoints ─────────────────────────────────────────────────────


class FeedbackCreateRequest(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=5000)


class FeedbackItem(BaseModel):
    id: str
    user_email: str
    subject: str
    content: str
    admin_reply: Optional[str] = None
    replied_at: Optional[datetime] = None
    is_read: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_serializer("replied_at", "created_at", "updated_at")
    def serialize_dt(self, v: Optional[datetime], _info: Any) -> Optional[str]:
        return v.isoformat() if v else None


class FeedbackListResponse(BaseModel):
    total: int
    feedbacks: List[FeedbackItem]


class FeedbackUnreadResponse(BaseModel):
    unread_count: int


def _fb_to_item(fb: FeedbackDB) -> FeedbackItem:
    return FeedbackItem(
        id=fb.id,
        user_email=fb.user_email,
        subject=fb.subject,
        content=fb.content,
        admin_reply=fb.admin_reply,
        replied_at=fb.replied_at,
        is_read=fb.is_read,
        created_at=fb.created_at,
        updated_at=fb.updated_at,
    )


@app.post("/v1/feedbacks", response_model=FeedbackItem, status_code=201)
def create_feedback(
    req: FeedbackCreateRequest,
    current_user: UserDB = Depends(_require_web_user),
    db: Session = Depends(get_db),
):
    fb = feedback_service.create_feedback(db, current_user, req.subject, req.content)
    return _fb_to_item(fb)


@app.get("/v1/feedbacks", response_model=FeedbackListResponse)
def list_feedbacks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: UserDB = Depends(_require_web_user),
    db: Session = Depends(get_db),
):
    items, total = feedback_service.list_feedbacks(db, current_user.id, page, page_size)
    return FeedbackListResponse(total=total, feedbacks=[_fb_to_item(fb) for fb in items])


@app.get("/v1/feedbacks/unread-count", response_model=FeedbackUnreadResponse)
def feedback_unread_count(
    current_user: UserDB = Depends(_require_web_user),
    db: Session = Depends(get_db),
):
    count = feedback_service.unread_count(db, current_user.id)
    return FeedbackUnreadResponse(unread_count=count)


@app.get("/v1/feedbacks/{feedback_id}", response_model=FeedbackItem)
def get_feedback(
    feedback_id: str,
    current_user: UserDB = Depends(_require_web_user),
    db: Session = Depends(get_db),
):
    fb = feedback_service.get_feedback(db, feedback_id)
    if not fb or fb.user_id != current_user.id:
        raise HTTPException(404, "未找到该反馈")
    # auto mark read
    if not fb.is_read and fb.admin_reply:
        feedback_service.mark_read(db, feedback_id, current_user.id)
        fb.is_read = True
    return _fb_to_item(fb)


@app.post("/v1/feedbacks/{feedback_id}/read")
def mark_feedback_read(
    feedback_id: str,
    current_user: UserDB = Depends(_require_web_user),
    db: Session = Depends(get_db),
):
    fb = feedback_service.mark_read(db, feedback_id, current_user.id)
    if not fb:
        raise HTTPException(404, "未找到该反馈")
    return {"ok": True}


# ─── Simulated Trading API Endpoints (Phase 5) ──────────────────────────────

from api.services.sim_trading_service import (
    get_account as _sim_get_account,
    get_positions as _sim_get_positions,
    place_order as _sim_place_order,
    cancel_order as _sim_cancel_order,
    modify_order as _sim_modify_order,
    get_orders as _sim_get_orders,
    get_deals as _sim_get_deals,
    execute_signal as _sim_execute_signal,
    get_acc_list as _sim_get_acc_list,
    get_trading_info as _sim_get_trading_info,
    get_history_orders as _sim_get_history_orders,
    SignalInput as _SignalInput,
    account_to_dict as _account_to_dict,
    position_to_dict as _position_to_dict,
    order_result_to_dict as _order_result_to_dict,
    order_info_to_dict as _order_info_to_dict,
    deal_info_to_dict as _deal_info_to_dict,
    signal_result_to_dict as _signal_result_to_dict,
)


class SimOrderRequest(BaseModel):
    """Request body for placing a simulated order."""
    symbol: str = Field(..., description="股票代码，如 AAPL、00700.HK、NVDA.US")
    side: str = Field(..., description="交易方向: BUY 或 SELL")
    quantity: float = Field(..., gt=0, description="订单数量")
    price: float = Field(0, ge=0, description="订单价格，市价单传 0")
    order_type: str = Field("NORMAL", description="订单类型: NORMAL/MARKET/AUCTION_LIMIT/AUCTION_MARKET")
    remark: Optional[str] = Field(None, description="订单备注")


class SimSignalRequest(BaseModel):
    """Request body for executing a trading signal."""
    symbol: str = Field(..., description="股票代码")
    signal: str = Field(..., description="信号: buy/sell/hold")
    confidence: float = Field(..., ge=0, le=1, description="置信度 0-1")
    target_price: Optional[float] = Field(None, description="目标价")
    stop_loss_price: Optional[float] = Field(None, description="止损价")
    max_position_pct: float = Field(0.25, ge=0, le=1, description="最大仓位比例")
    use_kelly: bool = Field(False, description="是否使用 Kelly 公式计算仓位")


class SimReflectRequest(BaseModel):
    """Request body for reflecting on a simulated trade."""
    trade_info: Dict[str, Any] = Field(
        ...,
        description=(
            "Original trade signal info. Keys: symbol, signal, confidence, "
            "price, quantity, reasoning (optional)."
        ),
    )
    trade_result: Dict[str, Any] = Field(
        ...,
        description=(
            "Execution result from SimExecutor/SimTradingService. Keys: "
            "action_taken, order_id, pnl (optional), outcome (optional), "
            "kelly_fraction (optional), reason (optional)."
        ),
    )


@app.get("/v1/sim/account")
def sim_account(current_user: UserDB = Depends(_require_api_user)) -> Dict[str, Any]:
    """查询模拟交易账户资金信息。"""
    try:
        info = _sim_get_account("SIMULATE")
        return {"ok": True, "data": _account_to_dict(info)}
    except Exception as e:
        logger.error(f"sim_account error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/v1/sim/accounts")
def sim_all_accounts(current_user: UserDB = Depends(_require_api_user)) -> Dict[str, Any]:
    """查询所有模拟交易账户（港股/美股）。"""
    accounts = []
    for market in ["HK", "US"]:
        try:
            info = _sim_get_account("SIMULATE", trd_market=market)
            accounts.append(_account_to_dict(info))
        except Exception as e:
            logger.warning(f"sim_all_accounts {market} error: {e}")
            accounts.append({
                "market": market,
                "error": str(e),
                "total_assets": 0,
                "cash_balance": 0,
                "market_val": 0,
                "currency": "HKD" if market == "HK" else "USD",
            })
    return {"ok": True, "data": accounts}


@app.get("/v1/real/accounts")
def real_all_accounts(current_user: UserDB = Depends(_require_api_user)) -> Dict[str, Any]:
    """Query all real trading accounts (HK + US)."""
    from api.services.real_account_service import get_all_real_accounts, account_to_dict
    try:
        accounts = get_all_real_accounts()
        return {"ok": True, "data": [account_to_dict(a) for a in accounts]}
    except Exception as e:
        logger.error(f"real_all_accounts error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/v1/sim/positions")
def sim_positions(
    trd_market: str = Query("HK", description="市场 HK/US"),
    page_size: int = Query(100, ge=1, le=1000),
    page_index: int = Query(0, ge=0),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """查询模拟交易持仓列表。"""
    try:
        positions = _sim_get_positions(
            "SIMULATE", trd_market=trd_market, page_size=page_size, page_index=page_index
        )
        return {
            "ok": True,
            "data": [_position_to_dict(p) for p in positions],
            "total": len(positions),
        }
    except Exception as e:
        logger.error(f"sim_positions error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/v1/sim/order")
def sim_place_order(
    req: SimOrderRequest,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """模拟下单。"""
    try:
        result = _sim_place_order(
            symbol=req.symbol,
            side=req.side,
            quantity=req.quantity,
            price=req.price,
            order_type=req.order_type,
            trd_env="SIMULATE",
            remark=req.remark,
        )
        return {"ok": True, "data": _order_result_to_dict(result)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"sim_place_order error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/v1/sim/order/{order_id}")
def sim_cancel_order(
    order_id: str,
    symbol: str = Query(..., description="股票代码，用于确定市场上下文"),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """撤销模拟订单。"""
    try:
        _sim_cancel_order(order_id, symbol, "SIMULATE")
        return {"ok": True, "message": f"Order {order_id} cancelled"}
    except Exception as e:
        logger.error(f"sim_cancel_order error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.put("/v1/sim/order/{order_id}")
def sim_modify_order(
    order_id: str,
    symbol: str = Query(..., description="Stock code"),
    price: float = Query(..., description="New price"),
    qty: float = Query(..., description="New quantity"),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """Modify a simulated order (price/quantity)."""
    try:
        _sim_modify_order(order_id, symbol, price, qty, "SIMULATE")
        return {"ok": True, "message": f"Order {order_id} modified"}
    except Exception as e:
        logger.error(f"sim_modify_order error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/v1/sim/orders")
def sim_orders(
    symbol: Optional[str] = Query(None, description="股票代码筛选"),
    trd_market: str = Query("HK", description="市场 HK/US"),
    page_size: int = Query(100, ge=1, le=1000),
    page_index: int = Query(0, ge=0),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """查询模拟交易订单列表。"""
    try:
        orders = _sim_get_orders(
            symbol=symbol,
            trd_env="SIMULATE",
            trd_market=trd_market,
            page_size=page_size,
            page_index=page_index,
        )
        return {
            "ok": True,
            "data": [_order_info_to_dict(o) for o in orders],
            "total": len(orders),
        }
    except Exception as e:
        logger.error(f"sim_orders error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/v1/sim/deals")
def sim_deals(
    symbol: Optional[str] = Query(None, description="股票代码筛选"),
    page_size: int = Query(100, ge=1, le=1000),
    page_index: int = Query(0, ge=0),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """查询模拟交易成交记录。"""
    try:
        deals = _sim_get_deals(
            symbol=symbol,
            trd_env="SIMULATE",
            page_size=page_size,
            page_index=page_index,
        )
        return {
            "ok": True,
            "data": [_deal_info_to_dict(d) for d in deals],
            "total": len(deals),
        }
    except Exception as e:
        logger.error(f"sim_deals error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/v1/sim/acc-list")
def sim_acc_list(
    trd_market: str = Query("HK", description="HK or US"),
    current_user: UserDB = Depends(_require_api_user),
):
    """Get list of trading accounts."""
    try:
        data = _sim_get_acc_list(trd_market=trd_market)
        return {"ok": True, "data": data}
    except Exception as e:
        logger.error(f"sim_acc_list error: {e}")
        raise HTTPException(500, detail=str(e))


@app.get("/v1/sim/trading-info")
def sim_trading_info(
    symbol: str = Query(..., description="Stock symbol"),
    price: float = Query(..., description="Order price"),
    order_type: str = Query("NORMAL", description="Order type"),
    current_user: UserDB = Depends(_require_api_user),
):
    """Query max buy/sell quantity before placing an order."""
    try:
        data = _sim_get_trading_info(symbol, price, order_type)
        return {"ok": True, "data": data}
    except Exception as e:
        logger.error(f"sim_trading_info error: {e}")
        raise HTTPException(500, detail=str(e))


@app.get("/v1/sim/history-orders")
def sim_history_orders(
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    start: str = Query("", description="Start time YYYY-MM-DD HH:MM:SS"),
    end: str = Query("", description="End time YYYY-MM-DD HH:MM:SS"),
    current_user: UserDB = Depends(_require_api_user),
):
    """Query historical orders (not limited to today)."""
    try:
        data = _sim_get_history_orders(symbol=symbol, start=start, end=end)
        return {"ok": True, "data": data, "total": len(data)}
    except Exception as e:
        logger.error(f"sim_history_orders error: {e}")
        raise HTTPException(500, detail=str(e))


@app.post("/v1/sim/signal")
def sim_execute_signal(
    req: SimSignalRequest,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """根据 Agent 分析信号自动下单。"""
    try:
        signal = _SignalInput(
            symbol=req.symbol,
            signal=req.signal,
            confidence=req.confidence,
            target_price=req.target_price,
            stop_loss_price=req.stop_loss_price,
            max_position_pct=req.max_position_pct,
            use_kelly=req.use_kelly,
        )
        result = _sim_execute_signal(signal)
        return {"ok": True, "data": _signal_result_to_dict(result)}
    except Exception as e:
        logger.error(f"sim_execute_signal error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/v1/sim/performance")
def sim_performance(
    current_user: UserDB = Depends(_require_web_user),
) -> Dict[str, Any]:
    """Return quantitative performance metrics for the simulated account.

    Computes max drawdown, Sharpe ratio, Sortino ratio, win rate, and Calmar
    ratio from the account's deal history by matching buy/sell pairs (FIFO).
    Returns a JSON object with all metrics.
    """
    try:
        deals = _sim_get_deals(trd_env="SIMULATE")
        if not deals:
            return {
                "ok": True,
                "data": {
                    "max_drawdown": 0.0,
                    "sharpe_ratio": 0.0,
                    "sortino_ratio": 0.0,
                    "win_rate": 0.0,
                    "calmar_ratio": 0.0,
                    "trade_count": 0,
                },
            }

        # ── Match buy/sell pairs per symbol (FIFO) ──────────────────────
        from collections import defaultdict, deque

        # Group buys by symbol; sells consume the earliest buy
        buy_queue: dict[str, deque] = defaultdict(deque)  # symbol -> deque[(price, qty)]
        trade_returns: list[float] = []

        for d in deals:
            side = d.side.lower() if hasattr(d, "side") else ""
            price = d.price if hasattr(d, "price") else 0.0
            qty = d.qty if hasattr(d, "qty") else 0.0
            code = d.code if hasattr(d, "code") else ""

            if side == "buy":
                buy_queue[code].append((price, qty))
            elif side == "sell" and buy_queue.get(code):
                # Match against earliest buy (FIFO)
                remaining = qty
                while remaining > 0 and buy_queue[code]:
                    buy_price, buy_qty = buy_queue[code][0]
                    matched = min(remaining, buy_qty)
                    if buy_price > 0:
                        trade_returns.append(
                            (price - buy_price) / buy_price
                        )
                    remaining -= matched
                    if matched >= buy_qty:
                        buy_queue[code].popleft()
                    else:
                        buy_queue[code][0] = (buy_price, buy_qty - matched)

        # ── Build equity curve from cumulative returns ──────────────────
        base_capital = 100_000.0
        equity_curve: list[float] = [base_capital]
        for r in trade_returns:
            equity_curve.append(equity_curve[-1] * (1.0 + r))

        qm = QuantMetrics()
        n = len(trade_returns)

        data = {
            "max_drawdown": round(qm.max_drawdown(equity_curve), 6) if len(equity_curve) >= 2 else 0.0,
            "sharpe_ratio": round(qm.sharpe_ratio(trade_returns), 4) if n >= 2 else 0.0,
            "sortino_ratio": round(qm.sortino_ratio(trade_returns), 4) if n >= 2 else 0.0,
            "win_rate": round(qm.win_rate(trade_returns), 4) if n > 0 else 0.0,
            "calmar_ratio": round(qm.calmar_ratio(equity_curve), 4) if len(equity_curve) >= 2 else 0.0,
            "trade_count": n,
        }
        return {"ok": True, "data": data}
    except Exception as e:
        logger.error(f"sim_performance error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


# ── Sim Trade Reflection endpoint (Phase 7.8) ──────────────────────────────

# Module-level cached SimTradeReflector instance (preserves BM25 memory across requests)
_sim_reflector_instance = None


def _get_sim_reflector():
    """Lazy-initialize a SimTradeReflector with the user's configured LLM.

    Returns a SimTradeReflector instance, or raises if LLM config is unavailable.
    """
    global _sim_reflector_instance
    if _sim_reflector_instance is not None:
        return _sim_reflector_instance

    from tradingagents.graph.reflection import SimTradeReflector
    from tradingagents.llm_clients.factory import create_llm_client

    config = _build_runtime_config({})
    provider = config.get("llm_provider", "openai")
    model = config.get("quick_think_llm")
    base_url = config.get("backend_url")
    api_key = config.get("api_key")

    if not model:
        raise ValueError("No LLM model configured (quick_think_llm is empty)")

    client = create_llm_client(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
    )
    llm = client.get_llm()

    _sim_reflector_instance = SimTradeReflector(llm)
    return _sim_reflector_instance


@app.post("/v1/sim/reflect")
def sim_reflect(
    req: SimReflectRequest,
    current_user: UserDB = Depends(_require_web_user),
) -> Dict[str, Any]:
    """Reflect on a completed simulated trade and store lessons in BM25 memory.

    Accepts trade_info (original signal) and trade_result (execution outcome),
    invokes the LLM to generate structured reflection, and stores the lessons
    in BM25 memory for future retrieval.
    """
    try:
        reflector = _get_sim_reflector()
        lesson_text = reflector.reflect_on_sim_trade(req.trade_info, req.trade_result)

        return {
            "ok": True,
            "data": {
                "lesson": lesson_text,
                "memory_count": len(reflector.memory.documents),
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"sim_reflect error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/v1/sim/reflections")
def sim_reflections(
    current_user: UserDB = Depends(_require_web_user),
) -> Dict[str, Any]:
    """List all stored sim trade reflections (lessons) from BM25 memory.

    Returns the paired (situation, recommendation) entries that were
    generated by the SimTradeReflector after each trade reflection.
    """
    try:
        reflector = _get_sim_reflector()
        memory = reflector.memory
        entries = []
        for i, (doc, rec) in enumerate(zip(memory.documents, memory.recommendations)):
            entries.append({
                "id": f"reflection_{i}",
                "situation": doc,
                "lesson": rec,
            })
        return {"ok": True, "data": entries}
    except ValueError:
        # Reflector not initialized (no LLM configured) — return empty
        return {"ok": True, "data": []}
    except Exception as e:
        logger.error(f"sim_reflections error: {e}")
        return {"ok": True, "data": []}


from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# ─── Static Files & SPA Routing ──────────────────────────────────────────────

# Serve uploaded files (avatars etc.) from shared uploads directory
_uploads_dir = Path(os.getenv("UPLOAD_DIR", str(Path(__file__).parent.parent / "uploads")))


# ── Autonomous Orchestrator endpoints (Phase 8) ────────────────────────────

from api.services import autonomous_service


class AutonomousCreateRequest(BaseModel):
    """Request to create an autonomous trading task."""
    command: str = Field(..., description="Natural language trading command")
    budget: Optional[float] = Field(None, description="Total capital override")
    currency: str = Field("USD", description="Currency code")
    mode: str = Field("simulate", description="Trading mode (simulate only)")
    max_iterations: int = Field(30, description="Max OODA loop iterations")
    fixed_symbols: Optional[List[str]] = Field(None, description="Fixed stock pool")


@app.post("/v1/autonomous/create")
async def autonomous_create(
    req: AutonomousCreateRequest,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """Create and start a new autonomous trading task.

    Parses the natural language command into a task DAG, then starts
    the OODA (Observe-Orient-Decide-Act) autonomous loop.
    """
    try:
        result = autonomous_service.create_task(
            command=req.command,
            budget=req.budget,
            currency=req.currency,
            mode=req.mode,
            max_iterations=req.max_iterations,
            fixed_symbols=req.fixed_symbols,
        )
        return {"ok": True, "data": result}
    except Exception as e:
        logger.error(f"autonomous_create error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/autonomous/{task_id}")
async def autonomous_get_status(
    task_id: str,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """Get status of an autonomous trading task.

    Returns full task details including current OODA phase,
    progress, checkpoint data, and any position alerts.
    """
    result = autonomous_service.get_task(task_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return {"ok": True, "data": result}


@app.post("/v1/autonomous/{task_id}/pause")
async def autonomous_pause(
    task_id: str,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """Pause a running autonomous task.

    The task can be resumed later from the last checkpoint.
    """
    result = autonomous_service.pause_task(task_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {"ok": True, "data": result}


@app.post("/v1/autonomous/{task_id}/resume")
async def autonomous_resume(
    task_id: str,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """Resume a paused autonomous task.

    Continues from the last checkpoint in the OODA loop.
    """
    result = autonomous_service.resume_task(task_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {"ok": True, "data": result}


@app.post("/v1/autonomous/{task_id}/stop")
async def autonomous_stop(
    task_id: str,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """Stop an autonomous task permanently.

    The task cannot be resumed after stopping.
    """
    result = autonomous_service.stop_task(task_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {"ok": True, "data": result}


@app.get("/v1/autonomous/{task_id}/alerts")
async def autonomous_alerts(
    task_id: str,
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """Get position alerts for an autonomous task.

    Returns alerts generated by the Observer (stop-loss, take-profit,
    trailing stop, concentration warnings).
    """
    alerts = autonomous_service.get_task_alerts(task_id)
    return {"ok": True, "data": {"alerts": alerts}}


@app.get("/v1/autonomous")
async def autonomous_list(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, description="Max results"),
    offset: int = Query(0, description="Pagination offset"),
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """List autonomous trading tasks.

    Filter by status (pending/running/paused/completed/failed)
    or list all tasks.
    """
    result = autonomous_service.list_tasks(
        status=status, limit=limit, offset=offset,
    )
    return {"ok": True, "data": result}


# ── Skills / Strategies API (Phase 9) ──────────────────────────────────────

@app.get("/v1/skills")
def list_skills(
    current_user: UserDB = Depends(_require_api_user),
) -> Dict[str, Any]:
    """List all registered trading strategy skills.

    Returns skill name, description, regime, and enabled status
    from the SkillRegistry.
    """
    try:
        from tradingagents.skills.skill_registry import SkillRegistry
        registry = SkillRegistry()
        registry.discover()
        all_skills = registry.list_all()
        return {
            "ok": True,
            "data": [
                {
                    "name": s.name,
                    "display_name": s.name.replace("_", " ").title(),
                    "description": s.description,
                    "regime": [r.value for r in s.supported_regimes],
                    "enabled": True,
                }
                for s in all_skills
            ],
        }
    except Exception as e:
        logger.error(f"list_skills error: {e}")
        return {"ok": True, "data": []}


# ── Bot platform webhook endpoints ───────────────────────────────────────────

@app.get("/v1/bots/status")
async def bots_status():
    """Return status of all registered bot platforms."""
    if not _bot_manager:
        return {"bots": [], "message": "Bot manager not initialized"}
    return {
        "bots": [
            {
                "name": name,
                "platform": bot.platform_type.value,
                "running": bot._running,
            }
            for name, bot in _bot_manager.bots.items()
        ],
        "active_count": len(_bot_manager.list_active()),
    }


@app.post("/v1/bots/dingtalk/webhook")
async def dingtalk_webhook(request: Request):
    """Webhook endpoint for DingTalk bot callbacks (webhook mode)."""
    if not _bot_manager:
        return {"error": "Bot manager not initialized"}
    bot = _bot_manager.get_bot("dingtalk")
    if not bot:
        return {"error": "DingTalk bot not registered"}
    payload = await request.json()
    from api.services.bot.dingtalk import DingTalkBot
    assert isinstance(bot, DingTalkBot)
    response = await bot.handle_webhook_callback(payload)
    if response:
        return {"msgtype": "text", "text": {"content": response.text}}
    return {"msgtype": "text", "text": {"content": ""}}


@app.post("/v1/bots/telegram/webhook")
async def telegram_webhook(request: Request):
    """Webhook endpoint for Telegram bot callbacks (webhook mode)."""
    if not _bot_manager:
        return {"ok": False}
    bot = _bot_manager.get_bot("telegram")
    if not bot:
        return {"ok": False, "error": "Telegram bot not registered"}
    payload = await request.json()
    from api.services.bot.telegram import TelegramBot
    assert isinstance(bot, TelegramBot)
    await bot.handle_webhook_update(payload)
    return {"ok": True}


@app.post("/v1/bots/feishu/webhook")
async def feishu_webhook(request: Request):
    """Webhook endpoint for Feishu bot event callbacks."""
    if not _bot_manager:
        return {"error": "Bot manager not initialized"}
    bot = _bot_manager.get_bot("feishu")
    if not bot:
        return {"error": "Feishu bot not registered"}
    payload = await request.json()

    # Handle Feishu URL verification challenge
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}

    return {"code": 0, "msg": "ok"}


if _uploads_dir.is_dir():
    app.mount("/uploads", StaticFiles(directory=str(_uploads_dir)), name="uploads")

# Mount frontend if dist exists
dist_path = os.path.join(os.getcwd(), "frontend/dist")
if os.path.exists(dist_path):
    app.mount("/assets", StaticFiles(directory=os.path.join(dist_path, "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        # 1. Define and resolve the absolute safe root
        base_path = os.path.realpath(dist_path)
        
        # 2. Resolve the requested path (handling .. and symlinks)
        # We lstrip("/") to prevent os.path.join from treating it as an absolute path
        fullpath = os.path.realpath(os.path.join(base_path, full_path.lstrip("/")))
        
        # 3. Security Check: The normalized path must start with the base_path
        if not fullpath.startswith(base_path):
            return FileResponse(os.path.join(base_path, "index.html"))
            
        # 4. Final check: if it's a valid file, serve it
        if os.path.isfile(fullpath):
            return FileResponse(fullpath)
            
        # Otherwise fallback to index.html for SPA routing
        return FileResponse(os.path.join(base_path, "index.html"))


def run() -> None:
    import uvicorn
    from pathlib import Path

    log_config = str(Path(__file__).parent / "logging_config.yaml")
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=False, log_config=log_config)
