import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import {
    Activity,
    ArrowDownRight,
    ArrowUpRight,
    ImagePlus,
    Loader2,
    Plus,
    RefreshCw,
    Search,
    Star,
    TrendingUp,
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

    // Board state
    const [board, setBoard] = useState<WatchlistBoardResponse | null>(null)
    const [loading, setLoading] = useState(true)
    const [refreshing, setRefreshing] = useState(false)
    const [error, setError] = useState<string | null>(null)

    // Search state
    const [searchQuery, setSearchQuery] = useState('')
    const [searchResults, setSearchResults] = useState<StockSearchResult[]>([])
    const [searchLoading, setSearchLoading] = useState(false)
    const [showDropdown, setShowDropdown] = useState(false)
    const [adding, setAdding] = useState(false)
    const [vlmParsing, setVlmParsing] = useState(false)
    const [feedback, setFeedback] = useState<{
        tone: 'success' | 'warning' | 'error'
        message: string
        details: string[]
    } | null>(null)
    const searchTimerRef = useRef<ReturnType<typeof setTimeout>>()
    const dropdownRef = useRef<HTMLDivElement>(null)
    const fileInputRef = useRef<HTMLInputElement>(null)

    const trimmedQuery = searchQuery.trim()
    const isBatchInput = trimmedQuery.length > 0 && WATCHLIST_BATCH_SPLIT_RE.test(trimmedQuery)
    const items = board?.items || []
    const refreshSeconds = board?.refresh_interval_seconds || 20

    // Load board
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
        let cancelled = false
        void loadBoard(false)
        const intervalId = window.setInterval(() => {
            if (!cancelled) void loadBoard(true)
        }, refreshSeconds * 1000)
        return () => { cancelled = true; window.clearInterval(intervalId) }
    }, [refreshSeconds, user?.id])

    // Close dropdown on outside click
    useEffect(() => {
        const handler = (e: MouseEvent) => {
            if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
                setShowDropdown(false)
            }
        }
        document.addEventListener('mousedown', handler)
        return () => document.removeEventListener('mousedown', handler)
    }, [])

    // Debounced search
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
            setFeedback({ tone: 'success', message: `已添加 ${item.name || symbol} 到自选`, details: [] })
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
                ? 'error'
                : response.summary.added > 0
                    ? 'success'
                    : 'warning'
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

    const handleImageUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0]
        if (!file) return
        e.target.value = ''
        setVlmParsing(true)
        setFeedback(null)
        try {
            const result = await api.parsePositionImage(file)
            if (result.positions.length === 0) {
                setFeedback({ tone: 'error', message: '未从截图中识别到股票代码', details: [] })
                return
            }
            const symbols = result.positions.map(p => p.symbol).join(',')
            setSearchQuery(symbols)
            setFeedback({ tone: 'success', message: `已识别 ${result.positions.length} 只，请确认后添加`, details: [] })
        } catch (err) {
            setFeedback({ tone: 'error', message: err instanceof Error ? err.message : '图片解析失败', details: [] })
        } finally {
            setVlmParsing(false)
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
            {/* Header */}
            <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">优质自选</h1>
                    <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">添加关注标的，实时跟踪行情与分析</p>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
                    <span className="rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-700/70">
                        自动刷新：{refreshSeconds}s
                    </span>
                    <span className="rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-700/70">
                        上一交易日：{board?.previous_trade_date || '--'}
                    </span>
                    <span className="rounded-full bg-emerald-50 px-3 py-1 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400">
                        共 {items.length} 只
                    </span>
                </div>
            </div>

            {/* Add Watchlist Section */}
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
                                placeholder="搜索代码/名称，批量粘贴，或点右侧📷上传截图识别"
                                className="input pl-9 pr-10 w-full"
                            />
                            <button
                                type="button"
                                onClick={() => fileInputRef.current?.click()}
                                disabled={vlmParsing}
                                className="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 rounded-lg text-slate-400 hover:text-indigo-500 hover:bg-indigo-50 transition-colors disabled:opacity-40 dark:hover:bg-indigo-500/10"
                                title="上传截图批量添加"
                            >
                                {vlmParsing ? <Loader2 className="w-4 h-4 animate-spin" /> : <ImagePlus className="w-4 h-4" />}
                            </button>
                            <input
                                ref={fileInputRef}
                                type="file"
                                accept="image/*"
                                className="hidden"
                                onChange={handleImageUpload}
                            />
                            {searchLoading && !vlmParsing && <Loader2 className="absolute right-9 top-1/2 -translate-y-1/2 w-4 h-4 animate-spin text-slate-400" />}
                        </div>
                        <button
                            type="button"
                            onClick={() => void submitInput()}
                            disabled={!trimmedQuery || adding}
                            className="btn-primary inline-flex items-center justify-center gap-2 whitespace-nowrap shrink-0"
                        >
                            {adding ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                            {isBatchInput ? '批量添加' : '添加'}
                        </button>
                    </div>

                    {feedback && (
                        <div className={`rounded-xl border px-3 py-3 text-sm ${
                            feedback.tone === 'success'
                                ? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/20 dark:bg-emerald-500/10 dark:text-emerald-300'
                                : feedback.tone === 'warning'
                                    ? 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-500/20 dark:bg-amber-500/10 dark:text-amber-300'
                                    : 'border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-500/20 dark:bg-rose-500/10 dark:text-rose-300'
                        }`}>
                            <div>{feedback.message}</div>
                            {feedback.details.length > 0 && (
                                <div className="mt-2 space-y-1 text-xs opacity-90">
                                    {feedback.details.map(d => <div key={d}>{d}</div>)}
                                </div>
                            )}
                        </div>
                    )}

                    {/* Search dropdown */}
                    {showDropdown && searchResults.length > 0 && (
                        <div className="border border-slate-200 dark:border-slate-700 rounded-lg bg-white dark:bg-slate-800 shadow-lg max-h-60 overflow-y-auto">
                            {searchResults.map(r => (
                                <button
                                    key={r.symbol}
                                    onClick={() => void addToWatchlist(r.symbol)}
                                    className="w-full flex items-center gap-3 px-3 py-2 text-left hover:bg-slate-50 dark:hover:bg-slate-700/50 transition-colors"
                                >
                                    <span className="text-sm font-medium text-slate-900 dark:text-slate-100">{r.name}</span>
                                    <span className="text-xs text-slate-400">{r.symbol}</span>
                                    <Plus className="w-3.5 h-3.5 text-blue-500 ml-auto" />
                                </button>
                            ))}
                        </div>
                    )}
                </div>
            </div>

            {/* Board Content */}
            {loading && !board ? (
                <div className="flex items-center justify-center py-12 text-slate-500 dark:text-slate-400">
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    正在加载优质自选...
                </div>
            ) : items.length === 0 ? (
                <div className="py-10 text-center">
                    <Star className="mx-auto mb-3 h-12 w-12 text-slate-300 dark:text-slate-600" />
                    <p className="text-slate-600 dark:text-slate-300">暂无自选标的</p>
                    <p className="mt-1 text-sm text-slate-400 dark:text-slate-500">
                        在上方搜索添加股票，即可实时跟踪行情。
                    </p>
                </div>
            ) : (
                <WatchlistBoardTable
                    items={items}
                    refreshing={refreshing}
                    error={error}
                    onAnalyze={symbol => navigate(`/analysis?symbol=${symbol}`)}
                    onRemove={symbol => void removeWatchlist(symbol)}
                />
            )}
        </div>
    )
}

/* ─── Board Table ──────────────────────────────────────────────────────── */

function WatchlistBoardTable({
    items,
    refreshing,
    error,
    onAnalyze,
    onRemove,
}: {
    items: WatchlistBoardItem[]
    refreshing: boolean
    error: string | null
    onAnalyze: (symbol: string) => void
    onRemove: (symbol: string) => void
}) {
    return (
        <div className="overflow-hidden rounded-[24px] border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-900">
            <div className="overflow-x-auto">
                <div className="min-w-[1000px]">
                    {/* Header */}
                    <div className="grid grid-cols-[1.5fr_0.8fr_0.8fr_0.8fr_1.2fr_0.8fr_0.8fr] gap-4 border-b border-slate-200 bg-slate-50 px-5 py-3 text-xs font-medium tracking-[0.12em] text-slate-500 dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-400">
                        <div>标的</div>
                        <div>最新价</div>
                        <div>涨跌幅</div>
                        <div>当日区间</div>
                        <div>交易建议</div>
                        <div>成交量</div>
                        <div>操作</div>
                    </div>

                    {/* Rows */}
                    {items.map(item => (
                        <WatchlistRow
                            key={item.symbol}
                            item={item}
                            onAnalyze={onAnalyze}
                            onRemove={onRemove}
                        />
                    ))}
                </div>
            </div>

            {/* Footer */}
            <div className="flex flex-col gap-2 border-t border-slate-200 px-5 py-4 text-sm text-slate-500 md:flex-row md:items-center md:justify-between dark:border-slate-700 dark:text-slate-400">
                <div className="flex items-center gap-2">
                    <span className={`inline-flex h-2.5 w-2.5 rounded-full ${error ? 'bg-amber-400' : 'bg-emerald-400'}`} />
                    <span>{error ? `刷新异常：${error}` : '实时监控中'}</span>
                    {refreshing && <RefreshCw className="h-3.5 w-3.5 animate-spin text-slate-400" />}
                </div>
                <div className="text-slate-400">共 {items.length} 只标的</div>
            </div>
        </div>
    )
}

/* ─── Single Row ───────────────────────────────────────────────────────── */

function WatchlistRow({
    item,
    onAnalyze,
    onRemove,
}: {
    item: WatchlistBoardItem
    onAnalyze: (symbol: string) => void
    onRemove: (symbol: string) => void
}) {
    const priceChangePct = item.price_change_pct ?? null
    const isUp = (priceChangePct ?? 0) >= 0
    const priceColor = priceChangePct == null
        ? 'text-slate-800 dark:text-slate-200'
        : isUp
            ? 'text-rose-600 dark:text-rose-400'
            : 'text-emerald-600 dark:text-emerald-400'

    const analysis = item.analysis
    const hasAnalysis = analysis != null

    return (
        <div className="grid grid-cols-[1.5fr_0.8fr_0.8fr_0.8fr_1.2fr_0.8fr_0.8fr] gap-4 border-b border-slate-200 px-5 py-5 last:border-b-0 dark:border-slate-700">
            {/* Name + Symbol */}
            <div className="min-w-0">
                <div className="truncate text-[18px] font-semibold text-slate-900 dark:text-slate-100">{item.name}</div>
                <div className="mt-1 flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400">
                    <span>{item.symbol}</span>
                </div>
            </div>

            {/* Price */}
            <div className={`self-center text-[22px] font-semibold ${priceColor}`}>
                {formatPrice(item.live_price)}
            </div>

            {/* Change % */}
            <div className="self-center">
                <span className={`inline-flex min-w-[96px] items-center justify-center rounded-full px-3 py-2 text-[18px] font-semibold ${
                    priceChangePct == null
                        ? 'bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500'
                        : isUp
                            ? 'bg-rose-50 text-rose-600 dark:bg-rose-500/10 dark:text-rose-400'
                            : 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400'
                }`}>
                    {formatSignedPercent(priceChangePct)}
                </span>
            </div>

            {/* Day Range */}
            <div className="space-y-1 self-center text-xs text-slate-500 dark:text-slate-400">
                <div className="flex items-center gap-1">
                    <span className="text-rose-500">高</span> {formatPrice(item.day_high)}
                </div>
                <div className="flex items-center gap-1">
                    <span className="text-emerald-500">低</span> {formatPrice(item.day_low)}
                </div>
            </div>

            {/* Analysis Summary */}
            <div className="self-center min-w-0">
                {hasAnalysis ? (
                    <div className="space-y-1">
                        <div className="flex items-center gap-2">
                            {analysis.direction === 'bullish' ? (
                                <ArrowUpRight className="h-4 w-4 text-rose-500 shrink-0" />
                            ) : analysis.direction === 'bearish' ? (
                                <ArrowDownRight className="h-4 w-4 text-emerald-500 shrink-0" />
                            ) : (
                                <Activity className="h-4 w-4 text-slate-400 shrink-0" />
                            )}
                            <span className="truncate text-sm font-medium text-slate-700 dark:text-slate-300">
                                {analysis.decision || analysis.direction || '--'}
                            </span>
                        </div>
                        {analysis.trader_advice_summary && (
                            <div className="truncate text-xs text-slate-400 dark:text-slate-500" title={analysis.trader_advice_summary}>
                                {analysis.trader_advice_summary}
                            </div>
                        )}
                        <button
                            type="button"
                            onClick={() => onAnalyze(item.symbol)}
                            className="text-xs text-blue-500 hover:text-blue-600 dark:text-blue-400"
                        >
                            查看报告 →
                        </button>
                    </div>
                ) : (
                    <span className="text-xs text-slate-400 dark:text-slate-500">暂无分析</span>
                )}
            </div>

            {/* Volume */}
            <div className="self-center text-sm text-slate-600 dark:text-slate-400">
                {formatVolume(item.volume)}
            </div>

            {/* Actions */}
            <div className="self-center flex items-center gap-2">
                <button
                    type="button"
                    onClick={() => onAnalyze(item.symbol)}
                    className="inline-flex items-center gap-1 rounded-lg bg-blue-50 px-2.5 py-1.5 text-xs font-medium text-blue-600 transition-colors hover:bg-blue-100 dark:bg-blue-500/10 dark:text-blue-400 dark:hover:bg-blue-500/20"
                >
                    <TrendingUp className="h-3.5 w-3.5" />
                    分析
                </button>
                <button
                    type="button"
                    onClick={() => onRemove(item.symbol)}
                    className="inline-flex items-center gap-1 rounded-lg bg-slate-50 px-2.5 py-1.5 text-xs font-medium text-slate-500 transition-colors hover:bg-rose-50 hover:text-rose-600 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-rose-500/10 dark:hover:text-rose-400"
                    title="移除自选"
                >
                    移除
                </button>
            </div>
        </div>
    )
}

/* ─── Formatters ────────────────────────────────────────────────────────── */

function formatPrice(v: number | null | undefined): string {
    if (v == null) return '--'
    return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function formatSignedPercent(v: number | null | undefined): string {
    if (v == null) return '--'
    const sign = v >= 0 ? '+' : ''
    return `${sign}${v.toFixed(2)}%`
}

function formatVolume(v: number | null | undefined): string {
    if (v == null) return '--'
    if (v >= 1e8) return `${(v / 1e8).toFixed(1)}亿`
    if (v >= 1e4) return `${(v / 1e4).toFixed(0)}万`
    return v.toLocaleString()
}
