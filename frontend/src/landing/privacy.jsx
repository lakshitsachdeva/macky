import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'
import PrivacyPage from './PrivacyPage'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <PrivacyPage />
  </StrictMode>,
)
