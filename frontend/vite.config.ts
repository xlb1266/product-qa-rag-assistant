import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 后端默认 8000，开发期间把 /api 转发过去，避免跨域问题
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
