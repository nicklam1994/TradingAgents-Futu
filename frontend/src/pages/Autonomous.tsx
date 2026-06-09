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
    XCircle, Loader2, Send,
} from 'lucide-react'

import { api } from '@/services/api'
import { formatTime } from '@/utils/formatTime'
import type { AutonomousTask, AutonomousTaskDetail } from '@/types'

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
    const [budget, setBudget] = useState('')
    const [creating, setCreating] = useState(false)
    const [createError, setCreateError] = useState<string | null>(null)

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

    const handleCreate = async () => {
        if (!command.trim()) return
        setCreating(true)
        setCreateError(null)
        try {
            const budgetNum = budget ? parseFloat(budget) : undefined
            const res = await api.createAutonomousTask(command.trim(), budgetNum)
            if (res.ok && res.data?.task_id) {
                setCommand('')
                setBudget('')
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

            {/* Create task */}
            <div className="card">
                <div className="flex gap-3">
                    <div className="flex-1">
                        <input
                            value={command}
                            onChange={e => setCommand(e.target.value)}
                            onKeyDown={e => e.key === 'Enter' && handleCreate()}
                            placeholder="输入交易指令，如：用1000美金模拟账户，选一只美股科技股做短线交易"
                            disabled={creating}
                            className="w-full rounded-lg border border-slate-200 bg-white px-4 py-2.5 text-sm text-slate-900 placeholder-slate-400 outline-none transition focus:border-green-400 focus:ring-2 focus:ring-green-400/20 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500 dark:focus:border-green-500"
                        />
                    </div>
                    <input
                        value={budget}
                        onChange={e => setBudget(e.target.value.replace(/[^0-9.]/g, ''))}
                        placeholder="预算(可选)"
                        disabled={creating}
                        className="w-28 rounded-lg border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-900 placeholder-slate-400 outline-none transition focus:border-green-400 focus:ring-2 focus:ring-green-400/20 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500"
                    />
                    <button
                        onClick={handleCreate}
                        disabled={creating || !command.trim()}
                        className="flex items-center gap-2 rounded-lg bg-green-600 px-5 py-2.5 text-sm font-medium text-white transition hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-green-600 dark:hover:bg-green-500"
                    >
                        {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                        {creating ? '创建中...' : '开始交易'}
                    </button>
                </div>
                {createError && (
                    <p className="mt-2 text-sm text-rose-500 dark:text-rose-400">{createError}</p>
                )}
                <p className="mt-2 text-xs text-slate-400 dark:text-slate-500">
                    💡 LLM 会自动解析指令，生成选股→分析→分配→执行→观察的 OODA 任务链
                </p>
            </div>

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


