function numberValue(value) {
  const number = Number(value)
  return Number.isFinite(number) ? Math.max(0, number) : 0
}

export function getSearchIndexNotice(status) {
  if (!status || typeof status !== 'object') return null

  const pending = Math.max(
    numberValue(status.pending_files),
    numberValue(status.queue_size),
    numberValue(status.background?.pending),
    numberValue(status.scan_progress?.remaining),
  )

  if (status.is_scanning) {
    const processed = numberValue(status.scan_progress?.processed)
    const total = numberValue(status.scan_progress?.total)
    const progress = total ? ` · ${processed.toLocaleString()} / ${total.toLocaleString()}` : ''
    return {
      kind: 'indexing',
      message: `Indexing files${progress} — some files may not be searchable yet.`,
    }
  }

  if (pending > 0) {
    return {
      kind: 'pending',
      message: `Search is ready, but ${pending.toLocaleString()} file${pending === 1 ? '' : 's'} still need indexing.`,
    }
  }

  if (numberValue(status.files_watched) > 0 && numberValue(status.unique_memories) === 0) {
    return {
      kind: 'pending',
      message: 'Your watched files are still being prepared for search.',
    }
  }

  return null
}
