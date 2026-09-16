import { computed, ref } from 'vue'

/**
 * 多店铺上下文 —— 前端唯一的"当前店铺"来源。
 *
 * 为什么用模块级 ref 而不是 Pinia/Vuex：
 *   本项目本来就没有引入状态管理（App.vue 用 activeView ref 切视图），
 *   模块级 ref 同样具备跨组件响应式，且不增加依赖。
 *
 * 三个 axios 实例（api.js / xhsApi.js / bulkApi.js）在请求拦截器里统一调用
 * applyShopHeaders()，把 X-Shop-Id / X-Operator 注入到**每一个**请求，
 * 所以各业务组件不需要自己关心店铺参数。
 *
 * 数据隔离由后端按 X-Shop-Id 完成：切换店铺后必须重新拉取数据，
 * 各组件 watch(currentShopId) 自行重置（BulkPublishPanel 会退回第 0 步）。
 */

const LS_SHOP = 'shop_id'
const LS_OPERATOR = 'operator'

/** 店铺清单（来自 GET /bulk-api/shops，后端已脱敏，不含任何密钥） */
export const shops = ref([])
export const shopsLoaded = ref(false)
export const shopsError = ref('')

/** 当前选中的店铺 / 操作人（持久化到 localStorage，刷新页面不丢） */
export const currentShopId = ref(localStorage.getItem(LS_SHOP) || '')
export const currentOperator = ref(localStorage.getItem(LS_OPERATOR) || '')

export const currentShop = computed(
  () => shops.value.find((item) => item.shop_id === currentShopId.value) || null,
)

/** 当前店铺对应的请求头；没有选中店铺时返回空对象，由后端回退到默认店。 */
export function shopHeaders() {
  const headers = {}
  if (currentShopId.value) headers['X-Shop-Id'] = currentShopId.value
  if (currentOperator.value) headers['X-Operator'] = currentOperator.value
  return headers
}

/** 把店铺请求头合并进 axios 的 config（axios 各版本 headers 结构略有差异，做兼容）。 */
export function applyShopHeaders(config) {
  const headers = shopHeaders()
  Object.entries(headers).forEach(([key, value]) => {
    if (config.headers && typeof config.headers.set === 'function') config.headers.set(key, value)
    else config.headers = { ...(config.headers || {}), [key]: value }
  })
  return config
}

export function setCurrentShop(shopId) {
  currentShopId.value = shopId || ''
  if (shopId) localStorage.setItem(LS_SHOP, shopId)
  else localStorage.removeItem(LS_SHOP)
}

export function setOperator(name) {
  currentOperator.value = name || ''
  if (name) localStorage.setItem(LS_OPERATOR, name)
  else localStorage.removeItem(LS_OPERATOR)
}

/**
 * 拉取店铺清单。若本地存的店铺已不存在或还没选过，自动回退到后端默认店。
 * 用原生 fetch 而不是 bulkApi，避免 shopContext ←→ bulkApi 的循环依赖。
 */
export async function loadShops() {
  const base = import.meta.env.VITE_BULK_API_BASE_URL || '/bulk-api'
  try {
    const response = await fetch(`${base}/shops`, { headers: shopHeaders() })
    const body = await response.json().catch(() => null)
    if (!response.ok) throw new Error(body?.detail || `HTTP ${response.status}`)
    shops.value = body?.result || []
    shopsError.value = ''
    const ids = shops.value.map((item) => item.shop_id)
    if (!currentShopId.value || !ids.includes(currentShopId.value)) {
      const fallback = shops.value.find((item) => item.is_default) || shops.value[0]
      if (fallback) setCurrentShop(fallback.shop_id)
    }
  } catch (error) {
    shopsError.value = error.message || String(error)
  } finally {
    shopsLoaded.value = true
  }
  return shops.value
}

/**
 * 给浏览器自己发起的请求（<img src>、<a download> 等）拼店铺参数 ——
 * 这类请求带不了自定义请求头，只能用 query 传店铺。
 */
export function withShopQuery(url) {
  if (!currentShopId.value) return url
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}shop_id=${encodeURIComponent(currentShopId.value)}`
}
