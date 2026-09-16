import axios from 'axios'
import { applyShopHeaders } from './shopContext'

const http = axios.create({
  baseURL: import.meta.env.VITE_XHS_API_BASE_URL || '/xhs-api',
  timeout: 30000,
})

// 多店铺:每个请求都带上 X-Shop-Id / X-Operator(未选店铺时后端回退默认店)
http.interceptors.request.use((config) => applyShopHeaders(config))

http.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const detail = error.response?.data?.detail
    throw new Error(typeof detail === 'string' ? detail : error.message || '小红书请求失败')
  },
)

export const xhsApi = {
  health: () => http.get('/health'),
  // shopId 可选：不传用"当前选中店铺"，传了就用指定店铺（店铺管理页逐个店巡检用）。
  // 依赖 shopContext.applyShopHeaders 的"显式头不覆盖"规则。
  tokenInfo: (shopId = '') => http.get('/token/info', { headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  tokenByCode: (code) => http.post('/token/code', { code }),
  tokenRefresh: (shopId = '') => http.post('/token/refresh', null, { headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  listItems: (params = {}) => http.get('/items', { params }),
  itemStatus: (itemIds) => http.post('/items/status', { item_ids: itemIds }),
  getItem: (itemId) => http.get(`/items/${itemId}`),
  categories: (params = {}) => http.get('/categories', { params }),
  brands: (categoryId, keyword = '') => http.get('/brands', { params: { category_id: categoryId, keyword } }),
  shippingTemplates: () => http.get('/shipping-templates'),
  logisticsPlans: () => http.get('/logistics-plans'),
  categoryAttributes: (categoryId) => http.get('/category-attributes', { params: { category_id: categoryId } }),
  categoryVariations: (categoryId) => http.get('/category-variations', { params: { category_id: categoryId } }),
  attributeValues: (categoryId, attributeId) => http.get('/attribute-values', { params: { category_id: categoryId, attribute_id: attributeId } }),
  uploadMaterial: (url) => http.post('/materials/upload', { url }),
  createItemAndSku: (body) => http.post('/items/and-sku', body),
  updateItem: (itemId, body) => http.put(`/items/${itemId}`, body),
  updateSku: (skuId, body) => http.put(`/skus/${skuId}`, body),
  setSkuAvailable: (skuId, available) => http.post(`/skus/${skuId}/available`, { available }),
}
