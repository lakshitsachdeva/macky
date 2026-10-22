import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'
import TermsPage from './TermsPage'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <TermsPage />
  </StrictMode>,
)
