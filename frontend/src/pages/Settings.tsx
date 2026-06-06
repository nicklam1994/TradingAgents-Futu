import { useState, useEffect, useMemo } from 'react'
import { Save, Key, Database, Loader2, Trash2, Link2, Copy, Plus, CheckCircle2, Mail, Flame, Webhook, Search, MessageCircle, BarChart3 } from 'lucide-react'
import { api } from '@/services/api'
import { useAuthStore } from '@/stores/authStore'
import type { RuntimeWarmupResult, UserToken } from '@/types'

type ProviderPreset = {
    id: string
    label: string
    provider: string
    baseUrl: string
    protocol: string
    editableBaseUrl?: boolean
}

type SearchProvider = {
    name: string
    label: string
    env_key: string
    api_key: string
    base_url?: string
    enabled: boolean
}

type DataSourceProvider = {
    name: string
    label: string
    env_key: string
    market: string  // "US" | "HK" | "both"
    api_key: string
    enabled: boolean
}

// Default providers (fallback if /providers.json fails to load)
const DEFAULT_PROVIDER_PRESETS: ProviderPreset[] = [
    { id: 'openai', label: 'OpenAI', provider: 'openai', baseUrl: 'https://api.openai.com/v1', protocol: 'OpenAI' },
    { id: 'custom-openai', label: '自定义 OpenAI 兼容', provider: 'openai', baseUrl: '', protocol: 'OpenAI 兼容', editableBaseUrl: true },
]

type ModelPreset = {
    id: string
    label: string
    tier: 'quick' | 'deep'

}

const DEFAULT_MODEL_PRESETS: ModelPreset[] = []

function inferPreset(llmProvider: string, backendUrl: string): string {
    const normalizedProvider = (llmProvider || '').toLowerCase()
    const normalizedUrl = (backendUrl || '').replace(/\/$/, '')
    const matched = DEFAULT_PROVIDER_PRESETS.find((preset) => {
        if (preset.provider !== normalizedProvider) return false
        if (!preset.baseUrl && preset.id !== 'custom-openai') return true
        return preset.baseUrl.replace(/\/$/, '') === normalizedUrl
    })
    if (matched) return matched.id
    if (normalizedProvider === 'openai') return 'custom-openai'
    return normalizedProvider || 'openai'
}

export default function Settings() {
    const { user } = useAuthStore()
    const [defaultAnalysts, setDefaultAnalysts] = useState(['market', 'social', 'news', 'fundamentals', 'macro', 'smart_money', 'volume_price'])
    const [customPrompt, setCustomPrompt] = useState('')
    const [llmApiKey, setLlmApiKey] = useState('')
    const [hasStoredApiKey, setHasStoredApiKey] = useState(false)
    const [wecomWebhook, setWecomWebhook] = useState('')
    const [hasStoredWebhook, setHasStoredWebhook] = useState(false)
    const [storedWebhookDisplay, setStoredWebhookDisplay] = useState('')

    const [providerPreset, setProviderPreset] = useState('openai')
    const [customBaseUrl, setCustomBaseUrl] = useState('')
    const [deepThinkLlm, setDeepThinkLlm] = useState('')
    const [quickThinkLlm, setQuickThinkLlm] = useState('')
    const [maxDebateRounds, setMaxDebateRounds] = useState(1)
    const [maxRiskRounds, setMaxRiskRounds] = useState(1)
    const [emailReportEnabled, setEmailReportEnabled] = useState(true)
    const [wecomReportEnabled, setWecomReportEnabled] = useState(true)
    const [configLoading, setConfigLoading] = useState(false)
    const [saving, setSaving] = useState(false)
    const [saveAllSaving, setSaveAllSaving] = useState(false)
    const [saveAllSaved, setSaveAllSaved] = useState(false)
    const [warmingUp, setWarmingUp] = useState(false)
    const [saved, setSaved] = useState(false)
    const [saveMessage, setSaveMessage] = useState('设置已保存')
    const [configError, setConfigError] = useState<string | null>(null)
    const [warmupResults, setWarmupResults] = useState<RuntimeWarmupResult[]>([])
    const [warmupError, setWarmupError] = useState<string | null>(null)
    const [wecomWarmingUp, setWecomWarmingUp] = useState(false)
    const [wecomWarmupMessage, setWecomWarmupMessage] = useState<string | null>(null)
    const [wecomWarmupError, setWecomWarmupError] = useState<string | null>(null)

    // API Token states
    const [tokens, setTokens] = useState<UserToken[]>([])
    const [tokensLoading, setTokensLoading] = useState(false)
    const [newTokenName, setNewTokenName] = useState('')
    const [isCreatingToken, setIsCreatingToken] = useState(false)
    const [copiedTokenId, setCopiedTokenId] = useState<string | null>(null)
    const [newlyCreatedToken, setNewlyCreatedToken] = useState<string | null>(null)

    // Search config states
    const [searchProviders, setSearchProviders] = useState<SearchProvider[]>([])
    
    // Data source config states
    const [dataSources, setDataSources] = useState<DataSourceProvider[]>([])
    const [dataSourceSaving, setDataSourceSaving] = useState(false)
    const [dataSourceSaved, setDataSourceSaved] = useState(false)
    const [dataSourceLoading, setDataSourceLoading] = useState(false)
    
    // Futu OpenD state
    const [futuHost, setFutuHost] = useState('127.0.0.1')
    const [futuPort, setFutuPort] = useState(11111)
    const [futuSaving, setFutuSaving] = useState(false)
    const [futuSaved, setFutuSaved] = useState(false)
    const [futuStatus, setFutuStatus] = useState<{connected: boolean; host?: string; port?: number; server_ver?: string; error?: string; user?: any; accounts?: any[]}>({connected: false})
    const [futuTesting, setFutuTesting] = useState(false)
    const [futuEncrypt, setFutuEncrypt] = useState(false)
    const [modelPresetsLoaded, setModelPresetsLoaded] = useState(false)
    const [providerPresets, setProviderPresets] = useState<ProviderPreset[]>(DEFAULT_PROVIDER_PRESETS)
    const [modelPresets, setModelPresets] = useState<ModelPreset[]>(DEFAULT_MODEL_PRESETS)
    const [searchConfigLoading, setSearchConfigLoading] = useState(false)
    const [searchSaving, setSearchSaving] = useState(false)

    // Social sentiment config states
    const [socialApiKey, setSocialApiKey] = useState('')
    const [socialBaseUrl, setSocialBaseUrl] = useState('https://api.adanos.org')
    const [socialSaving, setSocialSaving] = useState(false)
    const [socialHasKey, setSocialHasKey] = useState(false)
    const [searchSaved, setSearchSaved] = useState(false)
    const [socialSaved, setSocialSaved] = useState(false)

    const selectedPreset = useMemo(
        () => providerPresets.find((item) => item.id === providerPreset) || providerPresets[0],
        [providerPreset, providerPresets],
    )

    const effectiveProvider = selectedPreset.provider
    const effectiveBaseUrl = selectedPreset.editableBaseUrl ? customBaseUrl.trim() : selectedPreset.baseUrl

    // Auto-load providers from /providers.json on mount
    useEffect(() => {
        fetch('/providers.json')
            .then(r => r.json())
            .then(data => { if (data.providers?.length) setProviderPresets(data.providers) })
            .catch(() => {})
    }, [])

    // Load model list via backend proxy
    const [modelListLoading, setModelListLoading] = useState(false)
    const loadModelList = async () => {
        const baseUrl = effectiveBaseUrl
        const key = llmApiKey.trim()
        if (!baseUrl || !key) { alert('请先填写 Base URL 和 API Key'); return }
        setModelListLoading(true)
        try {
            const data = await api.get(`/v1/models?base_url=${encodeURIComponent(baseUrl)}&api_key=${encodeURIComponent(key)}`)
            const models: string[] = data.models || []
            if (models.length) {
                const toPreset = (id: string, tier: 'quick' | 'deep') => ({ id, label: id, tier })
                const presets = [...models.map((id: string) => toPreset(id, 'quick')), ...models.map((id: string) => toPreset(id, 'deep'))]
                setModelPresets(presets)
                setModelPresetsLoaded(true)
            } else {
                alert('未获取到模型列表')
            }
        } catch (e: any) {
            alert('加载失败: ' + (e.message || '无法连接'))
        } finally {
            setModelListLoading(false)
        }
    }

    useEffect(() => {
        setWarmupResults([])
        setWarmupError(null)
    }, [providerPreset, customBaseUrl, deepThinkLlm, quickThinkLlm, llmApiKey])

    useEffect(() => {
        setWecomWarmupMessage(null)
        setWecomWarmupError(null)
    }, [wecomWebhook])

    useEffect(() => {
        try {
            const stored = localStorage.getItem('tradingagents-settings')
            if (stored) {
                const s = JSON.parse(stored) as Record<string, unknown> & {
                    defaultAnalysts?: string[]
                }
                if ('apiUrl' in s) {
                    delete s.apiUrl
                    localStorage.setItem('tradingagents-settings', JSON.stringify(s))
                }
                if (s.defaultAnalysts) setDefaultAnalysts(s.defaultAnalysts)
                if (typeof s.customPrompt === 'string') setCustomPrompt(s.customPrompt)
            }
        } catch {}
    }, [])

    useEffect(() => {
        setConfigLoading(true)
        setConfigError(null)
        api.getConfig()
            .then(cfg => {
                setProviderPreset(inferPreset(cfg.llm_provider, cfg.backend_url))
                setCustomBaseUrl(cfg.backend_url || '')
                setDeepThinkLlm(cfg.deep_think_llm)
                setQuickThinkLlm(cfg.quick_think_llm)
                setMaxDebateRounds(cfg.max_debate_rounds)
                setMaxRiskRounds(cfg.max_risk_discuss_rounds)
                setHasStoredApiKey(!!cfg.has_api_key)
                setHasStoredWebhook(!!cfg.has_wecom_webhook)
                setStoredWebhookDisplay(cfg.wecom_webhook_display || '')
                setEmailReportEnabled(cfg.email_report_enabled !== false)
                setWecomReportEnabled(cfg.wecom_report_enabled !== false)
                if (Array.isArray(cfg.default_analysts) && cfg.default_analysts.length > 0) {
                    setDefaultAnalysts(cfg.default_analysts)
                }
            })
            .catch(err => {
                setConfigError(err instanceof Error ? err.message : '无法连接到后端')
            })
            .finally(() => setConfigLoading(false))

        // Fetch tokens
        fetchTokens()
        loadSearchConfig()
        loadDataSourceConfig()
        loadSocialSentimentConfig()
        loadFutuConfig()
    }, [])

    const fetchTokens = async () => {
        setTokensLoading(true)
        try {
            const data = await api.getTokens()
            setTokens(data)
        } catch (err) {
            console.error('Failed to fetch tokens:', err)
        } finally {
            setTokensLoading(false)
        }
    }

    async function loadSearchConfig() {
        setSearchConfigLoading(true)
        try {
            const data = await api.get('/v1/config/search')
            setSearchProviders(data.providers || [])
        } catch (e) {
            console.error('Failed to load search config', e)
        } finally {
            setSearchConfigLoading(false)
        }
    }

    async function loadDataSourceConfig() {
        setDataSourceLoading(true)
        try {
            const data = await api.get('/v1/config/data-sources')
            setDataSources(data.providers || [])
        } catch (e) {
            console.error('Failed to load data source config', e)
        } finally {
            setDataSourceLoading(false)
        }
    }

    async function saveDataSourceConfig() {
        setDataSourceSaving(true)
        try {
            await api.put('/v1/config/data-sources', { providers: dataSources })
            setDataSourceSaved(true)
            setTimeout(() => setDataSourceSaved(false), 3000)
        } catch (e: any) {
            setConfigError(e?.message || '保存失败')
        } finally {
            setDataSourceSaving(false)
        }
    }

    async function loadSocialSentimentConfig() {
        try {
            const data = await api.get('/v1/config/social-sentiment')
            setSocialApiKey(data.api_key || '')
            setSocialBaseUrl(data.base_url || 'https://api.adanos.org')
            setSocialHasKey(data.has_key || false)
        } catch (e) {
            console.error('Failed to load social sentiment config', e)
        }
    }

    async function saveSearchConfig() {
        setSearchSaving(true)
        try {
            await api.put('/v1/config/search', { providers: searchProviders })
            setSearchSaved(true)
            setTimeout(() => setSearchSaved(false), 3000)
        } catch (e: any) {
            setConfigError(e?.message || '保存失败')
        } finally {
            setSearchSaving(false)
        }
    }

    async function saveSocialSentimentConfig() {
        setSocialSaving(true)
        try {
            await api.put('/v1/config/social-sentiment', {
                api_key: socialApiKey,
                base_url: socialBaseUrl,
            })
            setSocialSaved(true)
            setSocialHasKey(!!socialApiKey)
            setTimeout(() => setSocialSaved(false), 3000)
        } catch (e: any) {
            setConfigError(e?.message || '保存失败')
        } finally {
            setSocialSaving(false)
        }
    }

    async function loadFutuConfig() {
        try {
            const data = await api.get('/v1/config/futu-opend')
            setFutuHost(data.host || '127.0.0.1')
            setFutuPort(data.port || 11111)
            setFutuEncrypt(data.host !== '127.0.0.1' && data.host !== 'localhost')
        } catch {}
    }

    async function saveFutuConfig() {
        setFutuSaving(true)
        try {
            await api.put('/v1/config/futu-opend', { host: futuHost, port: futuPort })
            setFutuSaved(true)
            setTimeout(() => setFutuSaved(false), 3000)
        } catch (e: any) {
            setConfigError(e?.message || '保存失败')
        } finally {
            setFutuSaving(false)
        }
    }

    async function testFutuConnection() {
        setFutuTesting(true)
        try {
            const data = await api.get('/v1/futu/status')
            setFutuStatus(data)
        } catch (e: any) {
            setFutuStatus({ connected: false, error: e?.message || '连接失败' })
        } finally {
            setFutuTesting(false)
        }
    }

    const handleCreateToken = async (e: React.FormEvent) => {
        e.preventDefault()
        if (!newTokenName.trim()) return
        setIsCreatingToken(true)
        try {
            const created = await api.createToken({ name: newTokenName.trim() })
            setNewTokenName('')
            setNewlyCreatedToken(created.token || null)
            await fetchTokens()
        } catch (err) {
            alert(err instanceof Error ? err.message : '创建 Token 失败')
        } finally {
            setIsCreatingToken(false)
        }
    }

    const handleDeleteToken = async (tokenId: string) => {
        if (!confirm('确定要吊销此 Token 吗？吊销后使用该 Token 的 API 请求将立即失效。')) return
        try {
            await api.deleteToken(tokenId)
            await fetchTokens()
        } catch (err) {
            alert(err instanceof Error ? err.message : '吊销 Token 失败')
        }
    }

    const copyToClipboard = (text: string, id: string) => {
        navigator.clipboard.writeText(text)
        setCopiedTokenId(id)
        setTimeout(() => setCopiedTokenId(null), 2000)
    }

    const persistLocalSettings = () => {
        localStorage.setItem('tradingagents-settings', JSON.stringify({
            defaultAnalysts,
            customPrompt,
        }))
        localStorage.setItem('ta-custom-prompt', customPrompt)
    }

    const buildRuntimeConfigPayload = (options?: { includeEmail?: boolean; includeWecom?: boolean }) => ({
        llm_provider: effectiveProvider,
        backend_url: effectiveBaseUrl || undefined,
        deep_think_llm: deepThinkLlm,
        quick_think_llm: quickThinkLlm,
        max_debate_rounds: maxDebateRounds,
        max_risk_discuss_rounds: maxRiskRounds,
        api_key: llmApiKey || undefined,
        ...(options?.includeWecom ? {
            wecom_webhook_url: wecomWebhook.trim() || undefined,
            wecom_report_enabled: wecomReportEnabled,
        } : {}),
        ...(options?.includeEmail ? { email_report_enabled: emailReportEnabled } : {}),
        default_analysts: defaultAnalysts,
    })

    const showSavedMessage = (message: string) => {
        setSaveMessage(message)
        setSaved(true)
        setTimeout(() => setSaved(false), 2000)
    }

    const submitConfig = async (options?: { forceWarmup?: boolean; successMessage?: string; includeEmail?: boolean; includeWecom?: boolean }) => {
        persistLocalSettings()
        const { forceWarmup = false, successMessage = '设置已保存', includeEmail = true, includeWecom = false } = options || {}
        const response = await api.updateConfig({
            ...buildRuntimeConfigPayload({ includeEmail, includeWecom }),
            warmup: true,
            force_warmup: forceWarmup,
        })
        setHasStoredApiKey(!!response.has_api_key)
        setHasStoredWebhook(!!response.current.has_wecom_webhook)
        setStoredWebhookDisplay(response.current.wecom_webhook_display || '')
        setWecomReportEnabled(response.current.wecom_report_enabled !== false)
        setLlmApiKey('')
        setWecomWebhook('')
        showSavedMessage(response.warmup?.message || successMessage)
        return response
    }

    // Per-provider config persistence
    const saveProviderConfig = () => {
        try {
            const key = `ta-provider-${providerPreset}`
            localStorage.setItem(key, JSON.stringify({
                baseUrl: customBaseUrl,
                quickThinkLlm,
                deepThinkLlm,
            }))
        } catch {}
    }

    const loadProviderConfig = (presetId: string) => {
        try {
            const stored = localStorage.getItem(`ta-provider-${presetId}`)
            if (stored) {
                const cfg = JSON.parse(stored)
                if (cfg.baseUrl) setCustomBaseUrl(cfg.baseUrl)
                if (cfg.quickThinkLlm) setQuickThinkLlm(cfg.quickThinkLlm)
                if (cfg.deepThinkLlm) setDeepThinkLlm(cfg.deepThinkLlm)
                return true
            }
        } catch {}
        return false
    }

    const handleSaveAll = async () => {
        setSaveAllSaving(true)
        try {
            saveProviderConfig()
            await submitConfig({ includeEmail: true, includeWecom: true, successMessage: '全部设置已保存' })
            setSaveAllSaved(true)
            setTimeout(() => setSaveAllSaved(false), 3000)
        } catch (err) {
            alert(err instanceof Error ? err.message : '保存全部设置失败')
        } finally {
            setSaveAllSaving(false)
        }
    }

    const handleWarmup = async () => {
        setWarmingUp(true)
        setWarmupError(null)
        setWarmupResults([])
        try {
            const response = await api.warmupConfig({
                ...buildRuntimeConfigPayload(),
                prompt: '你好',
            })
            setWarmupResults(response.results || [])
        } catch (err) {
            setWarmupError(err instanceof Error ? err.message : 'Warmup 触发失败')
        } finally {
            setWarmingUp(false)
        }
    }
    const handleClearApiKey = async () => {
        if (!hasStoredApiKey) return
        setSaving(true)
        try {
            const response = await api.updateConfig({ clear_api_key: true })
            setHasStoredApiKey(!!response.has_api_key)
            setLlmApiKey('')
            setSaved(true)
            setTimeout(() => setSaved(false), 2000)
        } catch (err) {
            alert(err instanceof Error ? err.message : '清除密钥失败')
        } finally {
            setSaving(false)
        }
    }

    const handleClearWebhook = async () => {
        if (!hasStoredWebhook) return
        setSaving(true)
        try {
            const response = await api.updateConfig({ clear_wecom_webhook: true })
            setHasStoredWebhook(!!response.current.has_wecom_webhook)
            setStoredWebhookDisplay(response.current.wecom_webhook_display || '')
            setWecomWebhook('')
            setWecomWarmupMessage(null)
            setWecomWarmupError(null)
            showSavedMessage('企业微信机器人已清除')
        } catch (err) {
            alert(err instanceof Error ? err.message : '清除企业微信机器人失败')
        } finally {
            setSaving(false)
        }
    }

    const handleWecomWarmup = async () => {
        setWecomWarmingUp(true)
        setWecomWarmupMessage(null)
        setWecomWarmupError(null)
        try {
            const response = await api.warmupWecom({
                wecom_webhook_url: wecomWebhook.trim() || undefined,
            })
            setWecomWarmupMessage(
                response.webhook_display
                    ? `${response.message}，目标：${response.webhook_display}`
                    : response.message
            )
        } catch (err) {
            setWecomWarmupError(err instanceof Error ? err.message : 'Webhook 测试发送失败')
        } finally {
            setWecomWarmingUp(false)
        }
    }

    const toggleAnalyst = (analyst: string) => {
        setDefaultAnalysts(prev =>
            prev.includes(analyst) ? prev.filter(a => a !== analyst) : [...prev, analyst]
        )
    }

    return (
        <div className="space-y-6">
            <div>
                <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">系统设置</h1>
                <p className="text-slate-500 dark:text-slate-400 mt-1">配置当前账户的分析参数与模型</p>
            </div>

            <div className="card space-y-4">
                <div className="flex items-center gap-2">
                    <Database className="w-5 h-5 text-purple-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">模型接入</h2>
                    {configLoading && <Loader2 className="ml-auto w-4 h-4 animate-spin text-slate-400" />}
                </div>

                {configError && (
                    <p className="text-sm text-amber-500">⚠ {configError}（显示本地默认值）</p>
                )}

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                            API Providers
                        </label>
                        <select
                            value={providerPreset}
                            onChange={e => {
                                const newPreset = e.target.value
                                setProviderPreset(newPreset)
                                // Try loading saved config for this provider
                                if (!loadProviderConfig(newPreset)) {
                                    // Use default baseUrl from preset
                                    const preset = providerPresets.find(p => p.id === newPreset)
                                    if (preset) setCustomBaseUrl(preset.baseUrl || '')
                                }
                            }}
                            className="input w-full"
                            disabled={configLoading}
                        >
                            {providerPresets.map((preset) => (
                                <option key={preset.id} value={preset.id}>{preset.label}</option>
                            ))}
                        </select>
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                            API Protocols
                        </label>
                        <div className="input w-full flex items-center gap-2 bg-slate-50 dark:bg-slate-900/70 text-slate-600 dark:text-slate-300">
                            <Link2 className="w-4 h-4 text-slate-400" />
                            <span>{selectedPreset.protocol}</span>
                        </div>
                    </div>

                    {/* Base URL + API Key */}
                    {(selectedPreset.baseUrl || selectedPreset.editableBaseUrl) && (
                        <>
                            <div>
                                <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                                    Base URL
                                </label>
                                <input
                                    type="text"
                                    value={selectedPreset.editableBaseUrl ? customBaseUrl : selectedPreset.baseUrl}
                                    onChange={e => setCustomBaseUrl(e.target.value)}
                                    className="input w-full"
                                    disabled={configLoading || !selectedPreset.editableBaseUrl}
                                    placeholder="https://your-openai-compatible-endpoint/v1"
                                />
                            </div>
                            <div>
                                <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                                    API Key
                                </label>
                                <div className="relative">
                                    <Key className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                                    <input
                                        type="password"
                                        value={llmApiKey}
                                        onChange={e => setLlmApiKey(e.target.value)}
                                        className="input w-full pl-10"
                                        placeholder={hasStoredApiKey ? '已保存，留空则保持不变' : '输入你的 API Key'}
                                        disabled={configLoading}
                                    />
                                </div>
                            </div>
                        </>
                    )}

                    {/* 加载模型清单 */}
                    <div className="md:col-span-2 flex items-center gap-3">
                        <button
                            type="button"
                            onClick={loadModelList}
                            disabled={!llmApiKey.trim() || modelListLoading}
                            title={!llmApiKey.trim() ? '请先填写 API Key' : ''}
                            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                                !llmApiKey.trim()
                                    ? 'bg-slate-100 dark:bg-slate-800 text-slate-400 dark:text-slate-600 cursor-not-allowed'
                                    : 'bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-700'
                            }`}
                        >
                            {modelListLoading ? '加载中...' : modelPresetsLoaded ? '✓ 已加载' : '加载模型清单'}
                        </button>
                        {!llmApiKey.trim() && (
                            <span className="text-xs text-amber-500">← 请先填写 API Key</span>
                        )}
                        {hasStoredApiKey && (
                            <button
                                type="button"
                                onClick={handleClearApiKey}
                                disabled={saving || saveAllSaving}
                                className="inline-flex items-center gap-1 text-xs text-rose-500 hover:text-rose-600 disabled:opacity-50 ml-auto"
                            >
                                <Trash2 className="w-3.5 h-3.5" />
                                清除密钥
                            </button>
                        )}
                    </div>

                    {/* 常规模型 */}
                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                            常规模型
                            <span className="ml-1 text-xs text-slate-400 font-normal">用于意图识别、JSON 提取等轻量任务</span>
                        </label>
                        <input
                            type="text"
                            list="quick-models"
                            value={quickThinkLlm}
                            onChange={e => setQuickThinkLlm(e.target.value)}
                            className="input w-full"
                            placeholder="输入或从列表选择"
                            disabled={configLoading}
                        />
                        <datalist id="quick-models">
                            {modelPresets.filter(m => m.tier === 'quick').map(m => (
                                <option key={m.id} value={m.id}>{m.label}</option>
                            ))}
                        </datalist>
                    </div>

                    {/* 推理模型 */}
                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                            推理模型
                            <span className="ml-1 text-xs text-slate-400 font-normal">用于深度分析、辩论等复杂任务</span>
                        </label>
                        <input
                            type="text"
                            list="deep-models"
                            value={deepThinkLlm}
                            onChange={e => setDeepThinkLlm(e.target.value)}
                            className="input w-full"
                            placeholder="输入或从列表选择"
                            disabled={configLoading}
                        />
                        <datalist id="deep-models">
                            {modelPresets.filter(m => m.tier === 'deep').map(m => (
                                <option key={m.id} value={m.id}>{m.label}</option>
                            ))}
                        </datalist>
                    </div>



                    <div className="md:col-span-2 rounded-2xl border border-slate-200/80 dark:border-slate-700/80 bg-slate-50/80 dark:bg-slate-900/40 p-4 space-y-3">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                            <div>
                                <div className="text-sm font-medium text-slate-900 dark:text-slate-100">连通性测试</div>
                                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                                    使用当前表单配置向模型发送“你好”，不会自动保存设置。
                                </p>
                            </div>
                            <div className="flex items-center gap-2">
                                <button onClick={handleWarmup} disabled={saving || saveAllSaving || warmingUp || configLoading} className="btn-secondary inline-flex items-center gap-2">
                                    {warmingUp ? <Loader2 className="w-4 h-4 animate-spin" /> : <Flame className="w-4 h-4" />}
                                    {warmingUp ? '测试中...' : '测试连接'}
                                </button>
                                <button
                                    onClick={handleSaveAll}
                                    disabled={saving || saveAllSaving || configLoading}
                                    style={saveAllSaved ? { backgroundColor: '#22c55e', backgroundImage: 'none', borderColor: '#22c55e', color: '#fff', cursor: 'default' } : undefined}
                                    className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                                        saveAllSaved ? '' : 'btn-primary'
                                    }`}
                                >
                                    {saveAllSaved ? '✓ 保存成功' : saveAllSaving ? '保存中...' : '保存配置'}
                                </button>
                            </div>
                        </div>

                        {warmupError && (
                            <div className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-600 dark:border-rose-900/60 dark:bg-rose-950/30 dark:text-rose-300">
                                {warmupError}
                            </div>
                        )}

                        {warmupResults.length > 0 && (
                            <div className="space-y-3">
                                {warmupResults.map((item, index) => (
                                    <div
                                        key={`${item.model}-${index}`}
                                        className="rounded-xl border border-slate-200/80 dark:border-slate-700/80 bg-white dark:bg-slate-950/40 px-4 py-3"
                                    >
                                        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
                                            <span className="font-medium text-slate-700 dark:text-slate-200">{item.targets.join(' / ')}</span>
                                            <span>{item.model}</span>
                                        </div>
                                        {item.content && (
                                            <pre className="mt-2 whitespace-pre-wrap break-words font-sans text-sm text-slate-700 dark:text-slate-200">
                                                {item.content}
                                            </pre>
                                        )}
                                        {item.error && (
                                            <p className="mt-2 text-sm text-rose-500 dark:text-rose-300">{item.error}</p>
                                        )}
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>

                </div>
            </div>

            {/* Futu OpenD 接入 */}
            <div className="card space-y-4">
                <div className="flex items-center gap-2">
                    <Link2 className="w-5 h-5 text-orange-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">Futu OpenD 接入</h2>
                </div>
                <p className="text-sm text-slate-500 dark:text-slate-400">配置 Futu OpenD 网关连接，用于港股/美股实时行情与模拟交易。</p>
                
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Host</label>
                        <input
                            type="text"
                            value={futuHost}
                            onChange={e => setFutuHost(e.target.value)}
                            placeholder="127.0.0.1"
                            className="input w-full"
                        />
                    </div>
                    <div>
                        <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">Port</label>
                        <input
                            type="number"
                            value={futuPort}
                            onChange={e => setFutuPort(parseInt(e.target.value) || 11111)}
                            placeholder="11111"
                            className="input w-full"
                        />
                    </div>
                </div>

                {/* 跨网提示 */}
                {futuHost !== '127.0.0.1' && futuHost !== 'localhost' && (
                    <div className="flex items-start gap-2 p-3 rounded-lg bg-amber-50 dark:bg-amber-500/10 border border-amber-200 dark:border-amber-500/30 text-sm text-amber-700 dark:text-amber-400">
                        <span>⚠️</span>
                        <span>跨网通信，交易连接需要加密。如果无需跨网通信，可以将配置文件中的监听地址修改为 <code className="font-mono bg-amber-100 dark:bg-amber-500/20 px-1 rounded">127.0.0.1</code></span>
                    </div>
                )}

                {/* 加密开关 */}
                <div className="rounded-xl border border-slate-200/80 bg-slate-50/80 px-4 py-3 dark:border-slate-700/80 dark:bg-slate-900/40">
                    <div className="flex items-center justify-between">
                        <div>
                            <div className="text-sm font-medium text-slate-700 dark:text-slate-200">启用连接加密</div>
                            <div className="text-xs text-slate-400 dark:text-slate-500 mt-0.5">跨网连接 OpenD 时需要 RSA 加密（localhost 不需要）</div>
                        </div>
                        <button
                            type="button"
                            onClick={() => setFutuEncrypt(!futuEncrypt)}
                            className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                                futuEncrypt ? 'bg-blue-500' : 'bg-slate-300 dark:bg-slate-600'
                            }`}
                        >
                            <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${futuEncrypt ? 'translate-x-6' : 'translate-x-1'}`} />
                        </button>
                    </div>
                    {futuEncrypt && (
                        <div className="mt-3 pt-3 border-t border-slate-200 dark:border-slate-700">
                            <label className="block text-xs font-medium text-slate-600 dark:text-slate-400 mb-1.5">RSA 私钥文件 (config/rsa_key.txt)</label>
                            <input
                                type="file"
                                accept=".txt,.pem,.key"
                                onChange={async (e) => {
                                    const file = e.target.files?.[0]
                                    if (!file) return
                                    const text = await file.text()
                                    try {
                                        await api.put('/v1/config/futu-opend', { host: futuHost, port: futuPort, rsa_key: text })
                                        alert('RSA 密钥已上传')
                                    } catch { alert('上传失败') }
                                }}
                                className="block w-full text-sm text-slate-600 dark:text-slate-400 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:text-sm file:font-medium file:bg-blue-50 file:text-blue-700 dark:file:bg-blue-500/10 dark:file:text-blue-400 hover:file:bg-blue-100 dark:hover:file:bg-blue-500/20"
                            />
                        </div>
                    )}
                </div>
                
                <div className="flex justify-end gap-2 pt-2">
                    <button
                        onClick={testFutuConnection}
                        disabled={futuTesting}
                        className="px-4 py-2 rounded-lg text-sm font-medium bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors"
                    >
                        {futuTesting ? (
                            <><Loader2 className="w-4 h-4 animate-spin inline mr-1" /> 测试中...</>
                        ) : '测试连接'}
                    </button>
                    <button
                        onClick={saveFutuConfig}
                        disabled={futuSaving}
                        style={futuSaved ? { backgroundColor: '#22c55e', backgroundImage: 'none', borderColor: '#22c55e', color: '#fff', cursor: 'default' } : undefined}
                        className="flex items-center gap-2 btn-primary"
                    >
                        <Save className="w-4 h-4" />
                        {futuSaved ? '保存成功' : '保存配置'}
                    </button>
                </div>

                {/* Connection Status */}
                {futuStatus.connected !== undefined && (
                    <div className={`p-4 rounded-lg border ${
                        futuStatus.connected
                            ? 'bg-green-50 dark:bg-green-500/10 border-green-200 dark:border-green-500/30'
                            : 'bg-red-50 dark:bg-red-500/10 border-red-200 dark:border-red-500/30'
                    }`}>
                        {futuStatus.connected ? (
                            <div className="space-y-4">
                                {/* 连接状态 */}
                                <div className="flex items-center gap-3">
                                    <CheckCircle2 className="w-5 h-5 text-green-500" />
                                    <span className="text-base font-semibold text-green-700 dark:text-green-400">OpenD 已连接</span>
                                    <span className="text-sm text-slate-500">{futuStatus.host}:{futuStatus.port}</span>
                                    <span className="text-xs px-2 py-0.5 rounded-full bg-green-100 dark:bg-green-500/10 text-green-600 dark:text-green-400">v{futuStatus.server_ver}</span>
                                </div>
                                
                                {/* 用户信息 + 额度 */}
                                {futuStatus.user && (
                                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                                        {/* 牛牛号 */}
                                        <div className="p-4 rounded-lg bg-green-50 dark:bg-green-500/5 border border-green-200 dark:border-green-500/20">
                                            <div className="flex items-center gap-3">
                                                {futuStatus.user.avatar_url && (
                                                    <img src={futuStatus.user.avatar_url} className="w-10 h-10 rounded-full ring-2 ring-green-200 dark:ring-green-500/30" alt="" />
                                                )}
                                                <div>
                                                    <div className="text-sm font-semibold text-slate-800 dark:text-slate-200">{futuStatus.user.nick_name || '未知'}</div>
                                                    <div className="text-xs text-slate-500">牛牛号 {futuStatus.user.user_id}</div>
                                                </div>
                                            </div>
                                        </div>
                                        {/* 股票/期货额度 */}
                                        <div className="p-4 rounded-lg bg-green-50 dark:bg-green-500/5 border border-green-200 dark:border-green-500/20">
                                            <div className="text-sm font-semibold text-slate-700 dark:text-slate-300 mb-2">📊 股票 / 期货</div>
                                            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
                                                <span className="text-slate-500">订阅</span>
                                                <span className="font-mono font-semibold text-slate-800 dark:text-slate-200">{futuStatus.user.sub_quota}</span>
                                                <span className="text-slate-500">历史K线</span>
                                                <span className="font-mono font-semibold text-slate-800 dark:text-slate-200">{futuStatus.user.history_kl_quota}</span>
                                            </div>
                                        </div>
                                        {/* 期权额度 */}
                                        <div className="p-4 rounded-lg bg-green-50 dark:bg-green-500/5 border border-green-200 dark:border-green-500/20">
                                            <div className="text-sm font-semibold text-slate-700 dark:text-slate-300 mb-2">📈 期权</div>
                                            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
                                                <span className="text-slate-500">订阅</span>
                                                <span className="font-mono font-semibold text-slate-800 dark:text-slate-200">—</span>
                                                <span className="text-slate-500">历史K线</span>
                                                <span className="font-mono font-semibold text-slate-800 dark:text-slate-200">—</span>
                                            </div>
                                        </div>
                                    </div>
                                )}
                                
                                {/* 行情权限 */}
                                {futuStatus.user && (
                                    <div className="p-4 rounded-lg bg-green-50 dark:bg-green-500/5 border border-green-200 dark:border-green-500/20">
                                        <div className="text-sm font-semibold text-slate-700 dark:text-slate-300 mb-3">行情权限</div>
                                        <div className="grid grid-cols-2 gap-4">
                                            <div>
                                                <div className="text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">🇭🇰 香港市场</div>
                                                <div className="space-y-1 text-sm">
                                                    <div className="flex justify-between"><span className="text-slate-500">股票</span><span className="font-mono font-semibold text-green-600 dark:text-green-400">{futuStatus.user.hk_qot_right}</span></div>
                                                    <div className="flex justify-between"><span className="text-slate-500">期权</span><span className="font-mono font-semibold text-green-600 dark:text-green-400">{futuStatus.user.hk_option_qot_right}</span></div>
                                                    <div className="flex justify-between"><span className="text-slate-500">期货</span><span className="font-mono font-semibold text-green-600 dark:text-green-400">{futuStatus.user.hk_future_qot_right}</span></div>
                                                </div>
                                            </div>
                                            <div>
                                                <div className="text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">🇺🇸 美国市场</div>
                                                <div className="space-y-1 text-sm">
                                                    <div className="flex justify-between"><span className="text-slate-500">股票</span><span className="font-mono font-semibold text-green-600 dark:text-green-400">{futuStatus.user.us_qot_right}</span></div>
                                                    <div className="flex justify-between"><span className="text-slate-500">期权</span><span className="font-mono font-semibold text-green-600 dark:text-green-400">{futuStatus.user.us_option_qot_right}</span></div>
                                                    <div className="flex justify-between"><span className="text-slate-500">期货</span><span className="font-mono text-slate-400">{futuStatus.user.us_future_qot_right || 'N/A'}</span></div>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                )}
                                
                                {/* 交易账户 */}
                                {futuStatus.accounts && futuStatus.accounts.length > 0 && (
                                    <div className="p-4 rounded-lg bg-green-50 dark:bg-green-500/5 border border-green-200 dark:border-green-500/20">
                                        <div className="text-sm font-semibold text-slate-700 dark:text-slate-300 mb-3">交易账户</div>
                                        <div className="space-y-2">
                                            {futuStatus.accounts.filter((a: any) => a.acc_status === 'ACTIVE').map((acc: any, i: number) => (
                                                <div key={i} className="flex items-center gap-3 text-sm">
                                                    <span className="text-base">{(acc.markets || []).includes('HK') ? '🇭🇰' : '🇺🇸'}</span>
                                                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                                                        acc.trd_env === 'SIMULATE' 
                                                            ? 'bg-amber-100 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400'
                                                            : 'bg-blue-100 dark:bg-blue-500/10 text-blue-700 dark:text-blue-400'
                                                    }`}>{acc.trd_env === 'SIMULATE' ? '模拟' : '实盘'}</span>
                                                    <span className="font-mono font-medium text-slate-800 dark:text-slate-200">{acc.acc_id}</span>
                                                    <span className="text-slate-600 dark:text-slate-400">{acc.acc_type}</span>
                                                    {acc.sim_acc_type && acc.sim_acc_type !== 'N/A' && (
                                                        <span className="text-xs text-slate-500">({acc.sim_acc_type})</span>
                                                    )}
                                                    <span className="text-xs text-slate-400 ml-auto">{(acc.markets || []).join(' + ')}</span>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )}
                            </div>
                        ) : (
                            <div className="flex items-center gap-2">
                                <span className="text-red-500">✗</span>
                                <span className="text-red-700 dark:text-red-400">连接失败：{futuStatus.error || '无法连接到 OpenD'}</span>
                            </div>
                        )}
                    </div>
                )}
            </div>

            {/* 数据源接入 */}
            <div className="card space-y-4">
                <div className="flex items-center gap-2">
                    <BarChart3 className="w-5 h-5 text-green-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">数据源接入</h2>
                    {dataSourceLoading && <Loader2 className="ml-auto w-4 h-4 animate-spin text-slate-400" />}
                </div>
                <p className="text-sm text-slate-500 dark:text-slate-400">配置行情数据源 API Key，用于增强实时行情和历史数据覆盖。Futu 和 YFinance 为内置免费源，无需配置。</p>
                
                <div className="space-y-3">
                    {dataSources.map((provider, idx) => (
                        <div key={provider.name} className="grid grid-cols-1 md:grid-cols-12 gap-3 items-center">
                            <div className="md:col-span-3">
                                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300">
                                    {provider.label}
                                    <span className="ml-1 text-xs text-slate-400">({provider.market})</span>
                                </label>
                            </div>
                            <div className="md:col-span-7">
                                <input
                                    type="password"
                                    placeholder={`输入 ${provider.label} API Key`}
                                    value={provider.api_key || ''}
                                    onChange={e => {
                                        const updated = [...dataSources]
                                        updated[idx] = { ...updated[idx], api_key: e.target.value }
                                        setDataSources(updated)
                                    }}
                                    className="input w-full"
                                    disabled={dataSourceLoading}
                                />
                            </div>
                            <div className="md:col-span-2 flex justify-end">
                                <button
                                    type="button"
                                    onClick={() => {
                                        const updated = [...dataSources]
                                        updated[idx] = { ...updated[idx], enabled: !updated[idx].enabled }
                                        setDataSources(updated)
                                    }}
                                    className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                                        provider.enabled ? 'bg-green-500' : 'bg-slate-300 dark:bg-slate-600'
                                    }`}
                                >
                                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${provider.enabled ? 'translate-x-6' : 'translate-x-1'}`} />
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
                
                <div className="flex justify-end pt-2">
                    <button
                        onClick={saveDataSourceConfig}
                        disabled={dataSourceSaving}
                        style={dataSourceSaved ? { backgroundColor: '#22c55e', backgroundImage: 'none', borderColor: '#22c55e', color: '#fff', cursor: 'default' } : undefined}
                        className="flex items-center gap-2 btn-primary"
                    >
                        <Save className="w-4 h-4" />
                        {dataSourceSaved ? '保存成功' : '保存配置'}
                    </button>
                </div>
            </div>

            {/* 搜索服务接入 */}
            <div className="card space-y-4">
                <div className="flex items-center gap-2">
                    <Search className="w-5 h-5 text-cyan-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">搜索服务接入</h2>
                    {searchConfigLoading && <Loader2 className="ml-auto w-4 h-4 animate-spin text-slate-400" />}
                </div>
                <p className="text-sm text-slate-500 dark:text-slate-400">配置搜索引擎 API Key，用于增强新闻数据源。未配置的引擎将自动跳过。</p>
                
                <div className="space-y-3">
                    {searchProviders.map((provider, idx) => (
                        <div key={provider.name} className="grid grid-cols-1 md:grid-cols-12 gap-3 items-center">
                            <div className="md:col-span-3">
                                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300">
                                    {provider.label}
                                    <span className="ml-1 text-xs text-slate-400">({provider.env_key})</span>
                                </label>
                            </div>
                            <div className="md:col-span-7">
                                <input
                                    type="password"
                                    placeholder={provider.name === 'searxng' ? 'https://your-instance.com' : '输入 API Key'}
                                    value={provider.api_key || ''}
                                    onChange={e => {
                                        const updated = [...searchProviders]
                                        updated[idx] = { ...updated[idx], api_key: e.target.value }
                                        setSearchProviders(updated)
                                    }}
                                    className="input w-full"
                                    disabled={searchConfigLoading}
                                />
                            </div>
                            <div className="md:col-span-2 flex justify-end">
                                <button
                                    type="button"
                                    onClick={() => {
                                        const updated = [...searchProviders]
                                        updated[idx] = { ...updated[idx], enabled: !updated[idx].enabled }
                                        setSearchProviders(updated)
                                    }}
                                    className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                                        provider.enabled ? 'bg-blue-500' : 'bg-slate-300 dark:bg-slate-600'
                                    }`}
                                >
                                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${provider.enabled ? 'translate-x-6' : 'translate-x-1'}`} />
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
                
                <div className="flex justify-end pt-2">
                    <button
                        onClick={saveSearchConfig}
                        disabled={searchSaving}
                        style={searchSaved ? { backgroundColor: '#22c55e', backgroundImage: 'none', borderColor: '#22c55e', color: '#fff', cursor: 'default' } : undefined}
                        className="flex items-center gap-2 btn-primary"
                    >
                        <Save className="w-4 h-4" />
                        {searchSaved ? '保存成功' : '保存配置'}
                    </button>
                </div>
            </div>

            {/* 社交舆情接入 */}
            <div className="card space-y-4">
                <div className="flex items-center gap-2">
                    <MessageCircle className="w-5 h-5 text-orange-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">社交舆情接入</h2>
                </div>
                <p className="text-sm text-slate-500 dark:text-slate-400">配置社交舆情数据源（Reddit / X / Polymarket），用于增强海外社交情绪分析。</p>

                <div className="space-y-3">
                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">
                            API Key
                            <span className="ml-1 text-xs text-slate-400">(SOCIAL_SENTIMENT_API_KEY)</span>
                        </label>
                        <input
                            type="password"
                            placeholder={socialHasKey ? '已保存（留空不更新）' : '输入 API Key'}
                            value={socialApiKey}
                            onChange={e => setSocialApiKey(e.target.value)}
                            className="input w-full"
                        />
                    </div>
                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">
                            API Base URL
                            <span className="ml-1 text-xs text-slate-400">(SOCIAL_SENTIMENT_BASE_URL)</span>
                        </label>
                        <input
                            type="text"
                            placeholder="https://api.adanos.org"
                            value={socialBaseUrl}
                            onChange={e => setSocialBaseUrl(e.target.value)}
                            className="input w-full"
                        />
                    </div>
                </div>

                <div className="flex justify-end pt-2">
                    <button
                        onClick={saveSocialSentimentConfig}
                        disabled={socialSaving}
                        style={socialSaved ? { backgroundColor: '#22c55e', backgroundImage: 'none', borderColor: '#22c55e', color: '#fff', cursor: 'default' } : undefined}
                        className="flex items-center gap-2 btn-primary"
                    >
                        <Save className="w-4 h-4" />
                        {socialSaved ? '保存成功' : '保存配置'}
                    </button>
                </div>
            </div>

            <div className="card space-y-4">
                <div className="flex items-center gap-2">
                    <Database className="w-5 h-5 text-green-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">默认分析配置</h2>
                </div>

                <div>
                    <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                        默认启用分析师
                    </label>
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                        {[
                            { key: 'market', label: '市场分析' },
                            { key: 'social', label: '舆情分析' },
                            { key: 'news', label: '新闻分析' },
                            { key: 'fundamentals', label: '基本面' },
                            { key: 'macro', label: '宏观板块' },
                            { key: 'smart_money', label: '主力资金' },
                            { key: 'volume_price', label: '量价分析' },
                        ].map((analyst) => {
                            const active = defaultAnalysts.includes(analyst.key)
                            return (
                                <button
                                    key={analyst.key}
                                    type="button"
                                    onClick={() => toggleAnalyst(analyst.key)}
                                    className={`rounded-xl border px-3 py-3 text-sm transition-colors ${
                                        active
                                            ? 'bg-blue-50 dark:bg-blue-500/10 border-blue-500 text-blue-600 dark:text-blue-400'
                                            : 'bg-slate-100 dark:bg-slate-800 border-slate-200 dark:border-slate-600 text-slate-600 dark:text-slate-400'
                                    }`}
                                >
                                    {analyst.label}
                                </button>
                            )
                        })}
                    </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                            辩论轮数上限
                        </label>
                        <input
                            type="number"
                            min={1}
                            max={5}
                            value={maxDebateRounds}
                            onChange={e => setMaxDebateRounds(Number(e.target.value))}
                            className="input w-full"
                            disabled={configLoading}
                        />
                    </div>
                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                            风险讨论轮数上限
                        </label>
                        <input
                            type="number"
                            min={1}
                            max={5}
                            value={maxRiskRounds}
                            onChange={e => setMaxRiskRounds(Number(e.target.value))}
                            className="input w-full"
                            disabled={configLoading}
                        />
                    </div>
                </div>

                <div>
                    <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                        自定义分析提示
                    </label>
                    <textarea
                        value={customPrompt}
                        onChange={e => setCustomPrompt(e.target.value)}
                        className="input w-full min-h-[80px] resize-y"
                        placeholder="例如：更关注估值安全边际、政策催化与机构资金行为。"
                    />
                </div>
            </div>

            <div className="card space-y-4">
                <div className="flex items-center gap-2">
                    <Key className="w-5 h-5 text-amber-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">API 访问令牌</h2>
                    {tokensLoading && <Loader2 className="w-4 h-4 animate-spin text-slate-400 ml-auto" />}
                </div>

                <div className="text-sm text-slate-500 dark:text-slate-400 mb-4">
                    使用 API Token 在三方应用（如 Open Claw）中调用投研分析接口。请妥善保管您的 Token。
                </div>

                {/* Newly created token — show once */}
                {newlyCreatedToken && (
                    <div className="p-3 rounded-2xl bg-emerald-50 dark:bg-emerald-900/20 border border-emerald-200 dark:border-emerald-800">
                        <div className="text-sm font-medium text-emerald-800 dark:text-emerald-200 mb-1">Token 创建成功 — 请立即复制，关闭后无法再次查看</div>
                        <div className="flex items-center gap-2">
                            <code className="text-xs text-emerald-700 dark:text-emerald-300 bg-white dark:bg-slate-950 px-1.5 py-0.5 rounded border font-mono tracking-tight break-all">
                                {newlyCreatedToken}
                            </code>
                            <button
                                onClick={() => copyToClipboard(newlyCreatedToken, '__new__')}
                                className="p-1 hover:bg-emerald-100 dark:hover:bg-emerald-800 rounded transition-colors text-emerald-600"
                                title="复制 Token"
                            >
                                {copiedTokenId === '__new__' ? <CheckCircle2 className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
                            </button>
                        </div>
                        <button onClick={() => setNewlyCreatedToken(null)} className="mt-2 text-xs text-emerald-600 hover:underline">我已复制，关闭提示</button>
                    </div>
                )}

                {/* Token List */}
                <div className="space-y-3">
                    {tokens.map((token) => (
                        <div key={token.id} className="flex flex-col sm:flex-row sm:items-center gap-3 p-3 rounded-2xl bg-slate-50 dark:bg-slate-900/50 border border-slate-100 dark:border-slate-800 transition-all group">
                            <div className="flex-1 min-w-0">
                                <div className="text-sm font-medium text-slate-900 dark:text-slate-100 truncate">{token.name}</div>
                                <div className="flex items-center gap-2 mt-1">
                                    <code className="text-xs text-slate-500 dark:text-slate-400 bg-white dark:bg-slate-950 px-1.5 py-0.5 rounded border border-slate-100 dark:border-slate-800 font-mono tracking-tight">
                                        ta-sk-{'•'.repeat(16)}{token.token_hint || '****'}
                                    </code>
                                </div>
                                <div className="text-[10px] text-slate-400 dark:text-slate-500 mt-1">
                                    创建于：{new Date(token.created_at).toLocaleDateString()}
                                    {token.last_used_at && ` • 最后使用：${new Date(token.last_used_at).toLocaleString()}`}
                                </div>
                            </div>
                            <button
                                onClick={() => handleDeleteToken(token.id)}
                                className="self-end sm:self-center p-2 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-xl transition-colors"
                                title="吊销 Token"
                            >
                                <Trash2 className="w-4 h-4" />
                            </button>
                        </div>
                    ))}

                    {tokens.length === 0 && !tokensLoading && (
                        <div className="text-center py-6 border-2 border-dashed border-slate-100 dark:border-slate-800 rounded-3xl text-slate-400 text-sm font-medium">
                            暂无活跃的 API Token
                        </div>
                    )}
                </div>

                {/* Create Token Form */}
                    <form onSubmit={handleCreateToken} className="flex items-center gap-2 pt-2">
                        <input
                            type="text"
                            value={newTokenName}
                            onChange={e => setNewTokenName(e.target.value)}
                            placeholder="给新 Token 起个名字，如：Open Claw"
                            className="input flex-1 h-10 text-sm"
                            disabled={isCreatingToken || tokens.length >= 10}
                        />
                    <button
                        type="submit"
                        disabled={isCreatingToken || !newTokenName.trim() || tokens.length >= 10}
                        className="btn-primary h-10 px-4 flex items-center gap-2 whitespace-nowrap text-sm"
                    >
                        {isCreatingToken ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
                        生成 Token
                    </button>
                </form>
                {tokens.length >= 10 && (
                    <p className="text-[10px] text-amber-500">已达到 Token 创建上限（10个）</p>
                )}
            </div>

            <div className="card space-y-4">
                <div className="flex items-center gap-2">
                    <Mail className="w-5 h-5 text-blue-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">报告推送</h2>
                </div>

                {/* 邮件推送 */}
                <div className="rounded-xl border border-slate-200/80 bg-slate-50/80 px-4 py-3 dark:border-slate-700/80 dark:bg-slate-900/40">
                    <div className="flex items-center justify-between">
                        <div>
                            <div className="text-sm font-medium text-slate-700 dark:text-slate-200">邮件推送</div>
                            <div className="text-xs text-slate-400 dark:text-slate-500 mt-0.5">定时分析完成时发送至 {user?.email || '-'}</div>
                        </div>
                        <button
                            type="button"
                            onClick={() => setEmailReportEnabled(!emailReportEnabled)}
                            disabled={configLoading}
                            className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                                emailReportEnabled ? 'bg-blue-500' : 'bg-slate-300 dark:bg-slate-600'
                            }`}
                        >
                            <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${emailReportEnabled ? 'translate-x-6' : 'translate-x-1'}`} />
                        </button>
                    </div>
                </div>

                {/* 企业微信 Webhook */}
                <div className="rounded-xl border border-slate-200/80 bg-slate-50/80 px-4 py-3 space-y-3 dark:border-slate-700/80 dark:bg-slate-900/40">
                    <div className="flex items-center justify-between">
                        <div>
                            <div className="text-sm font-medium text-slate-700 dark:text-slate-200">企业微信 Webhook</div>
                            <div className="text-xs text-slate-400 dark:text-slate-500 mt-0.5">
                                定时分析完成时向机器人推送摘要
                                {storedWebhookDisplay && <span className="ml-2 font-mono">({storedWebhookDisplay})</span>}
                            </div>
                        </div>
                        <button
                            type="button"
                            onClick={() => setWecomReportEnabled(!wecomReportEnabled)}
                            disabled={configLoading}
                            className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
                                wecomReportEnabled ? 'bg-blue-500' : 'bg-slate-300 dark:bg-slate-600'
                            }`}
                        >
                            <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${wecomReportEnabled ? 'translate-x-6' : 'translate-x-1'}`} />
                        </button>
                    </div>

                    <div className="flex items-center gap-2">
                        <div className="relative flex-1">
                            <Webhook className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                            <input
                                type="text"
                                value={wecomWebhook}
                                onChange={e => setWecomWebhook(e.target.value)}
                                className="input w-full pl-10"
                                placeholder={hasStoredWebhook ? '已保存，留空则保持不变' : 'Webhook 地址'}
                                disabled={configLoading}
                            />
                        </div>
                        <button
                            type="button"
                            onClick={handleWecomWarmup}
                            disabled={configLoading || saving || saveAllSaving || wecomWarmingUp || (!wecomWebhook.trim() && !hasStoredWebhook)}
                            className="btn-secondary inline-flex items-center gap-1.5 text-xs shrink-0"
                        >
                            {wecomWarmingUp ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Flame className="w-3.5 h-3.5" />}
                            {wecomWarmingUp ? '发送中...' : '测试连接'}
                        </button>
                        {hasStoredWebhook && (
                            <button
                                type="button"
                                onClick={handleClearWebhook}
                                disabled={saving || saveAllSaving}
                                className="inline-flex items-center gap-1 text-xs text-slate-400 hover:text-rose-500 disabled:opacity-50 shrink-0"
                            >
                                <Trash2 className="w-3 h-3" />
                                清除
                            </button>
                        )}
                    </div>

                    {wecomWarmupMessage && (
                        <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700 dark:border-emerald-900/60 dark:bg-emerald-950/30 dark:text-emerald-300">
                            {wecomWarmupMessage}
                        </div>
                    )}
                    {wecomWarmupError && (
                        <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-600 dark:border-rose-900/60 dark:bg-rose-950/30 dark:text-rose-300">
                            {wecomWarmupError}
                        </div>
                    )}
                </div>
            </div>

            <div className="flex items-center gap-4">
                <button onClick={handleSaveAll} disabled={saveAllSaving} className="btn-primary inline-flex items-center gap-2">
                    {saveAllSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                    保存全部
                </button>
                {saved && <span className="text-sm text-green-600 dark:text-green-400">✓ {saveMessage}</span>}
            </div>
        </div>
    )
}
