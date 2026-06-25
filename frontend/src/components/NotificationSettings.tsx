import { useState, useEffect, useCallback } from 'react'
import { Bell, Send, Loader2, Plus, Trash2, Edit3, CheckCircle2, AlertTriangle, Settings2, ChevronDown, ChevronUp, Shield } from 'lucide-react'
import { api } from '@/services/api'
import type {
    NotificationConfigResponse,
    NotificationDiagnosticsResponse,
    AlertRule,
    AlertRuleRequest,
} from '@/types'

// ─── Channel display metadata ──────────────────────────────────────────

const CHANNEL_META: Record<string, { label: string; icon: string; color: string; fields: { key: string; label: string; type?: string; placeholder?: string }[] }> = {
    wechat: {
        label: '企业微信',
        icon: '💬',
        color: 'emerald',
        fields: [
            { key: 'wechat_webhook_url', label: 'Webhook URL', placeholder: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...' },
        ],
    },
    feishu: {
        label: '飞书',
        icon: '🐦',
        color: 'blue',
        fields: [
            { key: 'feishu_webhook_url', label: 'Webhook URL', placeholder: 'https://open.feishu.cn/open-apis/bot/v2/hook/...' },
            { key: 'feishu_webhook_secret', label: '签名密钥（可选）', placeholder: '留空则不验签' },
        ],
    },
    telegram: {
        label: 'Telegram',
        icon: '✈️',
        color: 'sky',
        fields: [
            { key: 'telegram_bot_token', label: 'Bot Token', placeholder: '123456:ABC-DEF...' },
            { key: 'telegram_chat_id', label: 'Chat ID', placeholder: '-1001234567890' },
        ],
    },
    email: {
        label: '邮件',
        icon: '📧',
        color: 'amber',
        fields: [
            { key: 'email_sender', label: '发件邮箱', placeholder: 'alert@example.com' },
            { key: 'email_password', label: '授权码/密码', type: 'password', placeholder: 'SMTP 授权码' },
        ],
    },
    discord: {
        label: 'Discord',
        icon: '🎮',
        color: 'indigo',
        fields: [
            { key: 'discord_webhook_url', label: 'Webhook URL', placeholder: 'https://discord.com/api/webhooks/...' },
        ],
    },
    slack: {
        label: 'Slack',
        icon: '📱',
        color: 'purple',
        fields: [
            { key: 'slack_webhook_url', label: 'Webhook URL', placeholder: 'https://hooks.slack.com/services/...' },
        ],
    },
}

const ALL_CHANNELS = Object.keys(CHANNEL_META)

const ROUTE_LABELS: Record<string, string> = {
    report: '📊 分析报告',
    alert: '🔔 预警通知',
    system_error: '⚠️ 系统错误',
}

const CONDITION_OPTIONS = [
    { value: 'any_analysis', label: '任意分析完成' },
    { value: 'signal_match', label: '信号匹配' },
    { value: 'price_above', label: '价格高于' },
    { value: 'price_below', label: '价格低于' },
    { value: 'change_above', label: '涨幅超过 (%)' },
    { value: 'change_below', label: '跌幅超过 (%)' },
]

const SEVERITY_OPTIONS = [
    { value: 'info', label: '信息' },
    { value: 'warning', label: '警告' },
    { value: 'error', label: '错误' },
    { value: 'critical', label: '严重' },
]


// ─── Notification Settings Component ────────────────────────────────────

export default function NotificationSettings() {
    // Config state
    const [config, setConfig] = useState<NotificationConfigResponse | null>(null)
    const [configLoading, setConfigLoading] = useState(true)
    const [configError, setConfigError] = useState<string | null>(null)

    // Channel editing state
    const [editingChannel, setEditingChannel] = useState<string | null>(null)
    const [channelForm, setChannelForm] = useState<Record<string, string>>({})
    const [channelSaving, setChannelSaving] = useState(false)

    // Test state
    const [testingChannel, setTestingChannel] = useState<string | null>(null)
    const [testResult, setTestResult] = useState<{ channel: string; ok: boolean; message: string } | null>(null)

    // Diagnostics state
    const [diag, setDiag] = useState<NotificationDiagnosticsResponse | null>(null)
    const [diagLoading, setDiagLoading] = useState(false)

    // Route editing
    const [editingRoutes, setEditingRoutes] = useState(false)
    const [routeForm, setRouteForm] = useState<Record<string, string[]>>({})

    // Alert rules
    const [alertRules, setAlertRules] = useState<AlertRule[]>([])
    const [showAlertForm, setShowAlertForm] = useState(false)
    const [editingRule, setEditingRule] = useState<AlertRule | null>(null)
    const [ruleForm, setRuleForm] = useState<AlertRuleRequest>({
        name: '',
        description: '',
        condition: 'any_analysis',
        condition_value: '',
        severity: 'warning',
        stock_codes: [],
        channels: [],
        route_type: 'alert',
    })
    const [ruleSaving, setRuleSaving] = useState(false)
    const [stockInput, setStockInput] = useState('')

    // Expand/collapse
    const [channelsExpanded, setChannelsExpanded] = useState(true)
    const [routesExpanded, setRoutesExpanded] = useState(false)
    const [alertsExpanded, setAlertsExpanded] = useState(false)

    // ─── Load config ─────────────────────────────────────────────

    const loadConfig = useCallback(async () => {
        setConfigLoading(true)
        setConfigError(null)
        try {
            const data = await api.getNotificationConfig()
            setConfig(data)
            setRouteForm(data.routes || {})
        } catch (err) {
            setConfigError(err instanceof Error ? err.message : '加载通知配置失败')
        } finally {
            setConfigLoading(false)
        }
    }, [])

    const loadAlertRules = useCallback(async () => {
        try {
            const data = await api.getAlertRules()
            setAlertRules(data.rules || [])
        } catch (err) {
            console.error('Failed to load alert rules:', err)
        }
    }, [])

    useEffect(() => {
        loadConfig()
        loadAlertRules()
    }, [loadConfig, loadAlertRules])

    // ─── Channel config handlers ─────────────────────────────────

    const handleToggleChannel = async (channel: string, enabled: boolean) => {
        if (!config) return
        try {
            await api.updateNotificationConfig({
                channels: { [channel]: { enabled } },
            })
            await loadConfig()
        } catch (err) {
            alert(err instanceof Error ? err.message : '更新失败')
        }
    }

    const handleEditChannel = (channel: string) => {
        setEditingChannel(channel)
        setChannelForm({})
        setTestResult(null)
    }

    const handleSaveChannel = async () => {
        if (!editingChannel) return
        setChannelSaving(true)
        try {
            await api.updateNotificationConfig({
                channels: { [editingChannel]: { ...channelForm, enabled: true } },
            })
            setEditingChannel(null)
            setChannelForm({})
            await loadConfig()
        } catch (err) {
            alert(err instanceof Error ? err.message : '保存失败')
        } finally {
            setChannelSaving(false)
        }
    }

    const handleTestChannel = async (channel: string) => {
        setTestingChannel(channel)
        setTestResult(null)
        try {
            const res = await api.testNotification(channel)
            setTestResult({ channel, ok: true, message: res.message })
        } catch (err) {
            setTestResult({ channel, ok: false, message: err instanceof Error ? err.message : '测试失败' })
        } finally {
            setTestingChannel(null)
        }
    }

    // ─── Route handlers ──────────────────────────────────────────

    const handleSaveRoutes = async () => {
        try {
            await api.updateNotificationConfig({ routes: routeForm })
            setEditingRoutes(false)
            await loadConfig()
        } catch (err) {
            alert(err instanceof Error ? err.message : '保存路由配置失败')
        }
    }

    const toggleRouteChannel = (route: string, channel: string) => {
        setRouteForm(prev => {
            const current = prev[route] || []
            const next = current.includes(channel)
                ? current.filter(c => c !== channel)
                : [...current, channel]
            return { ...prev, [route]: next }
        })
    }

    // ─── Diagnostics ─────────────────────────────────────────────

    const handleRunDiagnostics = async () => {
        setDiagLoading(true)
        try {
            const data = await api.getNotificationDiagnostics()
            setDiag(data)
        } catch (err) {
            alert(err instanceof Error ? err.message : '诊断失败')
        } finally {
            setDiagLoading(false)
        }
    }

    // ─── Alert rule handlers ─────────────────────────────────────

    const handleCreateRule = async () => {
        if (!ruleForm.name.trim()) { alert('请填写规则名称'); return }
        setRuleSaving(true)
        try {
            await api.createAlertRule(ruleForm)
            setShowAlertForm(false)
            resetRuleForm()
            await loadAlertRules()
        } catch (err) {
            alert(err instanceof Error ? err.message : '创建失败')
        } finally {
            setRuleSaving(false)
        }
    }

    const handleUpdateRule = async () => {
        if (!editingRule) return
        setRuleSaving(true)
        try {
            await api.updateAlertRule(editingRule.id, ruleForm)
            setEditingRule(null)
            resetRuleForm()
            await loadAlertRules()
        } catch (err) {
            alert(err instanceof Error ? err.message : '更新失败')
        } finally {
            setRuleSaving(false)
        }
    }

    const handleDeleteRule = async (ruleId: string) => {
        if (!confirm('确认删除此预警规则？')) return
        try {
            await api.deleteAlertRule(ruleId)
            await loadAlertRules()
        } catch (err) {
            alert(err instanceof Error ? err.message : '删除失败')
        }
    }

    const resetRuleForm = () => {
        setRuleForm({
            name: '',
            description: '',
            condition: 'any_analysis',
            condition_value: '',
            severity: 'warning',
            stock_codes: [],
            channels: [],
            route_type: 'alert',
        })
        setStockInput('')
    }

    const startEditRule = (rule: AlertRule) => {
        setEditingRule(rule)
        setRuleForm({
            name: rule.name,
            description: rule.description,
            condition: rule.condition,
            condition_value: rule.condition_value,
            severity: rule.severity,
            stock_codes: rule.stock_codes,
            channels: rule.channels,
            route_type: rule.route_type,
        })
        setShowAlertForm(true)
    }

    const addStockCode = () => {
        const code = stockInput.trim().toUpperCase()
        if (code && !ruleForm.stock_codes?.includes(code)) {
            setRuleForm(prev => ({ ...prev, stock_codes: [...(prev.stock_codes || []), code] }))
            setStockInput('')
        }
    }

    // ─── Render ──────────────────────────────────────────────────

    if (configLoading && !config) {
        return (
            <div className="card space-y-4">
                <div className="flex items-center gap-2">
                    <Bell className="w-5 h-5 text-orange-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">通知系统</h2>
                    <Loader2 className="ml-auto w-4 h-4 animate-spin text-slate-400" />
                </div>
                <p className="text-sm text-slate-400">加载中...</p>
            </div>
        )
    }

    return (
        <div className="space-y-6">
            {configError && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-300">
                    ⚠ {configError}
                </div>
            )}
            {/* ─── Notification Channels ─── */}
            <div className="card space-y-4">
                <div
                    className="flex items-center gap-2 cursor-pointer select-none"
                    onClick={() => setChannelsExpanded(!channelsExpanded)}
                >
                    <Bell className="w-5 h-5 text-orange-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">通知渠道</h2>
                    {config && (
                        <span className="text-xs text-slate-400 ml-2">
                            {Object.values(config.channels).filter(c => c.enabled).length} / {ALL_CHANNELS.length} 已启用
                        </span>
                    )}
                    <div className="ml-auto">
                        {channelsExpanded ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronDown className="w-4 h-4 text-slate-400" />}
                    </div>
                </div>

                {channelsExpanded && (
                    <div className="space-y-3">
                        {ALL_CHANNELS.map(chName => {
                            const meta = CHANNEL_META[chName]
                            const chInfo = config?.channels[chName]
                            const isEnabled = chInfo?.enabled ?? false
                            const isEditing = editingChannel === chName

                            return (
                                <div
                                    key={chName}
                                    className={`rounded-xl border px-4 py-3 space-y-2 transition-colors ${
                                        isEnabled
                                            ? 'border-emerald-200/80 bg-emerald-50/50 dark:border-emerald-800/40 dark:bg-emerald-950/20'
                                            : 'border-slate-200/80 bg-slate-50/80 dark:border-slate-700/80 dark:bg-slate-900/40'
                                    }`}
                                >
                                    <div className="flex items-center justify-between">
                                        <div className="flex items-center gap-2">
                                            <span className="text-lg">{meta.icon}</span>
                                            <div>
                                                <span className="text-sm font-medium text-slate-700 dark:text-slate-200">{meta.label}</span>
                                                {isEnabled && (
                                                    <span className="ml-2 inline-block w-2 h-2 rounded-full bg-emerald-500" />
                                                )}
                                            </div>
                                        </div>
                                        <div className="flex items-center gap-2">
                                            <button
                                                type="button"
                                                onClick={() => handleEditChannel(chName)}
                                                className="text-xs text-slate-400 hover:text-blue-500 transition-colors"
                                            >
                                                <Edit3 className="w-3.5 h-3.5" />
                                            </button>
                                            <button
                                                type="button"
                                                onClick={() => handleToggleChannel(chName, !isEnabled)}
                                                className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                                                    isEnabled ? 'bg-emerald-500' : 'bg-slate-300 dark:bg-slate-600'
                                                }`}
                                            >
                                                <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${isEnabled ? 'translate-x-6' : 'translate-x-1'}`} />
                                            </button>
                                        </div>
                                    </div>

                                    {/* Key status indicators */}
                                    {chInfo && !isEditing && (
                                        <div className="flex flex-wrap gap-1.5">
                                            {meta.fields.map(f => {
                                                const keyStatus = chInfo.keys[f.key]
                                                const configured = keyStatus?.configured ?? false
                                                return (
                                                    <span
                                                        key={f.key}
                                                        className={`inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full ${
                                                            configured
                                                                ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300'
                                                                : 'bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500'
                                                        }`}
                                                    >
                                                        {configured ? <CheckCircle2 className="w-2.5 h-2.5" /> : <span className="w-2.5 h-2.5 rounded-full border border-slate-300" />}
                                                        {f.label}
                                                    </span>
                                                )
                                            })}
                                        </div>
                                    )}

                                    {/* Edit form */}
                                    {isEditing && (
                                        <div className="space-y-2 pt-1">
                                            {meta.fields.map(f => (
                                                <div key={f.key}>
                                                    <label className="block text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">{f.label}</label>
                                                    <input
                                                        type={f.type || 'text'}
                                                        value={channelForm[f.key] || ''}
                                                        onChange={e => setChannelForm(prev => ({ ...prev, [f.key]: e.target.value }))}
                                                        className="input w-full text-sm"
                                                        placeholder={f.placeholder}
                                                    />
                                                </div>
                                            ))}
                                            <div className="flex items-center gap-2 pt-1">
                                                <button
                                                    type="button"
                                                    onClick={handleSaveChannel}
                                                    disabled={channelSaving}
                                                    className="btn-primary inline-flex items-center gap-1.5 text-xs"
                                                >
                                                    {channelSaving ? <Loader2 className="w-3 h-3 animate-spin" /> : <CheckCircle2 className="w-3 h-3" />}
                                                    保存
                                                </button>
                                                <button
                                                    type="button"
                                                    onClick={() => handleTestChannel(chName)}
                                                    disabled={testingChannel === chName}
                                                    className="btn-secondary inline-flex items-center gap-1.5 text-xs"
                                                >
                                                    {testingChannel === chName ? <Loader2 className="w-3 h-3 animate-spin" /> : <Send className="w-3 h-3" />}
                                                    测试
                                                </button>
                                                <button
                                                    type="button"
                                                    onClick={() => { setEditingChannel(null); setChannelForm({}) }}
                                                    className="text-xs text-slate-400 hover:text-slate-600"
                                                >
                                                    取消
                                                </button>
                                            </div>
                                        </div>
                                    )}

                                    {/* Test result */}
                                    {testResult?.channel === chName && (
                                        <div className={`rounded-lg border px-3 py-2 text-xs ${
                                            testResult.ok
                                                ? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/60 dark:bg-emerald-950/30 dark:text-emerald-300'
                                                : 'border-rose-200 bg-rose-50 text-rose-600 dark:border-rose-900/60 dark:bg-rose-950/30 dark:text-rose-300'
                                        }`}>
                                            {testResult.message}
                                        </div>
                                    )}
                                </div>
                            )
                        })}
                    </div>
                )}
            </div>

            {/* ─── Route Configuration ─── */}
            <div className="card space-y-4">
                <div
                    className="flex items-center gap-2 cursor-pointer select-none"
                    onClick={() => setRoutesExpanded(!routesExpanded)}
                >
                    <Settings2 className="w-5 h-5 text-blue-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">路由配置</h2>
                    <div className="ml-auto">
                        {routesExpanded ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronDown className="w-4 h-4 text-slate-400" />}
                    </div>
                </div>

                {routesExpanded && config && (
                    <div className="space-y-3">
                        <p className="text-xs text-slate-400">配置每种通知类型发送到哪些渠道。未配置的渠道会被忽略。</p>
                        {Object.entries(ROUTE_LABELS).map(([routeType, label]) => {
                            const channels = editingRoutes ? (routeForm[routeType] || []) : (config.routes[routeType] || [])
                            return (
                                <div key={routeType} className="rounded-xl border border-slate-200/80 bg-slate-50/80 px-4 py-3 dark:border-slate-700/80 dark:bg-slate-900/40">
                                    <div className="text-sm font-medium text-slate-700 dark:text-slate-200 mb-2">{label}</div>
                                    <div className="flex flex-wrap gap-2">
                                        {ALL_CHANNELS.map(ch => {
                                            const selected = channels.includes(ch)
                                            const meta = CHANNEL_META[ch]
                                            return (
                                                <button
                                                    key={ch}
                                                    type="button"
                                                    onClick={() => editingRoutes && toggleRouteChannel(routeType, ch)}
                                                    disabled={!editingRoutes}
                                                    className={`inline-flex items-center gap-1 text-xs px-2.5 py-1 rounded-full transition-colors ${
                                                        selected
                                                            ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300 ring-1 ring-blue-300 dark:ring-blue-700'
                                                            : 'bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500'
                                                    } ${editingRoutes ? 'cursor-pointer hover:ring-1 hover:ring-blue-300' : 'cursor-default'}`}
                                                >
                                                    {meta.icon} {meta.label}
                                                </button>
                                            )
                                        })}
                                    </div>
                                </div>
                            )
                        })}
                        <div className="flex items-center gap-2">
                            {editingRoutes ? (
                                <>
                                    <button type="button" onClick={handleSaveRoutes} className="btn-primary text-xs">保存路由</button>
                                    <button type="button" onClick={() => { setEditingRoutes(false); setRouteForm(config.routes) }} className="text-xs text-slate-400 hover:text-slate-600">取消</button>
                                </>
                            ) : (
                                <button type="button" onClick={() => setEditingRoutes(true)} className="btn-secondary text-xs">编辑路由</button>
                            )}
                        </div>
                    </div>
                )}
            </div>

            {/* ─── Diagnostics ─── */}
            <div className="card space-y-4">
                <div className="flex items-center gap-2">
                    <Shield className="w-5 h-5 text-teal-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">配置诊断</h2>
                    <button
                        type="button"
                        onClick={handleRunDiagnostics}
                        disabled={diagLoading}
                        className="ml-auto btn-secondary inline-flex items-center gap-1.5 text-xs"
                    >
                        {diagLoading ? <Loader2 className="w-3 h-3 animate-spin" /> : <Shield className="w-3 h-3" />}
                        运行诊断
                    </button>
                </div>

                {diag && (
                    <div className="space-y-2">
                        <div className={`rounded-lg border px-3 py-2 text-xs ${
                            diag.ok
                                ? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/60 dark:bg-emerald-950/30 dark:text-emerald-300'
                                : 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-300'
                        }`}>
                            {diag.ok ? '✓ 配置正常' : `发现 ${diag.errors.length} 个错误、${diag.warnings.length} 个警告`}
                        </div>
                        {diag.errors.map((e, i) => (
                            <div key={`e-${i}`} className="flex items-start gap-2 text-xs text-rose-600 dark:text-rose-400">
                                <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />
                                <span>{e.message}</span>
                            </div>
                        ))}
                        {diag.warnings.map((w, i) => (
                            <div key={`w-${i}`} className="flex items-start gap-2 text-xs text-amber-600 dark:text-amber-400">
                                <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />
                                <span>{w.message}</span>
                            </div>
                        ))}
                    </div>
                )}
            </div>

            {/* ─── Alert Rules ─── */}
            <div className="card space-y-4">
                <div
                    className="flex items-center gap-2 cursor-pointer select-none"
                    onClick={() => setAlertsExpanded(!alertsExpanded)}
                >
                    <AlertTriangle className="w-5 h-5 text-rose-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">预警规则</h2>
                    <span className="text-xs text-slate-400 ml-2">{alertRules.length} 条规则</span>
                    <div className="ml-auto flex items-center gap-2">
                        {alertsExpanded && (
                            <button
                                type="button"
                                onClick={(e) => { e.stopPropagation(); resetRuleForm(); setEditingRule(null); setShowAlertForm(true) }}
                                className="btn-secondary inline-flex items-center gap-1 text-xs"
                            >
                                <Plus className="w-3 h-3" /> 新建
                            </button>
                        )}
                        {alertsExpanded ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronDown className="w-4 h-4 text-slate-400" />}
                    </div>
                </div>

                {alertsExpanded && (
                    <div className="space-y-3">
                        {/* Alert rule form */}
                        {showAlertForm && (
                            <div className="rounded-xl border border-blue-200 bg-blue-50/50 px-4 py-3 space-y-3 dark:border-blue-800/40 dark:bg-blue-950/20">
                                <div className="text-sm font-medium text-slate-700 dark:text-slate-200">
                                    {editingRule ? '编辑预警规则' : '新建预警规则'}
                                </div>
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                                    <div>
                                        <label className="block text-xs font-medium text-slate-500 mb-1">规则名称 *</label>
                                        <input
                                            type="text"
                                            value={ruleForm.name}
                                            onChange={e => setRuleForm(prev => ({ ...prev, name: e.target.value }))}
                                            className="input w-full text-sm"
                                            placeholder="如：茅台看多信号"
                                        />
                                    </div>
                                    <div>
                                        <label className="block text-xs font-medium text-slate-500 mb-1">触发条件</label>
                                        <select
                                            value={ruleForm.condition}
                                            onChange={e => setRuleForm(prev => ({ ...prev, condition: e.target.value }))}
                                            className="input w-full text-sm"
                                        >
                                            {CONDITION_OPTIONS.map(opt => (
                                                <option key={opt.value} value={opt.value}>{opt.label}</option>
                                            ))}
                                        </select>
                                    </div>
                                    {ruleForm.condition && !['any_analysis'].includes(ruleForm.condition) && (
                                        <div>
                                            <label className="block text-xs font-medium text-slate-500 mb-1">条件值</label>
                                            <input
                                                type="text"
                                                value={ruleForm.condition_value}
                                                onChange={e => setRuleForm(prev => ({ ...prev, condition_value: e.target.value }))}
                                                className="input w-full text-sm"
                                                placeholder={ruleForm.condition === 'signal_match' ? '如：看多' : '如：100'}
                                            />
                                        </div>
                                    )}
                                    <div>
                                        <label className="block text-xs font-medium text-slate-500 mb-1">严重级别</label>
                                        <select
                                            value={ruleForm.severity}
                                            onChange={e => setRuleForm(prev => ({ ...prev, severity: e.target.value }))}
                                            className="input w-full text-sm"
                                        >
                                            {SEVERITY_OPTIONS.map(opt => (
                                                <option key={opt.value} value={opt.value}>{opt.label}</option>
                                            ))}
                                        </select>
                                    </div>
                                    <div>
                                        <label className="block text-xs font-medium text-slate-500 mb-1">股票代码（留空=全部）</label>
                                        <div className="flex items-center gap-1.5">
                                            <input
                                                type="text"
                                                value={stockInput}
                                                onChange={e => setStockInput(e.target.value)}
                                                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addStockCode() } }}
                                                className="input flex-1 text-sm"
                                                placeholder="输入代码回车添加"
                                            />
                                            <button type="button" onClick={addStockCode} className="btn-secondary text-xs px-2">+</button>
                                        </div>
                                        {ruleForm.stock_codes && ruleForm.stock_codes.length > 0 && (
                                            <div className="flex flex-wrap gap-1 mt-1">
                                                {ruleForm.stock_codes.map(code => (
                                                    <span key={code} className="inline-flex items-center gap-1 text-[10px] bg-slate-100 dark:bg-slate-800 px-2 py-0.5 rounded-full">
                                                        {code}
                                                        <button
                                                            type="button"
                                                            onClick={() => setRuleForm(prev => ({ ...prev, stock_codes: prev.stock_codes?.filter(c => c !== code) }))}
                                                            className="text-rose-400 hover:text-rose-600"
                                                        >×</button>
                                                    </span>
                                                ))}
                                            </div>
                                        )}
                                    </div>
                                    <div>
                                        <label className="block text-xs font-medium text-slate-500 mb-1">描述</label>
                                        <input
                                            type="text"
                                            value={ruleForm.description}
                                            onChange={e => setRuleForm(prev => ({ ...prev, description: e.target.value }))}
                                            className="input w-full text-sm"
                                            placeholder="可选描述"
                                        />
                                    </div>
                                </div>
                                <div className="flex items-center gap-2">
                                    <button
                                        type="button"
                                        onClick={editingRule ? handleUpdateRule : handleCreateRule}
                                        disabled={ruleSaving}
                                        className="btn-primary inline-flex items-center gap-1.5 text-xs"
                                    >
                                        {ruleSaving ? <Loader2 className="w-3 h-3 animate-spin" /> : <CheckCircle2 className="w-3 h-3" />}
                                        {editingRule ? '更新' : '创建'}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => { setShowAlertForm(false); setEditingRule(null); resetRuleForm() }}
                                        className="text-xs text-slate-400 hover:text-slate-600"
                                    >
                                        取消
                                    </button>
                                </div>
                            </div>
                        )}

                        {/* Alert rules list */}
                        {alertRules.length === 0 && !showAlertForm && (
                            <div className="text-center py-6 text-sm text-slate-400">
                                暂无预警规则，点击「新建」创建第一条规则
                            </div>
                        )}
                        {alertRules.map(rule => (
                            <div
                                key={rule.id}
                                className="rounded-xl border border-slate-200/80 bg-slate-50/80 px-4 py-3 dark:border-slate-700/80 dark:bg-slate-900/40"
                            >
                                <div className="flex items-center justify-between">
                                    <div className="flex items-center gap-2">
                                        <span className={`inline-block w-2 h-2 rounded-full ${
                                            rule.status === 'active' ? 'bg-emerald-500' : rule.status === 'paused' ? 'bg-amber-500' : 'bg-slate-400'
                                        }`} />
                                        <span className="text-sm font-medium text-slate-700 dark:text-slate-200">{rule.name}</span>
                                        <span className="text-[10px] text-slate-400 bg-slate-100 dark:bg-slate-800 px-1.5 py-0.5 rounded">
                                            {CONDITION_OPTIONS.find(c => c.value === rule.condition)?.label || rule.condition}
                                        </span>
                                        <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                                            rule.severity === 'critical' ? 'bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300'
                                            : rule.severity === 'error' ? 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300'
                                            : rule.severity === 'warning' ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                                            : 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400'
                                        }`}>
                                            {SEVERITY_OPTIONS.find(s => s.value === rule.severity)?.label || rule.severity}
                                        </span>
                                    </div>
                                    <div className="flex items-center gap-2">
                                        {rule.trigger_count > 0 && (
                                            <span className="text-[10px] text-slate-400">触发 {rule.trigger_count} 次</span>
                                        )}
                                        <button
                                            type="button"
                                            onClick={() => startEditRule(rule)}
                                            className="text-slate-400 hover:text-blue-500 transition-colors"
                                        >
                                            <Edit3 className="w-3.5 h-3.5" />
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => handleDeleteRule(rule.id)}
                                            className="text-slate-400 hover:text-rose-500 transition-colors"
                                        >
                                            <Trash2 className="w-3.5 h-3.5" />
                                        </button>
                                    </div>
                                </div>
                                {rule.description && (
                                    <p className="text-xs text-slate-400 mt-1 ml-4">{rule.description}</p>
                                )}
                                {rule.stock_codes.length > 0 && (
                                    <div className="flex flex-wrap gap-1 mt-1 ml-4">
                                        {rule.stock_codes.map(code => (
                                            <span key={code} className="text-[10px] bg-slate-100 dark:bg-slate-800 px-1.5 py-0.5 rounded">{code}</span>
                                        ))}
                                    </div>
                                )}
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </div>
    )
}
