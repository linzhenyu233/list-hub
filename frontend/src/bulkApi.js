import axios from 'axios'
import { applyShopHeaders, withShopQuery } from './shopContext'

const http = axios.create({ baseURL: import.meta.env.VITE_BULK_API_BASE_URL || '/bulk-api', timeout: 60000 })
// 多店铺:每个请求都带上 X-Shop-Id / X-Operator(未选店铺时后端回退默认店)
http.interceptors.request.use((config) => applyShopHeaders(config))
http.interceptors.response.use((response) => response.data, (error) => {
  const status = error.response?.status
  const detail = error.response?.data?.detail
  let message = typeof detail === 'string' ? detail : error.message || '批量服务请求失败'
  if (status === 413) {
    message = '导入文件过大，超过了服务器允许的请求体上限。请减少图片数量/体积后重试，或联系运维调大反向代理的 client_max_body_size'
  }
  // 500 且后端没给 detail = 未捕获异常(裸报错)，光看 "Request failed with status code 500" 没法排查，
  // 这里补一句明确的下一步，省得运营来回问。
  if (status === 500 && typeof detail !== 'string') {
    message = '中台内部错误（500，后端未返回具体原因），请把中台控制台的最后一段报错发给开发排查'
  }
  return Promise.reject(new Error(message))
})

export const bulkApi = {
  health: () => http.get('/health'),
  // 多店铺:可切换的店铺清单(后端已脱敏,不含密钥)。供顶栏店铺选择器使用。
  shops: () => http.get('/shops'),
  template: () => http.get('/template', { responseType: 'blob' }),
  importBatch: (body) => http.post('/import', body, { timeout: 600000 }),
  importHuopai: (path) => http.post('/import-huopai', { path: path || null }, { timeout: 300000 }),
  // 货盘表只允许从服务端配置的货盘目录读取, 这里列出该目录下的 .xlsx 供下拉选择
  huopaiFiles: () => http.get('/huopai-files'),
  getBatch: (id) => http.get(`/import/${id}`),
  // 每个商品已有哪些发布记录(跨批次, 按商品编码汇总): step3 标记"已发布/失败/未发布", 避免重复勾选。
  // 注意是按商品而不是按批次 —— 重新导入货盘会生成新批次, 按批次统计会把历史记录丢掉。
  // shopId 可选：不传用"当前选中店铺"；跨店发布后要看目标店的发布记录时显式传目标店
  publishStatus: (shopId = '') => http.get('/publish-status', { headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  // 核对发布状态: 把本地"已发布"记录拿去平台核一遍, 后台删掉的商品改回"未发布"以便重发。
  // mode=reset 表示人工确认已删除, 不请求平台(小红书查不出来时的兜底)。逐条查平台, 给足超时。
  // shopId 可选：跨店发布后核对的是目标店的记录（不传=当前选中店铺）
  verifyPublishStatus: (body, shopId = '') => http.post('/publish-status/verify', body, { timeout: 600000, headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  updateItems: (id, items) => http.put(`/import/${id}/items`, { items }),
  updateMappings: (id, mappings) => http.put(`/import/${id}/mappings`, { mappings }),
  validate: (id) => http.post(`/import/${id}/validate`),
  // 跨店发布：targetShopId 不传=当前店铺（同店发布，行为与改造前完全一致）；
  // params 是目标店的店铺私有参数（微信运费模板 / 小红书运费模板+物流方案+品牌），后端存成任务快照
  publish: (id, platforms, productCodes = null, targetShopId = '', params = null) => http.post(`/import/${id}/publish`, {
    platforms, product_codes: productCodes, target_shop_id: targetShopId || null, params,
  }),
  imageCacheStats: () => http.get('/image-cache/stats'),
  clearImageCache: () => http.delete('/image-cache'),
  // shopId 可选：跨店发布的任务属于目标店，查进度/重试失败项必须带目标店，否则 404
  job: (id, shopId = '') => http.get(`/jobs/${id}`, { headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  retry: (id, itemIds = [], shopId = '') => http.post(`/jobs/${id}/retry`, { item_ids: itemIds }, { headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
  // 回填商品ID到货盘表: 直接把平台生成的商品ID写进原货盘表(培育钻的「微信小店」「小红书产品id」列),
  // 不用运营照着导出结果一个个登记。dryRun=true 只统计要写哪些行、不落盘(前端据此弹确认框)。
  // 取值口径=按商品编码取最近一次发布成功的ID(不限本次任务/批次), 只填空白单元格;
  // 写入前后端会备份原文件, 自检通过才替换。
  writebackHuopai: (id, dryRun = false, shopId = '') => http.post(`/jobs/${id}/writeback-huopai`, { dry_run: dryRun }, { timeout: 600000, headers: shopId ? { 'X-Shop-Id': shopId } : undefined }),
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
  // 预览服务端图片: disk:/local:// 是服务端路径, 浏览器渲染不了, 走这个接口转成可显示 URL。
  // ⚠️ 这是浏览器自己发的 <img> 请求, 带不了 X-Shop-Id 自定义头, 所以用 query 传店铺 ——
  //    否则切到别的店铺后, 图片白名单还按默认店校验, 预览会 404。
  imagePreviewUrl: (ref) => withShopQuery(`${import.meta.env.VITE_BULK_API_BASE_URL || '/bulk-api'}/images/preview?ref=${encodeURIComponent(ref || '')}`),
  // 数据面板：订单日报指标（按「天 × 平台」，可选按月/周归并）。纯读本地表，不打平台，切日期很快。
  // platform 可选（wechat / xhs），granularity 可选（day / week / month）。
  // 返回里除 rows/totals 外还带 prev_totals（上一周期）与 daily（按天原始值，用于画趋势图）。
  // shop：数据面板**自己的**店铺范围（'all'=全部店铺汇总；不传=跟随当前店铺）。
  // 与顶栏的当前店铺解耦，所以单独作为参数传，不走 X-Shop-Id。
  orderMetrics: (start = '', end = '', platform = '', granularity = 'day', shop = '') => {
    const params = {}
    if (start) params.start = start
    if (end) params.end = end
    if (platform) params.platform = platform
    if (granularity) params.granularity = granularity
    if (shop) params.shop = shop
    return http.get('/orders/metrics', { params })
  },

  // 数据面板导出 Excel（汇总 + 各平台日销售 + 各平台交易清单 + 口径说明）。
  // 交易清单要逐笔实时拉平台接口（小红书补详情很慢），所以给足超时。
  orderMetricsExport: (start = '', end = '', platform = '', granularity = 'day', shop = '') => {
    const params = {}
    if (start) params.start = start
    if (end) params.end = end
    if (platform) params.platform = platform
    if (granularity) params.granularity = granularity
    if (shop) params.shop = shop
    return http.get('/orders/metrics/export', { params, responseType: 'blob', timeout: 300000 })
  },
  // 商品明细：把总览的支付金额/件数按**单个商品**拆开（数据面板下方那块）。
  // 金额按订单实付等比分摊到商品，所以各商品之和 = 总览支付金额；退款暂未拆到商品。
  orderProductMetrics: (start = '', end = '', platform = '', shop = '',
                        keyword = '', sort = 'amount', limit = 50, offset = 0) => {
    const params = {}
    if (start) params.start = start
    if (end) params.end = end
    if (platform) params.platform = platform
    if (shop) params.shop = shop
    if (keyword) params.keyword = keyword
    if (sort) params.sort = sort
    params.limit = limit
    params.offset = offset
    return http.get('/orders/product-metrics', { params })
  },

  // 商品头图（商品维度列表用；后端带本地缓存，同一商品只调一次平台接口）
  orderProductImage: (platform, productId, shop = '', shopId = '') => http.get('/orders/product-image', {
    params: { platform, product_id: productId, shop, shop_id: shopId }
  }),

  // 手动同步订单指标：拉平台订单/售后算指标后落库（默认「昨天+今天」，T+1 补拉）。
  // 要逐单查平台详情，慢一些，所以给足超时。
  orderSync: (body = {}) => http.post('/orders/sync', body, { timeout: 900000 }),

  // 同步进度：POST /orders/sync 现在只登记任务、立即返回，进度轮询这个接口。
  // 不传 jobId 时后端返回"最近一个任务"（同步中刷新页面也能接着看进度）。
  orderSyncStatus: (jobId = '') => http.get('/orders/sync/status', { params: jobId ? { job_id: jobId } : {} }),

  // 商品明细：某一天的**订单级**明细（单日，不是区间）。
  // 后端不落订单明细表，这里是实时调平台接口（带 5 分钟缓存），小红书还要逐单补详情，
  // 所以给足超时；否则用默认 60s 会在订单多的时候先超时。
  orderDetails: (date = '', platform = '', shop = '', keyword = '', limit = 20, offset = 0) => {
    const params = {}
    if (date) params.date = date
    if (platform) params.platform = platform
    if (shop) params.shop = shop
    if (keyword) params.keyword = keyword
    params.limit = limit
    params.offset = offset
    return http.get('/orders/details', { params, timeout: 180000 })
  },
}
