import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { readFileSync } from 'node:fs'

const rootConfig = JSON.parse(
  readFileSync(new URL('../config.json', import.meta.url), 'utf-8')
)
const backendPort = Number(rootConfig.system?.port || 8000)
const backendTarget = `http://127.0.0.1:${backendPort}`

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0', // Allow access from other devices (TV, Phone)
    port: 5173,
    proxy: {
      '/api': backendTarget,
      '/add_song': backendTarget,
      '/debug': backendTarget,
      '/music': backendTarget,
      '/song': backendTarget,
      '/songs': backendTarget,
      '/state': backendTarget,
      '/ws': {
        target: backendTarget,
        ws: true
      }
    }
  }
})
