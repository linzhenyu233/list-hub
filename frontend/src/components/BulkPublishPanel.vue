<script setup>
import { computed, nextTick, onBeforeUnmount, onDeactivated, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, Check, Download, Upload, Refresh, Right, Search } from '@element-plus/icons-vue'
import { bulkApi } from '../bulkApi'
import { xhsApi } from '../xhsApi'
import { storeApi } from '../api'
import { currentShop, currentShopId, shops } from '../shopContext'
import CategoryAliasPanel from './CategoryAliasPanel.vue'
import ProductReviewDrawer from './ProductReviewDrawer.vue'

// 本页没有其它出口按钮,由父级 App.vue 监听 back 事件切回商品列表
const emit = defineEmits(['back'])
const step = ref(0)
const file = ref(null)
// 原生 input[type=file] 太丑:隐藏它,用 Element Plus 按钮触发,并把文件名单独展示
const excelInput = ref(null)
function pickExcelFile() {
  const input = excelInput.value
  if (!input) return
  input.value = ''   // 允许重复选同一个文件
  input.click()
}
const folderFiles = ref([])
const commonDetailFiles = ref([])
// 图片直读(共享盘): root 只能从服务端白名单里选(见 GET /image-roots); months 为空=扫全部月份
const imageRoot = ref('')
const imageRootOptions = ref([])
const imageMonths = ref([])
const IMAGE_MONTH_OPTIONS = ['1-3月', '4-6月份', '7-9月份', '培育裸钻']
// 货盘直读: 只列出服务端货盘目录(HUOPAI_DIR)下的 .xlsx, 前端不提供自由填写路径的入口
const huopaiFiles = ref([])
const huopaiPath = ref('')
const imageScanResult = ref(null)
const scanning = ref(false)
const loading = ref(false)
const productTableRef = ref(null)
const batch = ref(null)
// 某店铺配置了哪些平台（有凭证即视为已配置，与后端 has_platform 判定一致）
function shopPlatforms(shop) {
  const list = []
  if (shop?.wechat?.appid) list.push('wechat')
  if (shop?.xhs?.app_id) list.push('xhs')
  return list
}
const PLATFORM_LABELS = { wechat: '微信', xhs: '小红书' }
function platformLabel(platform) { return PLATFORM_LABELS[platform] || platform }
function shopPlatformText(shop) { return shopPlatforms(shop).map(platformLabel).join('、') || '无平台' }
// 页面上涉及平台的文案/列，一律只提当前要发的平台，免得单平台店里出现另一半没用的信息
const platformNameText = computed(() => availablePlatforms.value.map(platformLabel).join('/') || '平台')

// ------------------------------------------------------------------
// 跨店发布：批次与映射属于「来源店」(顶部选中的店)，但可以改成发到另一家店。
// 目标店铺 = 任务实际发到哪家店，决定用谁的凭证 + 谁的店铺参数。
// ⚠️ 只允许同平台跨店：类目/属性/SKU规格是平台级的可以复用，
//    而微信批次的映射发到小红书根本不成立；运费模板/物流方案/品牌是按店不同的，必须按目标店重选。
// ------------------------------------------------------------------
const targetShopId = ref(currentShopId.value || '')
const targetShop = computed(
  () => shops.value.find((item) => item.shop_id === (targetShopId.value || currentShopId.value)) || currentShop.value,
)
const crossShop = computed(() => Boolean(targetShopId.value) && targetShopId.value !== currentShopId.value)
// 可选目标店：来源店自己 + 与来源店有交集平台的其他启用店
const targetShopOptions = computed(() => (shops.value || []).filter((shop) => {
  if (shop.enabled === false) return false
  if (shop.shop_id === currentShopId.value) return true
  const sourcePlatforms = shopPlatforms(currentShop.value)
  if (!sourcePlatforms.length) return true
  return shopPlatforms(shop).some((platform) => sourcePlatforms.includes(platform))
}))

// 可发布平台 = 来源店与目标店的平台交集（同店时就是该店自己的平台）
const availablePlatforms = computed(() => {
  const source = currentShop.value
  const target = targetShop.value
  if (!source || !target) return ['wechat', 'xhs']
  const targetList = shopPlatforms(target)
  if (!targetList.length) return ['wechat', 'xhs']
  if (target.shop_id === source.shop_id) return targetList
  const shared = shopPlatforms(source).filter((platform) => targetList.includes(platform))
  return shared.length ? shared : targetList
})
const platforms = ref([...availablePlatforms.value])
watch(availablePlatforms, (list) => {
  const kept = platforms.value.filter((platform) => list.includes(platform))
  platforms.value = kept.length ? kept : [...list]
})
const mapping = ref({ mode: 'auto', products: {} })
const settingsLoading = ref(false)
const categoryMatching = ref(false)
const xhsCascade = ref({})
const aliasPanelVisible = ref(false)
const itemKeyword = ref('')
const itemErrorFilter = ref('all')
const itemPage = ref(1)
const itemPageSize = ref(20)
const mappingPage = ref(1)
const mappingPageSize = ref(20)
const platformSettings = ref({ xhsShipping: [], xhsLogistics: [], wechatBrand: '平台无品牌', wechatDelivery: '快递发货' })
const batchPlatformSettings = reactive({ wechatFreightId: '', xhsShippingId: '', xhsLogisticsId: '' })
// 跨店发布的「目标店参数」：同店发布时不使用（走上面的 batchPlatformSettings，行为与改造前一致）
const targetShopSettings = reactive({ wechatFreightId: '', xhsShippingId: '', xhsLogisticsId: '' })
const targetPlatformSettings = ref({ wechatFreight: [], xhsShipping: [], xhsLogistics: [] })
const targetSettingsLoading = ref(false)
const targetSettingsCache = {}   // shop_id -> { settings, picked }，切回同一个店不必重拉
const jobShopId = ref('')        // 当前进度/重试对应的任务属于哪家店（跨店发布后是目标店）
const groupAttrDefs = reactive({})
const attrConfigLoading = ref(false)
const attrDrawerVisible = ref(false)
const attrDrawerGroup = ref('')
function openAttrDrawer(key) {
  attrDrawerGroup.value = key
  attrDrawerVisible.value = true
  // 候选值不再持久化,打开抽屉时按需补拉(用户已确认:稍等可接受)
  void ensureGroupCandidates(key)
}
// 本组件在 App.vue 中被 <KeepAlive> 缓存:切走再切回时,若抽屉仍停留在"已打开"状态,
// 再次点击配置属性会把 true 再赋一次 true(无变化),表现为"点击没反应"。失活时强制关掉。
onDeactivated(() => { attrDrawerVisible.value = false })

// 按需确保某类目组的候选值已加载(供抽屉/编辑使用),已加载的跳过
const _candidatesLoading = new Set()
async function ensureGroupCandidates(key) {
  const config = groupAttrDefs[key]
  if (!config?.xhs) return
  const xid = (mappingGroups.value.find((g) => g.internal_category === key) || {}).mapping?.xhs_category_id
    || config.xhs.category_id || ''
  if (!xid) return
  const defs = config.xhs.attr_defs || []
  const missing = defs.filter((d) => !config.xhs.candidates[d.id] || !config.xhs.candidates[d.id].length)
  if (!missing.length) return
  // 去重:同一类目并发打开时只拉一次
  const lockKey = `${key}:${xid}`
  if (_candidatesLoading.has(lockKey)) return
  _candidatesLoading.add(lockKey)
  try {
    await Promise.all(missing.map(async (d) => {
      try {
        const v = await xhsApi.attributeValues(xid, d.id || d.propertyId)
        config.xhs.candidates[d.id] = listFrom(v, ['attributeValueV3s', 'values'])
      } catch { config.xhs.candidates[d.id] = [] }
    }))
  } finally {
    _candidatesLoading.delete(lockKey)
  }
}
const attrMatchStatus = reactive({})
// 有未匹配属性的类目数(用于工具栏提示)
const unmatchedCategoryCount = computed(() => Object.values(attrMatchStatus).filter((s) => (s?.unmatched || 0) > 0).length)
const reviewVisible = ref(false)
const reviewProduct = ref(null)
const currentJob = ref(null)
const retrying = ref(false)
let jobTimer = null
function clearJobProgress() {
  if (jobTimer) { clearTimeout(jobTimer); jobTimer = null }
  currentJob.value = null
}
watch(step, (value) => {
  if (value < 3) clearJobProgress()
  if (value !== 3) clearSelection()
  // 进入确认发布时拉一次已有发布记录: 标记哪些商品已经发过, 避免重复勾选;
  // 同时按目标店初始化参数（同店时沿用第 3 步已选好的，跨店则去拉目标店的模板/物流方案）
  if (value === 3) { void loadPublishStatus(); void loadTargetSettings() }
})
const steps = ['导入文件', '校验与编辑', '平台映射', '确认发布']
const allItems = computed(() => batch.value?.items || [])
const items = computed(() => pagedItems.value)
const products = computed(() => batch.value?.products || [])
const validCount = computed(() => allItems.value.filter((item) => !(item.errors || []).length).length)
const errorCount = computed(() => allItems.value.length - validCount.value)
// 缺规格图的 SKU 数:校验可能照样通过, 但要能单独筛出来集中补图
const missingImageCount = computed(() => allItems.value.filter((item) => !item.sku_image).length)
// 商品级主图索引: 商品编码 -> 该商品已有主图(取任意一行里第一份非空值)。
// ⚠️ 主图是商品级(同一商品各行共用一套图),而 Excel 只要求首行填写、图片扫描也只在空行回填,
//   所以"首行 3 张图、第 2~n 行为空"是常态。判断缺不缺主图必须按**商品**,不能按行:
//   按行判断会让次行误报"缺主图"(假警报),点补图还会把首行已有的图清掉(数据丢失)。
const productMainImages = computed(() => {
  const map = {}
  for (const item of allItems.value) {
    const code = item.product_code
    if (!code) continue
    const images = item.main_images || []
    if (!map[code] || (!map[code].length && images.length)) map[code] = images
  }
  return map
})
// 缺主图按**商品**去重计数(不是行数): 同一商品缺图只算 1 个,标签写"缺主图 N 个商品"
const missingMainImageCount = computed(
  () => Object.values(productMainImages.value).filter((images) => !images.length).length,
)
// 存在 SKU 间商品属性不一致提醒的商品数（不阻断发布，仅提醒）
const productWarningCount = computed(() => products.value.filter((product) => (product.warnings || []).length).length)
const filteredItems = computed(() => {
  const keyword = itemKeyword.value.trim().toLowerCase()
  return allItems.value.filter((item) => {
    const matchesKeyword = !keyword || [item.product_code, item.title, item.sku_code]
      .some((value) => String(value || '').toLowerCase().includes(keyword))
    const hasError = (item.errors || []).length > 0
    const missingImage = !item.sku_image
    // 按商品判断(与表格入口一致): 该商品任意一行有主图就不算缺,避免次行被误筛出来
    const missingMainImage = !(productMainImages.value[item.product_code] || []).length
    const matchesFilter = itemErrorFilter.value === 'all'
      || (itemErrorFilter.value === 'error' && hasError)
      || (itemErrorFilter.value === 'valid' && !hasError)
      || (itemErrorFilter.value === 'no_image' && missingImage)
      || (itemErrorFilter.value === 'no_main_image' && missingMainImage)
    return matchesKeyword && matchesFilter
  })
})
const pagedItems = computed(() => {
  const start = (itemPage.value - 1) * itemPageSize.value
  return filteredItems.value.slice(start, start + itemPageSize.value)
})
// step3「确认发布」表格独立分页:800 行全量渲染会卡,只渲染当前页
const productPage = ref(1)
const productPageSize = ref(50)
// step3 筛选:250 件商品里只想发一部分时, 要能按关键词/发布状态快速找出来
const productKeyword = ref('')
const productPublishFilter = ref('all')
const publishStatusLoading = ref(false)
// 每个商品已有的发布记录(跨批次, 按商品编码汇总):
// {商品编码: {platforms: {平台: {状态: 条数}}, last_success_at: ISO}}
const publishStatus = ref({})
async function loadPublishStatus() {
  publishStatusLoading.value = true
  try {
    // 跨店发布时按**目标店**查：发过 A 店不代表发过 B 店，用来源店的记录会把没发的商品误标成"已发布"
    const data = await bulkApi.publishStatus(crossShop.value ? targetShopId.value : '')
    publishStatus.value = data?.result || {}
  } catch (error) {
    publishStatus.value = {}
  } finally { publishStatusLoading.value = false }
}
// 汇总一个商品在「本次要发布的平台」上的情况:
//   success=已发出过 / pending=正在发 / failed=只失败过 / none=还没发过
// 只看当前勾选的平台: 只发过小红书、这次要发微信时, 应显示"未发布"而不是"已发布"
// deleted(核对后确认平台已删除)和 cancelled 都不算"发过", 落到 none 才能重新发布
function productPublishState(code) {
  const entry = publishStatus.value[code]
  if (!entry) return 'none'
  const byPlatform = entry.platforms || entry
  const flat = {}
  platforms.value.forEach((platform) => {
    Object.entries(byPlatform[platform] || {}).forEach(([key, count]) => {
      flat[key] = (flat[key] || 0) + count
    })
  })
  if (flat.success || flat.partial) return 'success'
  if (flat.queued || flat.running) return 'pending'
  if (flat.failed) return 'failed'
  return 'none'
}
// 核对发布状态: 运营会在平台后台把商品删掉, 但本地记录还是"已发布",
// 于是这里既不准确、又会撞上发布接口的幂等保护(同批次同商品已有记录就不再创建)导致发不出去。
// 核对范围: 勾选了就用勾选的, 没勾用当前筛选结果(通常先用「仅看已发布」筛出来再核对)。
const verifyingPublishStatus = ref(false)
function verifyTargetCodes() {
  if (selectedProductCodes.value.size) return [...selectedProductCodes.value]
  return filteredProducts.value.map((product) => product.product_code)
}
async function verifyPublishStatus(mode = 'verify') {
  if (verifyingPublishStatus.value) return
  const codes = verifyTargetCodes()
  if (!codes.length) return ElMessage.warning('没有可核对的商品')
  const tip = mode === 'reset'
    ? `确认把 ${codes.length} 件商品标记为"平台已删除"（会改回未发布，可重新发布）？`
    : `将逐件向平台核对这 ${codes.length} 件商品的发布记录，商品多的会慢一些，继续？`
  try {
    await ElMessageBox.confirm(tip, mode === 'reset' ? '人工标记为未发布' : '核对发布状态', { type: 'warning' })
  } catch { return }
  verifyingPublishStatus.value = true
  try {
    const data = await bulkApi.verifyPublishStatus(
      { product_codes: codes, mode },
      crossShop.value ? targetShopId.value : '',
    )
    const result = data?.result || {}
    await loadPublishStatus()
    if (result.deleted) ElMessage.success(`核对了 ${result.checked} 条记录，${result.deleted} 件在平台已删除，已改回"未发布"，可以直接重发`)
    else if (result.checked) ElMessage.info(`核对了 ${result.checked} 条记录，平台商品都还在`)
    if (result.unknown) ElMessage.warning(`${result.unknown} 条查不动（网络或权限问题），状态保持不变；确认已删掉的可再用「标记未发布」处理`)
  } catch (error) { ElMessage.error(error.message) } finally { verifyingPublishStatus.value = false }
}
// 最近一次成功发布的时间(后端给的是带时区的 ISO, new Date 会转成本机时区显示)
function productPublishedAt(code) {
  return publishStatus.value[code]?.last_success_at || ''
}
function formatPublishTime(iso) {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (value) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
const publishCounts = computed(() => {
  const acc = { success: 0, failed: 0, pending: 0, none: 0 }
  products.value.forEach((product) => { acc[productPublishState(product.product_code)] += 1 })
  return acc
})
const filteredProducts = computed(() => {
  const keyword = productKeyword.value.trim().toLowerCase()
  return products.value.filter((product) => {
    const matchesKeyword = !keyword || [product.product_code, product.title, product.internal_category]
      .some((value) => String(value || '').toLowerCase().includes(keyword))
    const matchesState = productPublishFilter.value === 'all'
      || productPublishFilter.value === productPublishState(product.product_code)
    return matchesKeyword && matchesState
  })
})
const pagedProducts = computed(() => {
  const start = (productPage.value - 1) * productPageSize.value
  return filteredProducts.value.slice(start, start + productPageSize.value)
})
function resetProductPage() { productPage.value = 1 }
// 已选商品里"已经发布成功"的那些: 再发会被平台以"标题重复"拒, 要能一眼看到并一键剔除
const selectedPublished = computed(() =>
  [...selectedProductCodes.value].filter((code) => productPublishState(code) === 'success'))
function dropPublishedSelection() {
  const next = new Set(selectedProductCodes.value)
  selectedPublished.value.forEach((code) => next.delete(code))
  selectedProductCodes.value = next
  nextTick(syncTableSelection)
}
const allFilteredSelected = computed(() => {
  const target = filteredProducts.value
  return target.length > 0 && target.every((product) => selectedProductCodes.value.has(product.product_code))
})
// step3 分批发布: 选中商品集(跨页保留)
const selectedProductCodes = ref(new Set())
function onSelectionChange(selection) {
  // 每次 selection 都是当前页被选中的行, 我们把它合并进全局集合
  const pageCodes = new Set(pagedProducts.value.map((p) => p.product_code))
  const selectedCodes = new Set(selection.map((p) => p.product_code))
  // 先移除当前页所有旧的,再加入当前页新选中的(正确反映反选)
  for (const code of pageCodes) selectedProductCodes.value.delete(code)
  for (const code of selectedCodes) selectedProductCodes.value.add(code)
  // 触发响应式
  selectedProductCodes.value = new Set(selectedProductCodes.value)
}
function toggleSelectAllFiltered() {
  // 只作用于「当前筛选结果」: 想发一部分商品时, 先筛出来再全选, 不会顺手把 250 件全勾上
  const target = filteredProducts.value.map((product) => product.product_code)
  const allSelected = target.length > 0 && target.every((code) => selectedProductCodes.value.has(code))
  const next = new Set(selectedProductCodes.value)
  target.forEach((code) => (allSelected ? next.delete(code) : next.add(code)))
  selectedProductCodes.value = next
  nextTick(syncTableSelection)
}
function clearSelection() { selectedProductCodes.value = new Set() }
// 程序化改选(全选全部/清空)后, 把当前页复选框同步到集合状态,
// 否则 el-table 的勾选态会与 selectedProductCodes 不一致(发布数量对但界面误导)
function syncTableSelection() {
  const table = productTableRef.value
  if (!table) return
  const codes = selectedProductCodes.value
  pagedProducts.value.forEach((row) => table.toggleRowSelection(row, codes.has(row.product_code)))
}
function resetItemPage() { itemPage.value = 1 }
const mappingRows = computed(() => products.value.map((product) => ({
  ...product,
  mapping: {
    ...(mapping.value.products?.[product.product_code] || {}),
    status: [mapping.value.products?.[product.product_code]?.status, mapping.value.products?.[product.product_code]?.xhs_status].filter(Boolean).join('；') || '待系统匹配',
  },
})))
const mappingGroups = computed(() => {
  const groups = new Map()
  for (const row of mappingRows.value) {
    const key = row.internal_category || '(未填写内部类目)'
    if (!groups.has(key)) groups.set(key, { internal_category: key, products: [], mapping: row.mapping })
    groups.get(key).products.push(row.product_code)
  }
  return [...groups.values()]
})
// 「类目属性匹配」列表:类目一多,逐条卡片堆叠会非常占高度、很难扫。
// 改成可搜索/可筛选的精简表格(内部滚动),未匹配数多的排前面,问题类目一眼可见。
const attrGroupKeyword = ref('')
const attrGroupFilter = ref('all')
// 类目属性概览：列出**全部**内部类目，不再要求"已加载属性定义"。
// ⚠️ 原来只保留有属性定义的分组，而属性定义只在"平台类目已匹配"之后才会加载，
//   于是还没匹配上类目的批次（典型：刚拆出来的小红书店）整张表直接消失，
//   运营连"这批货有几个类目、各多少件"都看不到，只剩一屏待确认类目。
const attrGroupTotal = computed(() => mappingGroups.value.length)
const attrGroupRows = computed(() => {
  const keyword = attrGroupKeyword.value.trim().toLowerCase()
  return mappingGroups.value
    .map((group) => {
      const status = attrMatchStatus[group.internal_category]
      const mapped = group.mapping || {}
      // 是否已匹配到本店平台的类目：没匹配上就没有属性定义可加载，
      // 界面要给出明确文案，别让运营误以为"加载失败"。
      const has_category = availablePlatforms.value.includes('wechat')
        ? !!(mapped.wechat_category_chain || []).length
        : !!mapped.xhs_category_id
      return {
        internal_category: group.internal_category,
        product_count: group.products.length,
        matched: status?.matched || 0,
        unmatched: status?.unmatched || 0,
        has_status: !!status,
        has_category,
      }
    })
    .filter((row) => !keyword || row.internal_category.toLowerCase().includes(keyword))
    .filter((row) => attrGroupFilter.value !== 'unmatched' || row.unmatched > 0)
    .sort((a, b) => b.unmatched - a.unmatched || a.internal_category.localeCompare(b.internal_category))
})
// step2「平台映射」主表格独立分页：几百个商品时只渲染当前页，避免页面上下滚动
const pagedMappingRows = computed(() => {
  const start = (mappingPage.value - 1) * mappingPageSize.value
  return mappingRows.value.slice(start, start + mappingPageSize.value)
})
function resetMappingPage() { mappingPage.value = 1 }
function confirmWechatCategory(group, value) {
  const candidate = group.mapping.wechat_candidates?.find((item) => item.path === value)
  if (!candidate) return
  for (const code of group.products) {
    const current = mapping.value.products[code] || {}
    mapping.value.products[code] = { ...current, wechat_category: candidate.path, wechat_category_chain: candidate.chain, status: '微信类目已人工确认' }
  }
  void saveGroupAlias(group.internal_category, { wechat: { category: candidate.path, chain: candidate.chain || [], attr_defaults: groupAttrDefs[group.internal_category]?.wechat?.defaults || {} } })
}
async function loadXhsLevel(group, level, parentId = null) {
  const data = await xhsApi.categories(parentId ? { category_id: parentId } : {})
  const result = data?.result || data || []
  const options = Array.isArray(result) ? result : (result.categoryV3s || result.categories || [])
  const state = xhsCascade.value[group.internal_category] || { levels: [[], [], []], selected: [] }
  state.levels[level] = options
  state.levels.splice(level + 1)
  state.selected.splice(level)
  xhsCascade.value = { ...xhsCascade.value, [group.internal_category]: state }
}
async function confirmXhsCategory(group, level, value) {
  const state = xhsCascade.value[group.internal_category]
  state.selected[level] = value
  const item = state.levels[level].find((row) => String(row.id || row.categoryId) === String(value))
  if (!item) return
  // 非叶子类目继续加载下一级,不写死 3 层,适配任意深度类目树
  if (!(item.isLeaf || item.leaf)) { await loadXhsLevel(group, level + 1, value); return }
  const chain = state.selected.slice(0, level + 1).map((id, index) => { const row = state.levels[index].find((option) => String(option.id || option.categoryId) === String(id)); return { id, name: row?.name || '' } })
  for (const code of group.products) mapping.value.products[code] = { ...(mapping.value.products[code] || {}), xhs_category: chain.map((row) => row.name).join(' > '), xhs_category_chain: chain, xhs_category_id: value, xhs_status: '小红书类目已人工确认' }
  xhsCascade.value = { ...xhsCascade.value, [group.internal_category]: state }
  void saveGroupAlias(group.internal_category, { xhs: { category: chain.map((row) => row.name).join(' > '), chain, category_id: value, attr_defaults: groupAttrDefs[group.internal_category]?.xhs?.defaults || {}, spec_map: groupAttrDefs[group.internal_category]?.xhs?.spec_map || {} } })
}

// 类目别名映射表:人工确认一次存表,后续批次自动命中不再重复选;后端合并微信/小红书两侧,这里只传本次确认的一侧即可

// 内部类目在 Excel 里全角/半角括号混用（「耳饰(对)」vs「耳饰（对）」），
// 直接按原字符串查别名会出现"看起来一模一样却匹配不上"的怪事，
// 所以比对前统一括号、去掉半角与全角空白。
function normalizeCategoryKey(value) {
  return String(value || '')
    .replace(/（/g, '(')
    .replace(/）/g, ')')
    .replace(/[\s\u3000]/g, '')
}

async function saveGroupAlias(internalCategory, payload) {
  try {
    await bulkApi.saveCategoryAlias({ internal_category: internalCategory, ...payload })
    ElMessage.success('已存为类目映射，下次同内部类目将自动命中')
  } catch { /* 别名保存失败不影响本次确认 */ }
}

function listFrom(data, keys) {
  const result = data?.result || data || {}
  for (const key of keys) if (Array.isArray(result[key])) return result[key]
  return Array.isArray(result) ? result : []
}

function optionName(item) { return item.name || item.templateName || item.template_name || item.planName || item.planInfoName || item.title || item.id || '--' }
function settingId(item) { return String(item.id || item.templateId || item.template_id || item.planInfoId || item.logisticsPlanId || '') }

// 按指定店铺拉「店铺私有参数」下拉（运费模板/物流方案）。shopId 为空 = 当前选中店铺。
// 拆店后一家店只属于一个平台：只拉该店「已配置」的平台，且各平台互不牵连。
// ⚠️ 绝不能把微信/小红书接口塞进同一个裸 Promise.all —— 小红书店没有微信配置，
//   微信接口必然报错，一报错整个 all 就 reject，连本店能读到的运费模板/物流方案也一起清空。
async function fetchShopPlatformSettings(shopId = '') {
  const shop = shopId
    ? (shops.value.find((item) => item.shop_id === shopId) || currentShop.value)
    : currentShop.value
  // 店铺清单异常缺失时按「都拉」处理，交给后端回退默认店，避免静默空下拉
  const hasXhs = shop ? Boolean(shop.xhs?.app_id) : true
  const hasWechat = shop ? Boolean(shop.wechat?.appid) : true
  const errors = []
  const [shipping, logistics, wechatFreight] = await Promise.all([
    hasXhs ? xhsApi.shippingTemplates(shopId).catch((e) => { errors.push(`小红书运费模板：${e.message}`); return null }) : Promise.resolve(null),
    hasXhs ? xhsApi.logisticsPlans(shopId).catch((e) => { errors.push(`小红书物流方案：${e.message}`); return null }) : Promise.resolve(null),
    hasWechat ? storeApi.freightTemplates({}, shopId).catch((e) => { errors.push(`微信运费模板：${e.message}`); return null }) : Promise.resolve(null),
  ])
  const wechatResult = wechatFreight?.result || wechatFreight?.data || wechatFreight || {}
  return {
    errors,
    settings: {
      wechatFreight: (wechatResult.templates || wechatResult.freight_templates || wechatResult.list || (Array.isArray(wechatResult) ? wechatResult : [])).filter((item) => item.is_valid !== false && !String(item.name || item.template_name || '').includes('测试')),
      xhsShipping: listFrom(shipping, ['templates', 'carriageTemplateList', 'list']).filter((item) => !optionName(item).includes('测试')),
      xhsLogistics: listFrom(logistics, ['logisticsPlans', 'logisticsList', 'plans', 'list']).filter((item) => item.isValid !== false),
    },
  }
}

async function loadPlatformSettings() {
  settingsLoading.value = true
  try {
    const { errors, settings } = await fetchShopPlatformSettings()
    platformSettings.value = { ...platformSettings.value, ...settings }
    // 选择顺序：当前已选 → 这家店上次选的(记住的) → 店里只有唯一模板时自动选中
    const remembered = restorePlatformSettings()
    const pick = (current, rememberedId, options) => {
      if (current) return current
      if (rememberedId && options.some((item) => settingId(item) === rememberedId)) return rememberedId
      return options.length === 1 ? settingId(options[0]) : ''
    }
    batchPlatformSettings.wechatFreightId = pick(batchPlatformSettings.wechatFreightId, remembered.wechatFreightId, settings.wechatFreight || [])
    batchPlatformSettings.xhsShippingId = pick(batchPlatformSettings.xhsShippingId, remembered.xhsShippingId, settings.xhsShipping || [])
    batchPlatformSettings.xhsLogisticsId = pick(batchPlatformSettings.xhsLogisticsId, remembered.xhsLogisticsId, settings.xhsLogistics || [])
    if (errors.length) ElMessage.warning(errors.join('；'))
  } catch (error) {
    ElMessage.warning(`读取店铺平台设置失败：${error.message}`)
  } finally { settingsLoading.value = false }
}

// 选了就记住（按店铺存本地）：下次进来直接带上次的选择，忘记选的概率小很多
watch(batchPlatformSettings, () => rememberPlatformSettings(), { deep: true })

// 目标店的店铺参数：同店发布直接沿用第 3 步（平台映射）已选好的；跨店则按目标店重新拉取并重选
async function loadTargetSettings() {
  const shop = targetShop.value
  if (!shop) return
  if (!crossShop.value) {
    targetPlatformSettings.value = {
      wechatFreight: platformSettings.value.wechatFreight || [],
      xhsShipping: platformSettings.value.xhsShipping || [],
      xhsLogistics: platformSettings.value.xhsLogistics || [],
    }
    targetShopSettings.wechatFreightId = batchPlatformSettings.wechatFreightId
    targetShopSettings.xhsShippingId = batchPlatformSettings.xhsShippingId
    targetShopSettings.xhsLogisticsId = batchPlatformSettings.xhsLogisticsId
    return
  }
  const cached = targetSettingsCache[shop.shop_id]
  if (cached) {
    targetPlatformSettings.value = cached.settings
    Object.assign(targetShopSettings, cached.picked)
    return
  }
  targetSettingsLoading.value = true
  try {
    const { errors, settings } = await fetchShopPlatformSettings(shop.shop_id)
    targetPlatformSettings.value = settings
    // 该店只有唯一模板时自动选中（与第 3 步一致，省一次手动选择）
    targetShopSettings.wechatFreightId = settings.wechatFreight?.length === 1 ? settingId(settings.wechatFreight[0]) : ''
    targetShopSettings.xhsShippingId = settings.xhsShipping.length === 1 ? settingId(settings.xhsShipping[0]) : ''
    targetShopSettings.xhsLogisticsId = settings.xhsLogistics.length === 1 ? settingId(settings.xhsLogistics[0]) : ''
    targetSettingsCache[shop.shop_id] = { settings, picked: { ...targetShopSettings } }
    if (errors.length) ElMessage.warning(errors.join('；'))
  } catch (error) {
    ElMessage.warning(`读取目标店铺设置失败：${error.message}`)
  } finally { targetSettingsLoading.value = false }
}

async function onTargetShopChange() {
  await loadTargetSettings()
  // 「已发布」标记必须按目标店查：发过 A 店不代表发过 B 店，否则会误标成"已发布"而漏发
  await loadPublishStatus()
}

// 跨店发布时随任务提交的目标店私有参数（后端存成任务快照，执行时覆盖来源店的值）
function buildTargetParams() {
  const params = {}
  const list = availablePlatforms.value
  if (list.includes('xhs')) {
    if (targetShopSettings.xhsShippingId) params.xhs_shipping_template_id = targetShopSettings.xhsShippingId
    if (targetShopSettings.xhsLogisticsId) params.xhs_logistics_plan_id = targetShopSettings.xhsLogisticsId
  }
  if (list.includes('wechat') && targetShopSettings.wechatFreightId) {
    params.wechat_freight_template_id = targetShopSettings.wechatFreightId
  }
  return params
}

// 跨店发布前校验：该店必选的参数都选了才放行（不选的话平台会以"模板不属于该店"拒掉）
function targetParamsMissingHint() {
  const list = availablePlatforms.value
  if (list.includes('xhs')) {
    if (!targetShopSettings.xhsShippingId) return '请先选择目标店铺的小红书运费模板'
    if (!targetShopSettings.xhsLogisticsId) return '请先选择目标店铺的小红书物流方案'
  }
  if (list.includes('wechat') && !targetShopSettings.wechatFreightId) {
    return '请先选择目标店铺的微信运费模板'
  }
  return ''
}

// ------------------------------------------------------------------
// 运费模板/物流方案忘了选 → 绝不建任务
// 以前的行为：任务照建，worker 逐个商品报"未配置微信运费模板"，运营看到满屏失败，
// 回第 2 步补选后还得再发一轮（而且还常发现"补选了也没用"，见下面 sync 的注释）。
// ------------------------------------------------------------------
// 把「平台映射」当前选的店铺参数同步进每个商品的映射，发布前也会再同步一次：
// 常见操作是"发现没选 → 回第 2 步选好 → 直接点发布"，不再回退走一遍"保存并下一步"。
// 以前这种情况发出的仍是旧映射（空模板）→ 又整批失败，现象就是"返回去选了也不行"。
function syncPlatformSettingsIntoMappings(codes = null) {
  const list = availablePlatforms.value
  const mappings = { ...(mapping.value.products || {}) }
  for (const product of products.value) {
    if (codes && !codes.includes(product.product_code)) continue
    const current = mappings[product.product_code]
    if (!current) continue
    const next = { ...current }
    if (list.includes('wechat')) {
      next.wechat_freight_template_id = next.wechat_freight_template_id || batchPlatformSettings.wechatFreightId
    }
    if (list.includes('xhs')) {
      next.xhs_shipping_template_id = next.xhs_shipping_template_id || batchPlatformSettings.xhsShippingId
      next.xhs_logistics_plan_id = next.xhs_logistics_plan_id || batchPlatformSettings.xhsLogisticsId
    }
    mappings[product.product_code] = next
  }
  mapping.value = { ...mapping.value, products: mappings }
  return mappings
}

// 发布前必填校验：缺哪个店铺级参数、影响多少件商品（与后端 _missing_shop_params 同一套规则）
function platformSettingGaps(codes = null) {
  const list = availablePlatforms.value
  if (crossShop.value) {
    const hint = targetParamsMissingHint()
    return hint ? [hint] : []
  }
  const rules = []
  if (list.includes('wechat')) {
    rules.push(['wechat_freight_template_id', '微信运费模板', batchPlatformSettings.wechatFreightId])
  }
  if (list.includes('xhs')) {
    rules.push(['xhs_shipping_template_id', '小红书运费模板', batchPlatformSettings.xhsShippingId])
    rules.push(['xhs_logistics_plan_id', '小红书物流方案', batchPlatformSettings.xhsLogisticsId])
  }
  const targets = products.value.filter((product) => !codes || codes.includes(product.product_code))
  const gaps = []
  for (const [key, label, chosen] of rules) {
    const missing = targets.filter((product) => !String(chosen || mapping.value.products?.[product.product_code]?.[key] || '').trim()).length
    if (missing) gaps.push(`${label}（${missing} 件商品未选）`)
  }
  return gaps
}
// 第 3 步显示的红条：进到确认发布这一步就能看到还缺什么，不用等到点发布才知道
const publishGuardGaps = computed(() => (step.value === 3 && products.value.length ? platformSettingGaps() : []))

// 「上次选的运费模板」按店铺记住：这一步最容易忘，记住上次的选择能少踩这个坑
const platformSettingsKey = (shopId) => `bulk-platform-settings:${shopId || 'default'}`
function rememberPlatformSettings() {
  const { wechatFreightId, xhsShippingId, xhsLogisticsId } = batchPlatformSettings
  if (!wechatFreightId && !xhsShippingId && !xhsLogisticsId) return   // 切店时会清空，别把记忆覆盖掉
  try {
    localStorage.setItem(platformSettingsKey(currentShopId.value), JSON.stringify({ wechatFreightId, xhsShippingId, xhsLogisticsId }))
  } catch { /* 隐私模式/存储禁用，忽略 */ }
}
function restorePlatformSettings() {
  try {
    return JSON.parse(localStorage.getItem(platformSettingsKey(currentShopId.value)) || '{}') || {}
  } catch { return {} }
}

async function autoMatchWechatCategories() {
  if (!products.value.length) return
  categoryMatching.value = true
  try {
    const mappings = { ...(mapping.value.products || {}) }
    // 先读类目别名映射表:命中直接应用,不再请求平台类目接口(映射表加载失败不阻塞自动匹配)
    const aliasMap = {}
    try {
      for (const alias of (await bulkApi.categoryAliases()).result || []) {
        aliasMap[normalizeCategoryKey(alias.internal_category)] = alias
      }
    } catch { /* ignore */ }
    // 从映射表恢复属性默认值：同类目下次自动带入
    for (const [key, alias] of Object.entries(aliasMap)) {
      if (alias?.wechat?.attr_defaults || alias?.xhs?.attr_defaults || alias?.xhs?.spec_map) {
        const cfg = ensureGroupConfig(key)
        if (alias.wechat?.attr_defaults) Object.assign(cfg.wechat.defaults, alias.wechat.attr_defaults)
        if (alias.xhs?.attr_defaults) Object.assign(cfg.xhs.defaults, alias.xhs.attr_defaults)
        if (alias.xhs?.spec_map) Object.assign(cfg.xhs.spec_map, alias.xhs.spec_map)
      }
    }
    let freightOptions = []
    try {
      const freight = await storeApi.freightTemplates()
      const result = freight?.result || freight?.data || freight || {}
      freightOptions = (result.templates || result.freight_templates || result.list || (Array.isArray(result) ? result : []))
        .filter((item) => item.is_valid !== false && !String(item.name || item.template_name || '').includes('测试'))
    } catch { freightOptions = [] }

    // 同一关键词只查一次后端(后端已有类目树缓存),避免每个商品重复全量搜索
    const searchCache = {}
    async function searchLocal(keyword) {
      if (!searchCache[keyword]) {
        try {
          const data = await storeApi.searchCategories(keyword)
          searchCache[keyword] = data.results || []
        } catch { searchCache[keyword] = [] }
      }
      return searchCache[keyword]
    }

    // 拆店后一家店只发一个平台：三个匹配阶段各自按本店平台门控。
    // ⚠️ 否则纯微信店点一次「立即匹配类目」会顺带把小红书类目、品牌全查一遍
    //   （几百次无用请求），并把 xhs_status 写进映射，界面随之冒出一整屏「小红书：xxx 读取类目」。
    await Promise.all((availablePlatforms.value.includes('wechat') ? products.value : []).map(async (product) => {
      const internal = String(product.internal_category || '').trim()
      if (!internal) {
        mappings[product.product_code] = { ...(mappings[product.product_code] || {}), status: '缺少内部类目' }
        return
      }
      try {
        const parts = internal.split('>').map((part) => part.trim()).filter(Boolean)
        const keyword = parts[parts.length - 1] || internal
        const candidates = (await searchLocal(keyword)).filter((item) => item.leaf)
        const current = mappings[product.product_code] || {}
        const freight = current.wechat_freight_template_id
          ? current
          : freightOptions.length === 1
            ? { ...current, wechat_freight_template_id: String(freightOptions[0].id || freightOptions[0].template_id || ''), wechat_freight_template: freightOptions[0].name || freightOptions[0].template_name || '' }
            : current
        // 历史映射只作为叶子类目提示，每次都用最新 cats_v2 候选刷新完整链路。
        const alias = aliasMap[normalizeCategoryKey(internal)]
        if (alias?.wechat?.category) {
          const savedChain = alias.wechat.chain || []
          const savedLeafId = String(savedChain[savedChain.length - 1]?.cat_id || '')
          const savedNames = savedChain.map((node) => String(node.name || '').trim())
          const refreshed = candidates.filter((item) => String(item.cat_id) === savedLeafId
            || (savedNames.length && (item.chain || []).map((node) => String(node.name || '').trim()).join('>') === savedNames.join('>')))
          if (refreshed.length === 1) {
            const latest = refreshed[0]
            mappings[product.product_code] = { ...freight, wechat_category: latest.path, wechat_category_chain: latest.chain || [], status: '微信类目已按最新 cats_v2 匹配' }
            if (JSON.stringify(savedChain) !== JSON.stringify(latest.chain || [])) {
              void saveGroupAlias(internal, { wechat: { ...alias.wechat, category: latest.path, chain: latest.chain || [] } })
            }
            return
          }
          // 兜底:搜索返回 0 结果(内部类目名不在微信 cats_v2 关键词里)但别名已存,直接信任已存别名
          if (candidates.length === 0 && savedChain.length) {
            mappings[product.product_code] = { ...freight, wechat_category: alias.wechat.category, wechat_category_chain: savedChain, status: '微信类目已按映射表匹配' }
            return
          }
        }
        const exact = candidates.filter((item) => {
          const names = (item.chain || []).map((node) => String(node.name || '').trim())
          return parts.length <= names.length && parts.every((part, index) => names[names.length - parts.length + index] === part)
        })
        const match = exact.length === 1 ? exact[0] : null
        mappings[product.product_code] = {
          ...freight,
          wechat_category: match?.path || '',
          wechat_category_chain: match?.chain || [],
          wechat_candidates: candidates.map((item) => ({ path: item.path, chain: item.chain || [] })),
          status: match && freightOptions.length === 1 ? '微信配置已自动匹配' : match ? '微信类目已匹配，运费模板待确认' : candidates.length ? '微信类目待确认' : '微信类目匹配失败',
        }
      } catch {
        mappings[product.product_code] = { ...(mappings[product.product_code] || {}), status: '微信类目匹配失败' }
      }
    }))
    // 微信阶段完成先刷一次界面,即使小红书阶段慢也不会全程停在“待系统匹配”
    mapping.value = { ...mapping.value, products: mappings }
    // 小红书类目阶段：只在当前店发小红书时执行（见上方门控说明）
    await Promise.all((availablePlatforms.value.includes('xhs') ? products.value : []).map(async (product) => {
      try {
        const alias = aliasMap[normalizeCategoryKey(product.internal_category)]
        if (alias?.xhs?.category_id) {
          mappings[product.product_code] = { ...(mappings[product.product_code] || {}), xhs_category: alias.xhs.category, xhs_category_chain: alias.xhs.chain || [], xhs_category_id: alias.xhs.category_id, xhs_status: '小红书类目已按映射表匹配' }
          return
        }
        mappings[product.product_code] = await autoMatchXhsCategory(product, mappings[product.product_code] || {})
      } catch { mappings[product.product_code] = { ...(mappings[product.product_code] || {}), xhs_status: '小红书类目匹配失败' } }
    }))
    // 小红书品牌阶段：同样只在发小红书时执行
    const xhsBrandCache = {}
    await Promise.all((availablePlatforms.value.includes('xhs') ? products.value : []).map(async (product) => {
      const current = mappings[product.product_code] || {}
      const categoryId = current.xhs_category_id
      let brandId = current.xhs_brand_id || ''
      if (categoryId && !brandId) {
        try {
          // 与单独发布页保持一致：先读取该末级类目的完整店铺品牌列表，避免关键词查询漏品牌
          const cacheKey = String(categoryId)
          const brandData = xhsBrandCache[cacheKey] || await xhsApi.brands(categoryId)
          xhsBrandCache[cacheKey] = brandData
          const brands = listFrom(brandData, ['brands', 'brandList', 'list'])
          const normalizeBrand = (value) => String(value || '').replace(/[\s（）()·・]/g, '').toLowerCase()
          if (product.brand) {
            const targetBrand = normalizeBrand(product.brand)
            const exact = brands.filter((item) => normalizeBrand(optionName(item)) === targetBrand)
            if (exact.length === 1) brandId = settingId(exact[0])
          }
          // 兜底:Excel 没填品牌时,默认取该类目下第一个品牌(运营可在「审核编辑」里逐条改)
          if (!brandId && brands.length) brandId = settingId(brands[0])
        } catch { /* brand remains unconfirmed */ }
      }
      mappings[product.product_code] = { ...current, xhs_brand_id: brandId,
        xhs_shipping_template_id: current.xhs_shipping_template_id || batchPlatformSettings.xhsShippingId,
        xhs_logistics_plan_id: current.xhs_logistics_plan_id || batchPlatformSettings.xhsLogisticsId }
    }))
    mapping.value = { ...mapping.value, products: mappings }
    // 类目匹配完成后自动加载属性定义并匹配Excel属性值
    await loadGroupAttrDefs(true)
  } finally { categoryMatching.value = false }
}

async function autoMatchXhsCategory(product, mappingItem) {
  const parts = String(product.internal_category || '').split('>').map((part) => part.trim()).filter(Boolean)
  if (!parts.length) return { ...mappingItem, xhs_status: '缺少内部类目' }
  let parentId = null
  const chain = []
  for (const part of parts) {
    const data = await xhsApi.categories(parentId ? { category_id: parentId } : {})
    const result = data?.result || data || []
    const options = Array.isArray(result) ? result : (result.categoryV3s || result.categories || [])
    let matches = options.filter((item) => String(item.name || '').trim() === part)
    if (!matches.length) {
      // 精确匹配不上时做包含关系的模糊兜底,命中唯一才算自动匹配
      matches = options.filter((item) => {
        const name = String(item.name || '').trim()
        return name && (name.includes(part) || part.includes(name))
      })
    }
    if (matches.length !== 1) return { ...mappingItem, xhs_status: matches.length ? '小红书类目待确认' : '小红书类目匹配失败' }
    const selected = matches[0]
    chain.push({ id: selected.id || selected.categoryId, name: selected.name })
    parentId = selected.id || selected.categoryId
    if (!parentId) return { ...mappingItem, xhs_status: '小红书类目匹配失败' }
  }
  const leaf = chain[chain.length - 1]
  return { ...mappingItem, xhs_category: parts.join(' > '), xhs_category_chain: chain, xhs_category_id: leaf.id, xhs_status: '小红书类目已自动匹配' }
}

function getAttrGroupsNeedingConfig() {
  return mappingGroups.value.filter((g) => {
    const existing = groupAttrDefs[g.internal_category]
    if (existing?.wechat?.attr_defs?.length || existing?.xhs?.attr_defs?.length) return false
    return !!(g.mapping.wechat_category_chain?.length || g.mapping.xhs_category_id)
  })
}

async function loadGroupAttrDefs(silent = false) {
  const groups = getAttrGroupsNeedingConfig()
  if (!groups.length) { if (!silent) ElMessage.info('所有类目组已加载属性定义'); return }
  attrConfigLoading.value = true
  try {
    await Promise.all(groups.map(async (group) => {
      const config = { wechat: { attr_defs: [], defaults: {} }, xhs: { attr_defs: [], var_defs: [], defaults: {}, spec_map: {}, candidates: {} } }
      const wc = group.mapping.wechat_category_chain
      if (wc?.length) {
        try {
          const catId = String(wc[wc.length - 1].cat_id || '')
          if (catId) {
            const data = await storeApi.categoryDetail(catId)
            const attr = data?.result || data || {}
            config.wechat.attr_defs = attr.product_attr_list || []
            config.wechat.defaults = {};
            config.wechat.attr_defs.forEach((a) => { config.wechat.defaults[a.name] = a.type_v2 === 'select_many' ? [] : '' })
          }
        } catch (e) { /* wechat attr load failed */ }
      }
      const xid = group.mapping.xhs_category_id
      if (xid) {
        try {
          const [ad, vd] = await Promise.all([xhsApi.categoryAttributes(xid), xhsApi.categoryVariations(xid)])
          config.xhs.attr_defs = listFrom(ad, ['attributeV3s', 'attributes'])
          config.xhs.var_defs = listFrom(vd, ['variations'])
          config.xhs.defaults = {};
          config.xhs.attr_defs.forEach((a) => { config.xhs.defaults[a.id] = a.isMulti ? [] : '' })
          // 性能优化:只拉「货盘实际用到的」商品属性的候选值(「材质」单属性就有 1731 条);
          // 规格维度(var_defs)的候选值不再需要——后端 SKU 规格值已改为直接用货盘文本
          const groupProds = products.value.filter((p) => (p.internal_category || '(未填写内部类目)') === group.internal_category)
          const usedNames = new Set()
          for (const p of groupProds) {
            for (const rawKey of Object.keys(p.attributes || {})) usedNames.add(ATTR_KEY_ALIASES[rawKey] || rawKey)
          }
          const usedList = [...usedNames]
          const needDefs = config.xhs.attr_defs.filter((d) => usedList.some((n) => d.name === n || d.name.includes(n) || n.includes(d.name)))
          await Promise.all(needDefs.map(async (d) => {
            try { const v = await xhsApi.attributeValues(xid, d.id || d.propertyId); config.xhs.candidates[d.id] = listFrom(v, ['attributeValueV3s', 'values']) } catch { config.xhs.candidates[d.id] = [] }
          }))
        } catch (e) { /* xhs attr load failed */ }
      }
      // 合并映射表已有的默认值(不被空值覆盖)
      const existing = groupAttrDefs[group.internal_category]
      if (existing?.wechat?.defaults) { for (const [k, v] of Object.entries(existing.wechat.defaults)) { if (v !== '' && !(Array.isArray(v) && !v.length)) config.wechat.defaults[k] = v } }
      if (existing?.xhs?.defaults) { for (const [k, v] of Object.entries(existing.xhs.defaults)) { if (v !== '' && !(Array.isArray(v) && !v.length)) config.xhs.defaults[k] = v } }
      if (existing?.xhs?.spec_map) Object.assign(config.xhs.spec_map, existing.xhs.spec_map)
      groupAttrDefs[group.internal_category] = config
    }))
    ElMessage.success(`已加载 ${groups.length} 个类目组的属性定义`)
    const result = autoMatchProductAttributes()
    if (result.matched + result.unmatched > 0) {
      ElMessage.success(`属性自动匹配：${result.matched} 个成功${result.unmatched ? `，${result.unmatched} 个待手动确认` : ''}`)
    }
  } finally { attrConfigLoading.value = false }
}

function getGroupAttrConfig(internalCategory) { return groupAttrDefs[internalCategory] || null }

// 微信 API 的 attr.value 可能是字符串或逗号/分号分隔字符串，需规范化为数组，避免 v-for 拆成单字符
function attrOptions(v) {
  if (Array.isArray(v)) return v
  if (v == null || v === '') return []
  if (typeof v === 'string') {
    if (/[,，;；]/.test(v)) return v.split(/[,，;；]/).map((s) => s.trim()).filter(Boolean)
    return [v]
  }
  return [v]
}

// 微信属性类型 type_v2 中文标签
const wxTypeLabels = {
  select_one: '单选',
  select_many: '多选',
  string: '文本',
  integer: '整数',
  decimal4: '小数',
  integer_unit: '整数+单位',
  decimal4_unit: '小数+单位',
}
function wxTypeLabel(t) {
  return wxTypeLabels[t] || t || ''
}

// 货盘「商品属性」键名 → 平台属性名 的别名(同义/缩略名兜底)。全店通用,不随类目变
const ATTR_KEY_ALIASES = {
  '主体材质': '材质',
  '赠链材质': '材质',
  '主钻克拉数': '主钻分数（最低）',
  '主钻分数': '主钻分数（最低）',
  '切工级别': '钻石切工',
  '镶嵌': '镶嵌方式',
  '鉴定证书': '鉴定标识',
}
// 货盘属性值 → 平台属性值(名称) 的别名(枚举值对不上时兜底)
const ATTR_VALUE_ALIASES = {
  '鉴定标识': {
    'NGTC（国检）': '国内鉴定',
    'NGTC': '国内鉴定',
    'IGI+NGTC': '国际鉴定',
    'GTC(省检）': '国内鉴定',
    'GDTC（省检）': '国内鉴定',
  },
  // 钻石切工:平台候选是「英文/中文」格式(如 Very good/优良),货盘可能写 VG/很好、VG、很好、Very good 等
  '钻石切工': {
    'Very good/优良': 'Very good/优良', 'VG/很好': 'Very good/优良',
    'Very good': 'Very good/优良', '优良': 'Very good/优良', 'VG': 'Very good/优良', 'vg': 'Very good/优良', '很好': 'Very good/优良',
    'Good/良好': 'Good/良好', 'GD/良好': 'Good/良好',
    'Good': 'Good/良好', '良好': 'Good/良好', 'GD': 'Good/良好', 'gd': 'Good/良好',
    'Excellent/极优': 'Excellent/极优', 'EX/极优': 'Excellent/极优',
    'Excellent': 'Excellent/极优', '极优': 'Excellent/极优', 'EX': 'Excellent/极优', 'ex': 'Excellent/极优',
    'Poor/未分级': 'Poor/未分级', '未分级': 'Poor/未分级',
  },
}
// 平台属性默认值(货盘没有对应数据的属性,如 认证标识/鉴定类别)。
// 这里存的是【文案 valueName】(人类可读),代码会自动从 candidates 里查 valueId
const ATTR_DEFAULTS = {
  '62b2846fa21ae000011a7686': ['CMA'],  // 认证标识(多选) — CMA 标识
  '62b2846fa21ae000011a77aa': ['国家珠宝玉石质量监督检验中心(NGTC国检)'],  // 鉴定类别(多选) — NGTC国检
}
// SKU 规格值别名:货盘里常用英文 SKU 编码 → 平台中文 valueName(用于规格匹配兜底)
const SPEC_VALUE_ALIASES = {
  '粉红色':   ['SHINING PINK', '粉色', '粉红', 'PINK', 'Rose'],
  '浅蓝色':   ['ICE BLUE', '浅蓝', 'LIGHT BLUE'],
  '香槟金色': ['CHAMPAGNE GOLD', '香槟金', 'CHAMPAGNE'],
  '玫瑰金色': ['ROSE GOLD', '玫瑰金'],
  '黄金色':   ['YELLOW GOLD', '黄金', 'GOLD'],
  '18K金色':  ['18K GOLD', '18K金'],
  '白色':     ['WHITE'],
  '黑色':     ['BLACK'],
  '无色':     ['COLORLESS', '透明'],
  // 其它颜色/规格值按需补充
}

function autoMatchProductAttributes() {
  let matched = 0, unmatched = 0
  // 拆店后一家店只属于一个平台：只统计本店「已配置」平台的匹配情况。
  // ⚠️ 否则纯小红书店里微信属性定义恒为空，货盘的每个属性都会被记成一次「微信未匹配」，
  //   界面就永远显示「N 个类目有未匹配属性」（拆店前是双平台店，两边都能拉到，所以看不到这个假警报）。
  const shop = currentShop.value
  const hasXhs = shop ? Boolean(shop.xhs?.app_id) : true
  const hasWechat = shop ? Boolean(shop.wechat?.appid) : true
  for (const group of mappingGroups.value) {
    const config = groupAttrDefs[group.internal_category]
    if (!config) continue
    const groupProducts = products.value.filter((p) => (p.internal_category || '(未填写内部类目)') === group.internal_category)
    let gMatched = 0, gUnmatched = 0
    // 自动映射规格名 → 小红书规格维度（规格维度动态，按名称匹配）
    if (config.xhs.var_defs?.length && groupProducts.length) {
      const firstSku = groupProducts[0]?.skus?.[0]
      if (firstSku) {
        const specNames = (firstSku.specs || []).map((s) => s.name).filter(Boolean)
        // 货盘规格名 → xhs 规格维度名 别名表(模糊匹配的补充)。
        // 值为「候选维度名数组」:同一货盘规格在不同类目下能挂的维度不同,按类目实际存在的维度择优。
        // 例:「主钻分数」在天然钻戒指类目挂「尺寸」;培育钻戒指类目没有「尺寸」维度,
        //     只能挂到「套装规格」(自由输入维度,已验证可承载分数且无需 valueId)。
        //     若仍按旧的单值别名只认「尺寸」,培育钻戒指就只剩「颜色分类」1 维 →
        //     同色多个分数报「重复的规格[红色]」。
        const SPEC_NAME_ALIASES = {
          '颜色': ['颜色分类'], '颜色分类': ['颜色分类'], '钻石颜色': ['颜色分类'], '戒托颜色': ['颜色分类'],
          '尺码': ['尺寸'], '尺寸': ['尺寸'],
          '主钻分数': ['尺寸', '套装规格', '重量/克拉'],
          '款式': ['款式'], '净度': ['钻石净度'], '钻石净度': ['钻石净度'],
          '圈号': ['圈口'], '圈口': ['圈口'], '规格': ['规格'], '长度': ['长度'],
          '重量': ['规格/重量'], '大小': ['大小'],
        }
        const usedSpecNames = new Set()   // 一个货盘规格列只能挂到一个维度,避免被两个维度抢
        for (const varDef of config.xhs.var_defs) {
          const hitName = specNames.find((n) => {
            if (usedSpecNames.has(n)) return null
            if (n === varDef.name) return n
            if (varDef.name.includes(n) || n.includes(varDef.name)) return n
            const alias = SPEC_NAME_ALIASES[n]   // 别名兜底
            if (alias && (Array.isArray(alias) ? alias : [alias]).includes(varDef.name)) return n
            return null
          })
          if (hitName) { config.xhs.spec_map[varDef.id] = hitName; usedSpecNames.add(hitName) }
        }
      }
    }
    // 每个商品的属性匹配
    for (const product of groupProducts) {
      const excelAttrs = product.attributes || {}
      const prodMapping = mapping.value.products?.[product.product_code] || {}
      const wxAttrs = { ...(prodMapping.wechat_attrs || {}) }
      const xhsAttrs = { ...(prodMapping.xhs_attrs || {}) }
      for (const [name, val] of Object.entries(excelAttrs)) {
        // 键名先走别名(主体材质→材质、主钻克拉数→主钻分数…)
        const resolvedName = ATTR_KEY_ALIASES[name] || name
        // 微信: 名称匹配(精确+模糊)，值直接用文本
        if (hasWechat) {
          const wxDef = (config.wechat.attr_defs || []).find((d) => d.name === resolvedName)
            || (config.wechat.attr_defs || []).find((d) => d.name.includes(resolvedName) || resolvedName.includes(d.name))
          if (wxDef) { wxAttrs[wxDef.name] = val; gMatched++ } else { gUnmatched++ }
        }
        // 小红书: 名称匹配(精确+模糊) + 候选值匹配 valueId
        if (!hasXhs) continue
        const xhsDef = (config.xhs.attr_defs || []).find((d) => d.name === resolvedName)
          || (config.xhs.attr_defs || []).find((d) => d.name.includes(resolvedName) || resolvedName.includes(d.name))
        if (xhsDef) {
          const cands = config.xhs.candidates[xhsDef.id] || []
          // 兜底:货盘值带 / 时(如「VG/很好」),拆开前后段都试一次查别名表
          const aliasMap = ATTR_VALUE_ALIASES[xhsDef.name] || {}
          let valueAlias = aliasMap[val]
          if (!valueAlias && val.includes('/')) {
            valueAlias = aliasMap[val.split('/')[0].trim()] || aliasMap[val.split('/').pop().trim()]
          }
          const hit = cands.find((c) => c.valueName === val) || cands.find((c) => c.valueName.includes(val) || val.includes(c.valueName))
          if (hit) { xhsAttrs[xhsDef.id] = { propertyId: xhsDef.id, name: xhsDef.name, valueId: hit.valueId, value: hit.valueName }; gMatched++ }
          else if (valueAlias) {
            // 值别名兜底:货盘值(如 NGTC（国检）)映射成平台值(国内鉴定)
            const aliases = Array.isArray(valueAlias) ? valueAlias : [valueAlias]
            const mapped = aliases.map((av) => { const ac = cands.find((c) => c.valueName === av); return { propertyId: xhsDef.id, name: xhsDef.name, valueId: ac?.valueId || '', value: ac?.valueName || av } })
            xhsAttrs[xhsDef.id] = xhsDef.isMulti ? mapped : mapped[0]
            gMatched++
          }
          else if (xhsDef.inputType === 0 || xhsDef.customizable) { xhsAttrs[xhsDef.id] = { propertyId: xhsDef.id, name: xhsDef.name, valueId: '', value: val }; gMatched++ }
          else { gUnmatched++ }
        } else { gUnmatched++ }
      }
      // 应用平台默认值(货盘没有对应数据的属性,如 认证标识/鉴定类别)
      for (const [attrId, defVal] of Object.entries(ATTR_DEFAULTS)) {
        const def = (config.xhs.attr_defs || []).find((d) => d.id === attrId)
        if (!def) continue
        const cands = config.xhs.candidates[attrId] || []
        const vals = Array.isArray(defVal) ? defVal : [defVal]
        // ATTR_DEFAULTS 存的是文案 valueName,自动从 cands 里查 valueId
        const mapped = vals.map((vname) => {
          const c = cands.find((cc) => cc.valueName === vname)
            || cands.find((cc) => (cc.valueName || '').includes(vname) || vname.includes(cc.valueName || ''))
          return { propertyId: attrId, name: def.name, valueId: c?.valueId || '', value: c?.valueName || vname }
        })
        xhsAttrs[attrId] = def.isMulti ? mapped : mapped[0]
      }
      // SKU 规格值 → 小红书 valueId
      const skuVariants = {}
      if (config.xhs.var_defs?.length) {
        for (const sku of (product.skus || [])) {
          const variants = []
          for (const varDef of config.xhs.var_defs) {
            const mapKey = config.xhs.spec_map[varDef.id]
            if (!mapKey) continue
            const specVal = resolveSpecValue(sku, mapKey)
            if (!specVal) continue
            const cands = config.xhs.candidates[varDef.id] || []
            // 优先用 cands 精确/模糊匹配;找不到时用 SPEC_VALUE_ALIASES(英文 SKU 编码→中文 valueName)兜底
            // 注:var_defs 的 candidates 是空数组(性能优化未拉),所以 hit 几乎一定走 fallback
            let hit = cands.find((c) => c.valueName === specVal) || cands.find((c) => c.valueName.includes(specVal) || specVal.includes(c.valueName))
            let displayValue = specVal
            let valueId = ''
            if (hit) {
              displayValue = hit.valueName
              valueId = hit.valueId
            } else {
              for (const [valueName, aliases] of Object.entries(SPEC_VALUE_ALIASES)) {
                if (aliases.includes(specVal)) {
                  displayValue = valueName  // 英文 SKU 编码 → 中文 valueName
                  break
                }
              }
            }
            variants.push({ id: varDef.id, name: varDef.name, value: displayValue, valueId: valueId })
          }
          if (variants.length) skuVariants[sku.sku_code] = variants
        }
      }
      // 更新组级默认显示(用第一个商品的值)
      if (product === groupProducts[0]) {
        Object.assign(config.wechat.defaults, wxAttrs)
        for (const [id, attr] of Object.entries(xhsAttrs)) {
          if (Array.isArray(attr)) config.xhs.defaults[id] = attr.map((a) => a.valueId).filter(Boolean)
          else if (attr.valueId) config.xhs.defaults[id] = attr.valueId
        }
      }
      mapping.value.products[product.product_code] = { ...prodMapping, wechat_attrs: wxAttrs, xhs_attrs: xhsAttrs, xhs_sku_variants: skuVariants }
    }
    matched += gMatched; unmatched += gUnmatched
    attrMatchStatus[group.internal_category] = { matched: gMatched, unmatched: gUnmatched }
  }
  return { matched, unmatched }
}
function ensureGroupConfig(key) {
  if (!groupAttrDefs[key]) groupAttrDefs[key] = { wechat: { attr_defs: [], defaults: {} }, xhs: { attr_defs: [], var_defs: [], defaults: {}, spec_map: {}, candidates: {} } }
  return groupAttrDefs[key]
}
function setGroupWxAttr(key, name, value) { ensureGroupConfig(key).wechat.defaults[name] = value }
function setGroupXhsAttr(key, attrId, value) { ensureGroupConfig(key).xhs.defaults[attrId] = value }
function setGroupSpecMap(key, varId, col) { ensureGroupConfig(key).xhs.spec_map[varId] = col }
// 规格映射取值：兼容旧的 'specN_value' 列名与新的规格维度名称
function resolveSpecValue(sku, mapKey) {
  if (!mapKey) return ''
  const specs = sku?.specs || []
  const legacy = /^spec(\d+)_value$/.exec(mapKey)
  if (legacy) return specs[Number(legacy[1]) - 1]?.value || ''
  const hit = specs.find((s) => s.name === mapKey)
  return hit?.value || ''
}
// 某内部类目组下所有出现过的规格维度名称（按列序去重），用于规格映射下拉
function groupSpecDimensions(catKey) {
  const names = []
  for (const p of products.value) {
    if ((p.internal_category || '(未填写内部类目)') !== catKey) continue
    for (const sku of (p.skus || [])) {
      for (const sp of (sku.specs || [])) {
        if (sp.name && !names.includes(sp.name)) names.push(sp.name)
      }
    }
  }
  return names
}
// 这些属性全店统一、逐行都靠组级默认值继承,展示出来没有信息量,表格里不再单独占列。
// 需要恢复某列时从下面集合里删掉即可。
const HIDDEN_TABLE_ATTRS = new Set(['镶嵌', '合成方法', '主钻克拉数', '圈号'])
const requiredAttrNames = computed(() => {
  const names = new Set()
  for (const config of Object.values(groupAttrDefs)) {
    for (const a of (config.wechat?.attr_defs || [])) {
      if (a.is_required && !HIDDEN_TABLE_ATTRS.has(a.name)) names.add(a.name)
    }
  }
  return [...names]
})
function getProductAttrValue(product, attrName) {
  const ov = mapping.value.products?.[product.product_code]?.wechat_attrs?.[attrName]
  if (ov !== undefined && ov !== '') return ov
  const key = product.internal_category
  return groupAttrDefs[key]?.wechat?.defaults?.[attrName] || ''
}

// 补规格图: 点表格里的「缺图」选一张本地图片 → 上传落盘 → 写回该行 sku_image 并持久化
const skuImageInput = ref(null)
const pendingSkuImageRow = ref(null)
const uploadingSkuImage = ref(false)
function pickSkuImage(row) {
  pendingSkuImageRow.value = row
  const input = skuImageInput.value
  if (!input) return
  input.value = ''          // 允许再次选择同一张图
  input.click()
}
function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || '').split(',')[1] || '')
    reader.onerror = () => reject(new Error('读取本地图片失败'))
    reader.readAsDataURL(file)
  })
}
async function onSkuImagePicked(event) {
  const file = event?.target?.files?.[0]
  const row = pendingSkuImageRow.value
  pendingSkuImageRow.value = null
  if (!file || !row) return
  uploadingSkuImage.value = true
  try {
    const contentBase64 = await fileToBase64(file)
    const data = await bulkApi.uploadSkuImage(batch.value.id, { filename: file.name, content_base64: contentBase64 })
    const ref = data?.result?.ref || data?.ref
    if (!ref) throw new Error('上传未返回图片引用')
    row.sku_image = ref
    await bulkApi.updateItems(batch.value.id, allItems.value)
    ElMessage.success('规格图已补上')
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    uploadingSkuImage.value = false
  }
}

// 主图操作: 选本地图片(可一次多选) → 逐张上传落盘 → 写回该商品 main_images 并持久化。
// 主图是商品级(同一商品所有 SKU 行共用一套图), 所以要写回该商品的所有行。
// 平台要的是多张主图(一般 3~5 张), 只补一张不够用, 所以支持一次多选; 顺序即选择顺序, 第一张为首图。
// 两种意图, 入口不同, 行为必须与入口承诺一致:
//   mode='fill'    「缺主图·点击补图」(仅当该商品没有任何主图时出现) → 只补空行, 已有主图的行绝不覆盖
//                  (Excel 里首行常填 3 张、次行留空, 无条件覆盖会把首行的图清掉)
//   mode='replace' 「换图」(行上有主图时出现) → 确认后把该商品所有行替换成这次选的这几张, 真的替换
const mainImageInput = ref(null)
const pendingMainImageRow = ref(null)
const pendingMainImageMode = ref('fill')
const uploadingMainImage = ref(false)
function pickMainImage(row, mode = 'fill') {
  pendingMainImageRow.value = row
  pendingMainImageMode.value = mode
  const input = mainImageInput.value
  if (!input) return
  input.value = ''          // 允许再次选择同一张图
  input.click()
}
async function onMainImagePicked(event) {
  const files = Array.from(event?.target?.files || [])
  const row = pendingMainImageRow.value
  const mode = pendingMainImageMode.value
  pendingMainImageRow.value = null
  pendingMainImageMode.value = 'fill'
  if (!files.length || !row) return
  const code = row.product_code
  const existing = productMainImages.value[code] || []
  const rowsWithImage = allItems.value.filter(
    (item) => item.product_code === code && (item.main_images || []).length,
  ).length
  // 只有「确实要把已有主图换掉」才走替换；商品本来没图就还是补图
  const doReplace = mode === 'replace' && existing.length > 0
  if (doReplace) {
    try {
      await ElMessageBox.confirm(
        `商品 ${code} 现有 ${existing.length} 张主图（${rowsWithImage} 行已填写）。`
          + `确认后会把它们全部替换成这次选的 ${files.length} 张，原有主图将丢失且无法撤销。`,
        '确认替换主图',
        { type: 'warning', confirmButtonText: '替换主图', cancelButtonText: '取消' },
      )
    } catch {
      return   // 用户取消, 什么都不做
    }
  }
  uploadingMainImage.value = true
  try {
    // 多张并行上传, Promise.all 保序 → refs 与选择顺序一致(第一张即首图)
    const refs = await Promise.all(files.map(async (file) => {
      const contentBase64 = await fileToBase64(file)
      const data = await bulkApi.uploadSkuImage(batch.value.id, { filename: file.name, content_base64: contentBase64 })
      const ref = data?.result?.ref || data?.ref
      if (!ref) throw new Error(`上传未返回图片引用：${file.name}`)
      return ref
    }))
    let filled = 0
    let kept = 0
    let replaced = 0
    for (const item of allItems.value) {
      if (item.product_code !== code) continue
      if (doReplace) {
        // 真替换: 该商品所有行统一置为这几张, 与确认框承诺一致
        item.main_images = [...refs]
        replaced += 1
        continue
      }
      // 补图: 只补空白行, 已有主图的行原样保留
      if ((item.main_images || []).length) { kept += 1; continue }
      item.main_images = [...refs]
      filled += 1
    }
    await bulkApi.updateItems(batch.value.id, allItems.value)
    if (doReplace) {
      ElMessage.success(`主图已替换为 ${refs.length} 张（同商品 ${replaced} 行）`)
    } else {
      const parts = [`主图已补 ${refs.length} 张到 ${filled} 行`]
      if (kept) parts.push(`同商品另 ${kept} 行已有主图，未改动`)
      ElMessage.success(parts.join('；'))
    }
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    uploadingMainImage.value = false
  }
}

watch(step, (value) => {
  if (value === 2) {
    resetMappingPage()
    void loadPlatformSettings(); void autoMatchWechatCategories()
    if (mapping.value.attr_groups) Object.assign(groupAttrDefs, mapping.value.attr_groups)
  }
})

// 多店铺：切换店铺后必须整块重置。
// 批次/商品/映射/发布记录全部按店铺隔离，若不重置，界面还显示 A 店的批次，
// 而请求已经带上 B 店的 X-Shop-Id —— 轻则 404（批次不属于该店），重则操作到错误数据。
watch(currentShopId, () => {
  const untouched = step.value === 0 && !batch.value
  step.value = 0
  batch.value = null
  file.value = null
  imageScanResult.value = null
  mapping.value = { mode: 'auto', products: {} }
  batchPlatformSettings.wechatFreightId = ''
  batchPlatformSettings.xhsShippingId = ''
  batchPlatformSettings.xhsLogisticsId = ''
  // 跨店发布状态也按店隔离：来源店换了，目标店选择/目标店参数缓存/任务归属全部重置
  targetShopId.value = currentShopId.value || ''
  targetShopSettings.wechatFreightId = ''
  targetShopSettings.xhsShippingId = ''
  targetShopSettings.xhsLogisticsId = ''
  targetPlatformSettings.value = { wechatFreight: [], xhsShipping: [], xhsLogistics: [] }
  Object.keys(targetSettingsCache).forEach((key) => { delete targetSettingsCache[key] })
  jobShopId.value = ''
  Object.keys(groupAttrDefs).forEach((key) => { delete groupAttrDefs[key] })
  huopaiPath.value = ''
  if (!untouched) ElMessage.info('已切换店铺，批量发布流程已重置')
  // 货盘文件列表与图片根目录也是按店铺取的，重新拉一次
  void loadServerPaths()
})

function readAsBase64(input) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '')
    reader.onerror = reject
    reader.readAsDataURL(input)
  })
}

async function readFolderFiles(fileList) {
  const files = Array.from(fileList || []).filter((item) => item.type?.startsWith('image/') || /\.(jpe?g|png|gif|webp)$/i.test(item.name))
  folderFiles.value = await Promise.all(files.map(async (item) => ({
    path: item.webkitRelativePath || item.name,
    name: item.name,
    content_base64: await readAsBase64(item),
  })))
}

async function readCommonDetailFiles(fileList) {
  const files = Array.from(fileList || []).filter((item) => item.type?.startsWith('image/') || /\.(jpe?g|png|gif|webp)$/i.test(item.name))
  commonDetailFiles.value = await Promise.all(files.map(async (item) => ({
    path: item.webkitRelativePath || item.name,
    name: item.name,
    content_base64: await readAsBase64(item),
  })))
}

// 图片直读: 扫描共享盘目录, 按编码把商品图/SKU 图挂到批次上(固定取该 SKU 的「主图2」)
async function scanImages(silent = false) {
  if (!batch.value?.id) return
  scanning.value = true
  try {
    const data = await bulkApi.scanImages(batch.value.id, {
      root: imageRoot.value || null,
      months: imageMonths.value.length ? imageMonths.value : null,
      dry_run: false,
    })
    imageScanResult.value = data
    batch.value = (await bulkApi.getBatch(batch.value.id)).result
    if (!silent) {
      ElMessage.success(`图片匹配完成：商品 ${data.products_matched}/${data.products_total}，SKU ${data.skus_matched}/${data.skus_total}`)
    }
  } catch (error) { ElMessage.error(`图片扫描失败：${error.message}`) } finally { scanning.value = false }
}

// 可选的服务端路径(货盘表 / 图片根目录)全部由后端白名单与目录列表提供,
// 前端不再接受手填任意服务器路径, 避免接口被当作任意文件读取入口
async function loadServerPaths() {
  try {
    const data = await bulkApi.huopaiFiles()
    huopaiFiles.value = data.files || []
    if (!huopaiPath.value && data.default) huopaiPath.value = data.default
  } catch (error) {
    huopaiFiles.value = []
    ElMessage.warning(`读取货盘目录失败：${error.message}`)
  }
  try {
    const data = await bulkApi.imageRoots()
    imageRootOptions.value = data.roots || []
    if (!imageRoot.value && data.default) imageRoot.value = data.default
  } catch {
    imageRootOptions.value = []
  }
}

onMounted(() => { void loadServerPaths() })

async function downloadTemplate() {
  try {
    const response = await bulkApi.template()
    const url = URL.createObjectURL(response)
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = '商品批量发布模板.xlsx'; anchor.click(); URL.revokeObjectURL(url)
  } catch (error) { ElMessage.error(error.message) }
}

async function importFile() {
  clearJobProgress()
  if (!file.value) return ElMessage.warning('请先选择 .xlsx 或 .csv 文件')
  loading.value = true
  try {
    const data = await bulkApi.importBatch({ filename: file.value.name, content_base64: await readAsBase64(file.value), folder_files: folderFiles.value, common_detail_files: commonDetailFiles.value })
    batch.value = (await bulkApi.getBatch(data.batch_id)).result
    itemKeyword.value = ''
    itemErrorFilter.value = 'all'
    resetItemPage()
    step.value = 1
    ElMessage.success(`已导入 ${data.total} 行 SKU`)
    // 必须等扫描完成:否则用户马上点「重新校验并继续/保存映射」时,会用还没挂上规格图的前端数据
    // 调 updateItems, 把后端刚写入的 sku_image 覆盖掉 —— 表现就是"明明有图却显示缺图"。
    await scanImages(true)
  } catch (error) { ElMessage.error(error.message) } finally { loading.value = false }
}

async function importHuopai() {
  // 直接从服务器货盘目录导入(不走浏览器上传大文件),后端自动跑货盘→标准模板转换。
  // path 必须是服务端货盘目录内的文件(后端白名单会再校验一次);传空则用服务端默认货盘表。
  clearJobProgress()
  loading.value = true
  try {
    const data = await bulkApi.importHuopai(huopaiPath.value)
    batch.value = (await bulkApi.getBatch(data.batch_id)).result
    itemKeyword.value = ''
    itemErrorFilter.value = 'all'
    resetItemPage()
    step.value = 1
    ElMessage.success(`已从货盘导入 ${data.total} 行 SKU（${data.product_count} 个商品）`)
    // 同上:等扫描完成再结束,避免后续 updateItems 用旧数据覆盖掉 sku_image
    await scanImages(true)
  } catch (error) { ElMessage.error(error.message) } finally { loading.value = false }
}

// 标题栏「返回」:还有上一步就退一步;第 0 步(导入)没有上一步,交给父级回商品列表
function goBackStep() {
  if (step.value > 0) { step.value -= 1; return }
  emit('back')
}

async function validateBatch() {
  loading.value = true
  try { await bulkApi.validate(batch.value.id); batch.value = (await bulkApi.getBatch(batch.value.id)).result; ElMessage.success('校验完成'); step.value = 2 } catch (error) { ElMessage.error(error.message) } finally { loading.value = false }
}

async function saveAndNext() {
  loading.value = true
  try {
    await bulkApi.updateItems(batch.value.id, allItems.value)
    const productsMapping = Object.fromEntries(products.value.map((product) => [product.product_code, {
      ...(mapping.value.products?.[product.product_code] || {}),
      internal_category: product.internal_category,
      xhs_shipping_template_id: mapping.value.products?.[product.product_code]?.xhs_shipping_template_id || batchPlatformSettings.xhsShippingId,
      xhs_logistics_plan_id: mapping.value.products?.[product.product_code]?.xhs_logistics_plan_id || batchPlatformSettings.xhsLogisticsId,
      wechat_freight_template_id: mapping.value.products?.[product.product_code]?.wechat_freight_template_id || batchPlatformSettings.wechatFreightId,
      status: '待系统匹配',
    }]))
    for (const code of Object.keys(productsMapping)) {
      const previous = mapping.value.products?.[code]
      if (previous?.status) productsMapping[code].status = previous.status
    }
    const attrGroupsSnapshot = {}
    for (const [key, config] of Object.entries(groupAttrDefs)) {
      // 性能优化:不持久化 candidates(「材质」单属性 1731 条,4 类目就 500KB+)。
      // 值翻译结果已存进 mapping.products[].xhs_attrs,发布链路不依赖 candidates;
      // 需要下拉选项时(打开属性抽屉)由 ensureGroupCandidates 按需重新拉取。
      attrGroupsSnapshot[key] = { wechat: { attr_defs: config.wechat?.attr_defs || [], defaults: { ...(config.wechat?.defaults || {}) } }, xhs: { attr_defs: config.xhs?.attr_defs || [], var_defs: config.xhs?.var_defs || [], defaults: { ...(config.xhs?.defaults || {}) }, spec_map: { ...(config.xhs?.spec_map || {}) } } }
    }
    mapping.value = { mode: 'auto', products: productsMapping, attr_groups: attrGroupsSnapshot }
    await bulkApi.updateMappings(batch.value.id, mapping.value)
    step.value = 3
  } catch (error) { ElMessage.error(error.message) } finally { loading.value = false }
}

function openReview(product) {
  reviewProduct.value = product
  reviewVisible.value = true
  // 候选值不再持久化,打开审核抽屉时按需补拉(响应式更新,下拉框数据到达后自动填选项)
  void ensureGroupCandidates(product.internal_category)
}
// 商品已配置属性项数(微信+小红书),供 step3 表格「属性状态」列展示
function productAttrCount(product) {
  const m = mapping.value?.products?.[product?.product_code] || {}
  return Object.keys(m.wechat_attrs || {}).length + Object.keys(m.xhs_attrs || {}).length
}
// 属性状态标签颜色:配了微信属性显示绿色,否则灰色
function productAttrTagType(product) {
  const m = mapping.value?.products?.[product?.product_code] || {}
  return Object.keys(m.wechat_attrs || {}).length ? 'success' : 'info'
}
function handleReviewSave(data) {
  const code = data.product_code
  const m = mapping.value.products?.[code] || {}
  mapping.value.products[code] = { ...m, wechat_attrs: data.wechat_attrs, xhs_attrs: data.xhs_attrs, wechat_title: data.wechat_title, xhs_title: data.xhs_title }
  // 同步更新 products 里的基础字段
  const prod = products.value.find((p) => p.product_code === code)
  if (prod) {
    prod.title = data.title; prod.brand = data.brand; prod.description = data.description
    // SKU 行内编辑回写：同步价格/库存/规格值，并重算汇总字段
    if (Array.isArray(data.skus)) {
      prod.skus = data.skus.map((sku) => ({ ...sku, specs: (sku.specs || []).map((s) => ({ ...s })) }))
      // 同步审核抽屉里自由增删的规格维度，保证下次打开与规格映射下拉一致
      if (Array.isArray(data.spec_dimensions)) prod.spec_dimensions = [...data.spec_dimensions]
      const prices = prod.skus.map((sku) => Number(sku.price)).filter(Number.isFinite)
      const stocks = prod.skus.map((sku) => Number(sku.stock)).filter(Number.isFinite)
      prod.price_min = prices.length ? Math.min(...prices) : null
      prod.price_max = prices.length ? Math.max(...prices) : null
      prod.stock_total = stocks.length ? stocks.reduce((sum, n) => sum + n, 0) : null
    }
  }
  for (const row of allItems.value.filter((item) => item.product_code === code)) {
    row.title = data.title
    row.brand = data.brand
    row.description = data.description
    row.weight = data.weight
    const sku = data.skus?.find((item) => item.sku_code === row.sku_code)
    if (sku) {
      row.price = sku.price
      row.original_price = sku.original_price
      row.stock = sku.stock
      row.specs = (sku.specs || []).map((spec) => ({ ...spec }))
    }
  }
}
function getReviewGroupConfig(product) {
  return groupAttrDefs[product?.internal_category] || null
}

function jobStatusText(status) {
  return ({ queued: '排队中', running: '发布中', success: '成功', failed: '失败', partial: '部分成功', cancelled: '已停止', deleted: '平台已删除', completed: '已完成', completed_with_errors: '已完成（有失败）' })[status] || status
}
// 发布进度表格：失败行加高亮类，运营一眼定位
function jobItemRowClass({ row }) {
  return row.status === 'failed' || row.status === 'partial' ? 'job-item--failed' : ''
}
// 商品选择表格：已发布失败的商品整行浅红，配合「仅看发布失败」筛选更醒目
function productPublishRowClass({ row }) {
  return productPublishState(row.product_code) === 'failed' ? 'publish-item--failed' : ''
}
async function pollJob(jobId) {
  try {
    if (jobTimer) { clearTimeout(jobTimer); jobTimer = null }
    // 任务可能属于目标店（跨店发布），查进度必须带对应店铺，否则后端按当前店查不到 → 404
    const shopId = jobShopId.value || currentShopId.value
    currentJob.value = (await bulkApi.job(jobId, shopId)).result
    if (!['completed', 'completed_with_errors'].includes(currentJob.value.status)) jobTimer = setTimeout(() => pollJob(jobId), 1500)
    // 任务跑完后刷新一次发布状态, 让"已发布/失败"标记立刻跟上
    else void loadPublishStatus()
  } catch (error) { ElMessage.error(`读取发布进度失败：${error.message}`) }
}

async function retryFailedItems(itemIds = []) {
  if (!currentJob.value?.job_id || retrying.value) return
  retrying.value = true
  try {
    const shopId = jobShopId.value || currentShopId.value
    const result = await bulkApi.retry(currentJob.value.job_id, itemIds, shopId)
    ElMessage.success(result.message || '失败商品已重新排队')
    await pollJob(currentJob.value.job_id)
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    retrying.value = false
  }
}
onBeforeUnmount(() => { if (jobTimer) clearTimeout(jobTimer) })

// 回填商品ID到货盘表：先 dry-run 看要写哪些行(不落盘)，确认后才真的写。
// 后端只填空白单元格(人工填过的不覆盖)，写前自动备份，写完自检通过才替换原文件。
const writebacking = ref(false)
const escapeHtml = (text) => String(text ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

async function writebackHuopaiIds() {
  const jobId = currentJob.value?.job_id
  if (!jobId || writebacking.value) return
  const shopId = jobShopId.value || currentShopId.value
  writebacking.value = true
  try {
    const preview = await bulkApi.writebackHuopai(jobId, true, shopId)
    const info = preview.result || {}
    const sheetLines = Object.entries(info.sheets || {})
      .map(([name, sheet]) => `${name} ${sheet.rows} 行（${Object.values(sheet.columns || {}).join(' / ')} 列）`)
    const platformLines = Object.entries(info.platform_ids || {})
      .map(([platform, count]) => `${platform === 'wechat' ? '微信' : '小红书'} ${count} 个商品ID`)
    const ambiguous = Object.values(info.ambiguous || {}).flat()
    const html = [
      '把已发布商品的平台ID直接写入货盘表原文件，两种ID各写一列，之后不用再手工登记：',
      `· 「小红书产品id」「微信小店」= <b>商品ID</b>（款级，后台商品列表里那个，可直接搜索）`,
      `· 「小红书规格id」「微信规格id」= <b>规格ID</b>（行级，后台商品规格列表里那个，一行一个）`,
      `· 待写入 <b>${info.filled || 0}</b> 个单元格（商品ID ${info.filled_product || 0} + 规格ID ${info.filled_sku || 0}）：${escapeHtml(sheetLines.join('；') || '无')}`,
      `· 会覆盖 <b>${info.overwritten || 0}</b> 个原来填错的ID单元格（之前把规格ID填进商品ID列、或反之，这次纠正过来）`,
      `· 人工填的其它内容 <b>${info.existing || 0}</b> 个不动（中文备注等不会覆盖）`,
      `· 货盘表里对不上的行 <b>${info.mismatch || 0}</b> 个（跳过，宁可不写也不写错行）`,
      `· 可用商品ID：${escapeHtml(platformLines.join('、') || '无')}；规格ID 是按行回平台查商品详情得到的（同款不同规格ID不同）`,
      info.sku_missing
        ? `· ⚠️ 有 ${info.sku_missing} 行没查到对应规格ID（平台上该规格可能已被删，或编码不一致）`
        : '',
      Object.values(info.gone || {}).flat().length
        ? `· ⚠️ 这些商品在平台上已不存在（后台删过），本次不写它们的ID：${escapeHtml(Object.values(info.gone || {}).flat().slice(0, 8).join('、'))}；建议先点「核对发布状态」把本地记录更新掉`
        : '',
      (info.sku_failed || []).length
        ? `· ⚠️ 这些商品查平台详情失败，规格id没写：${escapeHtml((info.sku_failed || []).slice(0, 5).join('；'))}`
        : '',
      ambiguous.length
        ? `· ⚠️ 这些商品在平台上有多个ID，已取最近一次：${escapeHtml(ambiguous.join('、'))}`
        : '',
      '· 写入的ID列会自动取消隐藏并加宽（货盘表的「小红书产品id」「微信小店」原本是隐藏列，宽度只有6个字符，ID会溢出到相邻列上）',
      '· 缺「小红书规格id」「微信规格id」列时会自动补在表尾；写入前会自动备份原文件，可回退',
      `<br>文件：${escapeHtml(info.path || '')}`,
      '写入前会自动备份原文件；请先确认货盘表没有被 Excel/WPS 打开。',
    ].filter(Boolean).join('<br>')
    await ElMessageBox.confirm(html, '回填商品ID到货盘表', {
      type: 'warning', dangerouslyUseHTMLString: true, confirmButtonText: '开始回填', cancelButtonText: '取消',
    })
    const done = await bulkApi.writebackHuopai(jobId, false, shopId)
    const result = done.result || {}
    ElMessage.success(result.message || '已回填到货盘表')
  } catch (error) {
    // 取消确认框时 reject 的是 'cancel'/'close'，不当错误提示
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || String(error))
  } finally {
    writebacking.value = false
  }
}

function exportJobResult() {
  const items = currentJob.value?.items || []
  if (!items.length) return ElMessage.info('暂无发布结果可导出')
  // 带平台商品ID:发布成功后平台会生成 product_id,导出后可直接回平台核对/定位
  const headers = ['商品编码', '平台', '状态', '平台商品ID', '失败原因']
  const rows = items.map((it) => [
    it.product_code || '',
    it.platform === 'wechat' ? '微信' : '小红书',
    jobStatusText(it.status),
    it.platform_product_id || '',
    it.error || '',
  ])
  const csv = [headers, ...rows]
    .map((r) => r.map((c) => `"${String(c ?? '').replace(/"/g, '""')}"`).join(','))
    .join('\r\n')
  // 加 BOM,避免 Excel 打开中文乱码
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `发布结果_${currentJob.value?.job_id || 'job'}.csv`
  anchor.click()
  URL.revokeObjectURL(url)
  ElMessage.success(`已导出 ${items.length} 条发布结果`)
}

async function publish(partial = false) {
  if (errorCount.value) return ElMessage.warning('请先修正全部校验错误')
  const codes = partial ? [...selectedProductCodes.value] : null
  if (partial && !codes.length) return ElMessage.warning('请先勾选要发布的商品')
  // 必填项没选就不建任务：建了也是整批失败，运营还得重发一轮
  const gaps = platformSettingGaps(codes)
  if (gaps.length) {
    if (!crossShop.value) step.value = 2      // 直接带回「平台映射」，运费模板就在那一步顶部
    return ElMessage.warning(`还没选：${gaps.join('；')}，已阻止发布。请选好后再点发布`)
  }
  const crossShopPublishing = crossShop.value
  if (crossShopPublishing) {
    // 跨店发布是真实上架，选错店/用错模板的代价高：先把该选的参数校验齐，再让运营确认一次
    const missing = targetParamsMissingHint()
    if (missing) return ElMessage.warning(missing)
    const target = targetShop.value
    const platformText = platforms.value.map(platformLabel).join('、') || '未选择平台'
    const countText = codes ? `已勾选的 ${codes.length} 件商品` : `全部 ${products.value.length} 件商品`
    try {
      await ElMessageBox.confirm(
        `确认把${countText}发布到「${target?.name || targetShopId.value}」（${platformText}）？`
          + '类目/属性沿用当前批次映射，运费模板与物流方案使用目标店铺的设置。',
        '确认跨店发布',
        { type: 'warning', confirmButtonText: '确认发布', cancelButtonText: '取消' },
      )
    } catch {
      return   // 用户取消, 什么都不做
    }
  }
  loading.value = true
  try {
    await bulkApi.updateItems(batch.value.id, allItems.value)
    // 第 2 步刚选的运费模板/物流方案要一起存下去（发布用的是这份映射）
    syncPlatformSettingsIntoMappings(codes)
    await bulkApi.updateMappings(batch.value.id, mapping.value)
    const result = await bulkApi.publish(
      batch.value.id,
      platforms.value,
      codes,
      crossShopPublishing ? targetShopId.value : '',
      crossShopPublishing ? buildTargetParams() : null,
    )
    batch.value.status = '发布任务已创建'
    // 任务归属目标店：后续查进度/重试都要用它，否则会 404
    jobShopId.value = result.target_shop_id || currentShopId.value
    ElMessage.success(partial ? `已创建分批发布任务 (${codes.length} 件) ${result.job_id}` : `已创建发布任务 ${result.job_id}`)
    void pollJob(result.job_id)
  } catch (error) { ElMessage.error(error.message) } finally { loading.value = false }
}
</script>

<template>
  <div class="bulk-view">
    <div class="page-heading"><div><h1>批量发布商品</h1><p>一次导入标准商品，分别映射并发布到微信和小红书</p></div><div style="display:flex;gap:10px"><el-button :icon="Download" @click="downloadTemplate">下载模板</el-button><el-button v-if="step === 0" :icon="ArrowLeft" @click="goBackStep">返回商品列表</el-button></div></div>
    <el-steps :active="step" finish-status="success" class="bulk-steps"><el-step v-for="title in steps" :key="title" :title="title" /></el-steps>
    <div v-if="step === 1" class="bulk-filters">
      <el-input v-model="itemKeyword" clearable :prefix-icon="Search" placeholder="搜索商品编码、标题或 SKU" @clear="resetItemPage" @input="resetItemPage" />
      <el-select v-model="itemErrorFilter" style="width: 215px" @change="resetItemPage"><el-option label="全部记录" value="all" /><el-option label="仅看异常" value="error" /><el-option label="仅看通过" value="valid" /><el-option :label="`仅看缺规格图 (${missingImageCount} 行)`" value="no_image" /><el-option :label="`仅看缺主图 (${missingMainImageCount} 个商品)`" value="no_main_image" /></el-select>
      <span class="muted-copy">匹配 {{ filteredItems.length }} 条</span>
    </div>
    <div v-if="step === 2" class="bulk-filters">
      <el-button type="primary" :loading="categoryMatching" @click="autoMatchWechatCategories">立即匹配{{ platformNameText }}类目</el-button>
      <el-button :loading="attrConfigLoading" @click="loadGroupAttrDefs()">重新加载属性</el-button>
      <el-button @click="aliasPanelVisible = true">核对类目映射表</el-button>
    </div>
    <div v-if="step === 2" class="bulk-filters">
      <template v-if="availablePlatforms.includes('wechat')">
        <span>微信运费模板</span>
        <el-select v-model="batchPlatformSettings.wechatFreightId" placeholder="选择微信运费模板" style="width:220px" :class="{ 'setting-required-select': !batchPlatformSettings.wechatFreightId }"><el-option v-for="item in (platformSettings.wechatFreight || [])" :key="settingId(item)" :label="optionName(item)" :value="settingId(item)" /></el-select>
        <span v-if="!batchPlatformSettings.wechatFreightId" class="setting-required-hint">必选，没选不能发布</span>
      </template>
      <template v-if="availablePlatforms.includes('xhs')">
        <span>小红书运费模板 / 物流方案</span>
        <el-select v-model="batchPlatformSettings.xhsShippingId" placeholder="选择运费模板" style="width:220px" :class="{ 'setting-required-select': !batchPlatformSettings.xhsShippingId }"><el-option v-for="item in platformSettings.xhsShipping" :key="settingId(item)" :label="optionName(item)" :value="settingId(item)" /></el-select>
        <el-select v-model="batchPlatformSettings.xhsLogisticsId" placeholder="选择物流方案" style="width:220px" :class="{ 'setting-required-select': !batchPlatformSettings.xhsLogisticsId }"><el-option v-for="item in platformSettings.xhsLogistics" :key="settingId(item)" :label="optionName(item)" :value="settingId(item)" /></el-select>
        <span v-if="!batchPlatformSettings.xhsShippingId || !batchPlatformSettings.xhsLogisticsId" class="setting-required-hint">必选，没选不能发布</span>
      </template>
    </div>
    <div v-if="step === 2 && availablePlatforms.includes('wechat') && mappingGroups.some((group) => group.mapping.wechat_candidates?.length > 1)" class="mapping-confirm-list">
      <div v-for="group in mappingGroups.filter((item) => item.mapping.wechat_candidates?.length > 1)" :key="group.internal_category" class="mapping-confirm-row">
        <span>{{ group.internal_category }}（{{ group.products.length }} 个商品）</span>
        <el-select placeholder="选择微信叶子类目" @change="(value) => confirmWechatCategory(group, value)"><el-option v-for="candidate in group.mapping.wechat_candidates" :key="candidate.path" :label="candidate.path" :value="candidate.path" /></el-select>
      </div>
    </div>
    <!-- 小红书类目确认列表：必须限定当前店发小红书。
         拆店后批次可能留在微信店，而 mappings_json 里仍带着拆店前算出的 xhs_status，
         不判断平台就会在纯微信店里冒出一整屏「小红书：xxx 读取类目」，让人以为要发小红书。 -->
    <div v-if="step === 2 && availablePlatforms.includes('xhs')" class="mapping-confirm-list">
      <div v-for="group in mappingGroups.filter((item) => item.mapping.xhs_status && !item.mapping.xhs_status.includes('已自动') && !item.mapping.xhs_status.includes('已人工') && !item.mapping.xhs_status.includes('已按映射表'))" :key="`xhs-${group.internal_category}`" class="mapping-confirm-row">
        <span>小红书：{{ group.internal_category }}（{{ group.products.length }} 个商品）</span>
        <el-button size="small" @click="loadXhsLevel(group, 0)">读取类目</el-button>
        <template v-for="(options, level) in (xhsCascade[group.internal_category]?.levels || [])" :key="level"><el-select :model-value="xhsCascade[group.internal_category]?.selected?.[level]" :placeholder="`第${level + 1}级类目`" @change="(value) => confirmXhsCategory(group, level, value)"><el-option v-for="item in options" :key="item.id || item.categoryId" :label="item.name" :value="item.id || item.categoryId" /></el-select></template>
      </div>
    </div>
    <div v-if="step === 2 && attrGroupTotal" class="bulk-filters" style="margin: 12px 0">
      <el-input v-model="attrGroupKeyword" clearable :prefix-icon="Search" placeholder="搜索内部类目" style="width: 240px" />
      <el-select v-model="attrGroupFilter" style="width: 180px"><el-option label="全部类目" value="all" /><el-option :label="`仅看未匹配 (${unmatchedCategoryCount})`" value="unmatched" /></el-select>
      <span class="muted-copy">共 {{ attrGroupTotal }} 个类目 · {{ unmatchedCategoryCount }} 个有未匹配属性</span>
    </div>
    <el-table v-if="step === 2 && attrGroupTotal" :data="attrGroupRows" max-height="420" size="small" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }">
      <el-table-column prop="internal_category" label="内部类目" min-width="260" show-overflow-tooltip />
      <el-table-column prop="product_count" label="商品数" width="90" align="center" />
      <el-table-column label="属性匹配" width="170" align="center"><template #default="{ row }"><el-tag v-if="row.has_status" size="small" :type="row.unmatched ? 'warning' : 'success'">匹配 {{ row.matched }}✓{{ row.unmatched ? ` / ${row.unmatched}✗` : '' }}</el-tag><span v-else-if="!row.has_category" class="muted-copy">未匹配类目</span><span v-else class="muted-copy">待加载</span></template></el-table-column>
      <el-table-column label="操作" width="140" align="center"><template #default="{ row }"><el-button v-if="row.has_category" size="small" type="primary" text @click="openAttrDrawer(row.internal_category)">配置属性</el-button><el-tooltip v-else placement="top" content="该类目还没匹配到平台的类目，匹配完成后才会加载属性定义；请在上方「读取类目」里完成匹配"><span class="muted-copy">先匹配类目</span></el-tooltip></template></el-table-column>
    </el-table>
    <el-drawer v-model="attrDrawerVisible" append-to-body :title="`${attrDrawerGroup} · 属性配置`" size="720px">
      <template v-if="groupAttrDefs[attrDrawerGroup]">
        <div v-if="groupAttrDefs[attrDrawerGroup]?.wechat?.attr_defs?.length" style="margin-bottom: 20px">
          <h4 style="margin: 0 0 8px">微信商品属性 <small class="muted-copy">(未匹配的可手动填写)</small></h4>
          <div class="form-grid form-grid-2">
            <div v-for="a in groupAttrDefs[attrDrawerGroup].wechat.attr_defs" :key="a.name" class="attr-config-item">
              <label>{{ a.is_required ? '* ' : '' }}{{ a.name }}<small v-if="a.type_v2" class="muted-copy"> ({{ wxTypeLabel(a.type_v2) }})</small></label>
              <el-select v-if="a.type_v2 === 'select_many'" multiple :model-value="groupAttrDefs[attrDrawerGroup].wechat.defaults[a.name]" placeholder="请选择" @update:model-value="(v) => setGroupWxAttr(attrDrawerGroup, a.name, v)"><el-option v-for="opt in attrOptions(a.value)" :key="opt" :label="opt" :value="opt" /></el-select>
              <el-select v-else-if="a.type_v2 === 'select_one'" :model-value="groupAttrDefs[attrDrawerGroup].wechat.defaults[a.name]" placeholder="请选择" @update:model-value="(v) => setGroupWxAttr(attrDrawerGroup, a.name, v)"><el-option v-for="opt in attrOptions(a.value)" :key="opt" :label="opt" :value="opt" /></el-select>
              <el-input-number v-else-if="a.type_v2?.includes('integer') || a.type_v2?.includes('decimal')" :model-value="groupAttrDefs[attrDrawerGroup].wechat.defaults[a.name]" @update:model-value="(v) => setGroupWxAttr(attrDrawerGroup, a.name, v)" />
              <el-input v-else :model-value="groupAttrDefs[attrDrawerGroup].wechat.defaults[a.name]" placeholder="请输入" @update:model-value="(v) => setGroupWxAttr(attrDrawerGroup, a.name, v)" />
            </div>
          </div>
        </div>
        <div v-if="groupAttrDefs[attrDrawerGroup]?.xhs?.attr_defs?.length" style="margin-bottom: 20px">
          <h4 style="margin: 0 0 8px">小红书商品属性 <small class="muted-copy">(未匹配的可手动选择)</small></h4>
          <div class="form-grid form-grid-2">
            <div v-for="a in groupAttrDefs[attrDrawerGroup].xhs.attr_defs" :key="a.id" class="attr-config-item">
              <label>{{ a.isRequired ? '* ' : '' }}{{ a.name }}<small v-if="a.isMulti" class="muted-copy"> (多选)</small></label>
              <el-select v-if="a.isMulti" multiple :model-value="groupAttrDefs[attrDrawerGroup].xhs.defaults[a.id]" placeholder="请选择" @update:model-value="(v) => setGroupXhsAttr(attrDrawerGroup, a.id, v)"><el-option v-for="c in (groupAttrDefs[attrDrawerGroup].xhs.candidates[a.id] || [])" :key="c.valueId" :label="c.valueName" :value="c.valueId" /></el-select>
              <el-select v-else-if="a.inputType === 1 && (groupAttrDefs[attrDrawerGroup].xhs.candidates[a.id] || []).length" :model-value="groupAttrDefs[attrDrawerGroup].xhs.defaults[a.id]" placeholder="请选择" @update:model-value="(v) => setGroupXhsAttr(attrDrawerGroup, a.id, v)"><el-option v-for="c in (groupAttrDefs[attrDrawerGroup].xhs.candidates[a.id] || [])" :key="c.valueId" :label="c.valueName" :value="c.valueId" /></el-select>
              <el-input v-else :model-value="groupAttrDefs[attrDrawerGroup].xhs.defaults[a.id]" placeholder="请输入" @update:model-value="(v) => setGroupXhsAttr(attrDrawerGroup, a.id, v)" />
            </div>
          </div>
        </div>
        <div v-if="groupAttrDefs[attrDrawerGroup]?.xhs?.var_defs?.length">
          <h4 style="margin: 0 0 8px">小红书规格映射 <small class="muted-copy">(可手动调整)</small></h4>
          <div class="form-grid form-grid-2">
            <div v-for="v in groupAttrDefs[attrDrawerGroup].xhs.var_defs" :key="v.id" class="attr-config-item">
              <label>{{ v.name }}</label>
              <el-select :model-value="groupAttrDefs[attrDrawerGroup].xhs.spec_map[v.id] || ''" placeholder="不映射" @update:model-value="(val) => setGroupSpecMap(attrDrawerGroup, v.id, val)"><el-option label="不映射" value="" /><el-option v-for="dim in groupSpecDimensions(attrDrawerGroup)" :key="dim" :label="dim" :value="dim" /></el-select>
            </div>
          </div>
        </div>
        <div class="form-actions">
          <el-button type="primary" @click="attrDrawerVisible = false">完成</el-button>
        </div>
      </template>
    </el-drawer>
    <section v-if="step === 0" class="content-panel bulk-card"><div class="bulk-drop" @click="pickExcelFile"><el-icon><Upload /></el-icon><h3>选择商品 Excel</h3><p>每个 SKU 一行</p><div class="bulk-drop__picker"><el-button type="primary" :icon="Upload" @click.stop="pickExcelFile">选择文件</el-button><span v-if="file" class="bulk-drop__filename">{{ file.name }}</span><span v-else class="muted-copy">未选择文件</span></div><input ref="excelInput" type="file" accept=".xlsx,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv" style="display:none" @change="file = $event.target.files[0]" /></div><div class="bulk-import-row"><el-button type="primary" :loading="loading" @click="importFile">导入并校验 <el-icon><Right /></el-icon></el-button><el-select v-model="huopaiPath" placeholder="选择服务器货盘表" clearable><el-option v-for="item in huopaiFiles" :key="item.path" :label="item.name" :value="item.path" /></el-select><el-button :loading="loading" @click="importHuopai">直接导入货盘表</el-button></div><div style="display:flex;gap:8px;align-items:center;margin-top:12px;flex-wrap:wrap"><span class="muted-copy">商品图片根目录</span><el-select v-model="imageRoot" placeholder="默认（服务端配置的图片根目录）" clearable style="width:400px"><el-option v-for="item in imageRootOptions" :key="item.path" :label="item.available ? item.path : `${item.path}（不可访问）`" :value="item.path" :disabled="!item.available" /></el-select><el-select v-model="imageMonths" multiple collapse-tags placeholder="全部月份" style="width:210px"><el-option v-for="m in IMAGE_MONTH_OPTIONS" :key="m" :label="m" :value="m" /></el-select><el-button :loading="scanning" :disabled="!batch?.id" @click="scanImages(false)">重新扫描图片</el-button></div><div class="folder-upload" style="margin-top: 12px"><label class="el-button el-button--default"><span>选择商品图片文件夹</span><input type="file" webkitdirectory directory multiple accept="image/*" hidden @change="readFolderFiles($event.target.files)" /></label><span v-if="folderFiles.length" class="muted-copy">已选择 {{ folderFiles.length }} 张商品图片</span><label class="el-button el-button--default" style="margin-left: 8px"><span>选择通用详情图文件夹</span><input type="file" webkitdirectory directory multiple accept="image/*" hidden @change="readCommonDetailFiles($event.target.files)" /></label><span v-if="commonDetailFiles.length" class="muted-copy">已选择 {{ commonDetailFiles.length }} 张通用详情图</span></div></section>
    <section v-else-if="step === 1" class="content-panel bulk-card"><input ref="skuImageInput" type="file" accept="image/*" style="display:none" @change="onSkuImagePicked" /><input ref="mainImageInput" type="file" accept="image/*" multiple style="display:none" @change="onMainImagePicked" /><div class="bulk-summary"><span>批次 {{ batch.id }}</span><el-tag type="success">可发布 {{ validCount }}</el-tag><el-tag type="danger">错误 {{ errorCount }}</el-tag><el-tag v-if="missingMainImageCount" type="warning">缺主图 {{ missingMainImageCount }} 个商品</el-tag><el-tag v-if="missingImageCount" type="warning">缺规格图 {{ missingImageCount }} 行</el-tag></div><el-alert v-if="imageScanResult" :type="(imageScanResult.unmatched_product_count || imageScanResult.unmatched_sku_count) ? 'warning' : 'success'" :closable="false" show-icon style="margin-bottom:12px"><template #title>图片匹配：商品 {{ imageScanResult.products_matched }}/{{ imageScanResult.products_total }}，SKU {{ imageScanResult.skus_matched }}/{{ imageScanResult.skus_total }}</template><template #default><div>扫描 {{ imageScanResult.folders }} 个商品文件夹 / {{ imageScanResult.scanned_images }} 张图；通用详情图 {{ imageScanResult.common_detail_images }} 张。</div><div v-if="imageScanResult.unmatched_product_count">有 {{ imageScanResult.unmatched_product_count }} 个商品没匹配到主图：发布会被平台以「至少需要一张主图」拒绝，需在下方列表点「缺主图·点击补图」补齐。</div><div v-if="imageScanResult.unmatched_sku_count">有 {{ imageScanResult.unmatched_sku_count }} 个 SKU 没匹配到规格图：图库里没有该款，需在下方列表点「缺图·点击补图」补齐。</div></template></el-alert><el-table :data="items" max-height="520" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }"><el-table-column prop="line" label="行" width="70" /><el-table-column prop="product_code" label="商品编码" width="150" show-overflow-tooltip /><el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip /><el-table-column prop="sku_code" label="SKU编码" width="150" show-overflow-tooltip /><el-table-column label="主图" width="100" align="center"><template #default="{ row }"><el-image v-if="(row.main_images || []).length" :src="bulkApi.imagePreviewUrl(row.main_images[0])" :preview-src-list="row.main_images.map((ref) => bulkApi.imagePreviewUrl(ref))" preview-teleported fit="cover" style="width:54px;height:54px;border-radius:4px" :title="`共 ${row.main_images.length} 张主图，点击可预览`" /><el-tag v-else-if="!(productMainImages[row.product_code] || []).length" type="warning" size="small" style="cursor:pointer" title="该商品没有任何主图，发布会被平台以「至少需要一张主图」拒绝；点击上传主图，可一次多选（只补空白行，已有主图的行不动）" @click="pickMainImage(row)">{{ uploadingMainImage ? '上传中…' : '缺主图·点击补图' }}</el-tag><span v-else class="muted-copy" style="font-size:11px;line-height:1.4" title="主图是商品级：同商品已有主图，本行留空不影响发布">同商品已填</span><div v-if="(row.main_images || []).length" class="muted-copy" style="font-size:11px;line-height:1.4">共 {{ row.main_images.length }} 张<span style="margin-left:4px;cursor:pointer;color:var(--el-color-primary)" title="换主图，可一次多选（会替换该商品现有全部主图，需确认）" @click="pickMainImage(row, 'replace')">换图</span></div></template></el-table-column><el-table-column label="规格图" width="120" align="center"><template #default="{ row }"><el-image v-if="row.sku_image" :src="bulkApi.imagePreviewUrl(row.sku_image)" :preview-src-list="[bulkApi.imagePreviewUrl(row.sku_image)]" preview-teleported fit="cover" style="width:54px;height:54px;border-radius:4px" /><el-tag v-else type="warning" size="small" style="cursor:pointer" title="点击上传该 SKU 的规格图" @click="pickSkuImage(row)">{{ uploadingSkuImage ? '上传中…' : '缺图·点击补图' }}</el-tag></template></el-table-column><el-table-column prop="price" label="售价" width="100" /><el-table-column prop="stock" label="库存" width="90" /><el-table-column label="校验结果" min-width="220" show-overflow-tooltip><template #default="{ row }"><el-tag v-if="row.errors?.length" type="danger">{{ row.errors.join('；') }}</el-tag><el-tag v-else type="success">通过</el-tag></template></el-table-column><template v-if="requiredAttrNames.length"><el-table-column v-for="attrName in requiredAttrNames" :key="attrName" :label="`*${attrName}`" width="160" show-overflow-tooltip><template #default="{ row }"><span class="muted-copy">{{ getProductAttrValue(row, attrName) || '继承组级默认' }}</span></template></el-table-column></template></el-table><div class="form-actions"><el-button :icon="ArrowLeft" @click="goBackStep">返回上一步</el-button><el-button type="primary" :loading="loading" @click="validateBatch">重新校验并继续</el-button></div></section>
    <section v-else-if="step === 2" class="content-panel bulk-card"><h3>平台映射</h3><el-table :data="pagedMappingRows" max-height="520" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }"><el-table-column prop="product_code" label="商品编码" min-width="150" show-overflow-tooltip /><el-table-column prop="internal_category" label="Excel 内部类目" min-width="150" show-overflow-tooltip /><el-table-column v-if="availablePlatforms.includes('wechat')" label="微信类目" min-width="280" show-overflow-tooltip><template #default="{ row }"><el-tag type="info">{{ row.mapping.wechat_category || '待系统匹配' }}</el-tag></template></el-table-column><el-table-column v-if="availablePlatforms.includes('xhs')" label="小红书类目" min-width="280" show-overflow-tooltip><template #default="{ row }"><el-tag type="info">{{ row.mapping.xhs_category || '待系统匹配' }}</el-tag></template></el-table-column><el-table-column label="匹配状态" min-width="110" show-overflow-tooltip><template #default="{ row }"><el-tag type="warning">{{ row.mapping.status || '待处理' }}</el-tag></template></el-table-column><template v-if="availablePlatforms.includes('wechat') && requiredAttrNames.length"><el-table-column v-for="attrName in requiredAttrNames" :key="attrName" :label="`*${attrName}`" min-width="100" show-overflow-tooltip><template #default="{ row }"><span class="muted-copy">{{ getProductAttrValue(row, attrName) || '未配置' }}</span></template></el-table-column></template></el-table><div class="form-actions"><el-button :icon="ArrowLeft" @click="goBackStep">返回上一步</el-button><el-button type="primary" :loading="loading" @click="saveAndNext">保存自动映射并继续</el-button></div></section>
    <section v-else class="content-panel bulk-card"><h3>确认发布</h3><p>默认先创建微信草稿；小红书创建商品和 SKU 后进入审核，不自动上架。</p><el-alert v-if="productWarningCount" type="warning" :closable="false" show-icon :title="`${productWarningCount} 个商品存在 SKU 间属性不一致提醒`" description="商品属性是 SPU 级，各 SKU 行填了不同值时仅第一行生效。点“审核编辑”查看具体提醒，必要时把该差异提升为规格维度。" style="margin-bottom:12px" /><div class="platform-picker"><span class="platform-picker__label">目标店铺</span><el-select v-model="targetShopId" style="width:340px" :loading="targetSettingsLoading" @change="onTargetShopChange"><el-option v-for="shop in targetShopOptions" :key="shop.shop_id" :label="`${shop.name}（${shopPlatformText(shop)}）`" :value="shop.shop_id" /></el-select><span class="muted-copy">默认 = 当前店铺；换成同平台的其他店即为跨店发布</span></div><div class="platform-picker"><span class="platform-picker__label">发布到平台</span><el-checkbox-group v-model="platforms"><el-checkbox-button v-if="availablePlatforms.includes('wechat')" label="wechat">微信</el-checkbox-button><el-checkbox-button v-if="availablePlatforms.includes('xhs')" label="xhs">小红书</el-checkbox-button></el-checkbox-group><span class="muted-copy">可多选，至少选一个</span></div><div v-if="crossShop" class="platform-picker"><span class="platform-picker__label">目标店参数</span><template v-if="availablePlatforms.includes('xhs')"><span class="muted-copy">运费模板</span><el-select v-model="targetShopSettings.xhsShippingId" :loading="targetSettingsLoading" style="width:260px"><el-option v-for="item in targetPlatformSettings.xhsShipping" :key="settingId(item)" :label="optionName(item)" :value="settingId(item)" /></el-select><span class="muted-copy">物流方案</span><el-select v-model="targetShopSettings.xhsLogisticsId" :loading="targetSettingsLoading" style="width:230px"><el-option v-for="item in targetPlatformSettings.xhsLogistics" :key="settingId(item)" :label="optionName(item)" :value="settingId(item)" /></el-select></template><template v-if="availablePlatforms.includes('wechat')"><span class="muted-copy">运费模板</span><el-select v-model="targetShopSettings.wechatFreightId" :loading="targetSettingsLoading" style="width:260px"><el-option v-for="item in targetPlatformSettings.wechatFreight" :key="settingId(item)" :label="optionName(item)" :value="settingId(item)" /></el-select></template><span class="muted-copy">跨店发布：类目/属性沿用当前批次映射，运费模板与物流方案按目标店铺选择</span></div><el-alert v-if="errorCount" type="error" :closable="false" show-icon title="仍有校验错误，不能发布" /><div class="bulk-filters" style="margin-bottom:12px"><el-input v-model="productKeyword" clearable :prefix-icon="Search" placeholder="搜索商品编码、标题或内部类目" style="width:300px" @clear="resetProductPage" @input="resetProductPage" /><el-select v-model="productPublishFilter" style="width:205px" @change="resetProductPage"><el-option :label="`全部 (${products.length})`" value="all" /><el-option :label="`仅看未发布 (${publishCounts.none})`" value="none" /><el-option :label="`仅看已发布 (${publishCounts.success})`" value="success" /><el-option :label="`仅看发布失败 (${publishCounts.failed})`" value="failed" /><el-option :label="`仅看发布中 (${publishCounts.pending})`" value="pending" /></el-select><el-button :loading="publishStatusLoading" @click="loadPublishStatus">刷新发布状态</el-button><el-button :loading="verifyingPublishStatus" title="把本地“已发布”记录拿去平台核对：在后台删掉的商品会自动改回“未发布”，可以重新发布" @click="verifyPublishStatus('verify')">核对发布状态</el-button><el-button v-if="selectedProductCodes.size" link type="warning" :loading="verifyingPublishStatus" title="确认这些商品已在平台后台删除，直接改回“未发布”（小红书核对不出时用它兜底）" @click="verifyPublishStatus('reset')">标记未发布 ({{ selectedProductCodes.size }})</el-button><span class="muted-copy">筛选后 {{ filteredProducts.length }} 件</span></div><div class="bulk-selection-bar"><div class="bulk-selection-bar__info"><span>共 <strong>{{ products.length }}</strong> 件商品，筛选后 <strong>{{ filteredProducts.length }}</strong> 件，已选 <strong>{{ selectedProductCodes.size }}</strong> 件</span><el-button link size="small" @click="toggleSelectAllFiltered">{{ allFilteredSelected ? '取消全选筛选结果' : '全选筛选结果' }}</el-button><el-button v-if="selectedProductCodes.size" link size="small" @click="clearSelection">清空选择</el-button><span v-if="selectedPublished.length" style="color:#e6a23c">⚠ 已选中有 {{ selectedPublished.length }} 件已经发布过，发布会因标题重复被平台拒</span><el-button v-if="selectedPublished.length" link type="warning" size="small" @click="dropPublishedSelection">移除已发布的</el-button></div></div><el-table ref="productTableRef" :data="pagedProducts" row-key="product_code" max-height="360" style="margin-bottom:16px" :row-class-name="productPublishRowClass" @selection-change="onSelectionChange" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }"><el-table-column type="selection" width="50" reserve-selection /><el-table-column prop="product_code" label="商品编码" width="150" show-overflow-tooltip /><el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip /><el-table-column prop="internal_category" label="内部类目" min-width="180" show-overflow-tooltip /><el-table-column label="发布状态" width="110" align="center"><template #default="{ row }"><el-tag v-if="productPublishState(row.product_code) === 'success'" type="success" size="small" :title="productPublishedAt(row.product_code) ? `已经在所选平台发布过（最近成功：${formatPublishTime(productPublishedAt(row.product_code))}），再发会被平台以「标题重复」拒绝` : '该商品已经发布成功过，再发会被平台以「标题重复」拒绝'">已发布</el-tag><el-tag v-else-if="productPublishState(row.product_code) === 'pending'" type="info" size="small">发布中</el-tag><el-tag v-else-if="productPublishState(row.product_code) === 'failed'" type="danger" size="small" title="发过但失败了，看下方发布进度的失败原因">发布失败</el-tag><span v-else class="muted-copy">未发布</span></template></el-table-column><el-table-column label="属性状态" width="130" show-overflow-tooltip><template #default="{ row }"><el-tag :type="productAttrTagType(row)" size="small">{{ productAttrCount(row) }} 项已配</el-tag></template></el-table-column><el-table-column label="SKU 提醒" width="110"><template #default="{ row }"><el-tooltip v-if="row.warnings?.length" placement="top"><template #content><div v-for="(w, i) in row.warnings" :key="i">{{ w }}</div></template><el-tag type="warning" size="small">{{ row.warnings.length }} 项不一致</el-tag></el-tooltip><span v-else class="muted-copy">--</span></template></el-table-column><el-table-column label="操作" width="100" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="openReview(row)">审核编辑</el-button></template></el-table-column></el-table><el-alert v-if="publishGuardGaps.length" type="error" :closable="false" show-icon style="margin:12px 0 0" title="以下必填项没选，发布会被平台整批拒掉，已阻止发布"><div v-for="gap in publishGuardGaps" :key="gap">{{ gap }}</div><div style="margin-top:6px"><el-button link type="primary" :icon="ArrowLeft" @click="step = 2">去「平台映射」选择运费模板 / 物流方案</el-button></div></el-alert><div class="form-actions"><el-button :icon="ArrowLeft" @click="goBackStep">返回上一步</el-button><div style="display:flex;gap:8px"><el-button :loading="loading" :disabled="!platforms.length || errorCount > 0 || !selectedProductCodes.size" title="优先执行：选中的这几件会插到队列前面，不必排在其它批次的大任务后面" @click="publish(true)">发布选中 ({{ selectedProductCodes.size }}) · 优先</el-button><el-button type="primary" :loading="loading" :disabled="!platforms.length || errorCount > 0 || publishGuardGaps.length" :icon="Check" @click="publish(false)">全部发布 ({{ products.length }})</el-button></div></div></section>
    <section v-if="currentJob" class="content-panel bulk-card" style="margin-top:16px">
      <div class="bulk-summary"><strong>发布进度</strong><span>{{ currentJob.processed }}/{{ currentJob.total }}</span><el-tag>{{ jobStatusText(currentJob.status) }}</el-tag><el-button v-if="currentJob.items?.length" size="small" @click="exportJobResult">导出结果</el-button><el-button v-if="currentJob.items?.some((item) => item.platform_product_id)" size="small" type="primary" plain :loading="writebacking" @click="writebackHuopaiIds">回填ID到货盘表</el-button><el-button v-if="currentJob.items?.some((item) => item.status === 'failed')" size="small" type="warning" :loading="retrying" @click="retryFailedItems()">重试全部失败</el-button></div>
      <el-progress :percentage="currentJob.total ? Math.round(currentJob.processed * 100 / currentJob.total) : 0" />
      <el-table :data="currentJob.items" max-height="320" style="margin-top:12px" :row-class-name="jobItemRowClass" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }">
        <el-table-column prop="product_code" label="商品编码" show-overflow-tooltip />
        <el-table-column label="平台" width="100"><template #default="{ row }">{{ row.platform === 'wechat' ? '微信' : '小红书' }}</template></el-table-column>
        <el-table-column label="状态" width="165" show-overflow-tooltip><template #default="{ row }"><el-tag :type="row.status === 'success' ? 'success' : row.status === 'failed' || row.status === 'partial' ? 'danger' : 'info'">{{ jobStatusText(row.status) }}</el-tag><el-tag v-if="row.priority" size="small" type="warning" style="margin-left:4px" title="分批发布的选中项：优先于其它批次的大任务执行">优先</el-tag></template></el-table-column>
        <el-table-column prop="platform_product_id" label="平台商品ID" width="180" show-overflow-tooltip />
        <el-table-column prop="error" label="失败原因" min-width="280" show-overflow-tooltip />
        <el-table-column label="操作" width="90" fixed="right"><template #default="{ row }"><el-button v-if="row.status === 'failed'" link type="primary" :loading="retrying" @click="retryFailedItems([row.id])">重试</el-button><span v-else class="muted-copy">--</span></template></el-table-column>
      </el-table>
    </section>
    <ProductReviewDrawer v-model:visible="reviewVisible" :product="reviewProduct" :product-mapping="mapping.products?.[reviewProduct?.product_code] || {}" :group-config="getReviewGroupConfig(reviewProduct)" :platforms="platforms" :batch-id="batch?.id || ''" @save="handleReviewSave" />
    <div v-if="step === 1" class="bulk-pagination"><el-pagination v-model:current-page="itemPage" v-model:page-size="itemPageSize" :page-sizes="[20, 50, 100]" layout="total, sizes, prev, pager, next, jumper" :total="filteredItems.length" /></div>
    <div v-if="step === 2" class="bulk-pagination"><el-pagination v-model:current-page="mappingPage" v-model:page-size="mappingPageSize" :page-sizes="[20, 50, 100]" layout="total, sizes, prev, pager, next, jumper" :total="mappingRows.length" /></div>
    <div v-if="step === 3" class="bulk-pagination"><el-pagination v-model:current-page="productPage" v-model:page-size="productPageSize" :page-sizes="[50, 100, 200]" layout="total, sizes, prev, pager, next, jumper" :total="filteredProducts.length" /></div>
    <el-dialog v-model="aliasPanelVisible" title="类目映射表" width="920px">
      <CategoryAliasPanel dialog />
    </el-dialog>
  </div>
</template>
