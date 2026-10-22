export function isSearchShortcut(event) {
  const key = event?.key
  const code = event?.code

  return Boolean(
    !event?.repeat
    && !event?.altKey
    && (event?.metaKey || event?.ctrlKey)
    && event?.shiftKey
    && (code === 'Space' || key === ' ' || key === 'Space' || key === 'Spacebar'),
  )
}
