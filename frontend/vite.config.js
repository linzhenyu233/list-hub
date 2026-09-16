import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, '..', ''), ...process.env }
  return {
  plugins: [vue()],
  build: {
    // 提高警告阈值，同时按依赖来源拆包，避免单个 chunk 过大
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          if (id.includes('element-plus') || id.includes('@element-plus')) return 'vendor-element'
          if (id.includes('/vue/') || id.includes('@vue/') || id.includes('vue-router')) return 'vendor-vue'
          return 'vendor'
        },
      },
    },
  },
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
