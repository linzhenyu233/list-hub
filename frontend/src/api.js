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
  // 直接上传本地图片：前端读成 base64 → 微信二进制上传接口 → 返回可发品的图片地址，
  // 运营不用再自己找公网图片地址（与批量发布里的补图/换图同一条链路）。
  uploadImageFile: (filename, contentBase64) => http.post('/images/upload-file', { filename, content_base64: contentBase64 }),
  // shopId 可选：不传用"当前选中店铺"，传了就用指定店铺（跨店发布要按目标店取该店运费模板）
  freightTemplates: (params = {}, shopId = '') => http.get('/freight-templates', { params, headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  // 按品牌 ID 查品牌名（编辑商品时把 10002926 显示成「钻石世家」）。一次请求即可，无需翻页。
  brandDetail: (brandId) => http.get('/brand', { params: { brand_id: brandId }, timeout: 60000 }),
  // 微信品牌列表。
  // ⚠️ 当前前端**没有使用**它：微信品牌库上万条，而接口只能游标翻页、每页 10 条
  //    （实测翻 40 页取 400 个耗时 58 秒，仍找不到本店在用的品牌），做不了下拉。
  //    编辑商品页已改为「输入框 + 可读提示」（见 ProductForm.vue 的 brandHint）。
  //    保留此方法以备后用（例如微信后续开放按名称搜索品牌）。
  brands: () => http.get('/brands', { timeout: 180000 }),
  categoryDetail: (catId) => http.get('/category-detail', { params: { cat_id: catId } }),
  listProducts: (params = {}) => http.get('/products', { params }),
  getProduct: (id) => http.get(`/products/${id}`),
  createProduct: (product) => http.post('/products', product),
  updateProduct: (id, product) => http.post(`/products/${id}/update`, product),
  listingProduct: (id) => http.post(`/products/${id}/listing`),
  delistingProduct: (id) => http.post(`/products/${id}/delisting`),
  deleteProduct: (id) => http.delete(`/products/${id}`),
}
