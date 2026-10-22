import { isTauri } from '@tauri-apps/api/core'
import { check } from '@tauri-apps/plugin-updater'
import { relaunch } from '@tauri-apps/plugin-process'

const UPDATE_TIMEOUT_MS = 30000

export async function checkForRecurdoUpdate() {
  if (!isTauri()) {
    return {
      available: false,
      unsupported: true,
      message: 'updates are only available in the desktop app',
    }
  }

  const update = await check({ timeout: UPDATE_TIMEOUT_MS })
  if (!update) {
    return { available: false, update: null }
  }

  return {
    available: true,
    update,
    version: update.version,
    currentVersion: update.currentVersion,
    date: update.date || '',
    notes: update.body || '',
  }
}

export async function installRecurdoUpdate(update, onProgress) {
  if (!update) {
    throw new Error('No update is ready to install')
  }

  let downloaded = 0
  let contentLength = 0

  await update.downloadAndInstall((event) => {
    if (event.event === 'Started') {
      downloaded = 0
      contentLength = Number(event.data?.contentLength || 0)
    }

    if (event.event === 'Progress') {
      downloaded += Number(event.data?.chunkLength || 0)
    }

    const percent = contentLength > 0
      ? Math.min(100, Math.round((downloaded / contentLength) * 100))
      : 0

    onProgress?.({
      event: event.event,
      downloaded,
      contentLength,
      percent,
    })
  }, { timeout: UPDATE_TIMEOUT_MS })

  await relaunch()
}
