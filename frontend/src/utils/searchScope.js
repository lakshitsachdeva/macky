export function caseId(value) {
  return value?.id ?? value?.case_id ?? value?.uuid ?? null
}

export function caseLabel(value) {
  return String(value?.name ?? value?.title ?? `Case ${caseId(value) ?? ''}`).trim()
}

export function resolveSearchCase(selectedCaseId, caseOptions = []) {
  const normalizedId = String(selectedCaseId || '').trim()
  if (!normalizedId) return { id: '', value: null }

  const value = caseOptions.find((item) => String(caseId(item)) === normalizedId) || null
  if (!value) return { id: '', value: null }

  // An empty case is not a useful global-search scope. Treat it as an
  // unselected case so a stale selection cannot make the whole library look
  // empty after the user created a case but has not imported evidence yet.
  if (Object.prototype.hasOwnProperty.call(value, 'evidence_count')) {
    const evidenceCount = Number(value.evidence_count)
    if (Number.isFinite(evidenceCount) && evidenceCount <= 0) {
      return { id: '', value: null }
    }
  }

  return { id: normalizedId, value }
}

export function searchScopeLabel(selectedCase) {
  return selectedCase ? `case · ${caseLabel(selectedCase)}` : 'all evidence'
}
