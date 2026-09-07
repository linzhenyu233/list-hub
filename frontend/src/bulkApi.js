import axios from 'axios'

const http = axios.create({ baseURL: import.meta.env.VITE_BULK_API_BASE_URL || '/bulk-api', timeout: 60000 })
http.interceptors.response.use((response) => response.data, (error) => {
  const detail = error.response?.data?.detail
  return Promise.reject(new Error(typeof detail === 'string' ? detail : error.message || '批量服务请求失败'))
})

export const bulkApi = {
  health: () => http.get('/health'),
  template: () => http.get('/template', { responseType: 'blob' }),
  importBatch: (body) => http.post('/import', body),
  getBatch: (id) => http.get(`/import/${id}`),
  updateItems: (id, items) => http.put(`/import/${id}/items`, { items }),
  updateMappings: (id, mappings) => http.put(`/import/${id}/mappings`, { mappings }),
  validate: (id) => http.post(`/import/${id}/validate`),
  publish: (id, platforms) => http.post(`/import/${id}/publish`, { platforms }),
  job: (id) => http.get(`/jobs/${id}`),
  retry: (id, itemIds = []) => http.post(`/jobs/${id}/retry`, { item_ids: itemIds }),
  categoryAliases: () => http.get('/category-aliases'),
  saveCategoryAlias: (body) => http.post('/category-aliases', body),
  deleteCategoryAlias: (id) => http.delete(`/category-aliases/${id}`),
  categoryAliasTemplate: () => http.get('/category-aliases/template', { responseType: 'blob' }),
  importCategoryAliases: (body) => http.post('/category-aliases/import', body, { timeout: 300000 }),
}
