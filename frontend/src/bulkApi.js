import axios from 'axios'

const http = axios.create({ baseURL: import.meta.env.VITE_BULK_API_BASE_URL || '/bulk-api', timeout: 60000 })
http.interceptors.response.use((response) => response.data, (error) => {
  const status = error.response?.status
  const detail = error.response?.data?.detail
  let message = typeof detail === 'string' ? detail : error.message || '批量服务请求失败'
  if (status === 413) {
    message = '导入文件过大，超过了服务器允许的请求体上限。请减少图片数量/体积后重试，或联系运维调大反向代理的 client_max_body_size'
  }
  return Promise.reject(new Error(message))
})

export const bulkApi = {
  health: () => http.get('/health'),
  template: () => http.get('/template', { responseType: 'blob' }),
  importBatch: (body) => http.post('/import', body, { timeout: 600000 }),
  importHuopai: (path) => http.post('/import-huopai', { path: path || null }, { timeout: 300000 }),
  // 货盘表只允许从服务端配置的货盘目录读取, 这里列出该目录下的 .xlsx 供下拉选择
  huopaiFiles: () => http.get('/huopai-files'),
  getBatch: (id) => http.get(`/import/${id}`),
  updateItems: (id, items) => http.put(`/import/${id}/items`, { items }),
  updateMappings: (id, mappings) => http.put(`/import/${id}/mappings`, { mappings }),
  validate: (id) => http.post(`/import/${id}/validate`),
  publish: (id, platforms, productCodes = null) => http.post(`/import/${id}/publish`, { platforms, product_codes: productCodes }),
  imageCacheStats: () => http.get('/image-cache/stats'),
  clearImageCache: () => http.delete('/image-cache'),
  job: (id) => http.get(`/jobs/${id}`),
  retry: (id, itemIds = []) => http.post(`/jobs/${id}/retry`, { item_ids: itemIds }),
  categoryAliases: () => http.get('/category-aliases'),
  saveCategoryAlias: (body) => http.post('/category-aliases', body),
  deleteCategoryAlias: (id) => http.delete(`/category-aliases/${id}`),
  categoryAliasTemplate: () => http.get('/category-aliases/template', { responseType: 'blob' }),
  importCategoryAliases: (body) => http.post('/category-aliases/import', body, { timeout: 300000 }),
  // 图片直读: 扫描共享盘目录, 按编码把商品图/SKU 图挂到批次上(不走浏览器上传, 几百个商品也快)
  scanImages: (id, body) => http.post(`/batches/${id}/images/scan`, body, { timeout: 900000 }),
  // 补规格图: 给某个 SKU 单张上传(返回 local:// 引用, 前端写回该行后 updateItems 持久化)
  uploadSkuImage: (id, body) => http.post(`/batches/${id}/sku-image`, body, { timeout: 120000 }),
  // 服务端允许扫描的图片根目录白名单(前端只做下拉选择, 不能自由填任意路径)
  imageRoots: () => http.get('/image-roots'),
  // 预览服务端图片: disk:/local:// 是服务端路径, 浏览器渲染不了, 走这个接口转成可显示 URL
  imagePreviewUrl: (ref) => `${import.meta.env.VITE_BULK_API_BASE_URL || '/bulk-api'}/images/preview?ref=${encodeURIComponent(ref || '')}`,
}
