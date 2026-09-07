import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, '..', ''), ...process.env }
  return {
  plugins: [vue()],
  server: {
    port: Number(env.VITE_DEV_PORT || 5173),
    proxy: {
      '/api': {
        target: env.VITE_WECHAT_PROXY_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
      '/xhs-api': {
        target: env.VITE_XHS_PROXY_TARGET || 'http://127.0.0.1:8010',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/xhs-api/, ''),
      },
      '/bulk-api': {
        target: env.VITE_BULK_PROXY_TARGET || 'http://127.0.0.1:8020',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/bulk-api/, ''),
      },
    },
  },
  }
})
