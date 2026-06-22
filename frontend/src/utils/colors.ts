/**
 * 统一涨跌颜色工具 — 绿涨红跌 (Green=Up/Profit, Red=Down/Loss)
 *
 * Convention:
 *   Positive / profit / up  → emerald/green
 *   Negative / loss  / down → rose/red
 *
 * All profit/loss color logic MUST go through these helpers
 * to prevent 红涨绿跌 / 绿涨红跌 inconsistency across pages.
 */

// ── Text color ──────────────────────────────────────────────────

/** Tailwind text classes for a signed numeric value (light + dark). */
export function pnlTextClass(value: number | null | undefined): string {
  return (value ?? 0) >= 0
    ? 'text-emerald-600 dark:text-emerald-400'
    : 'text-rose-600 dark:text-rose-400';
}

/** Shorter variant without dark: prefix (for inline overrides). */
export function pnlTextClassLight(value: number | null | undefined): string {
  return (value ?? 0) >= 0 ? 'text-emerald-500' : 'text-rose-500';
}

// ── Badge / chip background ─────────────────────────────────────

/** Bg + text combo for small badges/chips. */
export function pnlBadgeClass(value: number | null | undefined): string {
  return (value ?? 0) >= 0
    ? 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-400'
    : 'bg-rose-50 text-rose-600 dark:bg-rose-500/10 dark:text-rose-400';
}

// ── Trade action colors (buy=positive, sell=negative) ───────────

export function tradeActionTextClass(action: string): string {
  const a = action.toLowerCase();
  if (a === 'buy' || a === 'add' || a === '增持') {
    return 'text-emerald-600 dark:text-emerald-400';
  }
  if (a === 'sell' || a === 'reduce' || a === '减持') {
    return 'text-rose-600 dark:text-rose-400';
  }
  return 'text-slate-500';
}

export function tradeActionBgClass(action: string): string {
  const a = action.toLowerCase();
  if (a === 'buy' || a === 'add' || a === '增持') {
    return 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400 border-emerald-200 dark:border-emerald-500/30';
  }
  if (a === 'sell' || a === 'reduce' || a === '减持') {
    return 'bg-rose-100 dark:bg-rose-500/20 text-rose-700 dark:text-rose-400 border-rose-200 dark:border-rose-500/30';
  }
  return 'bg-slate-100 dark:bg-slate-500/20 text-slate-700 dark:text-slate-400 border-slate-200 dark:border-slate-500/30';
}

// ── Icon helper ─────────────────────────────────────────────────

export function pnlIconClass(value: number | null | undefined): string {
  return (value ?? 0) >= 0 ? 'text-emerald-500' : 'text-rose-500';
}

// ── Risk level colors (high=red, low=green — semantic, NOT pnl) ─

export function riskLevelColor(level: 'high' | 'medium' | 'low'): string {
  const map: Record<string, string> = {
    high: 'text-rose-400',
    medium: 'text-amber-400',
    low: 'text-emerald-400',
  };
  return map[level] ?? 'text-slate-400';
}

// ── Agent role colors (semantic — bull=green, bear=red) ─────────
// These are NOT profit/loss — they represent analytical stance.
// Keep them consistent: bull/bullish = emerald, bear/bearish = rose.

export const AGENT_ROLE_COLORS = {
  bull: 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-600 dark:text-emerald-400',
  bear: 'bg-rose-100 dark:bg-rose-500/20 text-rose-600 dark:text-rose-400',
  aggressive: 'bg-red-100 dark:bg-red-500/20 text-red-600 dark:text-red-400',
} as const;
