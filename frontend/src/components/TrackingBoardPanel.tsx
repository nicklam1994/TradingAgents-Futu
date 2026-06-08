import {
    ArrowDownRight,
    ArrowUpRight,
    Loader2,
    RefreshCw,
    ShieldAlert,
    Target,
    TrendingUp,
    Wallet,
} from 'lucide-react'
import { type CSSProperties, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '@/services/api'
import { useAuthStore } from '@/stores/authStore'
import type { TrackingBoardItem, TrackingBoardResponse } from '@/types'

const CLAMP_TWO_LINES_STYLE: CSSProperties = {
    display: '-webkit-box',
    WebkitLineClamp: 2,
    WebkitBoxOrient: 'vertical',
    overflow: 'hidden',
}

type BoardViewMode = 'simple' | 'detailed'

export default function TrackingBoardPanel() {
    const { user } = useAuthStore()
    const [trackingBoard, setTrackingBoard] = useState<TrackingBoardResponse | null>(null)
    const [trackingLoading, setTrackingLoading] = useState(true)
    const [trackingRefreshing, setTrackingRefreshing] = useState(false)
    const [trackingError, setTrackingError] = useState<string | null>(null)
    const [wsConnected, setWsConnected] = useState(false)
    const wsRef = useRef<WebSocket | null>(null)
    const [viewMode, setViewMode] = useState<BoardViewMode>(() => {
        try {
            const stored = localStorage.getItem('ta-tracking-board-view')
            return stored === 'simple' || stored === 'detailed' ? stored : 'simple'
        } catch {
            return 'simple'
        }
    })
    const navigate = useNavigate()
    const [realAccounts, setRealAccounts] = useState<Array<{
        market: string; total_assets: number; cash_balance: number; frozen_cash: number
        market_val: number; currency: string; available_cash: number
        unrealized_pnl: number; realized_pnl: number
    }>>([])

    const trackingItems = trackingBoard?.items || []
    const trackingRefreshSeconds = trackingBoard?.refresh_interval_seconds || 20
    const liveMarketValueTotal = trackingItems.reduce(
        (sum, item) => sum + (item.live_market_value ?? item.market_value ?? 0),
        0,
    )
    const floatingPnlTotal = trackingItems.reduce(
        (sum, item) => sum + (item.floating_pnl ?? 0),
        0,
    )
    const lastQuoteTime = useMemo(() => {
        const values = trackingItems
            .map(item => item.quote_time)
            .filter((value): value is string => Boolean(value))
        return values.length > 0 ? values[0] : null
    }, [trackingItems])

    useEffect(() => {
        try {
            localStorage.setItem('ta-tracking-board-view', viewMode)
        } catch {}
    }, [viewMode])

    // Fetch real account info
    useEffect(() => {
        api.getRealAllAccounts().then(res => {
            if (res.ok) setRealAccounts(res.data ?? [])
        }).catch(() => {})
    }, [])

    useEffect(() => {
        if (!user?.id) return
        let cancelled = false

        const loadTrackingBoard = async (silent: boolean) => {
            if (silent) {
                setTrackingRefreshing(true)
            } else {
                setTrackingLoading(true)
            }

            try {
                const response = await api.getDashboardTrackingBoard()
                if (cancelled) return
                setTrackingBoard(response)
                setTrackingError(null)
            } catch (error) {
                if (cancelled) return
                setTrackingError(error instanceof Error ? error.message : '真仓加载失败')
            } finally {
                if (!cancelled) {
                    setTrackingLoading(false)
                    setTrackingRefreshing(false)
                }
            }
        }

        void loadTrackingBoard(false)
        const intervalId = window.setInterval(() => {
            void loadTrackingBoard(true)
        }, trackingRefreshSeconds * 1000)

        return () => {
            cancelled = true
            window.clearInterval(intervalId)
        }
    }, [trackingRefreshSeconds, user?.id])

    // WebSocket for real-time price updates
    useEffect(() => {
        const token = localStorage.getItem('ta-access-token') || ''
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
        const ws = new WebSocket(`${protocol}//${window.location.host}/ws/quotes?token=${token}`)
        wsRef.current = ws
        ws.onopen = () => setWsConnected(true)
        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data)
                if ((msg.type === 'quotes' || msg.type === 'quote_update') && msg.data) {
                    setTrackingBoard(prev => {
                        if (!prev) return prev
                        const updatedItems = prev.items.map(item => {
                            const q = msg.type === 'quote_update' ? (msg.symbol === item.symbol ? msg.data : null) : msg.data[item.symbol]
                            const state = msg.states?.[item.symbol]
                            if (!q && !state) return item
                            return {
                                ...item,
                                live_price: q?.price ?? item.live_price,
                                price_change: q?.change ?? item.price_change,
                                price_change_pct: q?.change_pct ?? item.price_change_pct,
                                day_high: q?.high ?? item.day_high,
                                day_low: q?.low ?? item.day_low,
                                market_state: state || item.market_state,
                            }
                        })
                        return { ...prev, items: updatedItems }
                    })
                }
            } catch {}
        }
        ws.onclose = () => setWsConnected(false)
        ws.onerror = () => setWsConnected(false)
        return () => { ws.close() }
    }, [])

    // Subscribe to position symbols
    useEffect(() => {
        if (wsRef.current?.readyState === WebSocket.OPEN && trackingItems.length > 0) {
            wsRef.current.send(JSON.stringify({ type: 'subscribe', symbols: trackingItems.map(i => i.symbol) }))
        }
    }, [trackingItems.map(i => i.symbol).join(',')])

    // Currency switch state
    const [displayCurrency, setDisplayCurrency] = useState<'HKD' | 'USD'>('HKD')
    const [activeTab, setActiveTab] = useState<'securities' | 'fund' | 'bond'>('securities')

    // Get accounts by currency
    const hkAccount = realAccounts.find(a => a.market === 'HK')
    const usAccount = realAccounts.find(a => a.market === 'US')
    const displayAccount = displayCurrency === 'HKD' ? hkAccount : usAccount

    // Totals based on currency switch
    const totalAssets = displayAccount?.total_assets ?? realAccounts.reduce((s, a) => s + a.total_assets, 0)
    const availableCash = displayAccount?.available_cash ?? realAccounts.reduce((s, a) => s + a.available_cash, 0)
    const frozenCash = displayAccount?.frozen_cash ?? realAccounts.reduce((s, a) => s + a.frozen_cash, 0)

    const fmtNum = (v: number) => v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

    return (
        <div className="space-y-4">
            {/* Account Title + Currency Switch */}
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">保证金综合账户(9967)</h1>
                </div>
                <div className="flex items-center gap-2">
                    <button onClick={() => setDisplayCurrency('HKD')}
                        className={`rounded-lg px-3 py-1.5 text-sm font-medium transition ${displayCurrency === 'HKD' ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700'}`}>
                        HKD
                    </button>
                    <button onClick={() => setDisplayCurrency('USD')}
                        className={`rounded-lg px-3 py-1.5 text-sm font-medium transition ${displayCurrency === 'USD' ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700'}`}>
                        USD
                    </button>
                </div>
            </div>

            {/* Tabs: 证券 | 基金 | 债券 */}
            <div className="flex gap-1 border-b border-slate-200 dark:border-slate-700">
                <button onClick={() => setActiveTab('securities')}
                    className={`px-4 py-2.5 text-sm font-medium transition border-b-2 ${activeTab === 'securities' ? 'border-blue-600 text-blue-600' : 'border-transparent text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-300'}`}>
                    证券
                </button>
                <button disabled className="px-4 py-2.5 text-sm font-medium text-slate-300 dark:text-slate-600 cursor-not-allowed">
                    基金
                </button>
                <button disabled className="px-4 py-2.5 text-sm font-medium text-slate-300 dark:text-slate-600 cursor-not-allowed">
                    债券
                </button>
            </div>

            {/* Asset Cards */}
            {realAccounts.length > 0 && (
                <div className="grid grid-cols-4 gap-4">
                    <div className="rounded-2xl bg-gradient-to-br from-blue-50 to-blue-100/50 p-4 dark:from-blue-950/30 dark:to-blue-900/20">
                        <div className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400">
                            <Wallet className="h-4 w-4 text-blue-500" />资产净值
                        </div>
                        <div className="mt-2 text-2xl font-bold text-slate-900 dark:text-slate-100">{fmtNum(totalAssets)}</div>
                        <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{displayCurrency}</div>
                    </div>
                    <div className="rounded-2xl bg-gradient-to-br from-emerald-50 to-emerald-100/50 p-4 dark:from-emerald-950/30 dark:to-emerald-900/20">
                        <div className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400">
                            <TrendingUp className="h-4 w-4 text-emerald-500" />可用资金
                        </div>
                        <div className="mt-2 text-2xl font-bold text-slate-900 dark:text-slate-100">{fmtNum(availableCash)}</div>
                        <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">冻结 {fmtNum(frozenCash)}</div>
                    </div>
                    <div className="rounded-2xl bg-gradient-to-br from-purple-50 to-purple-100/50 p-4 dark:from-purple-950/30 dark:to-purple-900/20">
                        <div className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400">
                            <Target className="h-4 w-4 text-purple-500" />持仓市值
                        </div>
                        <div className="mt-2 text-2xl font-bold text-slate-900 dark:text-slate-100">{fmtNum(displayAccount?.market_val ?? 0)}</div>
                        <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{trackingItems.length} 只股票</div>
                    </div>
                    <div className={`rounded-2xl bg-gradient-to-br p-4 ${((displayAccount?.unrealized_pnl ?? 0) + (displayAccount?.realized_pnl ?? 0)) >= 0 ? 'from-emerald-50 to-emerald-100/50 dark:from-emerald-950/30 dark:to-emerald-900/20' : 'from-rose-50 to-rose-100/50 dark:from-rose-950/30 dark:to-rose-900/20'}`}>
                        <div className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400">
                            {((displayAccount?.unrealized_pnl ?? 0) + (displayAccount?.realized_pnl ?? 0)) >= 0 ? <TrendingUp className="h-4 w-4 text-emerald-500" /> : <ArrowDownRight className="h-4 w-4 text-rose-500" />}持仓盈亏
                        </div>
                        <div className={`mt-2 text-2xl font-bold ${((displayAccount?.unrealized_pnl ?? 0) + (displayAccount?.realized_pnl ?? 0)) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>{fmtNum((displayAccount?.unrealized_pnl ?? 0) + (displayAccount?.realized_pnl ?? 0))}</div>
                        <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">未实现 {fmtNum(displayAccount?.unrealized_pnl ?? 0)} · 已实现 {fmtNum(displayAccount?.realized_pnl ?? 0)}</div>
                    </div>
                </div>
            )}

            {/* Cash Balance Table + Trading Stats Cards */}
            {realAccounts.length > 0 && (
                <div className="flex gap-4">
                    {/* Cash Table */}
                    <div className="w-1/4">
                        <div className="card overflow-hidden">
                            <table className="w-full text-sm">
                                <thead>
                                    <tr className="border-b border-slate-200 dark:border-slate-700">
                                        <th className="px-4 py-2 text-left font-medium text-slate-500">币种</th>
                                        <th className="px-4 py-2 text-right font-medium text-slate-500">现金</th>
                                        <th className="px-4 py-2 text-right font-medium text-slate-500">可提</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {hkAccount && (
                                        <tr className="border-b border-slate-100 last:border-0 dark:border-slate-800">
                                            <td className="px-4 py-2 text-slate-700 dark:text-slate-300">HKD</td>
                                            <td className="px-4 py-2 text-right text-slate-700 dark:text-slate-300">{fmtNum(hkAccount.cash_balance)}</td>
                                            <td className="px-4 py-2 text-right text-slate-700 dark:text-slate-300">{fmtNum(hkAccount.available_cash)}</td>
                                        </tr>
                                    )}
                                    {usAccount && (
                                        <tr className="border-b border-slate-100 last:border-0 dark:border-slate-800">
                                            <td className="px-4 py-2 text-slate-700 dark:text-slate-300">USD</td>
                                            <td className="px-4 py-2 text-right text-slate-700 dark:text-slate-300">{fmtNum(usAccount.cash_balance)}</td>
                                            <td className="px-4 py-2 text-right text-slate-700 dark:text-slate-300">{fmtNum(usAccount.available_cash)}</td>
                                        </tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                    
                    {/* Trading Stats Cards */}
                    <div className="flex-1 grid grid-cols-3 gap-4">
                        <div className="rounded-2xl bg-gradient-to-br from-emerald-50 to-emerald-100/50 p-4 dark:from-emerald-950/30 dark:to-emerald-900/20">
                            <div className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400">
                                <TrendingUp className="h-4 w-4 text-emerald-500" />累计盈利
                            </div>
                            <div className="mt-2 text-2xl font-bold text-emerald-600">--</div>
                        </div>
                        <div className="rounded-2xl bg-gradient-to-br from-rose-50 to-rose-100/50 p-4 dark:from-rose-950/30 dark:to-rose-900/20">
                            <div className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400">
                                <ArrowDownRight className="h-4 w-4 text-rose-500" />累计亏损
                            </div>
                            <div className="mt-2 text-2xl font-bold text-rose-600">--</div>
                        </div>
                        <div className="rounded-2xl bg-gradient-to-br from-amber-50 to-amber-100/50 p-4 dark:from-amber-950/30 dark:to-amber-900/20">
                            <div className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400">
                                <Target className="h-4 w-4 text-amber-500" />交易胜率
                            </div>
                            <div className="mt-2 text-2xl font-bold text-slate-900 dark:text-slate-100">--</div>
                        </div>
                    </div>
                </div>
            )}

            {/* Current Positions */}
            <div className="flex items-center justify-between">
                <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">当前持仓</h2>
                <ViewModeSwitch value={viewMode} onChange={setViewMode} />
            </div>

            {trackingLoading && !trackingBoard ? (
                <div className="flex items-center justify-center py-12 text-slate-500 dark:text-slate-400">
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    正在加载...
                </div>
            ) : trackingItems.length === 0 ? (
                <div className="py-10 text-center">
                    <Wallet className="mx-auto mb-3 h-12 w-12 text-slate-300 dark:text-slate-600" />
                    <p className="text-slate-600 dark:text-slate-300">暂无持仓数据</p>
                    <p className="mt-1 text-sm text-slate-400 dark:text-slate-500">
                        请确保 Futu OpenD 已连接且账户有持仓。
                    </p>
                </div>
            ) : viewMode === 'simple' ? (
                <SimpleBoardView
                    items={trackingItems}
                    trackingRefreshing={trackingRefreshing}
                    trackingError={trackingError}
                    lastQuoteTime={lastQuoteTime}
                    wsConnected={wsConnected}
                />
            ) : (
                <DetailedBoardView
                    items={trackingItems}
                    onAnalyze={symbol => navigate(`/analysis?symbol=${symbol}`)}
                    onOpenReport={reportId => navigate(`/reports?report=${reportId}`)}
                />
            )}
        </div>
    )
}

function ViewModeSwitch({
    value,
    onChange,
}: {
    value: BoardViewMode
    onChange: (mode: BoardViewMode) => void
}) {
    return (
        <div className="inline-flex rounded-full bg-slate-100 p-1 dark:bg-slate-800">
            {([
                { id: 'detailed', label: '详细版' },
                { id: 'simple', label: '简洁版' },
            ] as const).map(option => (
                <button
                    key={option.id}
                    type="button"
                    onClick={() => onChange(option.id)}
                    className={`rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${
                        value === option.id
                            ? 'bg-white text-slate-900 shadow-sm dark:bg-slate-700 dark:text-slate-100'
                            : 'text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200'
                    }`}
                >
                    {option.label}
                </button>
            ))}
        </div>
    )
}

const TRACKING_STATE_MAP: Record<string, { label: string; color: string }> = {
    TRADING:        { label: '交易中', color: 'bg-emerald-500 text-white' },
    MORNING:        { label: '早盘',   color: 'bg-emerald-500 text-white' },
    AFTERNOON:      { label: '午盘',   color: 'bg-emerald-500 text-white' },
    CLOSED:         { label: '休市',   color: 'bg-slate-400 text-white dark:bg-slate-600' },
    PRE_MARKET:     { label: '盘前',   color: 'bg-amber-400 text-white' },
    AFTER_HOURS:    { label: '盘后',   color: 'bg-amber-400 text-white' },
    AFTER_HOURS_END:{ label: '盘后结束', color: 'bg-slate-400 text-white dark:bg-slate-600' },
    NIGHT:          { label: '夜盘',   color: 'bg-indigo-400 text-white' },
}

function TrackingMarketStateBadge({ state }: { state?: string | null }) {
    if (!state) return <div className="text-center text-xs text-slate-400">--</div>
    const info = TRACKING_STATE_MAP[state]
    const label = info?.label ?? state
    const color = info?.color ?? 'bg-slate-300 text-slate-600 dark:bg-slate-700 dark:text-slate-300'
    return (
        <span className={`inline-block rounded-full px-2 py-0.5 text-[11px] font-medium leading-tight ${color}`}>
            {label}
        </span>
    )
}

function SimpleBoardView({
    items,
    trackingRefreshing,
    trackingError,
    lastQuoteTime,
    wsConnected,
}: {
    items: TrackingBoardItem[]
    trackingRefreshing: boolean
    trackingError: string | null
    lastQuoteTime: string | null
    wsConnected: boolean
}) {
    return (
        <div className="overflow-hidden rounded-[24px] border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-900">
            <div className="overflow-x-auto">
                <div className="min-w-[1280px]">
                    <div className="grid grid-cols-[1.5fr_1.1fr_0.8fr_1fr_1fr_0.9fr_0.7fr] gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3 text-xs font-medium tracking-[0.1em] text-slate-500 dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-400">
                        <div>名称/代码</div>
                        <div className="text-center">当日区间</div>
                        <div>持仓数量</div>
                        <div>市值/成本市值</div>
                        <div>现价/成本价</div>
                        <div>持仓盈亏/盈亏比</div>
                        <div>持仓比</div>
                    </div>

                    {items.map(item => (
                        <SimpleTrackingRow key={item.symbol} item={item} />
                    ))}
                </div>
            </div>

            <div className="flex flex-col gap-2 border-t border-slate-200 px-5 py-4 text-sm text-slate-500 md:flex-row md:items-center md:justify-between dark:border-slate-700 dark:text-slate-400">
                <div className="flex items-center gap-2">
                    <span className={`inline-flex h-2.5 w-2.5 rounded-full ${wsConnected ? 'bg-emerald-400' : trackingError ? 'bg-amber-400' : 'bg-slate-400'}`} />
                    <span>{wsConnected ? 'WS 实时连接中' : trackingError ? `最近刷新异常：${trackingError}` : '连接中...'}</span>
                    {trackingRefreshing && <RefreshCw className="h-3.5 w-3.5 animate-spin text-slate-400" />}
                </div>
                <div className="text-slate-400">更新：{formatFooterTime(lastQuoteTime)}</div>
            </div>
        </div>
    )
}

function SimpleTrackingRow({ item }: { item: TrackingBoardItem }) {
    const priceChangePct = item.price_change_pct ?? null
    const isUp = (priceChangePct ?? 0) >= 0
    const holdingChangePct = item.floating_pnl_pct ?? null
    const priceColor = priceChangePct == null
        ? 'text-slate-800 dark:text-slate-200'
        : isUp
            ? 'text-rose-600 dark:text-rose-400'
            : 'text-emerald-600 dark:text-emerald-400'
    const pnlColor = (item.floating_pnl ?? 0) >= 0 ? 'text-rose-600 dark:text-rose-400' : 'text-emerald-600 dark:text-emerald-400'
    
    // Market value = live_price * qty, cost value = average_cost * qty
    const marketValue = item.live_price && item.current_position ? item.live_price * item.current_position : item.market_value ?? null
    const costValue = item.average_cost && item.current_position ? item.average_cost * item.current_position : null
    
    // Lots = shares / lot_size (HK default 1000, US default 1)
    const lotSize = item.lot_size ?? (item.symbol.endsWith('.HK') ? 1000 : 1)
    const lots = item.current_position ? Math.floor(item.current_position / lotSize) : null
    
    // Position ratio
    const positionPct = item.current_position_pct ?? null
    
    // Range progress for day range bar
    const rangeProgress = (low?: number | null, high?: number | null, val?: number | null): number | null => {
        if (low == null || high == null || val == null) return null
        if (!Number.isFinite(low) || !Number.isFinite(high) || !Number.isFinite(val)) return null
        const min = Math.min(low, high)
        const max = Math.max(low, high)
        if (max === min) return 50
        return Math.max(0, Math.min(100, ((val - min) / (max - min)) * 100))
    }
    
    const liveP = rangeProgress(item.day_low, item.day_high, item.live_price)
    const prevP = rangeProgress(item.day_low, item.day_high, item.previous_close)
    const openP = rangeProgress(item.day_low, item.day_high, item.day_open)

    return (
        <div className="grid grid-cols-[1.5fr_1.1fr_0.8fr_1fr_1fr_0.9fr_0.7fr] gap-3 border-b border-slate-200 px-4 py-5 last:border-b-0 dark:border-slate-700">
            {/* 状态 + 名称/代码 + 涨跌幅 */}
    <div className="min-w-0 flex items-center gap-2">
        <div className="flex-shrink-0 self-stretch flex items-center">
            <TrackingMarketStateBadge state={item.market_state} />
        </div>
        <div className="flex flex-col justify-center gap-0.5">
            <span className="truncate text-[17px] font-bold text-slate-900 dark:text-slate-100">{item.name}</span>
            <span className="text-sm text-slate-500 dark:text-slate-400">{item.symbol}</span>
            <div className="flex items-center gap-2">
                <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[13px] font-semibold ${
                    priceChangePct == null
                        ? 'bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500'
                        : isUp
                            ? 'bg-rose-50 text-rose-600 dark:bg-rose-500/10 dark:text-rose-400'
                            : 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400'
                }`}>
                    {formatSignedPercent(priceChangePct)}
                </span>
                <span className={`text-[13px] font-semibold ${
                    priceChangePct == null ? 'text-slate-400 dark:text-slate-500'
                    : isUp ? 'text-rose-500 dark:text-rose-400'
                    : 'text-emerald-500 dark:text-emerald-400'
                }`}>
                    {item.price_change != null ? (item.price_change >= 0 ? '+' : '') + formatPlainPrice(item.price_change) : ''}
                </span>
            </div>
        </div>
    </div>
            {/* 当日区间 + 模型高低 */}
            <div className="flex flex-col justify-center gap-1.5">
                <div className="flex items-center gap-2">
                    <span className="w-14 text-right text-[13px] text-slate-400">{formatPlainPrice(item.day_low)}</span>
                    <div className="relative h-6 flex-1">
                        <div className="absolute inset-y-0 left-0 right-0 my-auto h-1.5 rounded-full bg-slate-300 dark:bg-slate-600" />
                        {prevP != null && <div className="absolute top-1/2 h-4 w-1 -translate-y-1/2 rounded-full bg-rose-500 shadow-sm" style={{ left: `calc(${prevP}% - 2px)` }} />}
                        {openP != null && <div className="absolute top-1/2 h-4 w-1 -translate-y-1/2 rounded-full bg-emerald-500 shadow-sm" style={{ left: `calc(${openP}% - 2px)` }} />}
                        {liveP != null && <div className="absolute top-1/2 h-5 w-1.5 -translate-y-1/2 rounded-full bg-blue-500 shadow-sm" style={{ left: `calc(${liveP}% - 3px)` }} />}
                    </div>
                    <span className="w-14 text-[13px] text-slate-400">{formatPlainPrice(item.day_high)}</span>
                </div>
                <div className="flex items-center justify-center gap-3 text-[12px]">
                    <span className="inline-flex items-center gap-1"><span className="h-2.5 w-0.5 rounded-full bg-rose-500" /><span className="text-slate-500 dark:text-slate-400">昨收 {formatPlainPrice(item.previous_close)}</span></span>
                    <span className="inline-flex items-center gap-1"><span className="h-2.5 w-0.5 rounded-full bg-emerald-500" /><span className="text-slate-500 dark:text-slate-400">今开 {formatPlainPrice(item.day_open)}</span></span>
                    <span className="inline-flex items-center gap-1"><span className="h-2.5 w-0.5 rounded-full bg-blue-500" /><span className="text-slate-500 dark:text-slate-400">现价 {formatPlainPrice(item.live_price)}</span></span>
                </div>
                <div className="flex items-center justify-center gap-2 text-[12px]">
                    <span className="inline-flex items-center gap-1 text-purple-600 dark:text-purple-400">
                        <span className="h-2.5 w-0.5 rounded-full bg-purple-500" />
                        模型低 {formatPlainPrice(item.analysis?.low_price)}
                    </span>
                    <span className="text-slate-300 dark:text-slate-600">|</span>
                    <span className="inline-flex items-center gap-1 text-amber-600 dark:text-amber-400">
                        <span className="h-2.5 w-0.5 rounded-full bg-amber-500" />
                        模型高 {formatPlainPrice(item.analysis?.high_price)}
                    </span>
                </div>
            </div>

            {/* 持仓数量 + 手数 */}
            <div className="flex flex-col justify-center">
                <div className="text-[17px] font-medium text-slate-800 dark:text-slate-200">{formatShares(item.current_position)}</div>
                <div className="mt-0.5 text-sm text-slate-400 dark:text-slate-500">{lots != null ? `${lots} 手` : '-'}</div>
            </div>

            {/* 市值/成本市值 */}
            <div className="flex flex-col justify-center text-[15px]">
                <div className="font-medium text-slate-800 dark:text-slate-200">{formatAmount(marketValue)}</div>
                <div className="mt-0.5 text-slate-400 dark:text-slate-500">{costValue != null ? formatAmount(costValue) : '-'}</div>
            </div>

            {/* 现价/成本价 */}
            <div className="flex flex-col justify-center text-[15px]">
                <div className={`text-[17px] font-bold ${priceColor}`}>{formatPlainPrice(item.live_price)}</div>
                <div className="mt-0.5 text-slate-400 dark:text-slate-500">{formatPlainPrice(item.average_cost)}</div>
            </div>

            {/* 持仓盈亏/盈亏比 */}
            <div className="flex flex-col justify-center text-[15px]">
                <div className={`font-bold ${pnlColor}`}>
                    {item.floating_pnl != null ? (item.floating_pnl >= 0 ? '+' : '') + formatAmount(item.floating_pnl) : '-'}
                </div>
                <div className={`mt-0.5 text-[14px] font-semibold ${pnlColor}`}>
                    {holdingChangePct != null ? formatSignedPercent(holdingChangePct) : '-'}
                </div>
            </div>

            {/* 持仓比 */}
            <div className="flex items-center justify-center text-[16px] font-bold text-slate-700 dark:text-slate-300">
                {positionPct != null ? `${positionPct.toFixed(1)}%` : '-'}
            </div>
        </div>
    )
}

function DetailedBoardView({
    items,
    onAnalyze,
    onOpenReport,
}: {
    items: TrackingBoardItem[]
    trackingRefreshing: boolean
    trackingError: string | null
    liveMarketValueTotal: number
    floatingPnlTotal: number
    onAnalyze: (symbol: string) => void
    onOpenReport: (reportId: string) => void
}) {
    return (
        <div className="space-y-4 pt-4">
            <div className="space-y-3">
                {items.map(item => (
                    <DetailedTrackingRow
                        key={item.symbol}
                        item={item}
                        onAnalyze={() => onAnalyze(item.symbol)}
                        onOpenReport={() => {
                            if (item.analysis?.report_id) {
                                onOpenReport(item.analysis.report_id)
                            }
                        }}
                    />
                ))}
            </div>
        </div>
    )
}

function DetailedTrackingRow({
    item,
    onAnalyze,
    onOpenReport,
}: {
    item: TrackingBoardItem
    onAnalyze: () => void
    onOpenReport: () => void
}) {
    const priceChangePct = item.price_change_pct ?? null
    const floatingPnl = item.floating_pnl ?? null
    const analysis = item.analysis
    const rangeAlert = getModelRangeAlert(item)
    const rangeLabel = analysis?.is_previous_trade_day ? '昨日报告高低位' : `最近报告高低位 · ${analysis?.trade_date || '--'}`
    const decisionText = analysis?.decision?.toUpperCase() ?? ''
    const directionText = analysis?.direction ?? ''

    let decisionToneClass = 'bg-slate-100 text-slate-500 dark:bg-slate-700 dark:text-slate-300'
    if (decisionText.includes('BUY') || directionText.includes('增持')) {
        decisionToneClass = 'bg-rose-50 text-rose-600 dark:bg-rose-500/10 dark:text-rose-300'
    } else if (decisionText.includes('SELL') || directionText.includes('减持')) {
        decisionToneClass = 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-300'
    }

    let floatingClass = 'text-slate-900 dark:text-slate-100'
    if (floatingPnl != null) {
        floatingClass = floatingPnl >= 0 ? 'text-rose-600 dark:text-rose-300' : 'text-emerald-600 dark:text-emerald-300'
    }

    return (
        <div className="rounded-2xl border border-slate-200 bg-slate-50/70 p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900/40">
            <div className="flex flex-col gap-4 xl:flex-row xl:items-center">
                <div className="min-w-0 xl:w-[220px]">
                    <div className="flex items-center gap-2">
                        <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-emerald-100 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-300">
                            <TrendingUp className="h-4 w-4" />
                        </div>
                        <div className="min-w-0">
                            <p className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">{item.name}</p>
                            <p className="text-xs text-slate-400">{item.symbol}</p>
                        </div>
                    </div>
                    <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-slate-500 dark:text-slate-400">
                        <MetricPill label="持仓" value={formatShares(item.current_position)} />
                        <MetricPill label="可用" value={formatShares(item.available_position)} />
                        <MetricPill label="成本" value={formatPrice(item.average_cost)} />
                        <MetricPill label="仓位" value={formatWeight(item.current_position_pct)} />
                    </div>
                </div>

                <div className="grid flex-1 grid-cols-1 gap-3 lg:grid-cols-3">
                    <div className="rounded-2xl border border-slate-200 bg-white/80 p-3 dark:border-slate-700 dark:bg-slate-800/60">
                        <div className="flex items-start justify-between">
                            <div>
                                <p className="text-xs uppercase tracking-[0.14em] text-slate-400">实时价格</p>
                                <p className="mt-1 text-2xl font-semibold text-slate-900 dark:text-slate-100">
                                    {formatPrice(item.live_price)}
                                </p>
                            </div>
                            <div className={`flex items-center gap-1 rounded-full px-2 py-1 text-xs font-semibold ${
                                priceChangePct == null
                                    ? 'bg-slate-100 text-slate-500 dark:bg-slate-700 dark:text-slate-300'
                                    : priceChangePct >= 0
                                        ? 'bg-rose-50 text-rose-600 dark:bg-rose-500/10 dark:text-rose-300'
                                        : 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-300'
                            }`}>
                                {priceChangePct == null ? (
                                    <RefreshCw className="h-3.5 w-3.5" />
                                ) : priceChangePct >= 0 ? (
                                    <ArrowUpRight className="h-3.5 w-3.5" />
                                ) : (
                                    <ArrowDownRight className="h-3.5 w-3.5" />
                                )}
                                {formatSignedPercent(priceChangePct)}
                            </div>
                        </div>
                        <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-slate-500 dark:text-slate-400">
                            <MetricPill label="今开" value={formatPrice(item.day_open)} />
                            <MetricPill label="昨收" value={formatPrice(item.previous_close)} />
                            <MetricPill label="日高" value={formatPrice(item.day_high)} />
                            <MetricPill label="日低" value={formatPrice(item.day_low)} />
                        </div>
                        <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-slate-500 dark:text-slate-400">
                            <MetricPill label="成交额" value={item.amount ? `${(item.amount / 10000).toFixed(1)}万` : '--'} />
                            <MetricPill label="成交量" value={formatVolume(item.volume)} />
                        </div>
                        <div className="mt-3 rounded-xl border border-slate-200 bg-slate-50/70 px-3 py-3 dark:border-slate-700 dark:bg-slate-900/40">
                            <div className="flex items-center justify-between gap-3">
                                <div className="flex items-center gap-1">
                                    <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-slate-400">
                                        涨跌停 / 日内 / 模型区间
                                    </p>
                                    <RangeInfoTooltip variant="detailed" />
                                </div>
                                {rangeAlert && (
                                    <span className={`rounded-full px-2 py-1 text-[10px] font-medium ${
                                        rangeAlert.tone === 'danger'
                                            ? 'bg-rose-50 text-rose-600 dark:bg-rose-500/10 dark:text-rose-300'
                                            : 'bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-300'
                                    }`}>
                                        {rangeAlert.message}
                                    </span>
                                )}
                            </div>
                            <div className="mt-3 flex items-center gap-3">
                                <span className="w-16 text-right text-[11px] text-slate-400">
                                    <span className="block">{formatSignedPercent(-getDailyLimitPercent(item))}</span>
                                    <span className="mt-1 block">{formatPlainPrice(item.day_low)}</span>
                                </span>
                                <CombinedRangeTrack item={item} />
                                <span className="w-16 text-[11px] text-slate-400">
                                    <span className="block">{formatSignedPercent(getDailyLimitPercent(item))}</span>
                                    <span className="mt-1 block">{formatPlainPrice(item.day_high)}</span>
                                </span>
                            </div>
                            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-500 dark:text-slate-400">
                                <span className="inline-flex items-center gap-1">
                                    <span className="h-2.5 w-1 rounded-full bg-indigo-500" />
                                    当前涨跌 {formatSignedPercent(item.price_change_pct)}
                                </span>
                                <span className="inline-flex items-center gap-1">
                                    <span className="h-2.5 w-0.5 rounded-full bg-sky-500" />
                                    现价 {formatPlainPrice(item.live_price)}
                                </span>
                                <span className="inline-flex items-center gap-1">
                                    <span className="h-2.5 w-0.5 rounded-full bg-amber-400" />
                                    成本 {formatPlainPrice(item.average_cost)}
                                </span>
                                <span className="inline-flex items-center gap-1">
                                    <span className="h-2.5 w-2.5 rounded-full border border-emerald-600 bg-white" />
                                    模型低位 {formatPlainPrice(item.analysis?.low_price)}
                                </span>
                                <span className="inline-flex items-center gap-1">
                                    <span className="h-2.5 w-2.5 rounded-full border border-rose-600 bg-white" />
                                    模型高位 {formatPlainPrice(item.analysis?.high_price)}
                                </span>
                            </div>
                        </div>
                    </div>

                    <div className="rounded-2xl border border-slate-200 bg-white/80 p-3 dark:border-slate-700 dark:bg-slate-800/60">
                        <div className="flex items-start justify-between">
                            <div>
                                <p className="text-xs uppercase tracking-[0.14em] text-slate-400">持仓表现</p>
                                <p className={`mt-1 text-2xl font-semibold ${floatingClass}`}>
                                    {formatSignedMoney(floatingPnl)}
                                </p>
                            </div>
                            <div className={`rounded-full px-2 py-1 text-xs font-semibold ${
                                floatingPnl == null
                                    ? 'bg-slate-100 text-slate-500 dark:bg-slate-700 dark:text-slate-300'
                                    : floatingPnl >= 0
                                        ? 'bg-rose-50 text-rose-600 dark:bg-rose-500/10 dark:text-rose-300'
                                        : 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-300'
                            }`}>
                                {formatSignedPercent(item.floating_pnl_pct)}
                            </div>
                        </div>
                        <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-slate-500 dark:text-slate-400">
                            <MetricPill label="动态市值" value={formatMoney(item.live_market_value ?? item.market_value)} />
                            <MetricPill label="静态市值" value={formatMoney(item.market_value)} />
                            <MetricPill label="成交额" value={formatAmount(item.amount)} />
                            <MetricPill label="价格源" value={formatQuoteSource(item.quote_source)} />
                        </div>
                        <div className="mt-2">
                            <MetricPill label="导入时间" value={formatDateTime(item.last_imported_at)} />
                        </div>
                    </div>

                    <div className="rounded-2xl border border-slate-200 bg-white/80 p-3 dark:border-slate-700 dark:bg-slate-800/60">
                        <div className="flex items-center gap-2">
                            <Target className="h-4 w-4 text-sky-500" />
                            <p className="text-xs uppercase tracking-[0.14em] text-slate-400">{rangeLabel}</p>
                        </div>
                        {analysis ? (
                            <>
                                <div className="mt-3 flex items-center gap-2 text-xs">
                                    <span className={`rounded-full px-2 py-1 font-semibold ${decisionToneClass}`}>
                                        {analysis.direction || analysis.decision || '待定'}
                                    </span>
                                    <span className="rounded-full bg-slate-100 px-2 py-1 text-slate-500 dark:bg-slate-700 dark:text-slate-300">
                                        {analysis.trade_date}
                                    </span>
                                </div>

                                <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-slate-500 dark:text-slate-400">
                                    <MetricPill label="高位" value={formatPrice(analysis.high_price)} />
                                    <MetricPill label="低位" value={formatPrice(analysis.low_price)} />
                                </div>
                            </>
                        ) : (
                            <div className="mt-3 rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-3 py-4 text-xs text-slate-500 dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-400">
                                还没有找到可绑定到这只股票的分析结果。
                            </div>
                        )}
                    </div>
                </div>

                <div className="xl:w-[290px]">
                    <div className="rounded-2xl border border-slate-200 bg-white/80 p-3 dark:border-slate-700 dark:bg-slate-800/60">
                        <div className="flex items-center gap-2">
                            <ShieldAlert className="h-4 w-4 text-amber-500" />
                            <p className="text-xs uppercase tracking-[0.14em] text-slate-400">交易员建议</p>
                        </div>
                        <p className="mt-3 text-sm leading-6 text-slate-700 dark:text-slate-200" style={CLAMP_TWO_LINES_STYLE}>
                            {analysis?.trader_advice_summary || '暂未提取到建议摘要，可进入报告查看完整内容。'}
                        </p>
                        <div className="mt-4 flex flex-wrap gap-2">
                            {analysis?.report_id && (
                                <button
                                    type="button"
                                    onClick={onOpenReport}
                                    className="rounded-xl border border-slate-200 px-3 py-2 text-xs font-medium text-slate-700 transition-colors hover:border-blue-400 hover:text-blue-600 dark:border-slate-700 dark:text-slate-200 dark:hover:border-blue-500 dark:hover:text-blue-400"
                                >
                                    查看报告
                                </button>
                            )}
                            <button
                                type="button"
                                onClick={onAnalyze}
                                className="rounded-xl bg-slate-900 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-slate-700 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                            >
                                重新分析
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    )
}

function RangeInfoTooltip({ variant }: { variant: 'simple' | 'detailed' }) {
    const text = variant === 'simple'
        ? '短条表示当日最低价到最高价，蓝线是现价，黄线是成本价；下方补充模型低位、高位和破位预警。'
        : '外层细条表示跌停到涨停，内层短条表示当日最低价到最高价；图中同时标出当前涨跌、现价、成本价和模型区间。'

    return (
        <div className="group relative">
            <button
                type="button"
                aria-label="当日区间说明"
                className="flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 bg-white text-[10px] font-semibold tracking-normal text-slate-500 transition-colors hover:border-sky-300 hover:text-sky-600 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-400 dark:hover:border-sky-500/40 dark:hover:text-sky-300"
            >
                i
            </button>
            <div className="pointer-events-none absolute left-1/2 top-5 z-20 w-64 -translate-x-1/2 rounded-xl border border-slate-200 bg-white p-3 text-left text-[11px] normal-case tracking-normal text-slate-600 opacity-0 shadow-xl transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200">
                {text}
            </div>
        </div>
    )
}


function CombinedRangeTrack({ item }: { item: TrackingBoardItem }) {
    const limitPct = getDailyLimitPercent(item)
    const lowPct = -limitPct
    const highPct = limitPct
    const currentPct = item.price_change_pct
    const currentProgress = getRangeMarkerProgress(lowPct, highPct, currentPct)
    const low = item.day_low
    const high = item.day_high
    const live = item.live_price
    const cost = item.average_cost
    const analysisLow = item.analysis?.low_price
    const analysisHigh = item.analysis?.high_price
    const liveProgress = getRangeMarkerProgress(low, high, live)
    const costProgress = getRangeMarkerProgress(low, high, cost)
    const analysisLowProgress = getRangeMarkerProgress(low, high, analysisLow)
    const analysisHighProgress = getRangeMarkerProgress(low, high, analysisHigh)

    return (
        <div className="relative h-7 flex-1">
            <div className="absolute inset-x-0 top-1/2 h-1 -translate-y-1/2 rounded-full bg-gradient-to-r from-emerald-100 via-slate-200 to-rose-100" />
            <div className="absolute inset-y-0 left-0 w-px bg-slate-300" />
            <div className="absolute inset-y-0 right-0 w-px bg-slate-300" />
            {currentProgress != null && (
                <div
                    className="absolute top-1/2 h-5 w-1.5 -translate-y-1/2 rounded-full bg-indigo-500 shadow-sm"
                    style={{ left: `calc(${currentProgress}% - 3px)` }}
                />
            )}
            <div className="absolute left-[16%] right-[16%] top-1/2 h-2 -translate-y-1/2 rounded-full bg-slate-300" />
            {analysisLowProgress != null && (
                <div
                    className="absolute top-1/2 h-2.5 w-2.5 -translate-y-1/2 rounded-full border border-emerald-600 bg-white shadow-sm"
                    style={{ left: `calc(16% + ${analysisLowProgress * 0.68}% - 5px)` }}
                />
            )}
            {analysisHighProgress != null && (
                <div
                    className="absolute top-1/2 h-2.5 w-2.5 -translate-y-1/2 rounded-full border border-rose-600 bg-white shadow-sm"
                    style={{ left: `calc(16% + ${analysisHighProgress * 0.68}% - 5px)` }}
                />
            )}
            {costProgress != null && (
                <div
                    className="absolute top-1/2 h-4 w-1 -translate-y-1/2 rounded-full bg-amber-400 shadow-sm"
                    style={{ left: `calc(16% + ${costProgress * 0.68}% - 2px)` }}
                />
            )}
            {liveProgress != null && (
                <div
                    className="absolute top-1/2 h-4 w-1.5 -translate-y-1/2 rounded-full bg-sky-500 shadow-sm"
                    style={{ left: `calc(16% + ${liveProgress * 0.68}% - 3px)` }}
                />
            )}
        </div>
    )
}

function MetricPill({ label, value }: { label: string; value: string }) {
    return (
        <div className="rounded-xl bg-slate-100/90 px-2.5 py-2 dark:bg-slate-700/60">
            <p className="text-[11px] text-slate-400">{label}</p>
            <p className="mt-1 truncate font-medium text-slate-700 dark:text-slate-100">{value}</p>
        </div>
    )
}

function getRangeMarkerProgress(
    low?: number | null,
    high?: number | null,
    value?: number | null,
): number | null {
    if (low == null || high == null || value == null) return null
    if (!Number.isFinite(low) || !Number.isFinite(high) || !Number.isFinite(value)) return null

    const min = Math.min(low, high)
    const max = Math.max(low, high)
    if (max === min) return 50

    const raw = ((value - min) / (max - min)) * 100
    return Math.max(0, Math.min(100, raw))
}

function getDailyLimitPercent(item: TrackingBoardItem): number {
    const symbol = String(item.symbol || '').toUpperCase()
    const name = String(item.name || '').toUpperCase()

    if (name.includes('ST')) return 5
    if (symbol.endsWith('.BJ')) return 30
    if (symbol.startsWith('300') || symbol.startsWith('688')) return 20
    return 10
}

function getModelRangeAlert(
    item: TrackingBoardItem,
): { tone: 'danger' | 'warning'; message: string } | null {
    const livePrice = item.live_price
    const lowPrice = item.analysis?.low_price
    const highPrice = item.analysis?.high_price

    if (livePrice == null || !Number.isFinite(livePrice)) return null
    if (lowPrice != null && Number.isFinite(lowPrice) && livePrice < lowPrice) {
        return { tone: 'danger', message: '预警：已跌破模型低位' }
    }
    if (highPrice != null && Number.isFinite(highPrice) && livePrice > highPrice) {
        return { tone: 'warning', message: '预警：已突破模型高位' }
    }
    return null
}

function formatPrice(value?: number | null): string {
    if (value == null || !Number.isFinite(value)) return '--'
    return formatNumber(value, 3, 2)
}

function formatPlainPrice(value?: number | null): string {
    return formatPrice(value)
}

function formatMoney(value?: number | null): string {
    if (value == null || !Number.isFinite(value)) return '--'
    return formatWithChineseUnit(value, 2)
}

function formatSignedMoney(value?: number | null): string {
    if (value == null || !Number.isFinite(value)) return '--'
    const sign = value >= 0 ? '+' : '-'
    return `${sign}${formatMoney(Math.abs(value))}`
}

function formatSignedPercent(value?: number | null): string {
    if (value == null || !Number.isFinite(value)) return '--'
    return `${value >= 0 ? '+' : ''}${formatNumber(value, 2)}%`
}

function formatShares(value?: number | null): string {
    if (value == null || !Number.isFinite(value)) return '--'
    if (Math.abs(value) >= 1e4) {
        return `${formatWithChineseUnit(value, 0)}股`
    }
    return `${formatNumber(value, 0)}股`
}

function formatWeight(value?: number | null): string {
    if (value == null || !Number.isFinite(value)) return '--'
    return `${formatNumber(value, 2)}%`
}

function formatVolume(value?: number | null): string {
    if (value == null || !Number.isFinite(value)) return '--'
    return formatWithChineseUnit(value, 0)
}

function formatAmount(value?: number | null): string {
    if (value == null || !Number.isFinite(value)) return '--'
    return formatWithChineseUnit(value, 2)
}

function formatDateTime(value?: string | null): string {
    const parsed = parseLooseDate(value)
    if (!parsed) return value || '--'

    const year = parsed.getFullYear()
    const month = String(parsed.getMonth() + 1).padStart(2, '0')
    const day = String(parsed.getDate()).padStart(2, '0')
    const hours = String(parsed.getHours()).padStart(2, '0')
    const minutes = String(parsed.getMinutes()).padStart(2, '0')
    return `${year}-${month}-${day} ${hours}:${minutes}`
}

function formatFooterTime(value?: string | null): string {
    const parsed = parseLooseDate(value)
    if (!parsed) return value || '--'

    const month = String(parsed.getMonth() + 1).padStart(2, '0')
    const day = String(parsed.getDate()).padStart(2, '0')
    const hours = String(parsed.getHours()).padStart(2, '0')
    const minutes = String(parsed.getMinutes()).padStart(2, '0')
    const seconds = String(parsed.getSeconds()).padStart(2, '0')
    return `${month}-${day} ${hours}:${minutes}:${seconds}`
}

function formatQuoteSource(value?: string | null): string {
    if (!value) return '--'
    return value.replace('_hq', '').replace('_', ' ')
}

function formatNumber(value: number, maxDigits = 2, minDigits?: number): string {
    return new Intl.NumberFormat('zh-CN', {
        minimumFractionDigits: minDigits ?? maxDigits,
        maximumFractionDigits: maxDigits,
    }).format(value)
}

function formatWithChineseUnit(value: number, baseDigits = 2): string {
    const abs = Math.abs(value)
    if (abs >= 1e8) {
        return `${formatNumber(value / 1e8, 2)}亿`
    }
    if (abs >= 1e4) {
        return `${formatNumber(value / 1e4, baseDigits === 0 ? 0 : 2)}万`
    }
    return formatNumber(value, baseDigits)
}

function parseLooseDate(value?: string | null): Date | null {
    if (!value) return null
    const trimmed = value.trim()
    if (!trimmed) return null

    let match = /^(\d{4})(\d{2})(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(trimmed)
    if (match) {
        return new Date(
            Number(match[1]),
            Number(match[2]) - 1,
            Number(match[3]),
            Number(match[4]),
            Number(match[5]),
            Number(match[6] || 0),
        )
    }

    match = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$/.exec(trimmed)
    if (match) {
        return new Date(
            Number(match[1]),
            Number(match[2]) - 1,
            Number(match[3]),
            Number(match[4]),
            Number(match[5]),
            Number(match[6] || 0),
        )
    }

    const parsed = new Date(trimmed)
    return Number.isNaN(parsed.getTime()) ? null : parsed
}
