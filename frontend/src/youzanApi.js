import axios from 'axios'
import { applyShopHeaders } from './shopContext'

// 有赞平台服务（youzan/youzan_api.py，默认 :8030）。
// 与 xhsApi.js 保持同一套约定：显式传 shopId 时不覆盖 X-Shop-Id（见 shopContext.applyShopHeaders），
// 这样"店铺管理页逐店巡检""跨店发布按目标店取参数"都能复用。
const http = axios.create({
  baseURL: import.meta.env.VITE_YOUZAN_API_BASE_URL || '/youzan-api',
  timeout: 30000,
})

// 多店铺:每个请求都带上 X-Shop-Id / X-Operator(未选店铺时后端回退默认店)
http.interceptors.request.use((config) => applyShopHeaders(config))

http.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const detail = error.response?.data?.detail
    throw new Error(typeof detail === 'string' ? detail : error.message || '有赞请求失败')
  },
)

export const youzanApi = {
  health: () => http.get('/health'),
  // shopId 可选：不传用"当前选中店铺"，传了就用指定店铺（店铺管理页逐个店巡检用）
  tokenInfo: (shopId = '') => http.get('/token/info', { headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  // 自用型应用没有 code：入参可空，调用即"静默重取 access_token"
  // shopId 可选：店铺管理页逐店「重取凭证」时必须带，否则会重取了当前选中店铺的 token
  tokenByCode: (code = '', shopId = '') => http.post('/token/code', { code }, { headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  tokenRefresh: (shopId = '') => http.post('/token/refresh', null, { headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  // 连通性自检：凭证不对时这里最先报错
  shop: (shopId = '') => http.get('/shop', { headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  categories: (params = {}) => http.get('/categories', { params }),
  // shopId 可选：跨店发布时按目标店取该店的运费模板
  deliveryTemplates: (shopId = '') => http.get('/delivery-templates', { headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  uploadMaterial: (url) => http.post('/materials/upload', { url }),
  // 有赞商品接口只收素材 ID（image_ids），所以这里返回的是 image_id 而不是可访问 URL
  uploadMaterialFile: (filename, contentBase64) =>
    http.post('/materials/upload-file', { filename, content_base64: contentBase64 }, { timeout: 60000 }),
  createProduct: (body) => http.post('/products', body),
  listItems: (params = {}) => http.get('/items', { params }),
  // 按商家编码反查：ID 回填 / 重发去重靠它
  productByCode: (itemNo) => http.get('/products/custom', { params: { item_no: itemNo } }),
  skuByCode: (skuNo) => http.get('/skus/custom', { params: { sku_no: skuNo } }),
  getProduct: (itemId) => http.get(`/products/${itemId}`),
  updateProduct: (itemId, body) => http.put(`/products/${itemId}`, body),
  updateSku: (skuId, body) => http.put(`/skus/${skuId}`, body),
  updateQuantity: (skuId, body) => http.post(`/skus/${skuId}/quantity`, body),
  // listing: 1=上架 0=下架
  setListing: (itemId, listing) => http.post(`/products/${itemId}/listing`, { listing }),
}
