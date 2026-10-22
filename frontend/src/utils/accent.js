export const ACCENT_STORAGE_KEY = 'recurdo_accent_color_v1'

export const ACCENT_OPTIONS = [
  { id: 'gold', label: 'gold', color: '#C4A050', rgb: '196, 160, 80', brightRgb: '255, 222, 146' },
  { id: 'sage', label: 'sage', color: '#78A485', rgb: '120, 164, 133', brightRgb: '178, 226, 190' },
  { id: 'blue', label: 'blue', color: '#8EA0D5', rgb: '142, 160, 213', brightRgb: '190, 207, 255' },
  { id: 'rose', label: 'rose', color: '#D58A7A', rgb: '213, 138, 122', brightRgb: '255, 190, 178' },
  { id: 'violet', label: 'violet', color: '#B98CB9', rgb: '185, 140, 185', brightRgb: '232, 190, 232' },
]

export const DEFAULT_ACCENT_ID = 'gold'

export function normalizeAccentId(value) {
  const id = String(value || '').trim().toLowerCase()
  return ACCENT_OPTIONS.some((option) => option.id === id) ? id : DEFAULT_ACCENT_ID
}

export function accentForId(value) {
  const id = normalizeAccentId(value)
  return ACCENT_OPTIONS.find((option) => option.id === id) || ACCENT_OPTIONS[0]
}

export function loadStoredAccentId() {
  if (typeof window === 'undefined') {
    return DEFAULT_ACCENT_ID
  }
  return normalizeAccentId(window.localStorage.getItem(ACCENT_STORAGE_KEY))
}

export function applyAccent(value) {
  if (typeof document === 'undefined') {
    return DEFAULT_ACCENT_ID
  }

  const accent = accentForId(value)
  document.documentElement.dataset.accent = accent.id
  document.documentElement.style.setProperty('--accent', accent.color)
  document.documentElement.style.setProperty('--accent-rgb', accent.rgb)
  document.documentElement.style.setProperty('--accent-bright-rgb', accent.brightRgb)
  return accent.id
}

export function storeAndApplyAccent(value) {
  const id = applyAccent(value)
  if (typeof window !== 'undefined') {
    window.localStorage.setItem(ACCENT_STORAGE_KEY, id)
  }
  return id
}
