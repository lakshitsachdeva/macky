import { invoke, isTauri } from '@tauri-apps/api/core'
import { getCurrentWebviewWindow } from '@tauri-apps/api/webviewWindow'

export function detectWindowLabel() {
  if (typeof window === 'undefined') {
    return 'settings'
  }

  const params = new URLSearchParams(window.location.search)
  const forced = params.get('window')
  if (forced === 'settings' || forced === 'popover' || forced === 'login') {
    return forced
  }

  const hash = window.location.hash.replace('#', '')
  if (hash === 'settings' || hash === 'popover' || hash === 'login') {
    return hash
  }

  if (isTauri()) {
    try {
      return getCurrentWebviewWindow().label
    } catch {
      return 'settings'
    }
  }

  return 'settings'
}

export async function invokeIfTauri(command, args = {}) {
  if (!isTauri()) {
    return null
  }
  return invoke(command, args)
}
