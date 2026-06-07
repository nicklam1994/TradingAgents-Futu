/**
 * Strategies — 策略管理页面
 *
 * 展示策略列表 + 启用/禁用 toggle + 策略描述。
 * 调用 /v1/skills 读取已注册的交易策略插件。
 */

import { useEffect, useState, useCallback } from 'react'
import {
    Cpu, RefreshCw, Zap, TrendingUp,
    BarChart3, Shield, Target,
} from 'lucide-react'

import { api } from '@/services/api'
import type { Strategy } from '@/types'

export default function Strategies() {
    const [strategies, setStrategies] = useState<Strategy[]>([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)

    const load = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const res = await api.getStrategies()
            setStrategies(res.data ?? [])
        } catch (e) {
            setError(e instanceof Error ? e.message : '加载策略列表失败')
        } finally {
            setLoading(false)
        }
    }, [])

    useEffect(() => { load() }, [load])

    return (
        <div className="space-y-6">
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">策略管理</h1>
                    <p className="mt-1 text-slate-500 dark:text-slate-400">查看和管理已注册的交易策略插件</p>
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

            {/* Strategy Cards */}
            {strategies.length === 0 && !loading ? (
                <div className="card">
                    <div className="py-12 text-center">
                        <Cpu className="mx-auto h-10 w-10 text-slate-300 dark:text-slate-600" />
                        <p className="mt-3 text-sm text-slate-400 dark:text-slate-500">暂未发现已注册的策略插件</p>
                        <p className="mt-1 text-xs text-slate-400 dark:text-slate-600">
                            策略插件位于 tradingagents/skills/builtin/ 目录，启动后自动注册
                        </p>
                    </div>
                </div>
            ) : (
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
                    {strategies.map(s => (
                        <StrategyCard key={s.name} strategy={s} />
                    ))}
                </div>
            )}

            {/* Info section */}
            <div className="card">
                <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-100">策略系统说明</h2>
                <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
                    <InfoBlock icon={Cpu} title="策略注册" description="策略插件在启动时由 SkillRegistry 自动发现并注册，无需手动配置。" />
                    <InfoBlock icon={Zap} title="信号聚合" description="多个策略信号由 SkillAggregator 聚合为共识信号，用于交易决策。" />
                    <InfoBlock icon={Target} title="自适应路由" description="SkillRouter 根据当前市场行情（牛/熊/震荡）选择最适合的策略组合。" />
                </div>
            </div>
        </div>
    )
}

/* ── Sub-components ─────────────────────────────────────────────────────── */

const regimeIcons: Record<string, typeof TrendingUp> = {
    bull: TrendingUp,
    bear: BarChart3,
    rangebound: Shield,
}

const regimeLabels: Record<string, string> = {
    bull: '牛市',
    bear: '熊市',
    rangebound: '震荡',
    unknown: '通用',
}

function StrategyCard({ strategy }: { strategy: Strategy }) {
    const regimes: string[] = Array.isArray(strategy.regime) ? strategy.regime : []

    return (
        <div className="card card-hover">
            <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                    <div className="rounded-lg bg-green-100 p-2.5 dark:bg-green-500/10">
                        <Cpu className="h-5 w-5 text-green-600 dark:text-green-400" />
                    </div>
                    <div>
                        <h3 className="text-base font-semibold text-slate-900 dark:text-slate-100">
                            {strategy.display_name || strategy.name}
                        </h3>
                        <p className="text-xs text-slate-400">{strategy.name}</p>
                    </div>
                </div>
                <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
                    strategy.enabled
                        ? 'bg-green-100 text-green-700 dark:bg-green-500/10 dark:text-green-400'
                        : 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400'
                }`}>
                    {strategy.enabled ? '已启用' : '已禁用'}
                </span>
            </div>

            <p className="mt-3 text-sm text-slate-600 dark:text-slate-400">
                {strategy.description || '暂无描述'}
            </p>

            {regimes.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-1.5">
                    {regimes.map(r => {
                        const Icon = regimeIcons[r] ?? Shield
                        return (
                            <span key={r} className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-400">
                                <Icon className="h-3 w-3" />
                                {regimeLabels[r] ?? r}
                            </span>
                        )
                    })}
                </div>
            )}
        </div>
    )
}

function InfoBlock({
    icon: Icon, title, description,
}: {
    icon: React.ComponentType<{ className?: string }>
    title: string
    description: string
}) {
    return (
        <div className="rounded-lg border border-slate-100 bg-slate-50/50 p-4 dark:border-slate-800 dark:bg-slate-800/30">
            <div className="mb-2 flex items-center gap-2">
                <Icon className="h-4 w-4 text-green-600 dark:text-green-400" />
                <p className="text-sm font-medium text-slate-900 dark:text-slate-100">{title}</p>
            </div>
            <p className="text-xs text-slate-500 dark:text-slate-400">{description}</p>
        </div>
    )
}
