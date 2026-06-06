/**
 * SimTrading — 模拟交易页面
 *
 * 展示账户资金卡片、持仓列表、盈亏曲线、当日订单与成交记录。
 * 调用 /v1/sim/account, /v1/sim/positions, /v1/sim/orders, /v1/sim/deals
 */

import { useEffect, useState, useCallback } from 'react'
import {
    Wallet, TrendingUp, TrendingDown, Package,
    RefreshCw, ArrowUpCircle, ArrowDownCircle,
} from 'lucide-react'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'

import { api } from '@/services/api'
import type { SimAccount, SimPosition, SimOrder, SimDeal } from '@/types'

export default function SimTrading() {
    const [account, setAccount] = useState<SimAccount | null>(null)
    const [positions, setPositions] = useState<SimPosition[]>([])
    const [orders, setOrders] = useState<SimOrder[]>([])
    const [deals, setDeals] = useState<SimDeal[]>([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)

    const loadAll = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const [accRes, posRes, ordRes, dealRes] = await Promise.allSettled([
                api.getSimAccount(),
                api.getSimPositions(),
                api.getSimOrders(),
                api.getSimDeals(),
            ])
            if (accRes.status === 'fulfilled') setAccount(accRes.value.data)
            if (posRes.status === 'fulfilled') setPositions(posRes.value.data ?? [])
            if (ordRes.status === 'fulfilled') setOrders(ordRes.value.data ?? [])
            if (dealRes.status === 'fulfilled') setDeals(dealRes.value.data ?? [])

            const firstError = [accRes, posRes, ordRes, dealRes].find(r => r.status === 'rejected')
            if (firstError && firstError.status === 'rejected') {
                setError(firstError.reason?.message ?? '加载模拟交易数据失败')
            }
        } catch (e) {
            setError(e instanceof Error ? e.message : '加载失败')
        } finally {
            setLoading(false)
        }
    }, [])

    useEffect(() => { loadAll() }, [loadAll])

    // Build equity curve from deals (cumulative P&L)
    const equityCurve = buildEquityCurve(deals)

    return (
        <div className="space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">模拟交易</h1>
                    <p className="mt-1 text-slate-500 dark:text-slate-400">查看模拟账户资金、持仓与交易记录</p>
                </div>
                <button onClick={loadAll} disabled={loading}
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

            {/* Account Cards */}
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
                <AccountCard icon={Wallet} label="总资产" value={fmt(account?.total_assets)} subValue={`${account?.currency ?? 'HKD'}`} color="blue" />
                <AccountCard icon={Package} label="可用资金" value={fmt(account?.available_cash)} subValue={`冻结 ${fmt(account?.frozen_cash)}`} color="green" />
                <AccountCard icon={TrendingUp} label="持仓市值" value={fmt(account?.market_val)} subValue={`${positions.length} 只标的`} color="purple" />
                <AccountCard
                    icon={account && account.unrealized_pnl >= 0 ? TrendingUp : TrendingDown}
                    label="浮动盈亏"
                    value={fmt(account?.unrealized_pnl)}
                    subValue={`已实现 ${fmt(account?.realized_pnl)}`}
                    color={account && account.unrealized_pnl >= 0 ? 'green' : 'red'}
                />
            </div>

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
                                    <Th>代码</Th>
                                    <Th>数量</Th>
                                    <Th>成本价</Th>
                                    <Th>现价</Th>
                                    <Th>市值</Th>
                                    <Th>盈亏</Th>
                                    <Th>盈亏%</Th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                                {positions.map(p => (
                                    <tr key={p.code} className="hover:bg-slate-50 dark:hover:bg-slate-800/50">
                                        <Td>
                                            <div>
                                                <span className="font-medium text-slate-900 dark:text-slate-100">{p.code}</span>
                                                {p.symbol && p.symbol !== p.code && (
                                                    <span className="ml-1 text-xs text-slate-400">{p.symbol}</span>
                                                )}
                                            </div>
                                        </Td>
                                        <Td>{p.qty}</Td>
                                        <Td>{fmt(p.cost_price)}</Td>
                                        <Td>{fmt(p.current_price)}</Td>
                                        <Td>{fmt(p.market_val)}</Td>
                                        <Td className={p.unrealized_pnl >= 0 ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'}>
                                            {fmt(p.unrealized_pnl)}
                                        </Td>
                                        <Td className={p.unrealized_pnl_pct >= 0 ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'}>
                                            {(p.unrealized_pnl_pct * 100).toFixed(2)}%
                                        </Td>
                                    </tr>
                                ))}
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
                        <div className="space-y-2 max-h-80 overflow-y-auto">
                            {orders.map(o => (
                                <OrderRow key={o.order_id} order={o} />
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
                        <div className="space-y-2 max-h-80 overflow-y-auto">
                            {deals.map(d => (
                                <DealRow key={d.deal_id} deal={d} />
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </div>
    )
}

/* ── Sub-components ─────────────────────────────────────────────────────── */

function AccountCard({
    icon: Icon, label, value, subValue, color,
}: {
    icon: React.ComponentType<{ className?: string }>
    label: string
    value: string
    subValue: string
    color: 'blue' | 'green' | 'orange' | 'purple' | 'red'
}) {
    const bg = {
        blue: 'bg-blue-100 dark:bg-blue-500/10 text-blue-600 dark:text-blue-400',
        green: 'bg-green-100 dark:bg-green-500/10 text-green-600 dark:text-green-400',
        orange: 'bg-orange-100 dark:bg-orange-500/10 text-orange-600 dark:text-orange-400',
        purple: 'bg-purple-100 dark:bg-purple-500/10 text-purple-600 dark:text-purple-400',
        red: 'bg-red-100 dark:bg-red-500/10 text-red-600 dark:text-red-400',
    }
    return (
        <div className="card card-hover">
            <div className="flex items-start justify-between">
                <div>
                    <p className="text-sm text-slate-500 dark:text-slate-400">{label}</p>
                    <p className="mt-1 text-2xl font-bold text-slate-900 dark:text-slate-100">{value}</p>
                    <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">{subValue}</p>
                </div>
                <div className={`rounded-lg p-3 ${bg[color]}`}>
                    <Icon className="h-5 w-5" />
                </div>
            </div>
        </div>
    )
}

function OrderRow({ order }: { order: SimOrder }) {
    const isBuy = order.side.toUpperCase() === 'BUY'
    return (
        <div className="flex items-center justify-between rounded-lg border border-slate-100 px-3 py-2 dark:border-slate-800">
            <div className="flex items-center gap-2">
                {isBuy ? (
                    <ArrowUpCircle className="h-4 w-4 text-red-500" />
                ) : (
                    <ArrowDownCircle className="h-4 w-4 text-green-500" />
                )}
                <div>
                    <span className="text-sm font-medium text-slate-900 dark:text-slate-100">{order.code}</span>
                    <span className={`ml-2 text-xs ${isBuy ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'}`}>
                        {isBuy ? '买入' : '卖出'}
                    </span>
                </div>
            </div>
            <div className="text-right">
                <p className="text-sm text-slate-700 dark:text-slate-300">{order.qty} 股 @ {fmt(order.price)}</p>
                <p className="text-xs text-slate-400">{order.status} · {formatTime(order.create_time)}</p>
            </div>
        </div>
    )
}

function DealRow({ deal }: { deal: SimDeal }) {
    const isBuy = deal.side.toUpperCase() === 'BUY'
    return (
        <div className="flex items-center justify-between rounded-lg border border-slate-100 px-3 py-2 dark:border-slate-800">
            <div className="flex items-center gap-2">
                {isBuy ? (
                    <ArrowUpCircle className="h-4 w-4 text-red-500" />
                ) : (
                    <ArrowDownCircle className="h-4 w-4 text-green-500" />
                )}
                <div>
                    <span className="text-sm font-medium text-slate-900 dark:text-slate-100">{deal.code}</span>
                    {deal.stock_name && <span className="ml-1 text-xs text-slate-400">{deal.stock_name}</span>}
                </div>
            </div>
            <div className="text-right">
                <p className="text-sm text-slate-700 dark:text-slate-300">{deal.qty} 股 @ {fmt(deal.price)}</p>
                <p className="text-xs text-slate-400">{formatTime(deal.deal_time)}</p>
            </div>
        </div>
    )
}

function EmptyState({ text }: { text: string }) {
    return <p className="py-8 text-center text-sm text-slate-400 dark:text-slate-500">{text}</p>
}

function Th({ children }: { children: React.ReactNode }) {
    return <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">{children}</th>
}

function Td({ children, className = '' }: { children: React.ReactNode; className?: string }) {
    return <td className={`px-3 py-2.5 text-sm text-slate-700 dark:text-slate-300 ${className}`}>{children}</td>
}

/* ── Helpers ────────────────────────────────────────────────────────────── */

function fmt(value?: number | null): string {
    if (value == null) return '--'
    return value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function formatTime(value?: string | null): string {
    if (!value) return '--'
    const d = new Date(value.replace(' ', 'T'))
    if (Number.isNaN(d.getTime())) return value
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

/**
 * Build a cumulative equity curve from deal records.
 * Each point = cumulative realized P&L up to that deal time.
 */
function buildEquityCurve(deals: SimDeal[]): Array<{ time: string; value: number }> {
    if (deals.length === 0) return []

    // Sort by time ascending
    const sorted = [...deals].sort((a, b) => a.deal_time.localeCompare(b.deal_time))

    // FIFO match buy/sell per code to compute running P&L
    const buyQueues: Record<string, Array<{ price: number; qty: number }>> = {}
    let cumPnl = 0
    const points: Array<{ time: string; value: number }> = [{ time: '起始', value: 0 }]

    for (const d of sorted) {
        const side = d.side.toUpperCase()
        if (side === 'BUY') {
            if (!buyQueues[d.code]) buyQueues[d.code] = []
            buyQueues[d.code].push({ price: d.price, qty: d.qty })
        } else if (side === 'SELL' && buyQueues[d.code]?.length) {
            let remaining = d.qty
            while (remaining > 0 && buyQueues[d.code].length) {
                const buy = buyQueues[d.code][0]
                const matched = Math.min(remaining, buy.qty)
                cumPnl += (d.price - buy.price) * matched
                remaining -= matched
                buy.qty -= matched
                if (buy.qty <= 0) buyQueues[d.code].shift()
            }
        }
        points.push({ time: formatTime(d.deal_time), value: Math.round(cumPnl * 100) / 100 })
    }

    return points
}
