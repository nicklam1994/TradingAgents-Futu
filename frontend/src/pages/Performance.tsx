/**
 * Performance — 绩效指标页面
 *
 * 展示胜率/回撤/夏普/Sortino/Calmar 卡片 + 资金曲线图。
 * 调用 /v1/sim/performance
 */

import { useEffect, useState, useCallback } from 'react'
import {
    TrendingUp, TrendingDown, Percent,
    Shield, BarChart3, RefreshCw,
} from 'lucide-react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell } from 'recharts'

import { api } from '@/services/api'
import type { SimPerformance } from '@/types'

export default function Performance() {
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

    // Build bar chart data from performance metrics
    const chartData = perf ? [
        { name: '胜率', value: perf.win_rate * 100, unit: '%', color: '#22c55e' },
        { name: '夏普', value: perf.sharpe_ratio, unit: '', color: '#3b82f6' },
        { name: 'Sortino', value: perf.sortino_ratio, unit: '', color: '#8b5cf6' },
        { name: 'Calmar', value: perf.calmar_ratio, unit: '', color: '#f59e0b' },
    ] : []

    return (
        <div className="space-y-6">
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">绩效分析</h1>
                    <p className="mt-1 text-slate-500 dark:text-slate-400">基于模拟交易历史的量化绩效指标</p>
                </div>
                <button onClick={load} disabled={loading}
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

            {/* Metric Cards */}
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
                <MetricCard
                    icon={Percent}
                    label="胜率"
                    value={perf ? `${(perf.win_rate * 100).toFixed(1)}%` : '--'}
                    description={perf ? `${perf.trade_count} 笔交易` : '无交易数据'}
                    color="green"
                    good={perf ? perf.win_rate >= 0.5 : undefined}
                />
                <MetricCard
                    icon={TrendingDown}
                    label="最大回撤"
                    value={perf ? `${(perf.max_drawdown * 100).toFixed(2)}%` : '--'}
                    description="峰值到谷底"
                    color="red"
                    good={perf ? perf.max_drawdown < 0.2 : undefined}
                />
                <MetricCard
                    icon={TrendingUp}
                    label="夏普比率"
                    value={perf ? perf.sharpe_ratio.toFixed(3) : '--'}
                    description="风险调整收益"
                    color="blue"
                    good={perf ? perf.sharpe_ratio > 1 : undefined}
                />
                <MetricCard
                    icon={Shield}
                    label="Sortino 比率"
                    value={perf ? perf.sortino_ratio.toFixed(3) : '--'}
                    description="下行风险调整"
                    color="purple"
                    good={perf ? perf.sortino_ratio > 1 : undefined}
                />
                <MetricCard
                    icon={BarChart3}
                    label="Calmar 比率"
                    value={perf ? perf.calmar_ratio.toFixed(3) : '--'}
                    description="收益/最大回撤"
                    color="orange"
                    good={perf ? perf.calmar_ratio > 1 : undefined}
                />
            </div>

            {/* Bar Chart */}
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

            {/* Interpretation Guide */}
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

function MetricCard({
    icon: Icon, label, value, description, color, good,
}: {
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
