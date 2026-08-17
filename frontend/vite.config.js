import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 输入：Vite 配置环境 ``configEnv``，包含当前模式。
 * 输出：前端开发与构建配置对象。
 * 功能：把开发期 ``/api`` 请求代理到真实管理 API，并在配置时由代理安全注入管理令牌。
 */
function buildConfig(configEnv) {
  const env = loadEnv(configEnv.mode, process.cwd(), '')
  const proxy = {
    target: env.VITE_ADMIN_API_TARGET || 'http://127.0.0.1:8080',
    changeOrigin: true,
  }
  if (env.SUNING_ADMIN_API_TOKEN) proxy.headers = { Authorization: `Bearer ${env.SUNING_ADMIN_API_TOKEN}` }

  return {
    plugins: [vue()],
    server: { proxy: { '/api': proxy } },
  }
}

export default defineConfig(buildConfig)
