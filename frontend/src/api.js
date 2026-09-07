import axios from 'axios'

const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  timeout: 30000,
})

http.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const detail = error.response?.data?.detail
    const message = typeof detail === 'string' ? detail : error.message || '请求失败'
    return Promise.reject(new Error(message))
  },
)

export const storeApi = {
  health: () => http.get('/health'),
  testToken: () => http.post('/token/test'),
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
