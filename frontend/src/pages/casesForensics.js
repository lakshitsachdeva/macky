const SENSITIVE_VALUE_PATTERNS = [
  /\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|secret|password)\s*[:=]\s*[^\s,;]+/gi,
  /\bAIza[0-9A-Za-z_-]{20,}\b/gi,
  /\bsk-[0-9A-Za-z_-]{12,}\b/gi,
  /\bgh[pousr]_[0-9A-Za-z_]{20,}\b/gi,
  /\bxox[baprs]-[0-9A-Za-z-]{10,}\b/gi,
  /\bBearer\s+[0-9A-Za-z._-]{16,}\b/gi,
]

export function redactSensitiveText(value) {
  return String(value ?? '').replace(
    SENSITIVE_VALUE_PATTERNS[0],
    '$1: [redacted]',
  ).replace(
    SENSITIVE_VALUE_PATTERNS[1],
    '[redacted secret]',
  ).replace(
    SENSITIVE_VALUE_PATTERNS[2],
    '[redacted secret]',
  ).replace(
    SENSITIVE_VALUE_PATTERNS[3],
    '[redacted secret]',
  ).replace(
    SENSITIVE_VALUE_PATTERNS[4],
    '[redacted secret]',
  ).replace(
    SENSITIVE_VALUE_PATTERNS[5],
    'Bearer [redacted secret]',
  )
}

export function normalizeForensicTimeline(payload) {
  const entries = Array.isArray(payload?.entries) ? payload.entries : []
  return {
    ...(payload && typeof payload === 'object' ? payload : {}),
    entries,
  }
}

export function forensicReportState({ loading = false, error = '', report = null } = {}) {
  if (loading) return 'loading'
  if (error) return 'error'
  if (!report) return 'empty'
  return 'ready'
}

export function sourceCitationText(indices) {
  const values = Array.isArray(indices) ? indices.filter((value) => Number.isInteger(value)) : []
  return values.length ? values.map((value) => `[${value}]`).join(' ') : 'no source citation'
}
