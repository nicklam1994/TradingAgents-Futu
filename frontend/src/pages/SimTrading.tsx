/**
 * SimTrading — 模拟交易页面
 *
 * 展示所有模拟账户（港股/美股）资金卡片、持仓列表、盈亏曲线、当日订单与成交记录。
 * 切换市场标签时自动重新加载持仓/订单/成交。
 */

import { useEffect, useState, useCallback } from 'react'
import {
    Wallet, TrendingUp, TrendingDown, Package,
    RefreshCw, ArrowUpCircle, ArrowDownCircle,
} from 'lucide-react'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'

import { api } from '@/services/api'
import { formatTime } from '@/utils/formatTime'
import type { SimAccount, SimPosition, SimOrder, SimDeal } from '@/types'

const MARKET_LABELS: Record<string, string> = { HK: '港股', US: '美股' }
const MARKET_FLAGS: Record<string, string> = { HK: '🇭🇰', US: '🇺🇸' }
const ORDER_TYPE_LABEL: Record<string, string> = {
    NORMAL: 'LMT', MARKET: 'MKT',
    AUCTION_LIMIT: 'AUCTION_LMT', AUCTION: 'AUCTION_MKT',
    STOP: 'STOP', STOP_LIMIT: 'STOP_LMT',
}

export default function SimTrading() {
    const [accounts, setAccounts] = useState<SimAccount[]>([])
    const [activeMarket, setActiveMarket] = useState<string>('HK')
    const [positions, setPositions] = useState<SimPosition[]>([])
    const [orders, setOrders] = useState<SimOrder[]>([])
    const [deals, setDeals] = useState<SimDeal[]>([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)

    // Load accounts once
    const loadAccounts = useCallback(async () => {
        try {
            const res = await api.getSimAllAccounts()
            if (res.ok) setAccounts(res.data ?? [])
        } catch { /* ignore */ }
    }, [])

    // Load market-specific data when activeMarket changes
    const loadMarketData = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const [posRes, ordRes, dealRes] = await Promise.allSettled([
                api.getSimPositions(activeMarket),
                api.getSimOrders(undefined, activeMarket),
                api.getSimDeals(),
            ])
            if (posRes.status === 'fulfilled') setPositions(posRes.value.data ?? [])
            if (ordRes.status === 'fulfilled') setOrders(ordRes.value.data ?? [])
            if (dealRes.status === 'fulfilled') setDeals(dealRes.value.data ?? [])

            const firstError = [posRes, ordRes, dealRes].find(r => r.status === 'rejected')
            if (firstError && firstError.status === 'rejected') {
                setError(firstError.reason?.message ?? '加载模拟交易数据失败')
            }
        } catch (e) {
            setError(e instanceof Error ? e.message : '加载失败')
        } finally {
            setLoading(false)
        }
    }, [activeMarket])

    useEffect(() => { loadAccounts() }, [loadAccounts])
    useEffect(() => { loadMarketData() }, [loadMarketData])

    const refresh = () => {
        loadAccounts()
        loadMarketData()
    }

    const activeAccount = accounts.find(a => a.market === activeMarket) ?? accounts[0] ?? null
    const equityCurve = buildEquityCurve(deals)

    // Portfolio total for position % calculation
    const totalMarketVal = positions.reduce((s, p) => s + (p.market_val || 0), 0)

    return (
        <div className="space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">模拟交易</h1>
                    <p className="mt-1 text-slate-500 dark:text-slate-400">查看模拟账户资金、持仓与交易记录</p>
                </div>
                <button onClick={refresh} disabled={loading}
                    className="flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-600 transition hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800">
                    <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                    刷新
                </button>
            </div>

            {error && (
                <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-600 dark:border-rose-900/50 dark:bg-rose-950/30 dark:text-rose-300">
                    {error}
                </div>
            )}

            {/* Market Tabs */}
            <div className="flex gap-2">
                {accounts.map(acc => (
                    <button
                        key={acc.market}
                        onClick={() => setActiveMarket(acc.market)}
                        className={`flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-medium transition ${
                            activeMarket === acc.market
                                ? 'bg-blue-50 text-blue-700 ring-1 ring-blue-200 dark:bg-blue-500/10 dark:text-blue-400 dark:ring-blue-500/30'
                                : 'bg-slate-50 text-slate-600 hover:bg-slate-100 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700'
                        }`}
                    >
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
                    <AccountCard icon={TrendingUp} label="持仓市值" value={fmt(activeAccount.market_val)} subValue={`${positions.length} 只标的`} color="purple" />
                    <AccountCard
                        icon={activeAccount.unrealized_pnl >= 0 ? TrendingUp : TrendingDown}
                        label="浮动盈亏"
                        value={fmt(activeAccount.unrealized_pnl)}
                        subValue={`已实现 ${fmt(activeAccount.realized_pnl)}`}
                        color={activeAccount.unrealized_pnl >= 0 ? 'green' : 'red'}
                    />
                </div>
            )}

            {/* Equity Curve */}
            {equityCurve.length > 1 && (
                <div className="card">
                    <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-100">累计盈亏曲线</h2>
                    <div className="h-64">
                        <ResponsiveContainer width="100%" height="100%">
                            <AreaChart data={equityCurve}>
                                <defs>
                                    <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
                                        <stop offset="5%" stopColor="#22c55e" stopOpacity={0.3} />
                                        <stop offset="95%" stopColor="#22c55e" stopOpacity={0} />
                                    </linearGradient>
                                </defs>
                                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                                <XAxis dataKey="time" tick={{ fontSize: 11 }} stroke="#94a3b8" />
                                <YAxis tick={{ fontSize: 11 }} stroke="#94a3b8" tickFormatter={v => `${(v / 1000).toFixed(0)}k`} />
                                <Tooltip formatter={(v: number) => [`$${v.toFixed(2)}`, '累计盈亏']} />
                                <Area type="monotone" dataKey="value" stroke="#22c55e" fill="url(#equityGrad)" strokeWidth={2} />
                            </AreaChart>
                        </ResponsiveContainer>
                    </div>
                </div>
            )}

            {/* Positions Table */}
            <div className="card">
                <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-100">当前持仓</h2>
                {positions.length === 0 ? (
                    <EmptyState text="暂无持仓" />
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="border-b border-slate-200 dark:border-slate-700">
                                    <th className="px-3 py-2 text-left font-medium text-slate-500">股票名称/代码</th>
                                    <th className="px-3 py-2 text-right font-medium text-slate-500">持仓数量</th>
                                    <th className="px-3 py-2 text-right font-medium text-slate-500">现价/成本价</th>
                                    <th className="px-3 py-2 text-right font-medium text-slate-500">市值/成本市值</th>
                                    <th className="px-3 py-2 text-right font-medium text-slate-500">持仓盈亏/盈亏%</th>
                                    <th className="px-3 py-2 text-right font-medium text-slate-500">今日盈亏</th>
                                    <th className="px-3 py-2 text-right font-medium text-slate-500">持仓%</th>
                                </tr>
                            </thead>
                            <tbody>
                                {positions.map(p => {
                                    const posPct = totalMarketVal > 0 ? (p.market_val / totalMarketVal * 100) : 0
                                    return (
                                        <tr key={p.code} className="border-b border-slate-100 last:border-0 dark:border-slate-800">
                                            {/* 股票名称/代码 */}
                                            <td className="px-3 py-2">
                                                <div className="font-medium text-slate-900 dark:text-slate-100">{p.stock_name || '--'}</div>
                                                <div className="text-xs text-slate-400">{displayCode(p.code)}</div>
                                            </td>
                                            {/* 持仓数量 */}
                                            <td className="px-3 py-2 text-right text-slate-700 dark:text-slate-300">{p.qty}</td>
                                            {/* 现价/成本价 */}
                                            <td className="px-3 py-2 text-right">
                                                <div className="text-slate-900 dark:text-slate-100">{fmtPrice(p.current_price)}</div>
                                                <div className="text-xs text-slate-400">{fmtPrice(p.cost_price)}</div>
                                            </td>
                                            {/* 市值/成本市值 */}
                                            <td className="px-3 py-2 text-right">
                                                <div className="text-slate-900 dark:text-slate-100">{fmt(p.market_val)}</div>
                                                <div className="text-xs text-slate-400">{fmt(p.cost_val)}</div>
                                            </td>
                                            {/* 持仓盈亏/盈亏% */}
                                            <td className="px-3 py-2 text-right">
                                                <div className={`font-medium ${p.unrealized_pnl >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                                                    {fmt(p.unrealized_pnl)}
                                                </div>
                                                <div className={`text-xs ${p.unrealized_pnl_pct >= 0 ? 'text-emerald-500' : 'text-rose-500'}`}>
                                                    {p.unrealized_pnl_pct >= 0 ? '+' : ''}{p.unrealized_pnl_pct?.toFixed(2)}%
                                                </div>
                                            </td>
                                            {/* 今日盈亏 */}
                                            <td className={`px-3 py-2 text-right font-medium ${p.today_pnl >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                                                {p.prev_close ? fmt(p.today_pnl) : '--'}
                                            </td>
                                            {/* 持仓% */}
                                            <td className="px-3 py-2 text-right text-slate-700 dark:text-slate-300">
                                                {posPct.toFixed(1)}%
                                            </td>
                                        </tr>
                                    )
                                })}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            {/* Orders + Deals side by side */}
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
                {/* Orders */}
                <div className="card">
                    <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-100">当日订单</h2>
                    {orders.length === 0 ? (
                        <EmptyState text="暂无订单" />
                    ) : (
                        <div className="space-y-2">
                            {orders.map(o => (
                                <div key={o.order_id} className="flex items-center justify-between rounded-lg bg-slate-50 px-3 py-2 dark:bg-slate-800">
                                    <div className="flex items-center gap-2">
                                        {o.side === 'BUY' ? <ArrowUpCircle className="h-4 w-4 text-emerald-500" /> : <ArrowDownCircle className="h-4 w-4 text-rose-500" />}
                                        <span className="font-medium text-slate-900 dark:text-slate-100">{o.stock_name || o.code}</span>
                                        <span className={`text-xs ${o.side === 'BUY' ? 'text-emerald-600' : 'text-rose-600'}`}>{o.side}</span>
                                        <span className="rounded bg-slate-200 px-1.5 py-0.5 text-xs text-slate-500 dark:bg-slate-700 dark:text-slate-400">{o.status}</span>
                                    </div>
                                    <div className="text-right text-sm text-slate-600 dark:text-slate-400">
                                        <div>{o.filled_qty ? `${o.filled_qty}/${o.qty}` : o.qty} × {o.price?.toFixed(2)}</div>
                                        <div className="text-xs text-slate-400">{formatTime(o.create_time)}</div>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                </div>

                {/* Deals */}
                <div className="card">
                    <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-100">成交记录</h2>
                    {deals.length === 0 ? (
                        <EmptyState text="暂无成交记录" />
                    ) : (
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
                                        <div>{d.qty} × {d.price?.toFixed(2)}</div>
                                        <div className="text-xs text-slate-400">{formatTime(d.create_time)}</div>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </div>
    )
}

/* ─── Sub-components ──────────────────────────────────────────────────────── */

function AccountCard({ icon: Icon, label, value, subValue, color }: {
    icon: typeof Wallet
    label: string
    value: string
    subValue?: string
    color: 'blue' | 'green' | 'purple' | 'red'
}) {
    const bgMap = {
        blue: 'from-blue-50 to-blue-100/50 dark:from-blue-950/30 dark:to-blue-900/20',
        green: 'from-emerald-50 to-emerald-100/50 dark:from-emerald-950/30 dark:to-emerald-900/20',
        purple: 'from-purple-50 to-purple-100/50 dark:from-purple-950/30 dark:to-purple-900/20',
        red: 'from-rose-50 to-rose-100/50 dark:from-rose-950/30 dark:to-rose-900/20',
    }
    const iconMap = {
        blue: 'text-blue-500',
        green: 'text-emerald-500',
        purple: 'text-purple-500',
        red: 'text-rose-500',
    }
    return (
        <div className={`rounded-2xl bg-gradient-to-br ${bgMap[color]} p-4`}>
            <div className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400">
                <Icon className={`h-4 w-4 ${iconMap[color]}`} />
                {label}
            </div>
            <div className="mt-2 text-2xl font-bold text-slate-900 dark:text-slate-100">{value}</div>
            {subValue && <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{subValue}</div>}
        </div>
    )
}

function EmptyState({ text }: { text: string }) {
    return (
        <div className="flex items-center justify-center py-8 text-sm text-slate-400 dark:text-slate-500">
            {text}
        </div>
    )
}

/* ─── Helpers ──────────────────────────────────────────────────────────────── */

function fmt(v: number | undefined | null): string {
    if (v == null) return '--'
    return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function fmtPrice(v: number | undefined | null): string {
    if (v == null || v === 0) return '--'
    return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 3 })
}

/** Convert Futu code HK.00700 → 00700.HK, US.AAPL → AAPL */
function displayCode(code: string): string {
    if (code.startsWith('HK.')) return code.slice(3) + '.HK'
    if (code.startsWith('US.')) return code.slice(3)
    return code
}

function buildEquityCurve(deals: SimDeal[]): Array<{ time: string; value: number }> {
    if (!deals.length) return []

    const sorted = [...deals].sort((a, b) => (a.create_time ?? '').localeCompare(b.create_time ?? ''))
    const buys: Record<string, Array<{ price: number; qty: number }>> = {}
    let cumulative = 0
    const points: Array<{ time: string; value: number }> = []

    for (const d of sorted) {
        const code = d.code ?? ''
        if (!buys[code]) buys[code] = []

        if (d.side === 'BUY') {
            buys[code].push({ price: d.price, qty: d.qty })
        } else if (d.side === 'SELL' && buys[code].length > 0) {
            let remaining = d.qty
            while (remaining > 0 && buys[code].length > 0) {
                const lot = buys[code][0]
                const matchQty = Math.min(remaining, lot.qty)
                cumulative += (d.price - lot.price) * matchQty
                lot.qty -= matchQty
                remaining -= matchQty
                if (lot.qty <= 0) buys[code].shift()
            }
        }

        points.push({ time: d.create_time ?? '', value: Math.round(cumulative * 100) / 100 })
    }

    return points
}
