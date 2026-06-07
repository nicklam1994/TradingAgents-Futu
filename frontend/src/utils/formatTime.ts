/**
 * Format a datetime string for display.
 *
 * Accepts ISO strings or "YYYY-MM-DD HH:MM:SS" formats.
 * Returns '--' for null/undefined/invalid input.
 */
export function formatTime(value?: string | null): string {
    if (!value) return '--'
    const d = new Date(value.replace(' ', 'T'))
    if (Number.isNaN(d.getTime())) return value
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
