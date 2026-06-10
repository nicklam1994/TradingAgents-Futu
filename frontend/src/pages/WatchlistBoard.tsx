import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import {
    ChevronDown,
    ChevronUp,
    ChevronsUpDown,
    Loader2,
    Plus,
    RefreshCw,
    Search,
    Star,
} from 'lucide-react'
import { api } from '@/services/api'
import { useAuthStore } from '@/stores/authStore'
import type {
    StockSearchResult,
    WatchlistBoardItem,
    WatchlistBoardResponse,
} from '@/types'

const WATCHLIST_BATCH_SPLIT_RE = /[,\\s，、；;]+/

export default function WatchlistBoard() {
    const { user } = useAuthStore()
    const navigate = useNavigate()

    const [board, setBoard] = useState<WatchlistBoardResponse | null>(null)
    const [loading, setLoading] = useState(true)
    const [refreshing, setRefreshing] = useState(false)
    const [sortKey, setSortKey] = useState<'name' | 'pct' | 'state' | null>(null)
    const [sortAsc, setSortAsc] = useState(true)
    const [wsConnected, setWsConnected] = useState(false)
    const wsRef = useRef<WebSocket | null>(null)
    const [error, setError] = useState<string | null>(null)

    const [searchQuery, setSearchQuery] = useState('')
    const [searchResults, setSearchResults] = useState<StockSearchResult[]>([])
    const [searchLoading, setSearchLoading] = useState(false)
    const [showDropdown, setShowDropdown] = useState(false)
    const [adding, setAdding] = useState(false)
    const [feedback, setFeedback] = useState<{
        tone: 'success' | 'warning' | 'error'
        message: string
        details: string[]
    } | null>(null)
    const searchTimerRef = useRef<ReturnType<typeof setTimeout>>()
    const dropdownRef = useRef<HTMLDivElement>(null)

    const trimmedQuery = searchQuery.trim()
    const isBatchInput = trimmedQuery.length > 0 && WATCHLIST_BATCH_SPLIT_RE.test(trimmedQuery)
    const items = board?.items || []

    const toggleSort = (key: 'name' | 'pct' | 'state') => {
        if (sortKey === key) setSortAsc(!sortAsc)
        else { setSortKey(key); setSortAsc(key === 'name') }
    }

    const sortedItems = [...items].sort((a, b) => {
        if (!sortKey) return 0
        if (sortKey === 'name') {
            const cmp = extractName(a.name).localeCompare(extractName(b.name), 'zh')
            return sortAsc ? cmp : -cmp
        }
        if (sortKey === 'state') {
            // Trading states first, then by state priority
            const statePriority: Record<string, number> = {
                TRADING: 0, MORNING: 0, AFTERNOON: 0, NIGHT_OPEN: 0,
                PRE_MARKET_BEGIN: 1, AFTER_HOURS_BEGIN: 1,
                AUCTION: 2, WAITING_OPEN: 2,
                CLOSED: 3, AFTER_HOURS_END: 3, NIGHT_END: 3, OVERNIGHT: 3, PRE_MARKET_END: 3,
                REST: 4, HK_CAS: 4,
                NONE: 5,
            }
            const pa = statePriority[a.market_state ?? 'NONE'] ?? 5
            const pb = statePriority[b.market_state ?? 'NONE'] ?? 5
            if (pa !== pb) return sortAsc ? pa - pb : pb - pa
            // Same priority, sort by symbol
            return a.symbol.localeCompare(b.symbol)
        }
        const va = a.price_change_pct ?? -Infinity
        const vb = b.price_change_pct ?? -Infinity
        return sortAsc ? va - vb : vb - va
    })

    // Send subscribe message when items change OR WebSocket reconnects
    useEffect(() => {
        if (wsRef.current?.readyState === WebSocket.OPEN && items.length > 0) {
            wsRef.current.send(JSON.stringify({ type: 'subscribe', symbols: items.map(i => i.symbol) }))
        }
    }, [items.map(i => i.symbol).join(','), wsConnected])

    const loadBoard = async (silent: boolean) => {
        if (silent) setRefreshing(true)
        else setLoading(true)
        try {
            const response = await api.getDashboardWatchlistBoard()
            setBoard(response)
            setError(null)
        } catch (e) {
            setError(e instanceof Error ? e.message : '加载失败')
        } finally {
            setLoading(false)
            setRefreshing(false)
        }
    }

    useEffect(() => {
        if (!user?.id) return

        // Initial load via REST
        void loadBoard(false)

        // Connect WebSocket for real-time updates
        const token = localStorage.getItem('ta-access-token') || ''
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
        const ws = new WebSocket(`${protocol}//${window.location.host}/ws/quotes?token=${token}`)
        wsRef.current = ws

        ws.onopen = () => {
            setWsConnected(true)
        }

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data)

                // Full snapshot (all symbols at once)
                if (msg.type === 'quotes' && msg.data) {
                    setBoard(prev => {
                        if (!prev) return prev
                        const updatedItems = prev.items.map(item => {
                            const q = msg.data[item.symbol]
                            const state = msg.states?.[item.symbol]
                            if (!q && !state) return item
                            return {
                                ...item,
                                ...(q ? {
                                    live_price: q.price ?? item.live_price,
                                    price_change: q.change ?? item.price_change,
                                    price_change_pct: q.change_pct ?? item.price_change_pct,
                                    day_open: q.open ?? item.day_open,
                                    day_high: q.high ?? item.day_high,
                                    day_low: q.low ?? item.day_low,
                                    prev_close: q.prev_close ?? item.prev_close,
                                    volume: q.volume ?? item.volume,
                                    turnover: q.turnover ?? item.turnover,
                                    amplitude: q.amplitude ?? item.amplitude,
                                    turnover_rate: q.turnover_rate ?? item.turnover_rate,
                                } : {}),
                                ...(state ? { market_state: state } : {}),
                            }
                        })
                        return { ...prev, items: updatedItems }
                    })
                }

                // Individual symbol update (real-time push from Futu)
                if (msg.type === 'quote_update' && msg.symbol && msg.data) {
                    const sym = msg.symbol
                    const q = msg.data
                    setBoard(prev => {
                        if (!prev) return prev
                        const updatedItems = prev.items.map(item => {
                            if (item.symbol !== sym) return item
                            return {
                                ...item,
                                live_price: q.price ?? item.live_price,
                                price_change: q.change ?? item.price_change,
                                price_change_pct: q.change_pct ?? item.price_change_pct,
                                day_open: q.open ?? item.day_open,
                                day_high: q.high ?? item.day_high,
                                day_low: q.low ?? item.day_low,
                                prev_close: q.prev_close ?? item.prev_close,
                                volume: q.volume ?? item.volume,
                                turnover: q.turnover ?? item.turnover,
                                amplitude: q.amplitude ?? item.amplitude,
                                turnover_rate: q.turnover_rate ?? item.turnover_rate,
                            }
                        })
                        return { ...prev, items: updatedItems }
                    })
                }

                // Market states update (periodic refresh)
                if (msg.type === 'states' && msg.states) {
                    setBoard(prev => {
                        if (!prev) return prev
                        const updatedItems = prev.items.map(item => {
                            const state = msg.states[item.symbol]
                            if (!state || state === item.market_state) return item
                            return { ...item, market_state: state }
                        })
                        return { ...prev, items: updatedItems }
                    })
                }
            } catch {}
        }

        ws.onclose = () => setWsConnected(false)
        ws.onerror = () => setWsConnected(false)

        return () => { ws.close() }
    }, [user?.id])

    useEffect(() => {
        const handler = (e: MouseEvent) => {
            if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
                setShowDropdown(false)
            }
        }
        document.addEventListener('mousedown', handler)
        return () => document.removeEventListener('mousedown', handler)
    }, [])

    useEffect(() => {
        if (searchTimerRef.current) clearTimeout(searchTimerRef.current)
        if (!trimmedQuery || isBatchInput) {
            setSearchResults([])
            setShowDropdown(false)
            setSearchLoading(false)
            return
        }
        setSearchLoading(true)
        searchTimerRef.current = setTimeout(async () => {
            try {
                const res = await api.searchStocks(trimmedQuery)
                setSearchResults(res.results)
                setShowDropdown(true)
            } catch {
                setShowDropdown(false)
            }
            setSearchLoading(false)
        }, 300)
    }, [trimmedQuery, isBatchInput])

    const addToWatchlist = async (symbol: string) => {
        try {
            const response = await api.addToWatchlist(symbol)
            const item = response.results.find(r => r.status === 'added')?.item
            if (!item) throw new Error(response.results[0]?.message || '添加失败')
            setSearchQuery('')
            setShowDropdown(false)
            setFeedback({ tone: 'success', message: `已添加 ${item.name || symbol}`, details: [] })
            void loadBoard(true)
        } catch (e) {
            alert(e instanceof Error ? e.message : '添加失败')
        }
    }

    const submitInput = async () => {
        if (!trimmedQuery) return
        setAdding(true)
        setFeedback(null)
        try {
            const response = await api.addToWatchlist(trimmedQuery)
            const details = response.results
                .filter(r => r.status !== 'added')
                .slice(0, 6)
                .map(r => `${r.input}：${r.message}`)
            const tone = response.summary.failed > 0 && response.summary.added === 0
                ? 'error' : response.summary.added > 0 ? 'success' : 'warning'
            setFeedback({
                tone,
                message: `新增 ${response.summary.added}，重复 ${response.summary.duplicate}，失败 ${response.summary.failed}`,
                details,
            })
            setSearchQuery('')
            setShowDropdown(false)
            if (response.summary.added > 0) void loadBoard(true)
        } catch (e) {
            setFeedback({ tone: 'error', message: e instanceof Error ? e.message : '添加失败', details: [] })
        } finally {
            setAdding(false)
        }
    }

    const removeWatchlist = async (symbol: string) => {
        try {
            await api.removeFromWatchlist(symbol)
            void loadBoard(true)
        } catch (e) {
            alert(e instanceof Error ? e.message : '移除失败')
        }
    }

    return (
        <div className="space-y-4">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">优质自选</h1>
                    <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">添加关注股票，实时跟踪行情与分析</p>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
                    <span className="rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-700/70">上一交易日：{board?.previous_trade_date || '--'}</span>
                    <span className={`rounded-full px-3 py-1 ${wsConnected ? 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400' : 'bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400'}`}>
                        {wsConnected ? '實時連接中' : '離線'}
                    </span>
                    <span className="rounded-full bg-blue-50 px-3 py-1 text-blue-600 dark:bg-blue-500/10 dark:text-blue-400">訂閱額度 {board?.subscription_used ?? items.length}/{board?.subscription_limit ?? 300}</span>
                </div>
            </div>

            {/* Add Watchlist */}
            <div className="card space-y-3">
                <div className="flex items-center gap-2">
                    <Plus className="w-5 h-5 text-blue-500" />
                    <h2 className="font-semibold text-slate-900 dark:text-slate-100">添加自选</h2>
                </div>
                <div className="space-y-3" ref={dropdownRef}>
                    <div className="relative flex items-center gap-2">
                        <div className="relative flex-1">
                            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                            <input
                                type="text"
                                value={searchQuery}
                                onChange={e => setSearchQuery(e.target.value)}
                                onFocus={() => searchResults.length > 0 && !isBatchInput && setShowDropdown(true)}
                                onKeyDown={e => e.key === 'Enter' && trimmedQuery && void submitInput()}
                                placeholder="搜索代码/名称，批量粘贴"
                                className="input pl-9 pr-10 w-full"
                            />
                            {searchLoading && <Loader2 className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 animate-spin text-slate-400" />}
                        </div>
                        <button type="button" onClick={() => void submitInput()} disabled={!trimmedQuery || adding}
                            className="btn-primary inline-flex items-center justify-center gap-2 whitespace-nowrap shrink-0">
                            {adding ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                            {isBatchInput ? '批量添加' : '添加'}
                        </button>
                    </div>
                    {feedback && (
                        <div className={`rounded-xl border px-3 py-3 text-sm ${
                            feedback.tone === 'success' ? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/20 dark:bg-emerald-500/10 dark:text-emerald-300'
                            : feedback.tone === 'warning' ? 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-500/20 dark:bg-amber-500/10 dark:text-amber-300'
                            : 'border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-500/20 dark:bg-rose-500/10 dark:text-rose-300'
                        }`}>
                            <div>{feedback.message}</div>
                            {feedback.details.length > 0 && <div className="mt-2 space-y-1 text-xs opacity-90">{feedback.details.map(d => <div key={d}>{d}</div>)}</div>}
                        </div>
                    )}
                    {showDropdown && searchResults.length > 0 && (
                        <div className="border border-slate-200 dark:border-slate-700 rounded-lg bg-white dark:bg-slate-800 shadow-lg max-h-60 overflow-y-auto">
                            {searchResults.map(r => (
                                <button key={r.symbol} onClick={() => void addToWatchlist(r.symbol)}
                                    className="w-full flex items-center gap-3 px-3 py-2 text-left hover:bg-slate-50 dark:hover:bg-slate-700/50 transition-colors">
                                    <span className="text-sm font-medium text-slate-900 dark:text-slate-100">{r.name}</span>
                                    <span className="text-xs text-slate-400">{r.symbol}</span>
                                    <Plus className="w-3.5 h-3.5 text-blue-500 ml-auto" />
                                </button>
                            ))}
                        </div>
                    )}
                </div>
            </div>

            {/* Board */}
            {loading && !board ? (
                <div className="flex items-center justify-center py-12 text-slate-500 dark:text-slate-400">
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />正在加载优质自选...
                </div>
            ) : items.length === 0 ? (
                <div className="py-10 text-center">
                    <Star className="mx-auto mb-3 h-12 w-12 text-slate-300 dark:text-slate-600" />
                    <p className="text-slate-600 dark:text-slate-300">暂无自选股票</p>
                    <p className="mt-1 text-sm text-slate-400 dark:text-slate-500">在上方搜索添加股票，即可实时跟踪行情。</p>
                </div>
            ) : (
                <WatchlistTable items={sortedItems} refreshing={refreshing} error={error}
                    wsConnected={wsConnected} sortKey={sortKey} sortAsc={sortAsc} onToggleSort={toggleSort}
                    onAnalyze={s => navigate(`/analysis?symbol=${s}`)} onRemove={s => void removeWatchlist(s)} />
            )}
        </div>
    )
}

/* ─── 7-Column Table ───────────────────────────────────────────────────── */

function WatchlistTable({ items, refreshing, error, wsConnected, sortKey, sortAsc, onToggleSort, onAnalyze, onRemove }: {
    items: WatchlistBoardItem[]
    refreshing: boolean
    error: string | null
    wsConnected: boolean
    sortKey: 'name' | 'pct' | 'state' | null
    sortAsc: boolean
    onToggleSort: (key: 'name' | 'pct' | 'state') => void
    onAnalyze: (s: string) => void
    onRemove: (s: string) => void
}) {
    return (
        <div className="overflow-hidden rounded-[24px] border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-900">
            <div className="overflow-x-auto">
                <div className="min-w-[1200px]">
                    <div className="grid grid-cols-[0.45fr_1.4fr_0.9fr_0.7fr_0.7fr_1.4fr_0.7fr_0.9fr] gap-3 border-b border-slate-200 bg-slate-50 px-5 py-3 text-xs font-medium tracking-wider text-slate-500 dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-400">
                        <SortHeader label="市場 / 狀態" sortKey="state" activeKey={sortKey} asc={sortAsc} onToggle={onToggleSort} center />
                        <SortHeader label="名称 / 代码" sortKey="name" activeKey={sortKey} asc={sortAsc} onToggle={onToggleSort} />
                        <div>当日 K 线</div>
                        <div>实时现价</div>
                        <SortHeader label="涨跌幅" sortKey="pct" activeKey={sortKey} asc={sortAsc} onToggle={onToggleSort} center />
                        <div>当日区间</div>
                        <div>振幅 / 换手</div>
                        <div>成交额 / 成交量</div>
                    </div>
                    {items.map(item => <WatchlistRow key={item.symbol} item={item} onAnalyze={onAnalyze} onRemove={onRemove} />)}
                </div>
            </div>
            <div className="flex flex-col gap-2 border-t border-slate-200 px-5 py-4 text-sm text-slate-500 md:flex-row md:items-center md:justify-between dark:border-slate-700 dark:text-slate-400">
                <div className="flex items-center gap-2">
                    <span className={`inline-flex h-2.5 w-2.5 rounded-full ${error ? 'bg-amber-400' : 'bg-emerald-400'}`} />
<span>{wsConnected ? '實時報價中' : error ? `連接異常：${error}` : '正在連接...'}</span>
                    {refreshing && <RefreshCw className="h-3.5 w-3.5 animate-spin text-slate-400" />}
                </div>
                <div className="text-slate-400">共 {items.length} 只</div>
            </div>
        </div>
    )
}

/* ─── Single Row ───────────────────────────────────────────────────────── */

function WatchlistRow({ item }: {
    item: WatchlistBoardItem
    onAnalyze?: (s: string) => void
    onRemove?: (s: string) => void
}) {
    const pct = item.price_change_pct ?? null
    const isUp = (pct ?? 0) >= 0
    const priceColor = pct == null ? 'text-slate-800 dark:text-slate-200' : isUp ? 'text-rose-600 dark:text-rose-400' : 'text-emerald-600 dark:text-emerald-400'

    return (
        <div className="grid grid-cols-[0.45fr_1.4fr_0.9fr_0.7fr_0.7fr_1.4fr_0.7fr_0.9fr] gap-3 border-b border-slate-200 px-5 py-4 last:border-b-0 dark:border-slate-700">
            {/* Col 0: 状态 */}
            <MarketStateBadge state={item.market_state} symbol={item.symbol} />

            {/* Col 1: 名称/代码 */}
            <div className="min-w-0">
                <div className="truncate text-lg font-bold text-slate-900 dark:text-slate-100">{extractName(item.name)}</div>
                <div className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">{item.symbol}</div>
            </div>

            {/* Col 2: 当日 K 线 */}
            <DayCandle item={item} />

            {/* Col 3: 实时现价 */}
            <div className={`self-center text-xl font-semibold ${priceColor}`}>
                {fmtPrice(item.live_price)}
            </div>

            {/* Col 4: 涨跌幅 */}
            <div className="self-center text-center">
                <span className={`inline-flex min-w-[80px] items-center justify-center rounded-full px-2.5 py-1.5 text-base font-semibold ${
                    pct == null ? 'bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500'
                    : isUp ? 'bg-rose-50 text-rose-600 dark:bg-rose-500/10 dark:text-rose-400'
                    : 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400'
                }`}>{fmtPct(pct)}</span>
                <div className={`mt-1 text-xs font-bold ${
                    pct == null ? 'text-slate-400 dark:text-slate-500'
                    : isUp ? 'text-rose-500 dark:text-rose-400'
                    : 'text-emerald-500 dark:text-emerald-400'
                }`}>
                    {fmtSignedPrice(item.price_change)}
                </div>
            </div>

            {/* Col 5: 当日区间 */}
            <DayRange item={item} />

            {/* Col 6: 振幅/换手率 */}
            <div className="self-center space-y-1 text-sm text-slate-600 dark:text-slate-400">
                <div>{item.amplitude != null ? `${item.amplitude.toFixed(2)}%` : '--'}</div>
                <div className="text-xs text-slate-400 dark:text-slate-500">{item.turnover_rate != null ? `${item.turnover_rate.toFixed(2)}%` : '--'}</div>
            </div>

            {/* Col 7: 成交额/成交量 */}
            <div className="self-center space-y-1 text-sm text-slate-600 dark:text-slate-400">
                <div>{fmtAmount(item.turnover)}</div>
                <div className="text-xs text-slate-400 dark:text-slate-500">{fmtVol(item.volume)}</div>
            </div>
        </div>
    )
}

/* ─── Day Candle (K 线) ─────────────────────────────────────────────────── */

function DayCandle({ item }: { item: WatchlistBoardItem }) {
    const open = item.day_open ?? item.prev_close ?? null
    const close = item.live_price ?? null
    const high = item.day_high ?? null
    const low = item.day_low ?? null

    if (open == null || close == null || high == null || low == null) {
        return <div className="flex h-[56px] items-center text-sm text-slate-400 dark:text-slate-500">暂无数据</div>
    }

    const maxP = Math.max(high, low, open, close)
    const minP = Math.min(high, low, open, close)
    const range = maxP - minP || Math.max(maxP * 0.01, 0.01)
    const toY = (v: number) => 4 + ((maxP - v) / range) * 40

    const isUp = close >= open
    const color = isUp ? '#e11d48' : '#059669'
    const bodyTop = Math.min(toY(open), toY(close))
    const bodyH = Math.max(Math.abs(toY(close) - toY(open)), 2)

    return (
        <div className="flex items-center gap-2 self-center">
            <svg width="40" height="48" viewBox="0 0 40 48" className="shrink-0 overflow-visible">
                <rect x="0.5" y="0.5" width="39" height="47" rx="10" className="fill-slate-50 stroke-slate-200 dark:fill-slate-800 dark:stroke-slate-700" />
                <line x1="20" y1={toY(high)} x2="20" y2={toY(low)} stroke={color} strokeWidth="2" strokeLinecap="round" />
                <rect x="13" y={bodyTop} width="14" height={bodyH} rx="3" fill={isUp ? '#ffe4e6' : '#dcfce7'} stroke={color} strokeWidth="1.5" />
            </svg>
            <div className="space-y-0.5 text-[11px] text-slate-500 dark:text-slate-400">
                <div>开 {fmtPrice(open)}</div>
                <div className={isUp ? 'text-rose-600 dark:text-rose-400' : 'text-emerald-600 dark:text-emerald-400'}>收 {fmtPrice(close)}</div>
            </div>
        </div>
    )
}

/* ─── Day Range (当日区间) ──────────────────────────────────────────────── */

function DayRange({ item }: { item: WatchlistBoardItem }) {
    const low = item.day_low
    const high = item.day_high
    const live = item.live_price
    const prevClose = item.prev_close
    const open = item.day_open

    const liveP = rangeProgress(low, high, live)
    const prevP = rangeProgress(low, high, prevClose)
    const openP = rangeProgress(low, high, open)

    return (
        <div className="self-center space-y-2">
            <div className="flex items-center gap-2">
                <span className="w-12 text-right text-xs text-slate-400">{fmtPrice(low)}</span>
                <div className="relative h-5 flex-1">
                    <div className="absolute inset-y-0 left-0 right-0 my-auto h-1 rounded-full bg-slate-300 dark:bg-slate-600" />
                    {/* 昨收 红线 */}
                    {prevP != null && <div className="absolute top-1/2 h-3.5 w-1 -translate-y-1/2 rounded-full bg-rose-500 shadow-sm" style={{ left: `calc(${prevP}% - 2px)` }} />}
                    {/* 今开 绿线 */}
                    {openP != null && <div className="absolute top-1/2 h-3.5 w-1 -translate-y-1/2 rounded-full bg-emerald-500 shadow-sm" style={{ left: `calc(${openP}% - 2px)` }} />}
                    {/* 现价 蓝线 */}
                    {liveP != null && <div className="absolute top-1/2 h-4 w-1.5 -translate-y-1/2 rounded-full bg-blue-500 shadow-sm" style={{ left: `calc(${liveP}% - 3px)` }} />}
                </div>
                <span className="w-12 text-xs text-slate-400">{fmtPrice(high)}</span>
            </div>
            <div className="flex items-center justify-center gap-3 text-[11px]">
                <span className="inline-flex items-center gap-1"><span className="h-2 w-0.5 rounded-full bg-rose-500" /><span className="text-slate-500 dark:text-slate-400">昨收 {fmtPrice(prevClose)}</span></span>
                <span className="inline-flex items-center gap-1"><span className="h-2 w-0.5 rounded-full bg-emerald-500" /><span className="text-slate-500 dark:text-slate-400">今开 {fmtPrice(open)}</span></span>
                <span className="inline-flex items-center gap-1"><span className="h-2 w-0.5 rounded-full bg-blue-500" /><span className="text-slate-500 dark:text-slate-400">现价 {fmtPrice(live)}</span></span>
            </div>
        </div>
    )
}

/* ─── Sort Header ───────────────────────────────────────────────────────── */

function SortHeader({ label, sortKey, activeKey, asc, onToggle, center }: {
    label: string
    sortKey: 'name' | 'pct' | 'state'
    activeKey: string | null
    asc: boolean
    onToggle: (key: 'name' | 'pct' | 'state') => void
    center?: boolean
}) {
    const active = activeKey === sortKey
    return (
        <button
            type="button"
            onClick={() => onToggle(sortKey)}
            className={`inline-flex items-center gap-1 text-xs font-medium tracking-wider transition-colors ${
                active ? 'text-blue-600 dark:text-blue-400' : 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-300'
            } ${center ? 'justify-center w-full' : ''}`}
        >
            {label}
            {active ? (
                asc ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />
            ) : (
                <ChevronsUpDown className="h-3 w-3 opacity-40" />
            )}
        </button>
    )
}

/* ─── Market State Badge ─────────────────────────────────────────────────── */

const STATE_MAP: Record<string, { label: string; color: string }> = {
    // ── HK 港股 ──
    NONE:              { label: '无交易',   color: 'bg-slate-400 text-white dark:bg-slate-600' },
    AUCTION:           { label: '竞价',     color: 'bg-amber-400 text-white' },
    WAITING_OPEN:      { label: '等待开盘', color: 'bg-amber-400 text-white' },
    MORNING:           { label: '早盘',     color: 'bg-emerald-500 text-white' },
    REST:              { label: '午休',     color: 'bg-slate-400 text-white dark:bg-slate-600' },
    AFTERNOON:         { label: '午盘',     color: 'bg-emerald-500 text-white' },
    HK_CAS:            { label: '盘后竞价', color: 'bg-amber-400 text-white' },
    CLOSED:            { label: '休市',     color: 'bg-slate-400 text-white dark:bg-slate-600' },
    // ── US 美股 ──
    PRE_MARKET_BEGIN:  { label: '盘前',     color: 'bg-amber-400 text-white' },
    PRE_MARKET_END:    { label: '盘前结束', color: 'bg-slate-400 text-white dark:bg-slate-600' },
    AFTER_HOURS_BEGIN: { label: '盘后',     color: 'bg-amber-400 text-white' },
    AFTER_HOURS_END:   { label: '盘后收盘', color: 'bg-slate-400 text-white dark:bg-slate-600' },
    NIGHT_OPEN:        { label: '夜盘',     color: 'bg-indigo-400 text-white' },
    NIGHT_END:         { label: '夜盘收盘', color: 'bg-slate-400 text-white dark:bg-slate-600' },
    OVERNIGHT:         { label: '夜盘',     color: 'bg-indigo-400 text-white' },
    // ── 期货 ──
    FUTURE_DAY_OPEN:      { label: '期指开盘', color: 'bg-emerald-500 text-white' },
    FUTURE_DAY_BREAK:     { label: '期指休市', color: 'bg-slate-400 text-white dark:bg-slate-600' },
    FUTURE_DAY_CLOSE:     { label: '期指收盘', color: 'bg-slate-400 text-white dark:bg-slate-600' },
    FUTURE_DAY_WAIT_OPEN: { label: '期指待开', color: 'bg-amber-400 text-white' },
}

function MarketStateBadge({ state, symbol }: { state?: string | null; symbol?: string }) {
    // Extract market from symbol
    const market = symbol?.endsWith('.HK') ? 'HK' : symbol ? 'US' : null
    const marketLabel = market === 'HK' ? '🇭🇰' : market === 'US' ? '🇺🇸' : null

    if (!state) return (
        <div className="self-center text-center">
            {marketLabel && <div className="text-xs">{marketLabel}</div>}
            <div className="text-xs text-slate-400">--</div>
        </div>
    )
    const info = STATE_MAP[state]
    const label = info?.label ?? state
    const color = info?.color ?? 'bg-slate-300 text-slate-600 dark:bg-slate-700 dark:text-slate-300'
    return (
        <div className="self-center text-center">
            {marketLabel && <div className="text-xs">{marketLabel}</div>}
            <span className={`inline-block rounded-full px-2 py-0.5 text-[11px] font-medium leading-tight ${color}`}>
                {label}
            </span>
        </div>
    )
}

/* ─── Helpers ───────────────────────────────────────────────────────────── */

function rangeProgress(low?: number | null, high?: number | null, value?: number | null): number | null {
    if (low == null || high == null || value == null) return null
    if (!Number.isFinite(low) || !Number.isFinite(high) || !Number.isFinite(value)) return null
    const min = Math.min(low, high)
    const max = Math.max(low, high)
    if (max === min) return 50
    return Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100))
}

function extractName(name: string): string {
    // "02824 (黄金矿业ETF-易方达)" -> "黄金矿业ETF-易方达"
    const m = name.match(/\((.+)\)/)
    return m ? m[1] : name
}

function fmtPrice(v: number | null | undefined): string {
    if (v == null) return '--'
    return v.toLocaleString('en-US', { minimumFractionDigits: 3, maximumFractionDigits: 3 })
}

function fmtPct(v: number | null | undefined): string {
    if (v == null) return '--'
    return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

function fmtSignedPrice(v: number | null | undefined): string {
    if (v == null) return ''
    return `${v >= 0 ? '+' : ''}${v.toFixed(2)}`
}

function fmtVol(v: number | null | undefined): string {
    if (v == null) return '--'
    if (v >= 1e8) return `${(v / 1e8).toFixed(1)}亿`
    if (v >= 1e4) return `${(v / 1e4).toFixed(0)}万`
    return v.toLocaleString()
}

function fmtAmount(v: number | null | undefined): string {
    if (v == null) return '--'
    if (v >= 1e12) return `${(v / 1e12).toFixed(2)}万亿`
    if (v >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
    if (v >= 1e4) return `${(v / 1e4).toFixed(0)}万`
    return v.toLocaleString()
}
