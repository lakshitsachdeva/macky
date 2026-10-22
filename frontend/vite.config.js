import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath } from 'node:url'

function fromRoot(relativePath) {
  return fileURLToPath(new URL(relativePath, import.meta.url))
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      input: {
        main: fromRoot('./index.html'),
        landing: fromRoot('./landing/index.html'),
        privacy: fromRoot('./landing/privacy.html'),
        terms: fromRoot('./landing/terms.html'),
      },
    },
  },
})
