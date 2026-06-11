/**
 * SymbolTagInput — tag-style stock symbol input with fuzzy search
 *
 * Features:
 * - Type to fuzzy search stocks via /v1/market/stock-search
 * - Select from dropdown to add as tag
 * - Tags displayed as chips with × remove button
 * - Market flag emoji (🇭🇰/🇺🇸) based on code prefix
 */

import { useState, useRef, useEffect, useCallback } from 'react'
import { X, Loader2 } from 'lucide-react'
import { api } from '@/services/api'
import type { StockSearchResult } from '@/types'

interface SymbolTagInputProps {
    symbols: string[]
    onChange: (symbols: string[]) => void
    placeholder?: string
}

function getMarketFlag(symbol: string): string {
    const upper = symbol.toUpperCase()
    if (upper.startsWith('HK.') || upper.startsWith('HK:')) return '🇭🇰'
    if (upper.startsWith('US.') || upper.startsWith('US:')) return '🇺🇸'
    // Guess by numeric prefix (HK stocks are typically 5-digit numbers)
    if (/^\d{4,5}$/.test(symbol)) return '🇭🇰'
    return '🇺🇸'
}

export default function SymbolTagInput({ symbols, onChange, placeholder = '输入股票代码，回车添加' }: SymbolTagInputProps) {
    const [input, setInput] = useState('')
    const [results, setResults] = useState<StockSearchResult[]>([])
    const [searching, setSearching] = useState(false)
    const [showDropdown, setShowDropdown] = useState(false)
    const [highlightIdx, setHighlightIdx] = useState(-1)
    const wrapperRef = useRef<HTMLDivElement>(null)
    const inputRef = useRef<HTMLInputElement>(null)
    const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

    // Close dropdown on outside click
    useEffect(() => {
        const handler = (e: MouseEvent) => {
            if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
                setShowDropdown(false)
            }
        }
        document.addEventListener('mousedown', handler)
        return () => document.removeEventListener('mousedown', handler)
    }, [])

    // Debounced search
    const doSearch = useCallback(async (q: string) => {
        if (q.trim().length < 1) {
            setResults([])
            setShowDropdown(false)
            return
        }
        setSearching(true)
        try {
            const res = await api.searchStocks(q.trim())
            const items = (res.results ?? []).filter(r => !symbols.includes(r.symbol))
            setResults(items)
            setShowDropdown(items.length > 0)
            setHighlightIdx(-1)
        } catch {
            setResults([])
            setShowDropdown(false)
        } finally {
            setSearching(false)
        }
    }, [symbols])

    const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        const val = e.target.value
        setInput(val)
        if (debounceRef.current) clearTimeout(debounceRef.current)
        debounceRef.current = setTimeout(() => doSearch(val), 250)
    }

    const addSymbol = (symbol: string) => {
        const trimmed = symbol.trim().toUpperCase()
        if (!trimmed || symbols.includes(trimmed)) return
        onChange([...symbols, trimmed])
        setInput('')
        setResults([])
        setShowDropdown(false)
        inputRef.current?.focus()
    }

    const removeSymbol = (symbol: string) => {
        onChange(symbols.filter(s => s !== symbol))
    }

    const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
        if (e.key === 'ArrowDown') {
            e.preventDefault()
            setHighlightIdx(prev => Math.min(prev + 1, results.length - 1))
        } else if (e.key === 'ArrowUp') {
            e.preventDefault()
            setHighlightIdx(prev => Math.max(prev - 1, -1))
        } else if (e.key === 'Enter') {
            e.preventDefault()
            if (highlightIdx >= 0 && results[highlightIdx]) {
                addSymbol(results[highlightIdx].symbol)
            } else if (input.trim()) {
                addSymbol(input)
            }
        } else if (e.key === 'Backspace' && !input && symbols.length > 0) {
            removeSymbol(symbols[symbols.length - 1])
        } else if (e.key === 'Escape') {
            setShowDropdown(false)
        }
    }

    return (
        <div ref={wrapperRef} className="relative">
            {/* Tag container + input */}
            <div
                className="flex flex-wrap items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-sm transition focus-within:border-green-400 focus-within:ring-2 focus-within:ring-green-400/20 dark:border-slate-700 dark:bg-slate-800 dark:focus-within:border-green-500 cursor-text"
                onClick={() => inputRef.current?.focus()}
            >
                {symbols.map(sym => (
                    <span
                        key={sym}
                        className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-medium text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300"
                    >
                        <span>{getMarketFlag(sym)}</span>
                        <span>{sym}</span>
                        <button
                            type="button"
                            onClick={e => { e.stopPropagation(); removeSymbol(sym) }}
                            className="ml-0.5 rounded-full p-0.5 hover:bg-emerald-200 dark:hover:bg-emerald-800/50 transition"
                        >
                            <X className="h-3 w-3" />
                        </button>
                    </span>
                ))}
                <input
                    ref={inputRef}
                    type="text"
                    value={input}
                    onChange={handleInputChange}
                    onKeyDown={handleKeyDown}
                    onFocus={() => { if (results.length > 0) setShowDropdown(true) }}
                    placeholder={symbols.length === 0 ? placeholder : '继续添加...'}
                    className="flex-1 min-w-[120px] bg-transparent text-sm text-slate-900 placeholder-slate-400 outline-none dark:text-slate-100 dark:placeholder-slate-500"
                />
                {searching && <Loader2 className="h-3.5 w-3.5 animate-spin text-slate-400" />}
            </div>

            {/* Dropdown */}
            {showDropdown && results.length > 0 && (
                <div className="absolute z-50 mt-1 w-full max-h-48 overflow-y-auto rounded-lg border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-800">
                    {results.map((r, i) => (
                        <button
                            key={r.symbol}
                            type="button"
                            onMouseDown={e => { e.preventDefault(); addSymbol(r.symbol) }}
                            onMouseEnter={() => setHighlightIdx(i)}
                            className={`flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition ${
                                i === highlightIdx
                                    ? 'bg-green-50 text-green-700 dark:bg-green-900/30 dark:text-green-300'
                                    : 'text-slate-700 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-700/50'
                            }`}
                        >
                            <span>{getMarketFlag(r.symbol)}</span>
                            <span className="font-medium">{r.symbol}</span>
                            <span className="text-slate-400 dark:text-slate-500 truncate">{r.name}</span>
                        </button>
                    ))}
                </div>
            )}
        </div>
    )
}
