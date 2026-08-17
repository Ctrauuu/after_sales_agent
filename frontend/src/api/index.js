import axios from 'axios'
import { ElMessage } from 'element-plus'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api/admin',
  timeout: 15000,
})

/**
 * 输入：Axios 成功响应 ``response``。
 * 输出：响应中的业务数据。
 * 功能：统一去除 Axios 响应外壳，页面只处理后端业务字段。
 */
function unwrapResponse(response) {
  return response.data
}

/**
 * 输入：Axios 请求异常 ``error``，可通过配置 ``silent`` 禁止全局提示。
 * 输出：被拒绝的原始异常 Promise。
 * 功能：统一展示后端错误信息，同时允许自动刷新等场景静默失败。
 */
function handleResponseError(error) {
  if (!error.config?.silent) {
    ElMessage.error(error.response?.data?.message || '请求失败，请稍后重试')
  }
  return Promise.reject(error)
}

api.interceptors.response.use(unwrapResponse, handleResponseError)

export default api
