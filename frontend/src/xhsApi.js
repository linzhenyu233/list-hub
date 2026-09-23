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
  // brandId 可选：编辑页回显品牌的 ID 常不在第一页（平台每页只给 20 个），
  // 传了它后端会翻页把该品牌捞回来，否则下拉匹配不到、只能显示数字 ID。
  brands: (categoryId, keyword = '', brandId = '') => http.get('/brands', { params: { category_id: categoryId, keyword, brand_id: brandId || undefined } }),
  // shopId 可选：不传用"当前选中店铺"，传了就用指定店铺（跨店发布时按目标店取该店参数）。
  // 依赖 shopContext.applyShopHeaders 的"显式头不覆盖"规则。
  shippingTemplates: (shopId = '') => http.get('/shipping-templates', { headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  logisticsPlans: (shopId = '') => http.get('/logistics-plans', { headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  categoryAttributes: (categoryId) => http.get('/category-attributes', { params: { category_id: categoryId } }),
  categoryVariations: (categoryId) => http.get('/category-variations', { params: { category_id: categoryId } }),
  attributeValues: (categoryId, attributeId) => http.get('/attribute-values', { params: { category_id: categoryId, attribute_id: attributeId } }),
  uploadMaterial: (url) => http.post('/materials/upload', { url }),
  // 直接上传本地图片/视频：前端读成 base64 → 小红书素材接口 → 返回素材 URL
  // （type=IMAGE 时后端会自动放大到 ≥1200；type=VIDEO 不做图片处理）
  uploadMaterialFile: (filename, contentBase64, type = 'IMAGE') =>
    http.post('/materials/upload-file', { filename, content_base64: contentBase64, type },
      { timeout: type === 'VIDEO' ? 300000 : 60000 }),
  createItemAndSku: (body) => http.post('/items/and-sku', body),
  updateItem: (itemId, body) => http.put(`/items/${itemId}`, body),
  updateSku: (skuId, body) => http.put(`/skus/${skuId}`, body),
  setSkuAvailable: (skuId, available) => http.post(`/skus/${skuId}/available`, { available }),
  // 删除商品(平台侧彻底删除,不可恢复)。与微信商品列表的「删除」对齐。
  deleteItem: (itemId) => http.delete(`/items/${itemId}`),
}
