export function relativeTime(isoString) {
  if (!isoString) return ''

  const date = new Date(isoString)
  if (Number.isNaN(date.getTime())) return ''

  const now = new Date()
  const diffMs = now - date
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24))

  if (diffDays === 0) return 'today'
  if (diffDays === 1) return 'yesterday'
  if (diffDays < 7) return `${diffDays} days ago`
  if (diffDays < 14) return 'last week'
  if (diffDays < 30) return `${Math.floor(diffDays / 7)} weeks ago`
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

export function memoryDateLabel(item) {
  if (!item) return ''

  const createdValue = item.created_at || item.first_seen_at
  const modifiedValue = item.modified_at
  const createdDate = createdValue ? new Date(createdValue) : null
  const modifiedDate = modifiedValue ? new Date(modifiedValue) : null
  const hasCreated = createdDate && !Number.isNaN(createdDate.getTime())
  const hasModified = modifiedDate && !Number.isNaN(modifiedDate.getTime())
  const materiallyModified =
    hasCreated && hasModified
      ? Math.abs(modifiedDate.getTime() - createdDate.getTime()) > 60 * 1000
      : hasModified

  const timestamp = materiallyModified ? modifiedValue : createdValue
  const label = relativeTime(timestamp)
  if (!label) return ''

  return `${materiallyModified ? 'modified' : 'saved'} ${label}`
}
