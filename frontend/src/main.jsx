import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import AppErrorBoundary from './components/AppErrorBoundary.jsx'
import LocalSearchHost from './components/LocalSearchHost.jsx'
import Overlay from './components/Overlay.jsx'
import { applyAccent, loadStoredAccentId } from './utils/accent'
import { detectWindowLabel } from './utils/tauri'

const windowLabel = detectWindowLabel()

if (typeof document !== 'undefined') {
  document.documentElement.dataset.window = windowLabel
  document.body.dataset.window = windowLabel
  applyAccent(loadStoredAccentId())
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <AppErrorBoundary>
      {windowLabel === 'popover' ? <Overlay /> : <><App /><LocalSearchHost /></>}
    </AppErrorBoundary>
  </StrictMode>,
)
