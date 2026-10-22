export function boundedInteger(value, fallback, minimum, maximum) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.min(maximum, Math.max(minimum, Math.trunc(parsed)))
}

export function buildForensicTimelineQuery(options = {}) {
  const params = new URLSearchParams()
  const startAt = options.startAt ?? options.start_at
  const endAt = options.endAt ?? options.end_at
  if (startAt) params.set('start_at', String(startAt))
  if (endAt) params.set('end_at', String(endAt))
  if (options.limit !== undefined && options.limit !== null) {
    params.set('limit', String(boundedInteger(options.limit, 1000, 1, 5000)))
  }
  return params.toString() ? `?${params.toString()}` : ''
}

export function buildCaseReportPayload(payload = {}) {
  const hasQuestion = payload.question !== undefined && payload.question !== null
  const question = hasQuestion ? payload.question : payload.prompt
  return {
    ...(question !== undefined ? { [hasQuestion ? 'question' : 'prompt']: question } : {}),
    allow_model: Boolean(payload.allow_model ?? payload.allowModel ?? false),
    limit: boundedInteger(payload.limit, 10, 1, 20),
  }
}
