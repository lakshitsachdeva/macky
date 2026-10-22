import { useEffect, useState } from 'react'
import { isTauri } from '@tauri-apps/api/core'
import Overlay from './Overlay.jsx'
import { isSearchShortcut } from '../utils/keyboard'

function isLocalBrowser() {
  if (typeof window === 'undefined' || isTauri()) return false
  return ['localhost', '127.0.0.1', '::1'].includes(window.location.hostname)
}

export default function LocalSearchHost() {
  const [isOpen, setIsOpen] = useState(false)

  useEffect(() => {
    if (!isLocalBrowser()) return undefined

    const handleKeyDown = (event) => {
      if (!isSearchShortcut(event)) return
      event.preventDefault()
      setIsOpen((current) => !current)
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [])

  if (!isOpen) return null

  return (
    <div className="local-search-overlay">
      <Overlay onClose={() => setIsOpen(false)} />
    </div>
  )
}
