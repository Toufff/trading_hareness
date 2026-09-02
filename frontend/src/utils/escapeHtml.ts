/** Escape text that is interpolated into an HTML string (e.g. an ECharts
 * tooltip formatter) before it is rendered.
 *
 * ECharts tooltip/label formatters that return a string are inserted as raw
 * HTML.  Several formatters in this dashboard interpolate analyst-authored
 * text (evidence quotes, free-form labels) that ultimately originates from a
 * remote, untrusted source.  Escaping it here prevents a stored-XSS payload
 * embedded in that text from executing when the tooltip renders.
 */
export function escapeHtml(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
