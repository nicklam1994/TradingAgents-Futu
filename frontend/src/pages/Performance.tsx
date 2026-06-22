/**
 * Performance — 绩效分析页面
 *
 * 顶部 Tab 切换「真仓」和「模拟仓」。
 * - 真仓：基于 Futu 持仓快照的盈亏分布
 * - 模拟仓：基于 sim_deals 的 FIFO 量化指标
 */

import { useEffect, useState, useCallback } from 'react'
import {
    TrendingUp, TrendingDown, Percent,
    Shield, BarChart3,
    Wallet, Target,
} from 'lucide-react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell } from 'recharts'

import { api } from '@/services/api'
import type { SimPerformance, RealPerformance, RealPositionMetric } from '@/types'

type TabKey = 'real' | 'sim'

export default function Performance() {
    const [tab, setTab] = useState<TabKey>('real')

    return (
        <div className="space-y-6">
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">绩效分析</h1>
                    <p className="mt-1 text-slate-500 dark:text-slate-400">
                        {tab === 'real' ? '基于真仓持仓快照的盈亏分析' : '基于模拟交易历史的量化绩效指标'}
                    </p>
                </div>
                {/* Tab Switcher */}
                <div className="flex rounded-xl bg-slate-100 dark:bg-slate-800 p-1">
                    {([['real', '真仓'], ['sim', '模拟仓']] as [TabKey, string][]).map(([key, label]) => (
                        <button
                            key={key}
                            onClick={() => setTab(key)}
                            className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                                tab === key
                                    ? 'bg-white dark:bg-slate-700 text-slate-900 dark:text-slate-100 shadow-sm'
                                    : 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-300'
                            }`}
                        >
                            {label}
                        </button>
                    ))}
                </div>
            </div>

            {tab === 'real' ? <RealPerformancePanel /> : <SimPerformancePanel />}
        </div>
    )
}

/* ── 真仓绩效 ─────────────────────────────────────────────────────────── */

function RealPerformancePanel() {
    const [data, setData] = useState<RealPerformance | null>(null)
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)

    const load = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const res = await api.getRealPerformance()
            setData(res.data ?? null)
        } catch (e) {
            setError(e instanceof Error ? e.message : '加载真仓绩效失败')
        } finally {
            setLoading(false)
        }
    }, [])

    useEffect(() => { load() }, [load])

    if (loading) return <LoadingSkeleton />
    if (error) return <ErrorBanner message={error} />
    if (!data || (data.position_count === 0 && data.trade_count === 0)) {
        return <EmptyState message="真仓暂无持仓和交易数据" />
    }

    const pnlColor = data.total_pl_val >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'
    const pnlBg = data.total_pl_val >= 0 ? 'from-emerald-50 to-emerald-100/50 dark:from-emerald-950/30 dark:to-emerald-900/20' : 'from-rose-50 to-rose-100/50 dark:from-rose-950/30 dark:to-rose-900/20'

    return (
        <div className="space-y-6">
            {/* 量化指标卡片 (FIFO based) */}
            <div>
                <h2 className="text-sm font-medium text-slate-500 dark:text-slate-400 mb-3">📊 量化指标（基于历史成交 FIFO 匹配，近 90 天）</h2>
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
                    <MetricCard icon={Percent} label="交易胜率" value={`${(data.win_rate * 100).toFixed(1)}%`} description={`${data.trade_count} 笔配对`} color="green" good={data.win_rate >= 0.5} />
                    <MetricCard icon={TrendingDown} label="最大回撤" value={`${(data.max_drawdown * 100).toFixed(2)}%`} description="峰值到谷底" color="red" good={data.max_drawdown < 0.2} />
                    <MetricCard icon={TrendingUp} label="夏普比率" value={data.sharpe_ratio.toFixed(3)} description="风险调整收益" color="blue" good={data.sharpe_ratio > 1} />
                    <MetricCard icon={Shield} label="Sortino" value={data.sortino_ratio.toFixed(3)} description="下行风险调整" color="purple" good={data.sortino_ratio > 1} />
                    <MetricCard icon={BarChart3} label="Calmar" value={data.calmar_ratio.toFixed(3)} description="收益/最大回撤" color="orange" good={data.calmar_ratio > 1} />
                    <MetricCard icon={Target} label="持仓胜率" value={`${(data.position_win_rate * 100).toFixed(1)}%`} description={`${data.profitable_count} 盈 / ${data.losing_count} 亏`} color={data.position_win_rate >= 0.5 ? 'green' : 'orange'} />
                </div>
            </div>

            {/* 持仓盈亏总览 */}
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
                <div className={`rounded-2xl bg-gradient-to-br p-4 ${pnlBg}`}>
                    <div className="flex items-center gap-2 mb-1">
                        {data.total_pl_val >= 0 ? <TrendingUp className="h-4 w-4 text-emerald-500" /> : <TrendingDown className="h-4 w-4 text-rose-500" />}
                        <span className="text-sm text-slate-600 dark:text-slate-400">持仓盈亏</span>
                    </div>
                    <p className={`text-2xl font-bold ${pnlColor}`}>
                        {data.total_pl_val >= 0 ? '+' : ''}{data.total_pl_val.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </p>
                    <p className={`text-sm ${pnlColor}`}>
                        {data.total_pl_ratio >= 0 ? '+' : ''}{data.total_pl_ratio.toFixed(2)}%
                    </p>
                </div>
                <MetricCardSimple icon={Wallet} label="持仓市值" value={`$${data.total_market_val.toLocaleString('zh-CN', { minimumFractionDigits: 2 })}`} color="blue" />
                <MetricCardSimple icon={Target} label="持仓数" value={`${data.position_count} 只`} description={`HK ${data.hk_count} / US ${data.us_count}`} color="purple" />
                <MetricCardSimple icon={Percent} label="持仓胜率" value={`${(data.position_win_rate * 100).toFixed(1)}%`} description={`${data.profitable_count} 盈 / ${data.losing_count} 亏`} color={data.position_win_rate >= 0.5 ? 'green' : 'amber'} />
            </div>

            {/* 港美股市值分布 */}
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <MarketCard label="🇭🇰 港股" plVal={data.hk_pl_val} count={data.hk_count} />
                <MarketCard label="🇺🇸 美股" plVal={data.us_pl_val} count={data.us_count} />
            </div>

            {/* 最佳/最差持仓 */}
            {(data.best_position || data.worst_position) && (
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                    {data.best_position && <BestWorstCard title="🏆 最佳持仓" pos={data.best_position} />}
                    {data.worst_position && <BestWorstCard title="📉 最差持仓" pos={data.worst_position} />}
                </div>
            )}

            {/* 持仓明细表 */}
            <div className="card">
                <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-100">持仓明细</h2>
                <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="border-b border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-400">
                                <th className="py-2 text-left font-medium">股票</th>
                                <th className="py-2 text-right font-medium">数量</th>
                                <th className="py-2 text-right font-medium">成本价</th>
                                <th className="py-2 text-right font-medium">现价</th>
                                <th className="py-2 text-right font-medium">市值</th>
                                <th className="py-2 text-right font-medium">盈亏</th>
                                <th className="py-2 text-right font-medium">盈亏比</th>
                            </tr>
                        </thead>
                        <tbody>
                            {data.positions.map((p, i) => {
                                const cls = p.pl_val >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'
                                return (
                                    <tr key={i} className="border-b border-slate-100 dark:border-slate-800 hover:bg-slate-50 dark:hover:bg-slate-800/50">
                                        <td className="py-2">
                                            <div className="font-medium text-slate-900 dark:text-slate-100">{p.name || p.code}</div>
                                            <div className="text-xs text-slate-400">{p.code}</div>
                                        </td>
                                        <td className="py-2 text-right text-slate-700 dark:text-slate-300">{p.qty.toLocaleString()}</td>
                                        <td className="py-2 text-right text-slate-700 dark:text-slate-300">{p.cost_price.toFixed(3)}</td>
                                        <td className="py-2 text-right text-slate-700 dark:text-slate-300">{p.current_price.toFixed(3)}</td>
                                        <td className="py-2 text-right text-slate-700 dark:text-slate-300">{p.market_val.toLocaleString('zh-CN', { minimumFractionDigits: 2 })}</td>
                                        <td className={`py-2 text-right font-medium ${cls}`}>{p.pl_val >= 0 ? '+' : ''}{p.pl_val.toLocaleString('zh-CN', { minimumFractionDigits: 2 })}</td>
                                        <td className={`py-2 text-right font-medium ${cls}`}>{p.pl_ratio >= 0 ? '+' : ''}{p.pl_ratio.toFixed(2)}%</td>
                                    </tr>
                                )
                            })}
                        </tbody>
                    </table>
                </div>
            </div>

            {/* 持仓盈亏分布图 */}
            <PositionChart positions={data.positions} />

            {/* 最近成交记录 */}
            {data.recent_trades && data.recent_trades.length > 0 && (
                <div className="card">
                    <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-100">最近成交配对（FIFO）</h2>
                    <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="border-b border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-400">
                                    <th className="py-2 text-left font-medium">股票</th>
                                    <th className="py-2 text-right font-medium">买入价</th>
                                    <th className="py-2 text-right font-medium">卖出价</th>
                                    <th className="py-2 text-right font-medium">数量</th>
                                    <th className="py-2 text-right font-medium">收益率</th>
                                </tr>
                            </thead>
                            <tbody>
                                {data.recent_trades.map((t, i) => {
                                    const cls = t.return_pct >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'
                                    return (
                                        <tr key={i} className="border-b border-slate-100 dark:border-slate-800 hover:bg-slate-50 dark:hover:bg-slate-800/50">
                                            <td className="py-2 font-medium text-slate-900 dark:text-slate-100">{t.code}</td>
                                            <td className="py-2 text-right text-slate-700 dark:text-slate-300">{t.buy_price.toFixed(3)}</td>
                                            <td className="py-2 text-right text-slate-700 dark:text-slate-300">{t.sell_price.toFixed(3)}</td>
                                            <td className="py-2 text-right text-slate-700 dark:text-slate-300">{t.qty.toLocaleString()}</td>
                                            <td className={`py-2 text-right font-medium ${cls}`}>{t.return_pct >= 0 ? '+' : ''}{t.return_pct.toFixed(2)}%</td>
                                        </tr>
                                    )
                                })}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}
        </div>
    )
}

/* ── 模拟仓绩效 ───────────────────────────────────────────────────────── */

function SimPerformancePanel() {
    const [perf, setPerf] = useState<SimPerformance | null>(null)
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)

    const load = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const res = await api.getSimPerformance()
            setPerf(res.data ?? null)
        } catch (e) {
            setError(e instanceof Error ? e.message : '加载绩效数据失败')
        } finally {
            setLoading(false)
        }
    }, [])

    useEffect(() => { load() }, [load])

    if (loading) return <LoadingSkeleton />
    if (error) return <ErrorBanner message={error} />
    if (!perf || perf.trade_count === 0) {
        return <EmptyState message="模拟仓暂无交易记录" />
    }

    const chartData = [
        { name: '胜率', value: perf.win_rate * 100, unit: '%', color: '#22c55e' },
        { name: '夏普', value: perf.sharpe_ratio, unit: '', color: '#3b82f6' },
        { name: 'Sortino', value: perf.sortino_ratio, unit: '', color: '#8b5cf6' },
        { name: 'Calmar', value: perf.calmar_ratio, unit: '', color: '#f59e0b' },
    ]

    return (
        <div className="space-y-6">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
                <MetricCard icon={Percent} label="胜率" value={`${(perf.win_rate * 100).toFixed(1)}%`} description={`${perf.trade_count} 笔交易`} color="green" good={perf.win_rate >= 0.5} />
                <MetricCard icon={TrendingDown} label="最大回撤" value={`${(perf.max_drawdown * 100).toFixed(2)}%`} description="峰值到谷底" color="red" good={perf.max_drawdown < 0.2} />
                <MetricCard icon={TrendingUp} label="夏普比率" value={perf.sharpe_ratio.toFixed(3)} description="风险调整收益" color="blue" good={perf.sharpe_ratio > 1} />
                <MetricCard icon={Shield} label="Sortino 比率" value={perf.sortino_ratio.toFixed(3)} description="下行风险调整" color="purple" good={perf.sortino_ratio > 1} />
                <MetricCard icon={BarChart3} label="Calmar 比率" value={perf.calmar_ratio.toFixed(3)} description="收益/最大回撤" color="orange" good={perf.calmar_ratio > 1} />
            </div>

            {chartData.length > 0 && (
                <div className="card">
                    <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-100">指标概览</h2>
                    <div className="h-64">
                        <ResponsiveContainer width="100%" height="100%">
                            <BarChart data={chartData} barCategoryGap="30%">
                                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                                <XAxis dataKey="name" tick={{ fontSize: 12 }} stroke="#94a3b8" />
                                <YAxis tick={{ fontSize: 11 }} stroke="#94a3b8" />
                                <Tooltip
                                    formatter={(value: number, _name: string, props: { payload?: { unit?: string } }) => [
                                        `${value.toFixed(3)}${props.payload?.unit ?? ''}`, '数值'
                                    ]}
                                />
                                <Bar dataKey="value" radius={[6, 6, 0, 0]}>
                                    {chartData.map((entry, index) => (
                                        <Cell key={index} fill={entry.color} />
                                    ))}
                                </Bar>
                            </BarChart>
                        </ResponsiveContainer>
                    </div>
                </div>
            )}

            <div className="card">
                <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-100">指标说明</h2>
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                    <GuideItem title="胜率 (Win Rate)" description="盈利交易占比。一般 >50% 为佳，但需结合盈亏比综合判断。" />
                    <GuideItem title="最大回撤 (Max Drawdown)" description="资金曲线从峰值到谷底的最大跌幅。<20% 较健康。" />
                    <GuideItem title="夏普比率 (Sharpe)" description="每单位超额收益对应的风险。>1 表现良好，>2 优秀。" />
                    <GuideItem title="Sortino 比率" description="仅惩罚下行波动的夏普变体。>1 表示下行风险可控。" />
                    <GuideItem title="Calmar 比率" description="年化收益 / 最大回撤。>1 说明收益能覆盖回撤风险。" />
                    <GuideItem title="交易笔数" description="用于 FIFO 匹配的买卖成交对。笔数过少时指标参考价值有限。" />
                </div>
            </div>
        </div>
    )
}

/* ── Sub-components ─────────────────────────────────────────────────────── */

function MetricCardSimple({ icon: Icon, label, value, description, color }: {
    icon: React.ComponentType<{ className?: string }>
    label: string
    value: string
    description?: string
    color: 'blue' | 'green' | 'purple' | 'amber'
}) {
    const bg = {
        blue: 'bg-blue-100 dark:bg-blue-500/10 text-blue-600 dark:text-blue-400',
        green: 'bg-emerald-100 dark:bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
        purple: 'bg-purple-100 dark:bg-purple-500/10 text-purple-600 dark:text-purple-400',
        amber: 'bg-amber-100 dark:bg-amber-500/10 text-amber-600 dark:text-amber-400',
    }
    return (
        <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4">
            <div className="flex items-start justify-between">
                <div>
                    <p className="text-sm text-slate-500 dark:text-slate-400">{label}</p>
                    <p className="mt-1 text-xl font-bold text-slate-900 dark:text-slate-100">{value}</p>
                    {description && <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">{description}</p>}
                </div>
                <div className={`rounded-lg p-2.5 ${bg[color]}`}>
                    <Icon className="h-5 w-5" />
                </div>
            </div>
        </div>
    )
}

function MarketCard({ label, plVal, count }: { label: string; plVal: number; count: number }) {
    const isUp = plVal >= 0
    return (
        <div className={`rounded-2xl bg-gradient-to-br p-4 ${isUp ? 'from-emerald-50 to-emerald-100/50 dark:from-emerald-950/30 dark:to-emerald-900/20' : 'from-rose-50 to-rose-100/50 dark:from-rose-950/30 dark:to-rose-900/20'}`}>
            <div className="flex items-center justify-between mb-2">
                <span className="text-sm font-medium text-slate-700 dark:text-slate-300">{label}</span>
                <span className="text-xs text-slate-500 dark:text-slate-400">{count} 只</span>
            </div>
            <p className={`text-xl font-bold ${isUp ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}`}>
                {plVal >= 0 ? '+' : ''}{plVal.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </p>
        </div>
    )
}

function BestWorstCard({ title, pos }: { title: string; pos: RealPositionMetric }) {
    const isUp = pos.pl_ratio >= 0
    return (
        <div className="card">
            <h3 className="text-sm font-medium text-slate-500 dark:text-slate-400 mb-2">{title}</h3>
            <div className="flex items-center justify-between">
                <div>
                    <p className="font-semibold text-slate-900 dark:text-slate-100">{pos.name || pos.code}</p>
                    <p className="text-xs text-slate-400">{pos.code}</p>
                </div>
                <div className="text-right">
                    <p className={`text-lg font-bold ${isUp ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}`}>
                        {pos.pl_ratio >= 0 ? '+' : ''}{pos.pl_ratio.toFixed(2)}%
                    </p>
                    <p className={`text-sm ${isUp ? 'text-emerald-500' : 'text-rose-500'}`}>
                        {pos.pl_val >= 0 ? '+' : ''}{pos.pl_val.toLocaleString('zh-CN', { minimumFractionDigits: 2 })}
                    </p>
                </div>
            </div>
        </div>
    )
}

function PositionChart({ positions }: { positions: RealPerformance['positions'] }) {
    if (positions.length === 0) return null

    const chartData = positions
        .slice()
        .sort((a, b) => b.pl_ratio - a.pl_ratio)
        .map(p => ({
            name: p.name || p.code,
            value: p.pl_ratio,
            color: p.pl_ratio >= 0 ? '#22c55e' : '#ef4444',
        }))

    return (
        <div className="card">
            <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-100">持仓盈亏分布</h2>
            <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={chartData} barCategoryGap="20%">
                        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                        <XAxis dataKey="name" tick={{ fontSize: 10 }} stroke="#94a3b8" interval={0} angle={-30} textAnchor="end" height={60} />
                        <YAxis tick={{ fontSize: 11 }} stroke="#94a3b8" tickFormatter={(v: number) => `${v.toFixed(1)}%`} />
                        <Tooltip formatter={(value: number) => [`${value.toFixed(2)}%`, '盈亏比']} />
                        <Bar dataKey="value" radius={[4, 4, 0, 0]}>
                            {chartData.map((entry, index) => (
                                <Cell key={index} fill={entry.color} />
                            ))}
                        </Bar>
                    </BarChart>
                </ResponsiveContainer>
            </div>
        </div>
    )
}

function MetricCard({ icon: Icon, label, value, description, color, good }: {
    icon: React.ComponentType<{ className?: string }>
    label: string
    value: string
    description: string
    color: 'blue' | 'green' | 'orange' | 'purple' | 'red'
    good?: boolean
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
                    <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">{description}</p>
                </div>
                <div className="flex flex-col items-end gap-1">
                    <div className={`rounded-lg p-3 ${bg[color]}`}>
                        <Icon className="h-5 w-5" />
                    </div>
                    {good !== undefined && (
                        <span className={`text-xs font-medium ${good ? 'text-green-600 dark:text-green-400' : 'text-amber-600 dark:text-amber-400'}`}>
                            {good ? '良好' : '需关注'}
                        </span>
                    )}
                </div>
            </div>
        </div>
    )
}

function GuideItem({ title, description }: { title: string; description: string }) {
    return (
        <div className="rounded-lg border border-slate-100 bg-slate-50/50 p-3 dark:border-slate-800 dark:bg-slate-800/30">
            <p className="text-sm font-medium text-slate-900 dark:text-slate-100">{title}</p>
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{description}</p>
        </div>
    )
}

function LoadingSkeleton() {
    return (
        <div className="space-y-4">
            <div className="grid grid-cols-4 gap-4">
                {[1,2,3,4].map(i => <div key={i} className="h-24 rounded-2xl bg-slate-100 dark:bg-slate-800 animate-pulse" />)}
            </div>
            <div className="h-64 rounded-2xl bg-slate-100 dark:bg-slate-800 animate-pulse" />
        </div>
    )
}

function ErrorBanner({ message }: { message: string }) {
    return (
        <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-600 dark:border-rose-900/50 dark:bg-rose-950/30 dark:text-rose-300">
            {message}
        </div>
    )
}

function EmptyState({ message }: { message: string }) {
    return (
        <div className="flex flex-col items-center justify-center py-16 text-slate-400 dark:text-slate-500">
            <BarChart3 className="h-12 w-12 mb-4 opacity-50" />
            <p className="text-lg">{message}</p>
        </div>
    )
}
