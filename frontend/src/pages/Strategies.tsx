/**
 * Strategies — 策略管理页面 (Enhanced)
 *
 * 展示策略列表 + 热力图
 * 调用 /v1/strategies 系列 API
 */

import { useEffect, useState, useCallback } from 'react'
import {
    Cpu, RefreshCw, Zap, TrendingUp,
    BarChart3, Shield, Target, Activity,
    Award, Flame, Eye,
} from 'lucide-react'

import { api } from '@/services/api'
import type { Strategy, HeatmapCell } from '@/types'

// ─── Constants ─────────────────────────────────────────────────────────────

const recommendationColors: Record<string, string> = {
    strong_buy: 'bg-emerald-500',
    buy: 'bg-emerald-300',
    neutral: 'bg-slate-400',
    sell: 'bg-rose-300',
    strong_sell: 'bg-rose-500',
}

const regimeLabels: Record<string, string> = {
    trending_up: '上升趋势',
    trending_down: '下降趋势',
    ranging: '震荡盘整',
    volatile: '高波动',
}

const categoryIcons: Record<string, typeof TrendingUp> = {
    trend: TrendingUp,
    momentum: Zap,
    value: Shield,
    mean_reversion: Target,
    breakout: Activity,
    general: Cpu,
}

// ─── Main Component ────────────────────────────────────────────────────────

export default function Strategies() {
    const [strategies, setStrategies] = useState<Strategy[]>([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)
    const [selectedTab, setSelectedTab] = useState<'list' | 'heatmap'>('list')
    const [heatmap, setHeatmap] = useState<HeatmapCell[]>([])
    const [heatmapLoading, setHeatmapLoading] = useState(false)

    const load = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const res = await api.getStrategies()
            setStrategies(res.data?.strategies ?? [])
        } catch (e) {
            setError(e instanceof Error ? e.message : '加载策略列表失败')
        } finally {
            setLoading(false)
        }
    }, [])

    const loadHeatmap = useCallback(async () => {
        setHeatmapLoading(true)
        try {
            const res = await api.getStrategyHeatmap()
            setHeatmap(res.data?.cells ?? [])
        } catch (e) {
            console.error('Heatmap load error:', e)
        } finally {
            setHeatmapLoading(false)
        }
    }, [])

    useEffect(() => { load() }, [load])
    useEffect(() => { if (selectedTab === 'heatmap') loadHeatmap() }, [selectedTab, loadHeatmap])

    return (
        <div className="space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">策略管理</h1>
                    <p className="mt-1 text-slate-500 dark:text-slate-400">查看和管理已注册的交易策略插件</p>
                </div>
                <div className="flex items-center gap-2">
                    <button onClick={load} disabled={loading}
                        className="flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-600 transition hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800">
                        <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                        刷新
                    </button>
                </div>
            </div>

            {/* Tab Navigation */}
            <div className="flex gap-1 rounded-lg bg-slate-100 p-1 dark:bg-slate-800">
                <button
                    onClick={() => setSelectedTab('list')}
                    className={`flex-1 rounded-md px-3 py-2 text-sm font-medium transition ${
                        selectedTab === 'list'
                            ? 'bg-white shadow-sm dark:bg-slate-700 text-slate-900 dark:text-slate-100'
                            : 'text-slate-500 hover:text-slate-700 dark:text-slate-400'
                    }`}
                >
                    <Cpu className="mr-2 inline-block h-4 w-4" />
                    策略列表
                </button>
                <button
                    onClick={() => setSelectedTab('heatmap')}
                    className={`flex-1 rounded-md px-3 py-2 text-sm font-medium transition ${
                        selectedTab === 'heatmap'
                            ? 'bg-white shadow-sm dark:bg-slate-700 text-slate-900 dark:text-slate-100'
                            : 'text-slate-500 hover:text-slate-700 dark:text-slate-400'
                    }`}
                >
                    <Flame className="mr-2 inline-block h-4 w-4" />
                    策略热力图
                </button>
            </div>

            {error && (
                <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-600 dark:border-rose-900/50 dark:bg-rose-950/30 dark:text-rose-300">
                    {error}
                </div>
            )}

            {/* Tab Content */}
            {selectedTab === 'list' ? (
                <StrategyList strategies={strategies} loading={loading} />
            ) : (
                <HeatmapView cells={heatmap} loading={heatmapLoading} />
            )}

            {/* Info Section */}
            <div className="card">
                <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-100">策略系统说明</h2>
                <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
                    <InfoBlock icon={BarChart3} title="策略回测" description="使用 GlobalEquityEngine 计算真实佣金/印花税/滑点后的策略表现。" />
                    <InfoBlock icon={Award} title="策略评分" description="基于 Sharpe/胜率/回撤/一致性四维度综合评分 (A-F)。" />
                    <InfoBlock icon={Eye} title="Shadow Account" description="对比实际交易与理想信号，诊断执行质量。" />
                    <InfoBlock icon={Flame} title="热力图" description="按市场状态（趋势/震荡/波动）显示策略适配度。" />
                </div>
            </div>
        </div>
    )
}

// ─── Strategy List ─────────────────────────────────────────────────────────

function StrategyList({ strategies, loading }: { strategies: Strategy[]; loading: boolean }) {
    if (strategies.length === 0 && !loading) {
        return (
            <div className="card">
                <div className="py-12 text-center">
                    <Cpu className="mx-auto h-10 w-10 text-slate-300 dark:text-slate-600" />
                    <p className="mt-3 text-sm text-slate-400 dark:text-slate-500">暂未发现已注册的策略插件</p>
                    <p className="mt-1 text-xs text-slate-400 dark:text-slate-600">
                        策略插件位于 tradingagents/strategies/ 目录，启动后自动注册
                    </p>
                </div>
            </div>
        )
    }

    return (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            {strategies.map(s => (
                <StrategyCard key={s.name} strategy={s} />
            ))}
        </div>
    )
}

// ─── Strategy Card ─────────────────────────────────────────────────────────

function StrategyCard({ strategy }: { strategy: Strategy }) {
    const Icon = categoryIcons[strategy.category ?? 'general'] ?? Cpu

    return (
        <div className="card group hover:border-primary-300 dark:hover:border-primary-700 transition-colors">
            <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                    <div className="rounded-xl bg-primary-50 p-2.5 dark:bg-primary-900/30">
                        <Icon className="h-5 w-5 text-primary-600 dark:text-primary-400" />
                    </div>
                    <div>
                        <h3 className="font-semibold text-slate-900 dark:text-slate-100">
                            {strategy.display_name || strategy.name}
                        </h3>
                        <p className="text-xs text-slate-500 dark:text-slate-400">
                            {strategy.category || 'general'}
                        </p>
                    </div>
                </div>
                {strategy.default_active && (
                    <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400">
                        默认启用
                    </span>
                )}
            </div>

            <p className="mt-3 text-sm text-slate-600 dark:text-slate-300 line-clamp-2">
                {strategy.description}
            </p>

            {strategy.market_regimes && strategy.market_regimes.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-1">
                    {strategy.market_regimes.map(regime => (
                        <span key={regime} className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-400">
                            {regimeLabels[regime] || regime}
                        </span>
                    ))}
                </div>
            )}

            {strategy.aliases && strategy.aliases.length > 0 && (
                <div className="mt-2 text-xs text-slate-400">
                    别名: {strategy.aliases.join(', ')}
                </div>
            )}
        </div>
    )
}

// ─── Heatmap View ──────────────────────────────────────────────────────────

function HeatmapView({ cells, loading }: { cells: HeatmapCell[]; loading: boolean }) {
    if (loading) {
        return (
            <div className="card">
                <div className="py-12 text-center">
                    <RefreshCw className="mx-auto h-8 w-8 animate-spin text-slate-400" />
                    <p className="mt-3 text-sm text-slate-400">加载热力图...</p>
                </div>
            </div>
        )
    }

    if (cells.length === 0) {
        return (
            <div className="card">
                <div className="py-12 text-center">
                    <Flame className="mx-auto h-10 w-10 text-slate-300 dark:text-slate-600" />
                    <p className="mt-3 text-sm text-slate-400">暂无热力图数据</p>
                </div>
            </div>
        )
    }

    // Group by strategy
    const strategyMap = new Map<string, Map<string, HeatmapCell>>()
    const regimes = new Set<string>()
    for (const cell of cells) {
        if (!strategyMap.has(cell.strategy_name)) {
            strategyMap.set(cell.strategy_name, new Map())
        }
        strategyMap.get(cell.strategy_name)!.set(cell.market_regime, cell)
        regimes.add(cell.market_regime)
    }
    const regimeList = Array.from(regimes).sort()
    const strategyList = Array.from(strategyMap.keys()).sort()

    return (
        <div className="card overflow-hidden">
            <div className="overflow-x-auto">
                <table className="w-full">
                    <thead>
                        <tr className="border-b border-slate-200 dark:border-slate-700">
                            <th className="px-4 py-3 text-left text-sm font-medium text-slate-500">策略</th>
                            {regimeList.map(regime => (
                                <th key={regime} className="px-4 py-3 text-center text-sm font-medium text-slate-500">
                                    {regimeLabels[regime] || regime}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {strategyList.map(strategyName => (
                            <tr key={strategyName} className="border-b border-slate-100 dark:border-slate-800">
                                <td className="px-4 py-3 text-sm font-medium text-slate-900 dark:text-slate-100">
                                    {strategyName}
                                </td>
                                {regimeList.map(regime => {
                                    const cell = strategyMap.get(strategyName)?.get(regime)
                                    if (!cell) return <td key={regime} className="px-4 py-3 text-center">-</td>
                                    return (
                                        <td key={regime} className="px-4 py-3 text-center">
                                            <HeatmapCellView cell={cell} />
                                        </td>
                                    )
                                })}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>

            {/* Legend */}
            <div className="flex items-center justify-center gap-4 border-t border-slate-200 px-4 py-3 dark:border-slate-700">
                <div className="flex items-center gap-1">
                    <div className="h-3 w-3 rounded bg-emerald-500" />
                    <span className="text-xs text-slate-500">强烈推荐</span>
                </div>
                <div className="flex items-center gap-1">
                    <div className="h-3 w-3 rounded bg-emerald-300" />
                    <span className="text-xs text-slate-500">推荐</span>
                </div>
                <div className="flex items-center gap-1">
                    <div className="h-3 w-3 rounded bg-slate-400" />
                    <span className="text-xs text-slate-500">中性</span>
                </div>
                <div className="flex items-center gap-1">
                    <div className="h-3 w-3 rounded bg-rose-300" />
                    <span className="text-xs text-slate-500">不推荐</span>
                </div>
                <div className="flex items-center gap-1">
                    <div className="h-3 w-3 rounded bg-rose-500" />
                    <span className="text-xs text-slate-500">强烈不推荐</span>
                </div>
            </div>
        </div>
    )
}

// ─── Heatmap Cell ──────────────────────────────────────────────────────────

function HeatmapCellView({ cell }: { cell: HeatmapCell }) {
    const bgColor = recommendationColors[cell.current_recommendation] || 'bg-slate-400'
    const textColor = cell.current_recommendation.includes('buy')
        ? 'text-emerald-700 dark:text-emerald-300'
        : cell.current_recommendation.includes('sell')
        ? 'text-rose-700 dark:text-rose-300'
        : 'text-slate-700 dark:text-slate-300'

    return (
        <div className="flex flex-col items-center gap-1">
            <div className={`h-8 w-16 rounded-md ${bgColor} flex items-center justify-center`}>
                <span className="text-xs font-bold text-white">
                    {cell.suitability_score.toFixed(0)}
                </span>
            </div>
            <span className={`text-xs ${textColor}`}>
                {cell.current_recommendation === 'strong_buy' && '强烈推荐'}
                {cell.current_recommendation === 'buy' && '推荐'}
                {cell.current_recommendation === 'neutral' && '中性'}
                {cell.current_recommendation === 'sell' && '不推荐'}
                {cell.current_recommendation === 'strong_sell' && '强烈不推荐'}
            </span>
        </div>
    )
}

// ─── Info Block ────────────────────────────────────────────────────────────

function InfoBlock({ icon: Icon, title, description }: { icon: typeof Cpu; title: string; description: string }) {
    return (
        <div className="rounded-xl border border-slate-200 p-4 dark:border-slate-700">
            <Icon className="h-5 w-5 text-primary-600 dark:text-primary-400" />
            <h3 className="mt-2 text-sm font-semibold text-slate-900 dark:text-slate-100">{title}</h3>
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{description}</p>
        </div>
    )
}
