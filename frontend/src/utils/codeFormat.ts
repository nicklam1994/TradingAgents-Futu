/**
 * Stock Code Format Converter — Single source of truth for frontend.
 *
 * Formats:
 *   - canonical: "AAPL.US", "00700.HK" (DB storage, API exchange)
 *   - futu:      "US.AAPL", "HK.00700" (Futu OpenD API)
 *   - display:   "AAPL", "00700"       (UI display, pure code)
 *   - pure:      "AAPL", "00700"       (user input)
 *
 * Usage:
 *   import { toCanonical, toFutu, toDisplay, detectMarket } from '@/utils/codeFormat'
 */

// ── Canonical (CODE.MARKET) ─────────────────────────────────────────────────

export function toCanonical(code: string): string {
  if (!code) return code

  const upper = code.trim().toUpperCase()

  // Already canonical: CODE.MARKET
  if (isCanonical(upper)) return upper

  // Futu format: MARKET.CODE
  if (upper.startsWith('HK.')) return upper.slice(3) + '.HK'
  if (upper.startsWith('US.')) return upper.slice(3) + '.US'
  if (upper.startsWith('SH.')) return upper.slice(3) + '.SH'
  if (upper.startsWith('SZ.')) return upper.slice(3) + '.SZ'

  // Pure code (no market info)
  return upper
}

function isCanonical(code: string): boolean {
  const parts = code.split('.')
  if (parts.length !== 2) return false
  const [codePart, market] = parts
  return market.length === 2 && /^[A-Z]+$/.test(market) && codePart.length > 0
}

// ── Futu (MARKET.CODE) ──────────────────────────────────────────────────────

export function toFutu(code: string): string {
  if (!code) return code

  const upper = code.trim().toUpperCase()

  // Already Futu format: MARKET.CODE
  if (isFutu(upper)) return upper

  // Canonical format: CODE.MARKET
  if (isCanonical(upper)) {
    const [codePart, market] = upper.split('.')
    return `${market}.${codePart}`
  }

  // Pure code - infer market
  return inferAndFormat(upper)
}

function isFutu(code: string): boolean {
  const parts = code.split('.')
  if (parts.length !== 2) return false
  const [market, codePart] = parts
  return ['HK', 'US', 'SH', 'SZ'].includes(market) && codePart.length > 0
}

function inferAndFormat(code: string): string {
  // Numeric codes are HK stocks
  if (/^\d+$/.test(code)) return `HK.${code}`
  // Alpha codes are US stocks
  return `US.${code}`
}

// ── Display Format (pure code for UI) ───────────────────────────────────────

export function toDisplay(code: string): string {
  if (!code) return code

  const upper = code.trim().toUpperCase()

  // Remove MARKET. prefix (Futu format)
  for (const prefix of ['HK.', 'US.', 'SH.', 'SZ.']) {
    if (upper.startsWith(prefix)) return upper.slice(prefix.length)
  }

  // Remove .MARKET suffix (canonical format)
  if (isCanonical(upper)) return upper.split('.')[0]

  return upper
}

// ── Market Detection ────────────────────────────────────────────────────────

export function detectMarket(code: string): string | null {
  if (!code) return null

  const upper = code.trim().toUpperCase()

  // Canonical: CODE.MARKET
  if (isCanonical(upper)) return upper.split('.')[1]

  // Futu: MARKET.CODE
  if (isFutu(upper)) return upper.split('.')[0]

  // Pure code - infer
  if (/^\d+$/.test(upper)) return 'HK'
  if (/^[A-Z]+$/.test(upper)) return 'US'

  return null
}

// ── Validation ──────────────────────────────────────────────────────────────

export function isValidCode(code: string): boolean {
  if (!code) return false

  const upper = code.trim().toUpperCase()

  // Check all formats
  if (isCanonical(upper) || isFutu(upper)) return true

  // Pure code - must be alphanumeric, min 4 chars
  return /^[A-Z0-9]+$/.test(upper) && upper.length >= 4
}

// ── Market Flag Emoji ───────────────────────────────────────────────────────

export function marketFlag(code: string): string {
  const market = detectMarket(code)
  switch (market) {
    case 'HK': return '🇭🇰'
    case 'US': return '🇺🇸'
    case 'SH':
    case 'SZ': return '🇨🇳'
    default: return '📈'
  }
}
