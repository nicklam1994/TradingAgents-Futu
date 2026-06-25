/**
 * SimTrading — 模拟交易页面
 *
 * 布局：资产卡片 → 当前持仓(全宽) → 左：选股下单 / 右：当日订单+成交记录
 */

import { useEffect, useState, useCallback, useRef } from 'react'
import {
    Wallet, TrendingUp, TrendingDown, Package,
    RefreshCw, ArrowUpCircle, ArrowDownCircle,
    Search, Loader2,
} from 'lucide-react'

import { api } from '@/services/api'
import { formatTime } from '@/utils/formatTime'
import type { SimAccount, SimPosition, SimOrder, SimDeal, StockSearchResult } from '@/types'

const MARKET_LABELS: Record<string, string> = { HK: '港股', US: '美股' }
const MARKET_FLAGS: Record<string, string> = { HK: '🇭🇰', US: '🇺🇸' }
const ORDER_TYPE_LABEL: Record<string, string> = {
    NORMAL: 'LMT', MARKET: 'MKT',
    AUCTION_LIMIT: 'AUCTION_LMT', AUCTION: 'AUCTION_MKT',
    STOP: 'STOP', STOP_LIMIT: 'STOP_LMT',
}

interface QuoteData {
    price: number; change: number; change_pct: number
    open: number; high: number; low: number; volume: number
    name?: string
}

export default function SimTrading() {
    const [accounts, setAccounts] = useState<SimAccount[]>([])
    const [activeMarket, setActiveMarket] = useState<string>('HK')
    const [positions, setPositions] = useState<SimPosition[]>([])
    const [posSortKey, setPosSortKey] = useState<'name' | 'pnl' | 'pnl_pct' | 'qty' | 'val' | 'today'>('pnl')
    const [posSortAsc, setPosSortAsc] = useState(false)
    const [orders, setOrders] = useState<SimOrder[]>([])
    const [deals, setDeals] = useState<SimDeal[]>([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)
    const wsRef = useRef<WebSocket | null>(null)
    const [wsConnected, setWsConnected] = useState(false)

    // ── Trading panel state ──
    const [searchQuery, setSearchQuery] = useState('')
    const [searchResults, setSearchResults] = useState<StockSearchResult[]>([])
    const [searchLoading, setSearchLoading] = useState(false)
    const [showDropdown, setShowDropdown] = useState(false)
    const dropdownRef = useRef<HTMLDivElement>(null)
    const searchTimerRef = useRef<ReturnType<typeof setTimeout>>()
    const [selectedStock, setSelectedStock] = useState<{ code: string; name: string } | null>(null)
    const selectedStockRef = useRef(selectedStock)
    selectedStockRef.current = selectedStock
    const [quote, setQuote] = useState<QuoteData | null>(null)
    const prevQuotePriceRef = useRef<number | null>(null)
    const quotePriceRef = useRef<HTMLSpanElement>(null)
    // 价格变动闪光
    useEffect(() => {
        const p = quote?.price
        const prev = prevQuotePriceRef.current
        prevQuotePriceRef.current = p ?? null
        if (p != null && p > 0 && prev != null && p !== prev && quotePriceRef.current) {
            const el = quotePriceRef.current
            const cls = p > prev ? 'flash-up' : 'flash-down'
            el.classList.remove('flash-up', 'flash-down')
            void el.offsetWidth
            el.classList.add(cls)
            const t = setTimeout(() => el.classList.remove(cls), 600)
            return () => clearTimeout(t)
        }
    }, [quote?.price])

    const [orderSide, setOrderSide] = useState<'BUY' | 'SELL'>('BUY')
    const [orderType, setOrderType] = useState('NORMAL')
    const [orderPrice, setOrderPrice] = useState('')
    const [orderQty, setOrderQty] = useState('')
    const [triggerPrice, setTriggerPrice] = useState('')
    const [submitting, setSubmitting] = useState(false)
    const [orderMsg, setOrderMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null)
    const [modifyOrderId, setModifyOrderId] = useState<string | null>(null)

    const isStopOrder = orderType === 'STOP_LIMIT' || orderType === 'STOP'
    const isMarketOrder = orderType === 'MARKET'
    const effectivePrice = parseFloat(orderPrice) || quote?.price || 0
    const orderAmount = effectivePrice * (parseFloat(orderQty) || 0)

    // ── Data loading ──
    const loadAccounts = useCallback(async () => {
        try { const res = await api.getSimAllAccounts(); if (res.ok) setAccounts(res.data ?? []) } catch {}
    }, [])

    const loadMarketData = useCallback(async () => {
        setLoading(true); setError(null)
        try {
            const [posRes, ordRes, dealRes] = await Promise.allSettled([
                api.getSimPositions(activeMarket),
                api.getSimOrders(undefined, activeMarket),
                api.getSimDeals(),
            ])
            if (posRes.status === 'fulfilled') setPositions(posRes.value.data ?? [])
            if (ordRes.status === 'fulfilled') setOrders(ordRes.value.data ?? [])
            if (dealRes.status === 'fulfilled') setDeals((dealRes.value.data ?? []).filter(d => (d.deal_market ?? '') === activeMarket))
            const err = [posRes, ordRes, dealRes].find(r => r.status === 'rejected')
            if (err?.status === 'rejected') setError(err.reason?.message ?? '加载失败')
        } catch (e) { setError(e instanceof Error ? e.message : '加载失败') }
        finally { setLoading(false) }
    }, [activeMarket])

    useEffect(() => { loadAccounts() }, [loadAccounts])
    useEffect(() => { loadMarketData() }, [loadMarketData])

    // ── WebSocket ──
    useEffect(() => {
        const token = localStorage.getItem('ta-access-token') || ''
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
        const ws = new WebSocket(`${protocol}//${window.location.host}/ws/quotes?token=${token}`)
        wsRef.current = ws
        ws.onopen = () => setWsConnected(true)
        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data)
                // Normalize code to canonical format for comparison
                const toCanonical = (c: string) => {
                    if (c.startsWith('HK.')) return c.slice(3) + '.HK'
                    if (c.startsWith('US.')) return c.slice(3) + '.US'
                    return c
                }
                const updateQuote = (code: string, q: Record<string, number | string>) => {
                    const sel = selectedStockRef.current
                    if (!sel) return
                    // Compare in canonical format
                    if (toCanonical(code) !== toCanonical(sel.code)) return
                    setQuote(prev => prev ? {
                        ...prev,
                        price: (q.price as number) ?? prev.price,
                        change: (q.change as number) ?? prev.change,
                        change_pct: (q.change_pct as number) ?? prev.change_pct,
                        open: (q.open as number) ?? prev.open,
                        high: (q.high as number) ?? prev.high,
                        low: (q.low as number) ?? prev.low,
                        volume: (q.volume as number) ?? prev.volume,
                    } : prev)
                }
                if (msg.type === 'quotes' && msg.data) {
                    setPositions(prev => prev.map(p => {
                        const q = msg.data[p.code]; if (!q) return p
                        const np = q.price ?? p.current_price
                        return { ...p, current_price: np, market_val: r3(np * p.qty), unrealized_pnl: r3((np - p.cost_price) * p.qty), unrealized_pnl_pct: p.cost_price > 0 ? r2((np / p.cost_price - 1) * 100) : p.unrealized_pnl_pct, today_pnl: p.prev_close ? r3((np - p.prev_close) * p.qty) : p.today_pnl }
                    }))
                    for (const [code, q] of Object.entries(msg.data)) updateQuote(code, q as Record<string, number | string>)
                }
                if (msg.type === 'quote_update' && msg.symbol && msg.data) {
                    const q = msg.data
                    setPositions(prev => prev.map(p => {
                        if (toCanonical(p.code) !== toCanonical(msg.symbol)) return p
                        const np = q.price ?? p.current_price
                        return { ...p, current_price: np, market_val: r3(np * p.qty), unrealized_pnl: r3((np - p.cost_price) * p.qty), unrealized_pnl_pct: p.cost_price > 0 ? r2((np / p.cost_price - 1) * 100) : p.unrealized_pnl_pct, today_pnl: p.prev_close ? r3((np - p.prev_close) * p.qty) : p.today_pnl }
                    }))
                    updateQuote(msg.symbol, q)
                }
            } catch {}
        }
        ws.onclose = () => setWsConnected(false)
        ws.onerror = () => setWsConnected(false)
        return () => { ws.close() }
    }, [])

    // Subscribe to all relevant symbols (positions + selected stock)
    const subscribeAll = useCallback(() => {
        if (wsRef.current?.readyState !== WebSocket.OPEN) return
        const symbols = new Set<string>()
        positions.forEach(p => symbols.add(p.code))
        if (selectedStockRef.current) symbols.add(selectedStockRef.current.code)
        if (symbols.size > 0) {
            wsRef.current.send(JSON.stringify({ type: 'replace', symbols: Array.from(symbols) }))
        }
    }, [positions])

    // Re-subscribe when positions change
    useEffect(() => { subscribeAll() }, [positions.map(p => p.code).join(','), subscribeAll])

    // ── Search ──
    useEffect(() => {
        if (searchTimerRef.current) clearTimeout(searchTimerRef.current)
        const q = searchQuery.trim()
        if (!q) { setSearchResults([]); setShowDropdown(false); setSearchLoading(false); return }
        setSearchLoading(true)
        searchTimerRef.current = setTimeout(async () => {
            try { const res = await api.searchStocks(q); setSearchResults((res.results ?? []).slice(0, 8)); setShowDropdown(true) }
            catch { setSearchResults([]) }
            finally { setSearchLoading(false) }
        }, 300)
    }, [searchQuery])

    useEffect(() => {
        const h = (e: MouseEvent) => { if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) setShowDropdown(false) }
        document.addEventListener('mousedown', h)
        return () => document.removeEventListener('mousedown', h)
    }, [])

    const selectStock = (r: StockSearchResult) => {
        setSelectedStock({ code: r.symbol, name: r.name })
        setSearchQuery(''); setShowDropdown(false); setSearchResults([])
        setQuote({ price: 0, change: 0, change_pct: 0, open: 0, high: 0, low: 0, volume: 0, name: r.name })

        // Subscribe to selected stock + all positions (replace previous extra)
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            const symbols = new Set([r.symbol, ...positions.map(p => p.code)])
            wsRef.current.send(JSON.stringify({ type: 'replace', symbols: Array.from(symbols) }))
        }

        // Fallback: fetch quote via HTTP API immediately
        api.getStockQuote(r.symbol).then(res => {
            if (res.ok && res.data) {
                const q = res.data
                setQuote(prev => prev ? {
                    ...prev,
                    price: q.price ?? 0,
                    change: q.change ?? 0,
                    change_pct: q.change_pct ?? 0,
                    open: q.open ?? 0,
                    high: q.high ?? 0,
                    low: q.low ?? 0,
                    volume: q.volume ?? 0,
                } : prev)
            }
        }).catch(() => {})
    }

    // ── Order actions ──
    const cancelOrder = async (o: SimOrder) => {
        if (!confirm(`确认撤单 #${o.order_id}?`)) return
        try { await api.cancelSimOrder(o.order_id, o.code); loadMarketData() }
        catch (e) { alert(e instanceof Error ? e.message : '撤单失败') }
    }

    const loadOrderToForm = (o: SimOrder) => {
        setSelectedStock({ code: o.code, name: o.stock_name || o.code })
        setOrderSide(o.side as 'BUY' | 'SELL')
        setOrderType(o.order_type || 'NORMAL')
        setOrderPrice(o.price?.toFixed(3) ?? '')
        setOrderQty(String(o.qty ?? ''))
        setModifyOrderId(o.order_id)
        setOrderMsg(null)
        // Subscribe to this stock + all positions (replace previous extra)
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            const symbols = new Set([o.code, ...positions.map(p => p.code)])
            wsRef.current.send(JSON.stringify({ type: 'replace', symbols: Array.from(symbols) }))
        }
        setQuote({ price: 0, change: 0, change_pct: 0, open: 0, high: 0, low: 0, volume: 0, name: o.stock_name || o.code })
        // Fetch quote via HTTP API
        api.getStockQuote(o.code).then(res => {
            if (res.ok && res.data) {
                const q = res.data
                setQuote(prev => prev ? { ...prev, price: q.price ?? 0, change: q.change ?? 0, change_pct: q.change_pct ?? 0, open: q.open ?? 0, high: q.high ?? 0, low: q.low ?? 0, volume: q.volume ?? 0 } : prev)
            }
        }).catch(() => {})
    }

    const cancelModify = () => { setModifyOrderId(null); setOrderPrice(''); setOrderQty(''); setOrderMsg(null) }

    const submitOrder = async () => {
        if (!selectedStock) return
        setSubmitting(true); setOrderMsg(null)
        try {
            if (modifyOrderId) {
                await api.modifySimOrder(modifyOrderId, selectedStock.code, effectivePrice, parseFloat(orderQty) || 0)
                setOrderMsg({ type: 'ok', text: `订单已修改 #${modifyOrderId}` })
                setModifyOrderId(null)
            } else {
                const res = await api.placeSimOrder({ symbol: selectedStock.code, side: orderSide, quantity: parseFloat(orderQty) || 0, price: isMarketOrder ? 0 : effectivePrice, order_type: orderType })
                if (res.ok) {
                    setOrderMsg({ type: 'ok', text: `订单已提交 #${res.data?.order_id ?? ''}` })
                    setOrderPrice(''); setOrderQty(''); setTriggerPrice('')
                    // Add to watchlist so it gets permanent real-time subscription
                    api.addToWatchlist(selectedStock.code).catch(() => {})
                }
                else setOrderMsg({ type: 'err', text: '下单失败' })
            }
            // Reload market data (positions, orders, deals)
            await loadMarketData()
            // Re-subscribe to include new position
            setTimeout(() => subscribeAll(), 500)
        } catch (e) { setOrderMsg({ type: 'err', text: e instanceof Error ? e.message : '操作失败' }) }
        finally { setSubmitting(false) }
    }

    // ── Derived ──
    const activeAccount = accounts.find(a => a.market === activeMarket) ?? accounts[0] ?? null
    const totalMarketVal = positions.reduce((s, p) => s + (p.market_val || 0), 0)

    return (
        <div className="space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">模拟交易</h1>
                    <p className="mt-1 text-slate-500 dark:text-slate-400">
                        模拟账户资金、持仓与交易
                        <span className={`ml-2 inline-block h-2 w-2 rounded-full ${wsConnected ? 'bg-emerald-500' : 'bg-slate-400'}`} />
                        <span className="ml-1 text-xs">{wsConnected ? '实时' : '离线'}</span>
                    </p>
                </div>
                <button onClick={() => { loadAccounts(); loadMarketData() }} disabled={loading}
                    className="flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-600 transition hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800">
                    <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                    刷新
                </button>
            </div>

            {/* Sim Environment Notice */}
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 dark:border-amber-800/50 dark:bg-amber-950/20">
                <div className="flex items-center gap-2">
                    <span className="text-amber-600 dark:text-amber-400">⚠️</span>
                    <p className="text-sm text-amber-700 dark:text-amber-300">
                        <span className="font-medium">模拟环境</span> — 数据来自 Futu 模拟账户，不计算佣金/印花税/滑点。
                        回测引擎（策略管理）使用真实费用计算。真实交易费用请参考
                        <span className="mx-1 font-medium">HK: 万1.5 + 印花税0.13%</span> /
                        <span className="mx-1 font-medium">US: 零佣金</span>
                    </p>
                </div>
            </div>

            {error && <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-600 dark:border-rose-900/50 dark:bg-rose-950/30 dark:text-rose-300">{error}</div>}

            {/* Market Tabs */}
            <div className="flex gap-2">
                {accounts.map(acc => (
                    <button key={acc.market} onClick={() => setActiveMarket(acc.market)}
                        className={`flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-medium transition ${activeMarket === acc.market ? 'bg-blue-50 text-blue-700 ring-1 ring-blue-200 dark:bg-blue-500/10 dark:text-blue-400 dark:ring-blue-500/30' : 'bg-slate-50 text-slate-600 hover:bg-slate-100 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700'}`}>
                        <span>{MARKET_FLAGS[acc.market] ?? '🏳️'}</span>
                        <span>{MARKET_LABELS[acc.market] ?? acc.market}</span>
                        <span className="text-xs opacity-70">{acc.currency}</span>
                    </button>
                ))}
            </div>

            {/* Account Cards */}
            {activeAccount && (
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
                    <AccountCard icon={Wallet} label="总资产" value={fmt(activeAccount.total_assets)} subValue={activeAccount.currency} color="blue" />
                    <AccountCard icon={Package} label="可用资金" value={fmt(activeAccount.available_cash)} subValue={`冻结 ${fmt(activeAccount.frozen_cash)}`} color="green" />
                    <AccountCard icon={TrendingUp} label="持仓市值" value={fmt(activeAccount.market_val)} subValue={`${positions.length} 只股票`} color="purple" />
                    <AccountCard icon={activeAccount.unrealized_pnl >= 0 ? TrendingUp : TrendingDown} label="浮动盈亏" value={fmt(activeAccount.unrealized_pnl)} subValue={`已实现 ${fmt(activeAccount.realized_pnl)}`} color={activeAccount.unrealized_pnl >= 0 ? 'green' : 'red'} />
                </div>
            )}

            {/* Positions Table (full width) */}
            <div className="card">
                <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-100">当前持仓</h2>
                {(() => {
                    const sorted = [...positions].sort((a, b) => {
                        const dir = posSortAsc ? 1 : -1
                        switch (posSortKey) {
                            case 'name': return dir * (a.stock_name || a.code).localeCompare(b.stock_name || b.code, 'zh')
                            case 'pnl': return dir * (a.unrealized_pnl - b.unrealized_pnl)
                            case 'pnl_pct': return dir * (a.unrealized_pnl_pct - b.unrealized_pnl_pct)
                            case 'qty': return dir * (a.qty - b.qty)
                            case 'val': return dir * (a.market_val - b.market_val)
                            case 'today': return dir * (a.today_pnl - b.today_pnl)
                            default: return 0
                        }
                    })
                    const toggleSort = (key: typeof posSortKey) => {
                        if (posSortKey === key) setPosSortAsc(!posSortAsc)
                        else { setPosSortKey(key); setPosSortAsc(key === 'name') }
                    }
                    const sortIcon = (key: typeof posSortKey) => posSortKey === key ? (posSortAsc ? ' ↑' : ' ↓') : ''
                    return positions.length === 0 ? <EmptyState text="暂无持仓" /> : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="border-b border-slate-200 dark:border-slate-700">
                                    <th className="px-3 py-2 text-center font-medium text-slate-500">市场/状态</th>
                                    <th className="px-3 py-2 text-left font-medium text-slate-500 cursor-pointer hover:text-slate-700 dark:hover:text-slate-300 select-none" onClick={() => toggleSort('name')}>股票名称/代码{sortIcon('name')}</th>
                                    <th className="px-3 py-2 text-right font-medium text-slate-500 cursor-pointer hover:text-slate-700 dark:hover:text-slate-300 select-none" onClick={() => toggleSort('qty')}>持仓数量{sortIcon('qty')}</th>
                                    <th className="px-3 py-2 text-right font-medium text-slate-500">现价/成本价</th>
                                    <th className="px-3 py-2 text-right font-medium text-slate-500 cursor-pointer hover:text-slate-700 dark:hover:text-slate-300 select-none" onClick={() => toggleSort('val')}>市值/成本市值{sortIcon('val')}</th>
                                    <th className="px-3 py-2 text-right font-medium text-slate-500 cursor-pointer hover:text-slate-700 dark:hover:text-slate-300 select-none" onClick={() => toggleSort('pnl')}>持仓盈亏/盈亏比{sortIcon('pnl')}</th>
                                    <th className="px-3 py-2 text-right font-medium text-slate-500 cursor-pointer hover:text-slate-700 dark:hover:text-slate-300 select-none" onClick={() => toggleSort('today')}>今日盈亏{sortIcon('today')}</th>
                                    <th className="px-3 py-2 text-right font-medium text-slate-500">持仓%</th>
                                </tr>
                            </thead>
                            <tbody>
                                {sorted.map(p => {
                                    const posPct = totalMarketVal > 0 ? (p.market_val / totalMarketVal * 100) : 0
                                    return (
                                        <tr key={p.code} className="border-b border-slate-100 last:border-0 dark:border-slate-800">
                                            <td className="px-3 py-2 text-center">
                                                <MarketStateBadge state={p.market_state} symbol={p.code} />
                                            </td>
                                            <td className="px-3 py-2">
                                                <div className="font-medium text-slate-900 dark:text-slate-100">{p.stock_name || '--'}</div>
                                                <div className="text-xs text-slate-400">{displayCode(p.code)}</div>
                                            </td>
                                            <td className="px-3 py-2 text-right text-slate-700 dark:text-slate-300">{p.qty}</td>
                                            <td className="px-3 py-2 text-right">
                                                <div className="text-slate-900 dark:text-slate-100">{fmtPrice(p.current_price)}</div>
                                                <div className="text-xs text-slate-400">{fmtPrice(p.cost_price)}</div>
                                            </td>
                                            <td className="px-3 py-2 text-right">
                                                <div className="text-slate-900 dark:text-slate-100">{fmt(p.market_val)}</div>
                                                <div className="text-xs text-slate-400">{fmt(p.cost_val)}</div>
                                            </td>
                                            <td className="px-3 py-2 text-right">
                                                <div className={`font-medium ${p.unrealized_pnl >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>{fmt(p.unrealized_pnl)}</div>
                                                <div className={`text-xs ${p.unrealized_pnl_pct >= 0 ? 'text-emerald-500' : 'text-rose-500'}`}>{p.unrealized_pnl_pct >= 0 ? '+' : ''}{p.unrealized_pnl_pct?.toFixed(2)}%</div>
                                            </td>
                                            <td className={`px-3 py-2 text-right font-medium ${p.today_pnl >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                                                {p.prev_close ? fmt(p.today_pnl) : '--'}
                                            </td>
                                            <td className="px-3 py-2 text-right text-slate-700 dark:text-slate-300">{posPct.toFixed(1)}%</td>
                                        </tr>
                                    )
                                })}
                            </tbody>
                        </table>
                    </div>
                )})()}
            </div>

            {/* Bottom: Trading Panel + Orders/Deals */}
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">

                {/* ── Left: Trading Panel (2/5) ── */}
                <div className="space-y-4 lg:col-span-2">

                    {/* Search */}
                    <div className="card space-y-3" ref={dropdownRef}>
                        <div className="flex items-center gap-2">
                            <Search className="h-5 w-5 text-blue-500" />
                            <h2 className="font-semibold text-slate-900 dark:text-slate-100">选择股票</h2>
                        </div>
                        <div className="relative">
                            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                            <input type="text" value={searchQuery} onChange={e => setSearchQuery(e.target.value)}
                                onFocus={() => searchResults.length > 0 && setShowDropdown(true)}
                                placeholder="输入代码或名称搜索"
                                className="input w-full pl-9 pr-10" />
                            {searchLoading && <Loader2 className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-slate-400" />}
                        </div>
                        {showDropdown && searchResults.length > 0 && (
                            <div className="max-h-60 overflow-y-auto rounded-lg border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-800">
                                {searchResults.map(r => (
                                    <button key={r.symbol} onClick={() => selectStock(r)}
                                        className="flex w-full items-center gap-3 px-3 py-2 text-left transition-colors hover:bg-slate-50 dark:hover:bg-slate-700/50">
                                        <span className="text-sm font-medium text-slate-900 dark:text-slate-100">{r.name}</span>
                                        <span className="ml-auto text-xs text-slate-400">{r.symbol}</span>
                                    </button>
                                ))}
                            </div>
                        )}
                    </div>

                    {/* Quote Bar */}
                    {selectedStock && (
                        <div className="card">
                            <div className="flex items-baseline justify-between">
                                <div>
                                    <span className="text-lg font-bold text-slate-900 dark:text-slate-100">{quote?.name || selectedStock.name}</span>
                                    <span className="ml-2 text-sm text-slate-400">{displayCode(selectedStock.code)}</span>
                                </div>
                                {quote && quote.price > 0 ? (
                                    <div className="text-right">
                                        <span ref={quotePriceRef} className={`text-2xl font-bold ${quote.change >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>{fmtPrice(quote.price)}</span>
                                        <span className={`ml-2 text-sm font-medium ${quote.change >= 0 ? 'text-emerald-500' : 'text-rose-500'}`}>
                                            {quote.change >= 0 ? '+' : ''}{quote.change.toFixed(2)} ({quote.change_pct >= 0 ? '+' : ''}{quote.change_pct.toFixed(2)}%)
                                        </span>
                                    </div>
                                ) : <span className="text-sm text-slate-400">等待行情...</span>}
                            </div>
                            {quote && quote.price > 0 && (
                                <div className="mt-2 flex justify-between text-xs text-slate-500 dark:text-slate-400">
                                    <span>开 {fmtPrice(quote.open)}</span>
                                    <span>高 {fmtPrice(quote.high)}</span>
                                    <span>低 {fmtPrice(quote.low)}</span>
                                    <span>量 {fmtVol(quote.volume)}</span>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Order Form */}
                    <div className="card space-y-4">
                        <div className="flex items-center justify-between">
                            <h2 className="font-semibold text-slate-900 dark:text-slate-100">{modifyOrderId ? '改单' : '下单'}</h2>
                            {modifyOrderId && <button onClick={cancelModify} className="text-xs text-slate-400 hover:text-slate-600">取消修改</button>}
                        </div>

                        <div className="grid grid-cols-2 gap-2">
                            <button onClick={() => setOrderSide('BUY')} className={`rounded-lg py-2.5 text-sm font-semibold transition ${orderSide === 'BUY' ? 'bg-emerald-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700'}`}>模拟买入</button>
                            <button onClick={() => setOrderSide('SELL')} className={`rounded-lg py-2.5 text-sm font-semibold transition ${orderSide === 'SELL' ? 'bg-rose-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700'}`}>模拟卖出</button>
                        </div>

                        <div>
                            <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">类型</label>
                            <select value={orderType} onChange={e => setOrderType(e.target.value)} className="input w-full">
                                <option value="NORMAL">限价单</option>
                                <option value="MARKET">市价单</option>
                                <option value="STOP_LIMIT">止损限价单</option>
                                <option value="STOP">止损市价单</option>
                            </select>
                        </div>

                        {isStopOrder && (
                            <div>
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">触发价</label>
                                <input type="number" value={triggerPrice} onChange={e => setTriggerPrice(e.target.value)} placeholder="0.00" step="0.01" className="input w-full" />
                            </div>
                        )}

                        {!isMarketOrder && (
                            <div>
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">价格</label>
                                <input type="number" value={orderPrice} onChange={e => setOrderPrice(e.target.value)} placeholder="0.00" step="0.01" className="input w-full" />
                            </div>
                        )}

                        <div>
                            <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">数量</label>
                            <input type="number" value={orderQty} onChange={e => setOrderQty(e.target.value)} placeholder="0" step="1" min="1" className="input w-full" />
                        </div>

                        <div className="flex items-center justify-between rounded-lg bg-slate-50 px-3 py-2 text-sm dark:bg-slate-800">
                            <span className="text-slate-500 dark:text-slate-400">金额</span>
                            <span className="font-medium text-slate-900 dark:text-slate-100">{orderAmount > 0 ? `${activeAccount?.currency ?? 'USD'} ${fmt(orderAmount)}` : '--'}</span>
                        </div>
                        <p className="text-xs text-slate-400 dark:text-slate-500">* 模拟环境，不计佣金/印花税</p>
                        {quote && quote.price > 0 && !orderPrice && !isMarketOrder && (
                            <button type="button" onClick={() => setOrderPrice(quote.price.toFixed(3))} className="text-xs text-blue-500 hover:text-blue-600">
                                使用当前价 {fmtPrice(quote.price)}
                            </button>
                        )}

                        {orderMsg && (
                            <div className={`rounded-lg px-3 py-2 text-sm ${orderMsg.type === 'ok' ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400' : 'bg-rose-50 text-rose-700 dark:bg-rose-500/10 dark:text-rose-400'}`}>{orderMsg.text}</div>
                        )}
                        <button onClick={submitOrder} disabled={submitting || !orderQty || (!isMarketOrder && !orderPrice && !quote?.price)}
                            className={`w-full rounded-lg py-3 text-sm font-semibold text-white transition disabled:opacity-50 ${modifyOrderId ? 'bg-blue-600 hover:bg-blue-700' : orderSide === 'BUY' ? 'bg-emerald-600 hover:bg-emerald-700' : 'bg-rose-600 hover:bg-rose-700'}`}>
                            {submitting ? <Loader2 className="mx-auto h-4 w-4 animate-spin" /> : modifyOrderId ? '确认修改' : (orderSide === 'BUY' ? '买入' : '卖出') + ' ' + (selectedStock?.name ?? '')}
                        </button>
                    </div>
                </div>

                {/* ── Right: Orders + Deals (3/5) ── */}
                <div className="space-y-6 lg:col-span-3">

                    {/* Orders */}
                    <div className="card">
                        <div className="mb-4 flex items-center justify-between">
                            <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">当日订单</h2>
                            <button onClick={loadMarketData} className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-300" title="刷新订单"><RefreshCw className="h-4 w-4" /></button>
                        </div>
                        {orders.length === 0 ? <EmptyState text="暂无订单" /> : (
                            <div className="space-y-2">
                                {orders.map(o => (
                                    <div key={o.order_id} className="flex items-center justify-between rounded-lg bg-slate-50 px-3 py-2 dark:bg-slate-800">
                                        <div className="flex items-center gap-2">
                                            {o.side === 'BUY' ? <ArrowUpCircle className="h-4 w-4 text-emerald-500" /> : <ArrowDownCircle className="h-4 w-4 text-rose-500" />}
                                            <span className="font-medium text-slate-900 dark:text-slate-100">{o.stock_name || o.code}</span>
                                            <span className={`text-xs ${o.side === 'BUY' ? 'text-emerald-600' : 'text-rose-600'}`}>{o.side}</span>
                                            <span className="rounded bg-slate-200 px-1.5 py-0.5 text-xs text-slate-500 dark:bg-slate-700 dark:text-slate-400">{o.status}</span>
                                        </div>
                                        <div className="flex items-center gap-2">
                                            <div className="text-right text-sm text-slate-600 dark:text-slate-400">
                                                <div>{o.filled_qty ? `${o.filled_qty}/${o.qty}` : o.qty} x {o.price?.toFixed(3)}</div>
                                                <div className="text-xs text-slate-400">{formatTime(o.create_time)}</div>
                                            </div>
                                            {(o.status === 'SUBMITTED' || o.status === 'SUBMITTING' || o.status === 'WAITING') && (
                                                <div className="flex flex-col gap-1">
                                                    <button onClick={() => loadOrderToForm(o)} className="rounded px-2 py-0.5 text-xs text-blue-600 hover:bg-blue-50 dark:text-blue-400 dark:hover:bg-blue-500/10">改单</button>
                                                    <button onClick={() => cancelOrder(o)} className="rounded px-2 py-0.5 text-xs text-rose-600 hover:bg-rose-50 dark:text-rose-400 dark:hover:bg-rose-500/10">撤单</button>
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>

                    {/* Deals */}
                    <div className="card">
                        <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-100">成交记录</h2>
                        {deals.length === 0 ? <EmptyState text="暂无成交记录" /> : (
                            <div className="space-y-2">
                                {deals.map(d => (
                                    <div key={d.deal_id} className="flex items-center justify-between rounded-lg bg-slate-50 px-3 py-2 dark:bg-slate-800">
                                        <div className="flex items-center gap-2">
                                            {d.side === 'BUY' ? <ArrowUpCircle className="h-4 w-4 text-emerald-500" /> : <ArrowDownCircle className="h-4 w-4 text-rose-500" />}
                                            <span className="font-medium text-slate-900 dark:text-slate-100">{d.stock_name || displayCode(d.code)}</span>
                                            <span className={`text-xs ${d.side === 'BUY' ? 'text-emerald-600' : 'text-rose-600'}`}>{d.side}</span>
                                            <span className="rounded bg-slate-200 px-1.5 py-0.5 text-xs text-slate-500 dark:bg-slate-700 dark:text-slate-400">{ORDER_TYPE_LABEL[d.order_type] ?? d.order_type}</span>
                                        </div>
                                        <div className="text-right text-sm text-slate-600 dark:text-slate-400">
                                            <div>{d.qty} x {d.price?.toFixed(3)}</div>
                                            <div className="text-xs text-slate-400">{formatTime(d.create_time)}</div>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </div>
    )
}

/* ─── Sub-components ─── */

function AccountCard({ icon: Icon, label, value, subValue, color }: {
    icon: typeof Wallet; label: string; value: string; subValue?: string
    color: 'blue' | 'green' | 'purple' | 'red'
}) {
    const bgMap = { blue: 'from-blue-50 to-blue-100/50 dark:from-blue-950/30 dark:to-blue-900/20', green: 'from-emerald-50 to-emerald-100/50 dark:from-emerald-950/30 dark:to-emerald-900/20', purple: 'from-purple-50 to-purple-100/50 dark:from-purple-950/30 dark:to-purple-900/20', red: 'from-rose-50 to-rose-100/50 dark:from-rose-950/30 dark:to-rose-900/20' }
    const iconMap = { blue: 'text-blue-500', green: 'text-emerald-500', purple: 'text-purple-500', red: 'text-rose-500' }
    return (
        <div className={`rounded-2xl bg-gradient-to-br ${bgMap[color]} p-4`}>
            <div className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400"><Icon className={`h-4 w-4 ${iconMap[color]}`} />{label}</div>
            <div className="mt-2 text-2xl font-bold text-slate-900 dark:text-slate-100">{value}</div>
            {subValue && <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{subValue}</div>}
        </div>
    )
}

function EmptyState({ text }: { text: string }) {
    return <div className="flex items-center justify-center py-8 text-sm text-slate-400 dark:text-slate-500">{text}</div>
}

/* ─── Helpers ─── */

function fmt(v: number | undefined | null): string { if (v == null) return '--'; return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }
function fmtPrice(v: number | undefined | null): string { if (v == null || v === 0) return '--'; return v.toLocaleString('en-US', { minimumFractionDigits: 3, maximumFractionDigits: 3 }) }
function fmtVol(v: number | undefined | null): string { if (v == null || v === 0) return '--'; if (v >= 1e8) return (v / 1e8).toFixed(1) + '亿'; if (v >= 1e4) return (v / 1e4).toFixed(1) + '万'; return v.toLocaleString() }
function displayCode(code: string): string { 
    if (code.startsWith('HK.')) return code.slice(3) + '.HK'
    if (code.startsWith('US.')) return code.slice(3) + '.US'
    return code 
}
function r3(v: number): number { return Math.round(v * 1000) / 1000 }
function r2(v: number): number { return Math.round(v * 100) / 100 }

/* ── Market State Badge (from WatchlistBoard) ─────────────────────────── */

const STATE_MAP: Record<string, { label: string; color: string }> = {
    NONE:              { label: '无交易',   color: 'bg-slate-400 text-white dark:bg-slate-600' },
    AUCTION:           { label: '竞价',     color: 'bg-amber-400 text-white' },
    WAITING_OPEN:      { label: '等待开盘', color: 'bg-amber-400 text-white' },
    MORNING:           { label: '早盘',     color: 'bg-emerald-500 text-white' },
    REST:              { label: '午休',     color: 'bg-slate-400 text-white dark:bg-slate-600' },
    AFTERNOON:         { label: '午盘',     color: 'bg-emerald-500 text-white' },
    HK_CAS:            { label: '盘后竞价', color: 'bg-amber-400 text-white' },
    CLOSED:            { label: '休市',     color: 'bg-slate-400 text-white dark:bg-slate-600' },
    PRE_MARKET_BEGIN:  { label: '盘前',     color: 'bg-amber-400 text-white' },
    PRE_MARKET_END:    { label: '盘前结束', color: 'bg-slate-400 text-white dark:bg-slate-600' },
    AFTER_HOURS_BEGIN: { label: '盘后',     color: 'bg-amber-400 text-white' },
    AFTER_HOURS_END:   { label: '盘后收盘', color: 'bg-slate-400 text-white dark:bg-slate-600' },
    NIGHT_OPEN:        { label: '夜盘',     color: 'bg-indigo-400 text-white' },
    NIGHT_END:         { label: '夜盘收盘', color: 'bg-slate-400 text-white dark:bg-slate-600' },
    OVERNIGHT:         { label: '夜盘',     color: 'bg-indigo-400 text-white' },
    FUTURE_DAY_OPEN:      { label: '期指开盘', color: 'bg-emerald-500 text-white' },
    FUTURE_DAY_BREAK:     { label: '期指休市', color: 'bg-slate-400 text-white dark:bg-slate-600' },
    FUTURE_DAY_CLOSE:     { label: '期指收盘', color: 'bg-slate-400 text-white dark:bg-slate-600' },
    FUTURE_DAY_WAIT_OPEN: { label: '期指待开', color: 'bg-amber-400 text-white' },
}

function MarketStateBadge({ state, symbol }: { state?: string | null; symbol?: string }) {
    const market = symbol?.endsWith('.HK') || symbol?.startsWith('HK.') ? 'HK' : symbol ? 'US' : null
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
