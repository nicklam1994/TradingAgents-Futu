/**
 * Reflections — 交易反思日志页面
 *
 * 展示反思日志列表 + 搜索筛选 + 详情展开。
 * 调用 /v1/sim/reflections 读取 BM25 记忆中的反思记录。
 */

import { useEffect, useState, useCallback, useMemo } from 'react'
import {
    Brain, RefreshCw, Search, ChevronDown, ChevronRight,
    BookOpen, Lightbulb, CheckCircle, XCircle,
} from 'lucide-react'

import { api } from '@/services/api'
import type { ReflectionEntry } from '@/types'

export default function Reflections() {
    const [reflections, setReflections] = useState<ReflectionEntry[]>([])
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)
    const [search, setSearch] = useState('')
    const [expandedId, setExpandedId] = useState<string | null>(null)

    const load = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const res = await api.getReflections()
            setReflections(res.data ?? [])
        } catch (e) {
            setError(e instanceof Error ? e.message : '加载反思日志失败')
        } finally {
            setLoading(false)
        }
    }, [])

    useEffect(() => { load() }, [load])

    // Filter by search keyword
    const filtered = useMemo(() => {
        if (!search.trim()) return reflections
        const q = search.toLowerCase()
        return reflections.filter(r =>
            r.symbol.toLowerCase().includes(q) ||
            r.lesson_summary.toLowerCase().includes(q) ||
            r.lessons.some(l => l.toLowerCase().includes(q)) ||
            r.what_was_right.toLowerCase().includes(q) ||
            r.what_was_wrong.toLowerCase().includes(q)
        )
    }, [reflections, search])

    return (
        <div className="space-y-6">
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">交易反思</h1>
                    <p className="mt-1 text-slate-500 dark:text-slate-400">
                        每笔交易的反思记录与经验教训，用于改进未来决策
                    </p>
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

            {/* Search bar */}
            <div className="relative">
                <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                <input
                    type="text"
                    value={search}
                    onChange={e => setSearch(e.target.value)}
                    placeholder="搜索反思内容（按股票、关键词）..."
                    className="w-full rounded-lg border border-slate-200 bg-white py-2.5 pl-10 pr-4 text-sm text-slate-900 placeholder-slate-400 transition focus:border-green-400 focus:outline-none focus:ring-1 focus:ring-green-400 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500 dark:focus:border-green-500 dark:focus:ring-green-500"
                />
            </div>

            {/* Stats bar */}
            <div className="flex items-center gap-4 text-sm text-slate-500 dark:text-slate-400">
                <span>共 {reflections.length} 条反思</span>
                {search && <span>筛选结果: {filtered.length} 条</span>}
            </div>

            {/* Reflection list */}
            {filtered.length === 0 && !loading ? (
                <div className="card">
                    <div className="py-12 text-center">
                        <Brain className="mx-auto h-10 w-10 text-slate-300 dark:text-slate-600" />
                        <p className="mt-3 text-sm text-slate-400 dark:text-slate-500">
                            {reflections.length === 0 ? '暂无反思记录' : '没有匹配的反思记录'}
                        </p>
                        <p className="mt-1 text-xs text-slate-400 dark:text-slate-600">
                            {reflections.length === 0
                                ? '完成模拟交易后，系统会自动生成反思日志'
                                : '尝试其他搜索关键词'
                            }
                        </p>
                    </div>
                </div>
            ) : (
                <div className="space-y-3">
                    {filtered.map(r => (
                        <ReflectionCard
                            key={r.id}
                            reflection={r}
                            expanded={expandedId === r.id}
                            onToggle={() => setExpandedId(expandedId === r.id ? null : r.id)}
                        />
                    ))}
                </div>
            )}
        </div>
    )
}

/* ── Sub-components ─────────────────────────────────────────────────────── */

function ReflectionCard({
    reflection,
    expanded,
    onToggle,
}: {
    reflection: ReflectionEntry
    expanded: boolean
    onToggle: () => void
}) {
    const verdictColor = {
        good_trade: 'bg-green-100 text-green-700 dark:bg-green-500/10 dark:text-green-400',
        bad_trade: 'bg-rose-100 text-rose-700 dark:bg-rose-500/10 dark:text-rose-400',
        neutral: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400',
    }
    const verdictLabel = { good_trade: '好交易', bad_trade: '需改进', neutral: '中性' }

    return (
        <div className="card card-hover">
            <button onClick={onToggle} className="w-full text-left">
                <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                        <div className="rounded-lg bg-purple-100 p-2 dark:bg-purple-500/10">
                            <BookOpen className="h-4 w-4 text-purple-600 dark:text-purple-400" />
                        </div>
                        <div>
                            <div className="flex items-center gap-2">
                                <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                                    {reflection.symbol}
                                </span>
                                <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${verdictColor[reflection.verdict]}`}>
                                    {verdictLabel[reflection.verdict]}
                                </span>
                                <span className="text-xs text-slate-400 dark:text-slate-500">
                                    信心 {Math.round(reflection.confidence * 100)}%
                                </span>
                            </div>
                            <p className="mt-0.5 line-clamp-1 text-xs text-slate-400 dark:text-slate-500">
                                {reflection.lesson_summary}
                            </p>
                        </div>
                    </div>
                    {expanded ? (
                        <ChevronDown className="h-4 w-4 text-slate-400" />
                    ) : (
                        <ChevronRight className="h-4 w-4 text-slate-400" />
                    )}
                </div>
            </button>

            {expanded && (
                <div className="mt-4 space-y-3 border-t border-slate-100 pt-4 dark:border-slate-800">
                    {/* Lessons */}
                    {reflection.lessons.length > 0 && (
                        <div>
                            <div className="mb-1 flex items-center gap-1.5">
                                <Lightbulb className="h-3.5 w-3.5 text-amber-500" />
                                <span className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">经验教训</span>
                            </div>
                            <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 dark:border-amber-900/30 dark:bg-amber-950/20">
                                <ul className="list-disc space-y-1 pl-4 text-sm text-slate-700 dark:text-slate-300">
                                    {reflection.lessons.map((l, i) => (
                                        <li key={i}>{l}</li>
                                    ))}
                                </ul>
                            </div>
                        </div>
                    )}

                    {/* What was right */}
                    <div>
                        <div className="mb-1 flex items-center gap-1.5">
                            <CheckCircle className="h-3.5 w-3.5 text-green-500" />
                            <span className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">做对了什么</span>
                        </div>
                        <p className="rounded-lg bg-green-50 p-3 text-sm text-slate-700 dark:bg-green-950/20 dark:text-slate-300">
                            {reflection.what_was_right}
                        </p>
                    </div>

                    {/* What was wrong */}
                    <div>
                        <div className="mb-1 flex items-center gap-1.5">
                            <XCircle className="h-3.5 w-3.5 text-rose-500" />
                            <span className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">做错了什么</span>
                        </div>
                        <p className="rounded-lg bg-rose-50 p-3 text-sm text-slate-700 dark:bg-rose-950/20 dark:text-slate-300">
                            {reflection.what_was_wrong}
                        </p>
                    </div>
                </div>
            )}
        </div>
    )
}
