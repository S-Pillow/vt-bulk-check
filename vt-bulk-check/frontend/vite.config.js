import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  base: '/tools/vt-bulk-check/',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true
  }
})
