import axios from 'axios'
import { applyShopHeaders } from './shopContext'

const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  // 微信商品列表接口很慢(每个商品带全部图片/SKU,20 条约 28 秒),
  // 原 30 秒超时刚好卡在边界,偶发超时会导致列表被清空成"暂无商品数据"。
  timeout: 120000,
})

// 多店铺:每个请求都带上 X-Shop-Id / X-Operator(未选店铺时后端回退默认店)
http.interceptors.request.use((config) => applyShopHeaders(config))

http.interceptors.response.use(
  (response) => response.data,
  (error) => {
    if (error.code === 'ECONNABORTED') {
      return Promise.reject(new Error('请求超时：微信接口响应较慢，请稍后重试，或把每页条数调小（10 条约 14 秒）'))
    }
    const detail = error.response?.data?.detail
    const message = typeof detail === 'string' ? detail : error.message || '请求失败'
    return Promise.reject(new Error(message))
  },
)

export const storeApi = {
  health: () => http.get('/health'),
  // shopId 可选：不传用"当前选中店铺"，传了就用指定店铺（店铺管理页逐店检查用）
  testToken: (shopId = '') => http.post('/token/test', null, { headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  searchCategories: (keyword) => http.get('/categories', { params: { keyword } }),
  uploadImage: (imgUrl) => http.post('/images/upload', { img_url: imgUrl }),
  freightTemplates: (params = {}) => http.get('/freight-templates', { params }),
  categoryDetail: (catId) => http.get('/category-detail', { params: { cat_id: catId } }),
  listProducts: (params = {}) => http.get('/products', { params }),
  getProduct: (id) => http.get(`/products/${id}`),
  createProduct: (product) => http.post('/products', product),
  updateProduct: (id, product) => http.post(`/products/${id}/update`, product),
  listingProduct: (id) => http.post(`/products/${id}/listing`),
  delistingProduct: (id) => http.post(`/products/${id}/delisting`),
  deleteProduct: (id) => http.delete(`/products/${id}`),
}
