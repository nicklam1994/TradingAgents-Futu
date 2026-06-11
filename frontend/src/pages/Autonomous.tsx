/**
 * Autonomous — 自主交易任务页面
 *
 * 展示任务列表 + 实时日志 + 进度条 + 持仓信息。
 * 调用 /v1/autonomous, /v1/autonomous/{id}
 */

import { useEffect, useState, useCallback, useRef } from 'react'
import {
    Bot, Play, Pause, Square, RefreshCw,
    ChevronRight, ChevronDown, Clock, CheckCircle,
    XCircle, Loader2, Send, BarChart3, Trophy,
} from 'lucide-react'

import { api } from '@/services/api'
import { formatTime } from '@/utils/formatTime'
import SymbolTagInput from '@/components/SymbolTagInput'
import type { AutonomousTask, AutonomousTaskDetail, StrategyPerformance } from '@/types'

export default function Autonomous() {
    const [tasks, setTasks] = useState<AutonomousTask[]>([])
    const [counts, setCounts] = useState<Record<string, number>>({})
    const [selectedId, setSelectedId] = useState<string | null>(null)
    const [detail, setDetail] = useState<AutonomousTaskDetail | null>(null)
    const [loading, setLoading] = useState(true)
    const [detailLoading, setDetailLoading] = useState(false)
    const [detailError, setDetailError] = useState<string | null>(null)
    const [error, setError] = useState<string | null>(null)
    const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined)
    const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

    // ── Create task state ──
    const [command, setCommand] = useState('')
    const [creating, setCreating] = useState(false)
    const [createError, setCreateError] = useState<string | null>(null)
    const [strategies, setStrategies] = useState<Array<{ name: string; display_name: string; description: string; category: string; default_active: boolean }>>([])
    const [strategyPerf, setStrategyPerf] = useState<StrategyPerformance[]>([])
    const [perfLoading, setPerfLoading] = useState(false)
    const [showPerf, setShowPerf] = useState(false)

    // ── Parse → Edit → Confirm state ──
    const [parsing, setParsing] = useState(false)
    const [parseError, setParseError] = useState<string | null>(null)
    const [plates, setPlates] = useState<Array<{ code: string; name: string }>>([])
    const [platesLoading, setPlatesLoading] = useState(false)
    const [parsed, setParsed] = useState<null | {
        command: string; budget: number; currency: string; market: string
        fixed_symbols: string[]; top_n: number; max_iterations: number
        strategy_name: string; category: string
        dag_summary: Array<{ label: string; icon: string }>
        available_strategies: Array<{ name: string; display_name: string; description: string; category: string; default_active: boolean }>
    }>(null)

    // Load strategies on mount
    useEffect(() => {
        api.getStrategies().then(res => {
            const list = res.data?.strategies ?? []
            setStrategies(list)
        }).catch(() => {})
    }, [])

    // Load plates when parsed market changes
    useEffect(() => {
        if (!parsed) return
        setPlatesLoading(true)
        api.getPlates(parsed.market).then(res => {
            setPlates(res.plates ?? [])
        }).catch(() => setPlates([])).finally(() => setPlatesLoading(false))
    }, [parsed?.market])

    // Load strategy performance data
    const loadPerf = useCallback(async () => {
        setPerfLoading(true)
        try {
            const res = await api.getStrategyPerformance()
            setStrategyPerf(res.data ?? [])
        } catch {
            // Silently ignore — no data yet is expected
        } finally {
            setPerfLoading(false)
        }
    }, [])

    useEffect(() => {
        loadPerf()
    }, [loadPerf])

    const loadTasks = useCallback(async () => {
        try {
            const res = await api.getAutonomousTasks(statusFilter)
            setTasks(res.data?.tasks ?? [])
            setCounts(res.data?.counts ?? {})
        } catch (e) {
            if (!selectedId) setError(e instanceof Error ? e.message : '加载任务列表失败')
        }
    }, [statusFilter, selectedId])

    const loadDetail = useCallback(async (taskId: string) => {
        setDetailLoading(true)
        try {
            const res = await api.getAutonomousTask(taskId)
            setDetail(res.data ?? null)
            setDetailError(null)
        } catch (e) {
            const msg = e instanceof Error ? e.message : '加载任务详情失败'
            setDetailError(msg)
            setDetail(null)
        } finally {
            setDetailLoading(false)
        }
    }, [])

    // Initial load
    useEffect(() => {
        setLoading(true)
        loadTasks().finally(() => setLoading(false))
    }, [loadTasks])

    // Auto-select first task
    useEffect(() => {
        if (tasks.length > 0 && !selectedId) {
            setSelectedId(tasks[0].task_id)
        }
    }, [tasks, selectedId])

    // Load detail when selectedId changes
    useEffect(() => {
        if (selectedId) loadDetail(selectedId)
    }, [selectedId, loadDetail])

    // Poll for running tasks
    useEffect(() => {
        const hasRunning = tasks.some(t => t.status === 'running')
        if (hasRunning) {
            pollRef.current = setInterval(() => {
                loadTasks()
                if (selectedId) loadDetail(selectedId)
            }, 5000)
        }
        return () => {
            if (pollRef.current) clearInterval(pollRef.current)
        }
    }, [tasks, selectedId, loadTasks, loadDetail])

    const handleAction = async (action: 'pause' | 'resume' | 'stop', taskId: string) => {
        try {
            if (action === 'pause') await api.pauseAutonomousTask(taskId)
            else if (action === 'resume') await api.resumeAutonomousTask(taskId)
            else await api.stopAutonomousTask(taskId)
            await loadTasks()
            if (selectedId === taskId) await loadDetail(taskId)
        } catch (e) {
            setError(e instanceof Error ? e.message : `操作失败: ${action}`)
        }
    }

    const handleParse = async () => {
        if (!command.trim()) return
        setParsing(true)
        setParseError(null)
        setParsed(null)
        try {
            const res = await api.parseAutonomousCommand(command.trim())
            if (res.ok && res.data) {
                setParsed(res.data)
            }
        } catch (e) {
            setParseError(e instanceof Error ? e.message : '解析失败')
        } finally {
            setParsing(false)
        }
    }

    const handleConfirm = async () => {
        if (!parsed) return
        setCreating(true)
        setCreateError(null)
        try {
            const res = await api.createAutonomousTask(
                parsed.command, parsed.budget, parsed.currency,
                parsed.strategy_name, parsed.fixed_symbols.length > 0 ? parsed.fixed_symbols : undefined,
                parsed.top_n, parsed.max_iterations, parsed.category || undefined,
            )
            if (res.ok && res.data?.task_id) {
                setParsed(null)
                setCommand('')
                await loadTasks()
                setSelectedId(res.data.task_id)
            }
        } catch (e) {
            setCreateError(e instanceof Error ? e.message : '创建任务失败')
        } finally {
            setCreating(false)
        }
    }

    return (
        <div className="space-y-6">
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">自主交易</h1>
                    <p className="mt-1 text-slate-500 dark:text-slate-400">管理 OODA 自主交易任务的运行状态</p>
                </div>
                <button onClick={loadTasks} disabled={loading}
                    className="flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-600 transition hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800">
                    <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                    刷新
                </button>
            </div>

            {error && (
                <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-600 dark:border-rose-900/50 dark:bg-rose-950/30 dark:text-rose-300">
                    {error}
                    <button onClick={() => setError(null)} className="ml-2 underline">关闭</button>
                </div>
            )}

            {/* Create task: Parse → Edit → Confirm */}
            <div className="card">
                <div className="flex gap-3">
                    <div className="flex-1">
                        <input
                            value={command}
                            onChange={e => setCommand(e.target.value)}
                            onKeyDown={e => e.key === 'Enter' && handleParse()}
                            placeholder="输入交易指令，如：用50000港元模拟账户，选3只港股做短线交易"
                            disabled={parsing || creating}
                            className="w-full rounded-lg border border-slate-200 bg-white px-4 py-2.5 text-sm text-slate-900 placeholder-slate-400 outline-none transition focus:border-green-400 focus:ring-2 focus:ring-green-400/20 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500 dark:focus:border-green-500"
                        />
                    </div>
                    <button
                        onClick={handleParse}
                        disabled={parsing || creating || !command.trim()}
                        className="flex items-center gap-2 rounded-lg bg-green-600 px-5 py-2.5 text-sm font-medium text-white transition hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-green-600 dark:hover:bg-green-500"
                    >
                        {parsing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                        {parsing ? '解析中...' : '解析'}
                    </button>
                </div>
                {parseError && (
                    <p className="mt-2 text-sm text-rose-500 dark:text-rose-400">{parseError}</p>
                )}
                {createError && (
                    <p className="mt-2 text-sm text-rose-500 dark:text-rose-400">{createError}</p>
                )}

                {/* Parsed keywords pills */}
                {parsed && parsed.dag_summary.length > 0 && (
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                        <span className="text-xs text-slate-400 dark:text-slate-500">LLM 解析：</span>
                        {parsed.dag_summary.map((kw, i) => (
                            <span key={i} className="inline-flex items-center gap-1 rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-700 dark:bg-green-900/30 dark:text-green-400">
                                {kw.icon && <span>{kw.icon}</span>}{kw.label}
                            </span>
                        ))}
                    </div>
                )}

                {/* Editable parameters table */}
                {parsed && (
                    <div className="mt-4 space-y-3">
                        <div className="text-sm font-medium text-slate-700 dark:text-slate-300">📋 待执行指令清单 <span className="text-xs font-normal text-slate-400">（可编辑后确认执行）</span></div>
                        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                            {/* 市场 */}
                            <div>
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">市场</label>
                                <select value={parsed.market} onChange={e => setParsed({ ...parsed, market: e.target.value, currency: e.target.value === 'HK' ? 'HKD' : 'USD' })}
                                    className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100">
                                    <option value="HK">🇭🇰 港股</option>
                                    <option value="US">🇺🇸 美股</option>
                                </select>
                            </div>
                            {/* 预算 */}
                            <div>
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">预算</label>
                                <input type="number" value={parsed.budget} onChange={e => setParsed({ ...parsed, budget: parseFloat(e.target.value) || 0 })}
                                    className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100" />
                            </div>
                            {/* 币种 */}
                            <div>
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">币种</label>
                                <select value={parsed.currency} onChange={e => setParsed({ ...parsed, currency: e.target.value, market: e.target.value === 'HKD' ? 'HK' : 'US' })}
                                    className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100">
                                    <option value="HKD">HKD 港元</option>
                                    <option value="USD">USD 美元</option>
                                </select>
                            </div>
                            {/* 选股数 */}
                            <div>
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">选股数</label>
                                <input type="number" min={1} max={10} value={parsed.top_n} onChange={e => setParsed({ ...parsed, top_n: parseInt(e.target.value) || 3 })}
                                    className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100" />
                            </div>
                            {/* 策略 */}
                            <div className="col-span-2">
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">交易策略</label>
                                <select value={parsed.strategy_name} onChange={e => setParsed({ ...parsed, strategy_name: e.target.value })}
                                    className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100">
                                    {(parsed.available_strategies.length > 0 ? parsed.available_strategies : strategies).map((s: { name: string; display_name: string }) => (
                                        <option key={s.name} value={s.name}>{s.display_name}</option>
                                    ))}
                                </select>
                            </div>
                            {/* 板块 */}
                            <div className="col-span-2">
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">板块（留空则全市场选股）</label>
                                <select value={parsed.category} onChange={e => setParsed({ ...parsed, category: e.target.value })}
                                    className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100">
                                    <option value="">全部板块</option>
                                    {plates.map(p => (
                                        <option key={p.code} value={p.name}>{p.name}</option>
                                    ))}
                                    {platesLoading && <option disabled>加载中...</option>}
                                </select>
                            </div>
                            {/* 最大迭代 */}
                            <div>
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">最大迭代</label>
                                <input type="number" min={1} max={100} value={parsed.max_iterations} onChange={e => setParsed({ ...parsed, max_iterations: parseInt(e.target.value) || 30 })}
                                    className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100" />
                            </div>
                            {/* 指定股票 */}
                            <div className="col-span-2 sm:col-span-3 lg:col-span-4">
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">指定股票（留空则自动选股）</label>
                                <SymbolTagInput
                                    symbols={parsed.fixed_symbols}
                                    onChange={symbols => setParsed({ ...parsed, fixed_symbols: symbols })}
                                    placeholder="输入股票代码，回车添加（如 HK.00700）"
                                />
                            </div>
                        </div>
                        {/* Confirm button */}
                        <div className="flex justify-end gap-3 pt-2">
                            <button onClick={() => setParsed(null)} disabled={creating}
                                className="rounded-lg border border-slate-200 px-4 py-2 text-sm text-slate-600 transition hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800">
                                取消
                            </button>
                            <button onClick={handleConfirm} disabled={creating}
                                className="flex items-center gap-2 rounded-lg bg-green-600 px-5 py-2 text-sm font-medium text-white transition hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-50">
                                {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                                {creating ? '执行中...' : '确认执行'}
                            </button>
                        </div>
                    </div>
                )}
            </div>

            {/* Strategy Performance Section */}
            {strategyPerf.length > 0 && (
                <div className="card">
                    <button
                        onClick={() => setShowPerf(!showPerf)}
                        className="flex w-full items-center justify-between"
                    >
                        <div className="flex items-center gap-2">
                            <BarChart3 className="h-5 w-5 text-blue-600 dark:text-blue-400" />
                            <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">策略績效</h2>
                            <span className="rounded-full bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-700 dark:bg-blue-900/30 dark:text-blue-400">
                                {strategyPerf.length} 策略
                            </span>
                        </div>
                        {showPerf ? <ChevronDown className="h-5 w-5 text-slate-400" /> : <ChevronRight className="h-5 w-5 text-slate-400" />}
                    </button>
                    {showPerf && (
                        <div className="mt-4 overflow-x-auto">
                            <table className="w-full text-sm">
                                <thead>
                                    <tr className="border-b border-slate-200 dark:border-slate-700">
                                        <th className="pb-2 pr-4 text-left font-medium text-slate-500 dark:text-slate-400">策略名稱</th>
                                        <th className="pb-2 pr-4 text-right font-medium text-slate-500 dark:text-slate-400">交易次數</th>
                                        <th className="pb-2 pr-4 text-right font-medium text-slate-500 dark:text-slate-400">勝率</th>
                                        <th className="pb-2 pr-4 text-right font-medium text-slate-500 dark:text-slate-400">平均收益</th>
                                        <th className="pb-2 pr-4 text-right font-medium text-slate-500 dark:text-slate-400">夏普比率</th>
                                        <th className="pb-2 pr-4 text-right font-medium text-slate-500 dark:text-slate-400">最大回撤</th>
                                        <th className="pb-2 text-right font-medium text-slate-500 dark:text-slate-400">總盈虧</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {strategyPerf.map((p, i) => {
                                        const isBest = i === 0
                                        const displayName = strategies.find(s => s.name === p.strategy_name)?.display_name || p.strategy_name
                                        return (
                                            <tr key={p.strategy_name}
                                                className={`border-b border-slate-100 dark:border-slate-800 ${isBest ? 'bg-green-50/50 dark:bg-green-950/20' : ''}`}>
                                                <td className="py-2.5 pr-4">
                                                    <div className="flex items-center gap-1.5">
                                                        {isBest && <Trophy className="h-3.5 w-3.5 text-amber-500" />}
                                                        <span className={`font-medium ${isBest ? 'text-green-700 dark:text-green-400' : 'text-slate-900 dark:text-slate-100'}`}>
                                                            {displayName}
                                                        </span>
                                                    </div>
                                                </td>
                                                <td className="py-2.5 pr-4 text-right text-slate-700 dark:text-slate-300">{p.total_trades}</td>
                                                <td className="py-2.5 pr-4 text-right">
                                                    <span className={p.win_rate >= 0.5 ? 'text-green-600 dark:text-green-400' : 'text-rose-600 dark:text-rose-400'}>
                                                        {(p.win_rate * 100).toFixed(1)}%
                                                    </span>
                                                </td>
                                                <td className="py-2.5 pr-4 text-right">
                                                    <span className={p.avg_return_pct >= 0 ? 'text-green-600 dark:text-green-400' : 'text-rose-600 dark:text-rose-400'}>
                                                        {(p.avg_return_pct * 100).toFixed(2)}%
                                                    </span>
                                                </td>
                                                <td className="py-2.5 pr-4 text-right text-slate-700 dark:text-slate-300">{p.sharpe_ratio.toFixed(2)}</td>
                                                <td className="py-2.5 pr-4 text-right">
                                                    <span className="text-rose-600 dark:text-rose-400">
                                                        {(p.max_drawdown * 100).toFixed(1)}%
                                                    </span>
                                                </td>
                                                <td className="py-2.5 text-right">
                                                    <span className={p.total_pnl >= 0 ? 'text-green-600 dark:text-green-400 font-medium' : 'text-rose-600 dark:text-rose-400 font-medium'}>
                                                        {p.total_pnl >= 0 ? '+' : ''}{p.total_pnl.toFixed(2)}
                                                    </span>
                                                </td>
                                            </tr>
                                        )
                                    })}
                                </tbody>
                            </table>
                            <div className="mt-2 flex items-center justify-between">
                                <p className="text-xs text-slate-400 dark:text-slate-500">
                                    需 ≥3 策略才能排名 · 自動選擇最佳策略用於新任務
                                </p>
                                <button onClick={loadPerf} disabled={perfLoading}
                                    className="flex items-center gap-1 text-xs text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300">
                                    <RefreshCw className={`h-3 w-3 ${perfLoading ? 'animate-spin' : ''}`} />
                                    刷新
                                </button>
                            </div>
                        </div>
                    )}
                </div>
            )}

            {/* Status filter chips */}
            <div className="flex flex-wrap gap-2">
                <FilterChip label={`全部 (${Object.values(counts).reduce((a, b) => a + b, 0)})`}
                    active={!statusFilter} onClick={() => setStatusFilter(undefined)} />
                {['running', 'paused', 'completed', 'failed'].map(s => (
                    counts[s] != null && counts[s] > 0 && (
                        <FilterChip key={s} label={`${statusLabel(s)} (${counts[s]})`}
                            active={statusFilter === s} onClick={() => setStatusFilter(s === statusFilter ? undefined : s)} />
                    )
                ))}
            </div>

            {/* Main content: list + detail */}
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
                {/* Task List */}
                <div className="card lg:col-span-1">
                    <h2 className="mb-3 text-lg font-semibold text-slate-900 dark:text-slate-100">任务列表</h2>
                    {tasks.length === 0 ? (
                        <div className="py-12 text-center">
                            <Bot className="mx-auto h-10 w-10 text-slate-300 dark:text-slate-600" />
                            <p className="mt-3 text-sm text-slate-400 dark:text-slate-500">暂无自主交易任务</p>
                        </div>
                    ) : (
                        <div className="space-y-2 max-h-[600px] overflow-y-auto">
                            {tasks.map(t => (
                                <button key={t.task_id}
                                    onClick={() => setSelectedId(t.task_id)}
                                    className={`w-full rounded-lg border p-3 text-left transition-all ${
                                        selectedId === t.task_id
                                            ? 'border-green-400 bg-green-50 dark:border-green-600 dark:bg-green-950/30'
                                            : 'border-slate-200 hover:border-slate-300 dark:border-slate-700 dark:hover:border-slate-600'
                                    }`}>
                                    <div className="flex items-center justify-between">
                                        <span className="text-sm font-medium text-slate-900 dark:text-slate-100 truncate max-w-[180px]">
                                            {t.title || t.task_id}
                                        </span>
                                        <StatusBadge status={t.status} />
                                    </div>
                                    <div className="mt-2 flex items-center gap-2">
                                        <div className="h-1.5 flex-1 rounded-full bg-slate-200 dark:bg-slate-700">
                                            <div className="h-1.5 rounded-full bg-green-500 transition-all"
                                                style={{ width: `${Math.min(t.progress * 100, 100)}%` }} />
                                        </div>
                                        <span className="text-xs text-slate-400">{Math.round(t.progress * 100)}%</span>
                                    </div>
                                    <p className="mt-1 text-xs text-slate-400">
                                        {formatTime(t.created_at)}
                                    </p>
                                </button>
                            ))}
                        </div>
                    )}
                </div>

                {/* Task Detail */}
                <div className="card lg:col-span-2">
                    {!selectedId ? (
                        <div className="py-12 text-center">
                            <p className="text-sm text-slate-400 dark:text-slate-500">选择左侧任务查看详情</p>
                        </div>
                    ) : detailLoading ? (
                        <div className="flex items-center justify-center py-12">
                            <Loader2 className="h-6 w-6 animate-spin text-slate-400" />
                        </div>
                    ) : detail ? (
                        <TaskDetailView task={detail} onAction={handleAction} />
                    ) : detailError ? (
                        <div className="py-12 text-center space-y-3">
                            <XCircle className="mx-auto h-8 w-8 text-rose-400" />
                            <p className="text-sm text-rose-600 dark:text-rose-400">{detailError}</p>
                            <button onClick={() => loadDetail(selectedId)}
                                className="text-sm text-blue-600 underline dark:text-blue-400">重试</button>
                        </div>
                    ) : (
                        <div className="py-12 text-center">
                            <p className="text-sm text-slate-400">无法加载任务详情</p>
                        </div>
                    )}
                </div>
            </div>
        </div>
    )
}

/* ── Task Detail View ──────────────────────────────────────────────────── */

function TaskDetailView({
    task,
    onAction,
}: {
    task: AutonomousTaskDetail
    onAction: (action: 'pause' | 'resume' | 'stop', taskId: string) => void
}) {
    const [showLogs, setShowLogs] = useState(true)

    const loopStatus = task.loop_status as Record<string, unknown> | undefined
    const checkpoint = task.checkpoint as Record<string, unknown> | undefined
    const state = checkpoint?.state as Record<string, unknown> | undefined
    const logs = (state?.logs as string[]) ?? (checkpoint?.logs as string[]) ?? []
    const positions = (state?.positions as Array<Record<string, unknown>>) ?? []

    return (
        <div className="space-y-4">
            {/* Header with actions */}
            <div className="flex items-center justify-between">
                <div>
                    <h3 className="text-lg font-semibold text-slate-900 dark:text-slate-100">
                        {task.title || task.task_id}
                    </h3>
                    <p className="text-xs text-slate-400">ID: {task.task_id}</p>
                </div>
                <div className="flex items-center gap-2">
                    {task.status === 'running' && (
                        <button onClick={() => onAction('pause', task.task_id)}
                            className="flex items-center gap-1 rounded-lg border border-amber-300 px-3 py-1.5 text-sm text-amber-700 hover:bg-amber-50 dark:border-amber-700 dark:text-amber-400 dark:hover:bg-amber-950/30">
                            <Pause className="h-3.5 w-3.5" /> 暂停
                        </button>
                    )}
                    {task.status === 'paused' && (
                        <button onClick={() => onAction('resume', task.task_id)}
                            className="flex items-center gap-1 rounded-lg border border-green-300 px-3 py-1.5 text-sm text-green-700 hover:bg-green-50 dark:border-green-700 dark:text-green-400 dark:hover:bg-green-950/30">
                            <Play className="h-3.5 w-3.5" /> 恢复
                        </button>
                    )}
                    {(task.status === 'running' || task.status === 'paused') && (
                        <button onClick={() => onAction('stop', task.task_id)}
                            className="flex items-center gap-1 rounded-lg border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:bg-rose-50 dark:border-rose-700 dark:text-rose-400 dark:hover:bg-rose-950/30">
                            <Square className="h-3.5 w-3.5" /> 停止
                        </button>
                    )}
                </div>
            </div>

            {/* Progress */}
            <div>
                <div className="mb-1 flex items-center justify-between text-sm">
                    <span className="text-slate-600 dark:text-slate-400">进度</span>
                    <span className="font-medium text-slate-900 dark:text-slate-100">{Math.round(task.progress * 100)}%</span>
                </div>
                <div className="h-2.5 rounded-full bg-slate-200 dark:bg-slate-700">
                    <div className="h-2.5 rounded-full bg-green-500 transition-all"
                        style={{ width: `${Math.min(task.progress * 100, 100)}%` }} />
                </div>
            </div>

            {/* Info grid */}
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                <InfoCard label="状态" value={statusLabel(task.status)} />
                <InfoCard label="当前阶段" value={String(loopStatus?.phase ?? loopStatus?.current_phase ?? '--')} />
                <InfoCard label="迭代次数" value={String(loopStatus?.iteration ?? loopStatus?.iterations ?? '--')} />
                <InfoCard label="创建时间" value={formatTime(task.created_at)} />
            </div>

            {task.error && (
                <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-600 dark:border-rose-900/50 dark:bg-rose-950/30 dark:text-rose-300">
                    {task.error}
                </div>
            )}

            {/* Positions in checkpoint */}
            {positions.length > 0 && (
                <div>
                    <h4 className="mb-2 text-sm font-medium text-slate-700 dark:text-slate-300">任务持仓</h4>
                    <div className="space-y-1">
                        {positions.map((p, i) => (
                            <div key={i} className="flex items-center justify-between rounded-lg border border-slate-100 px-3 py-2 text-sm dark:border-slate-800">
                                <span className="font-medium text-slate-900 dark:text-slate-100">{String(p.symbol ?? p.code ?? '--')}</span>
                                <span className="text-slate-500">{String(p.qty ?? p.quantity ?? '--')} 股</span>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {/* Logs */}
            <div>
                <button onClick={() => setShowLogs(!showLogs)}
                    className="flex items-center gap-1 text-sm font-medium text-slate-700 dark:text-slate-300">
                    {showLogs ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                    运行日志 ({logs.length})
                </button>
                {showLogs && (
                    <div className="mt-2 max-h-64 overflow-y-auto rounded-lg bg-slate-900 p-3 font-mono text-xs text-green-400 dark:bg-slate-950">
                        {logs.length === 0 ? (
                            <p className="text-slate-500">暂无日志</p>
                        ) : (
                            logs.map((line, i) => <p key={i} className="whitespace-pre-wrap">{line}</p>)
                        )}
                    </div>
                )}
            </div>
        </div>
    )
}

/* ── Sub-components ─────────────────────────────────────────────────────── */

function StatusBadge({ status }: { status: string }) {
    const cfg: Record<string, { icon: typeof Play; color: string }> = {
        running: { icon: Loader2, color: 'text-blue-600 bg-blue-100 dark:text-blue-400 dark:bg-blue-500/10' },
        paused: { icon: Pause, color: 'text-amber-600 bg-amber-100 dark:text-amber-400 dark:bg-amber-500/10' },
        completed: { icon: CheckCircle, color: 'text-green-600 bg-green-100 dark:text-green-400 dark:bg-green-500/10' },
        failed: { icon: XCircle, color: 'text-rose-600 bg-rose-100 dark:text-rose-400 dark:bg-rose-500/10' },
        pending: { icon: Clock, color: 'text-slate-600 bg-slate-100 dark:text-slate-400 dark:bg-slate-500/10' },
    }
    const { icon: Icon, color } = cfg[status] ?? cfg.pending
    return (
        <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${color}`}>
            <Icon className={`h-3 w-3 ${status === 'running' ? 'animate-spin' : ''}`} />
            {statusLabel(status)}
        </span>
    )
}

function FilterChip({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
    return (
        <button onClick={onClick}
            className={`rounded-full px-3 py-1 text-xs font-medium transition ${
                active
                    ? 'bg-green-600 text-white dark:bg-green-500'
                    : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700'
            }`}>
            {label}
        </button>
    )
}

function InfoCard({ label, value }: { label: string; value: string }) {
    return (
        <div className="rounded-lg border border-slate-200 bg-slate-50/80 px-3 py-2 dark:border-slate-700 dark:bg-slate-800/40">
            <p className="text-xs uppercase tracking-wider text-slate-400">{label}</p>
            <p className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">{value}</p>
        </div>
    )
}

/* ── Helpers ────────────────────────────────────────────────────────────── */

function statusLabel(status: string): string {
    const map: Record<string, string> = {
        running: '运行中', paused: '已暂停', completed: '已完成',
        failed: '失败', pending: '等待中',
    }
    return map[status] ?? status
}


