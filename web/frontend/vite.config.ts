import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Backend (web/app.py) serves this app's production build directly and
// answers everything under /api and /api/media. In dev, proxy both so the
// Vite server can run standalone against a locally running backend.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8080',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
