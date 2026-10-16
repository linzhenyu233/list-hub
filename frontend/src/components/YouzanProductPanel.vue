<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Check, Goods, Picture, Top, Bottom, View } from '@element-plus/icons-vue'
import { youzanApi } from '../youzanApi'
// 编辑保存走中台（单位换算 / 库存增量 / "接口说成功但没生效"的回读校验都在中台）
import { bulkApi } from '../bulkApi'
import { currentShop } from '../shopContext'
import { useIsMobile } from '../useResponsive'
import ProductSearchBar from './ProductSearchBar.vue'
import EllipsisText from './EllipsisText.vue'
// 详情版式与小红书/微信共用同一个组件（三个平台视觉对齐，不再各写一套）
import ProductDetailView from './ProductDetailView.vue'

// 有赞商品列表（只读巡检 + 上下架）。
// 没做单商品发布页：有赞官方后台本身就是一条条发，单条场景运营直接用官方后台，
// 我们的重心是货盘批量发布。列表页解决的是"看状态还得登有赞后台"的问题。
//
// ⚠️ 字段名属 ★待核对项（youzan.items.onsale.get / inventory.get 的真实返回结构
// 没经过真机核对），下面所有取值都对多种写法做了兼容；联调时按真实报文字段收紧即可。
const props = defineProps({ refreshTick: { type: Number, default: 0 } })
const isMobile = useIsMobile()

const loading = ref(false)
const serviceOnline = ref(false)
const serviceMode = ref('')           // live / mock（mock=联调模式，不发外网请求，无需真凭证）
const tokenConfigured = ref(false)
// token 查询失败的具体原因（服务活着但这家店的凭证查不了时，要给运营看原因而不是笼统报"未连接"）
const tokenError = ref('')
const loadVersion = ref(0)
const actionId = ref('')

const items = ref([])
const stateTab = ref('onsale')          // onsale=出售中 inventory=仓库中（服务端筛选）
const totalOnsale = ref(0)
const totalInventory = ref(0)
const page = ref(1)
const pageSize = ref(20)
const serverTotal = ref(0)

// 当前店是否开通有赞：没开通直接给提示，别拿着空凭证去打接口
const shopHasYouzan = computed(() => {
  const yz = currentShop.value?.youzan
  return Boolean(yz?.client_id || yz?.kdt_id || yz?.mode === 'mock')
})

// ---- 字段兼容取值 ----------------------------------------------------------
function listFrom(data, keys) {
  const source = data?.result ?? data
  if (Array.isArray(source)) return source
  if (!source || typeof source !== 'object') return []
  for (const key of keys) {
    const value = source[key]
    if (Array.isArray(value)) return value
    if (value && typeof value === 'object') {
      for (const inner of Object.values(keys)) {
        if (Array.isArray(value[inner])) return value[inner]
      }
    }
  }
  return []
}
function totalOf(data) {
  const source = data?.result ?? data
  // 真机（2026-10-15）商品列表返回 {"count": N, "items": [...]} —— 总数键是 count
  const total = source?.count ?? source?.total_count ?? source?.total_results ?? source?.total
  return Number.isFinite(Number(total)) ? Number(total) : null
}
// ✅ 2026-10-15 真机核对（云测试店铺发了一个商品后的真实报文）：
//   列表 `youzan.items.onsale.get` → {"count": N, "items":[{item_id,title,item_no,price,
//     quantity,image,item_imgs:[{url,thumbnail,medium,...}],is_listing,alias,detail_url,...}]}
//     ——注意列表**不带 skus**，价格/库存在商品级；
//   详情 `youzan.item.get` → {"item":{...}}（外面多包一层 item），里面有 skus/sku_images/
//     delivery_template_info/video 等。
function unwrapItem(input) {
  const raw = input && typeof input === 'object' ? input : {}
  return raw.item && typeof raw.item === 'object' ? raw.item : raw
}
function collectImages(raw) {
  const out = []
  // item_imgs 是对象数组，图片地址在 url（thumbnail/medium 是缩略图，优先取原图 url）
  for (const one of Array.isArray(raw.item_imgs) ? raw.item_imgs : []) {
    const url = one?.url || one?.medium || one?.thumbnail
    if (url) out.push(String(url))
  }
  for (const one of [raw.image, raw.pic_url, raw.pic_thumb_url, raw.image_urls, raw.pics, raw.images]) {
    for (const url of Array.isArray(one) ? one : one ? [one] : []) {
      if (url && !out.includes(String(url))) out.push(String(url))
    }
  }
  return out
}
// ★真机修正（2026-10-15）：`youzan.items.onsale.get` 返回的报文里**没有 `is_listing`**。
//   原来用 `raw.is_listing === 1` 判状态 → 从「出售中」接口取回的商品全被标成"仓库中"、
//   操作列全变成"上架"（运营在有赞后台看到是销售中，回我们页面看是仓库中，直接看懵）。
//   真相是"这条数据是哪个接口返回的"：有赞把状态拆成 onsale / inventory 两个接口，
//   所以**以当前 tab 为准**；只有报文真的带这个字段时才用它覆盖（兼容平台以后补上）。
function listingOf(raw, fallback) {
  const value = raw.is_listing ?? raw.isListing
  if (value === undefined || value === null) return fallback
  return value === 1 || value === true || value === '1' || value === 'true'
}
function normalizeItem(input, fallbackOnsale = null) {
  const raw = unwrapItem(input)
  const skus = raw.skus || raw.sku_list || raw.skuInfos || []
  const images = collectImages(raw)
  const prices = skus.map((sku) => Number(sku.price ?? sku.sell_price ?? sku.sale_price)).filter(Number.isFinite)
  const quantities = skus.map((sku) => Number(sku.quantity ?? sku.stock_num ?? sku.stock)).filter(Number.isFinite)
  // 列表接口不带 skus：价格/库存必须回落到商品级字段，否则列表整列显示 "--"
  const itemPriceValue = Number(raw.price ?? raw.origin_price)
  const itemQuantity = Number(raw.quantity ?? raw.num)
  return {
    id: String(raw.item_id ?? raw.id ?? raw.itemId ?? ''),
    title: raw.title || raw.name || '未命名商品',
    itemNo: raw.item_no ?? raw.itemNo ?? raw.itemno ?? '',
    cid: raw.cid ?? raw.category_id ?? '',
    listing: listingOf(raw, fallbackOnsale),
    images,
    prices: prices.length ? prices : (Number.isFinite(itemPriceValue) ? [itemPriceValue] : []),
    quantities: quantities.length ? quantities : (Number.isFinite(itemQuantity) ? [itemQuantity] : []),
    skus: skus.map((sku) => ({
      id: String(sku.sku_id ?? sku.id ?? sku.skuId ?? ''),
      code: sku.item_no ?? sku.sku_no ?? sku.skuNo ?? sku.erpCode ?? '',
      price: sku.price ?? sku.sell_price ?? sku.sale_price ?? '',
      quantity: sku.quantity ?? sku.stock_num ?? sku.stock ?? '--',
    })),
  }
}
// ⚠️ 价格单位（2026-10-15 用户实填确认）：
//   查询接口返回的是**分**（该商品 price=2800000，后台填的是 28000 元），
//   而写入（item.create/update）要传**元**（如 28000.00）—— 两个方向相反，别写反。
const fenToYuan = (value) => `¥${(Number(value) / 100).toFixed(2)}`
const itemPrice = (item) => {
  if (!item.prices.length) return '--'
  const min = Math.min(...item.prices)
  const max = Math.max(...item.prices)
  return min === max ? fenToYuan(min) : `${fenToYuan(min)}～${fenToYuan(max)}`
}
const itemStock = (item) => (item.quantities.length ? item.quantities.reduce((sum, n) => sum + n, 0) : '--')
const itemImage = (item) => item.images[0] || ''

// ---- 搜索栏（有赞列表接口不支持关键字筛选，在当前页结果里本地过滤） ----
const searchForm = ref({ ids: '', codes: '', keyword: '', minPrice: null, maxPrice: null, minStock: null, maxStock: null })
function splitTokens(str) {
  return String(str || '').split(/[\s,，;；]+/).map((s) => s.trim()).filter(Boolean)
}
function resetSearch() {
  searchForm.value = { ids: '', codes: '', keyword: '', minPrice: null, maxPrice: null, minStock: null, maxStock: null }
  page.value = 1
}
function matchSearchForm(item) {
  const f = searchForm.value
  const ids = splitTokens(f.ids)
  if (ids.length && !ids.some((id) => item.id.includes(id))) return false
  const codes = splitTokens(f.codes).map((c) => c.toLowerCase())
  if (codes.length) {
    const own = [item.itemNo, ...item.skus.map((sku) => sku.code)].filter(Boolean).map((c) => c.toLowerCase())
    if (!codes.some((c) => own.some((oc) => oc.includes(c)))) return false
  }
  const keywords = splitTokens(f.keyword).map((k) => k.toLowerCase())
  if (keywords.length && !keywords.every((k) => item.title.toLowerCase().includes(k))) return false
  const price = item.prices.length ? Math.min(...item.prices) : null
  if (f.minPrice != null && f.minPrice !== '' && (price == null || price < Number(f.minPrice))) return false
  if (f.maxPrice != null && f.maxPrice !== '' && (price == null || price > Number(f.maxPrice))) return false
  const stock = item.quantities.length ? itemStock(item) : null
  if (f.minStock != null && f.minStock !== '' && (stock == null || stock < Number(f.minStock))) return false
  if (f.maxStock != null && f.maxStock !== '' && (stock == null || stock > Number(f.maxStock))) return false
  return true
}
const filteredItems = computed(() => items.value.filter(matchSearchForm))

// ⚠️ 2026-10-16：没接通有赞（服务没起 / 这家店没配 youzan / 凭证没取到）时不再把整页内容藏掉，
//    改成和小红书商品页一致——统计卡、搜索栏、状态 tab、表格、分页照常渲染，只有数字显示 '—'。
//    为什么用 listReady 而不是"值是不是 0"：真·0 商品的有赞店应该显示 0，只有"没取到数据"才该显示 '—'。
const listReady = ref(false)
const stats = computed(() => ({
  total: listReady.value ? (totalOnsale.value || 0) + (totalInventory.value || 0) : '—',
  onsale: listReady.value ? totalOnsale.value : '—',
  inventory: listReady.value ? totalInventory.value : '—',
  page: listReady.value ? items.value.length : '—',
}))
// 状态筛选版式与小红书商品页统一（下划线 tab + 计数），不再用 radio-button
const stateTabs = computed(() => [
  { label: `出售中（${stats.value.onsale}）`, value: 'onsale' },
  { label: `仓库中（${stats.value.inventory}）`, value: 'inventory' },
])
// 空表文案按"为什么没数据"分情况写，比笼统的"暂无商品"更能说明问题
const emptyText = computed(() => {
  if (!serviceOnline.value) return '有赞服务未连接，暂时取不到有赞商品'
  if (!shopHasYouzan.value) return '当前店铺未开通有赞，还没有有赞商品'
  if (!tokenConfigured.value) return '有赞凭证未就绪，暂时取不到有赞商品'
  return '这个状态下暂无有赞商品'
})

async function checkHealth() {
  tokenError.value = ''
  try {
    const health = await youzanApi.health()
    serviceOnline.value = true
    serviceMode.value = health?.mode || ''
  } catch {
    // 只有 /health 都不通才算"服务未连接"
    serviceOnline.value = false
    tokenConfigured.value = false
    return
  }
  // 这家店根本没配 youzan 时不要去问 token：后端对没配凭证的店会直接 400，
  // 那是"该去补 shops.json 配置"，不是"服务没起"——两种提示必须分开
  if (!shopHasYouzan.value) {
    tokenConfigured.value = false
    return
  }
  // mock 模式整条链路不发外网请求，token 是占位，不该被"凭证尚未取得"拦住
  if (serviceMode.value === 'mock') {
    tokenConfigured.value = true
    return
  }
  try {
    const token = await youzanApi.tokenInfo()
    tokenConfigured.value = Boolean(token.configured)
  } catch (error) {
    tokenConfigured.value = false
    tokenError.value = error.message
  }
}

async function loadItems() {
  if (!shopHasYouzan.value) return
  const version = ++loadVersion.value
  loading.value = true
  listReady.value = false
  try {
    // 两个 tab 的总量各拉一次（page_size=1 只要 total），统计卡和分页总数都要用
    const totals = await Promise.all([
      youzanApi.listItems({ state: 'onsale', page_no: 1, page_size: 1 }),
      youzanApi.listItems({ state: 'inventory', page_no: 1, page_size: 1 }),
    ])
    if (version !== loadVersion.value) return
    totalOnsale.value = totalOf(totals[0]) ?? listFrom(totals[0], ['items', 'item_list', 'list', 'itemList']).length
    totalInventory.value = totalOf(totals[1]) ?? listFrom(totals[1], ['items', 'item_list', 'list', 'itemList']).length
    const data = await youzanApi.listItems({ state: stateTab.value, page_no: page.value, page_size: pageSize.value })
    if (version !== loadVersion.value) return
    // 状态以 tab 为准（有赞按状态分了两个接口），不能靠报文里的 is_listing（真机报文没有该字段）
    const onsale = stateTab.value === 'onsale'
    items.value = listFrom(data, ['items', 'item_list', 'list', 'itemList'])
      .map((row) => normalizeItem(row, onsale))
    serverTotal.value = totalOf(data) ?? items.value.length
    listReady.value = true
  } catch (error) {
    items.value = []
    ElMessage.error(error.message)
  } finally {
    loading.value = false
  }
}

function switchState(tab) {
  stateTab.value = tab
  page.value = 1
  void loadItems()
}

// 上下架：listing 1=上架 0=下架。
// ✅ 2026-10-15 真机核对：接口名正确（youzan.item.update.listing / .delisting），
//    返回 {"item_id":…, "is_success": true}；但**列表接口会比详情晚约 2 秒**同步
//    （实测上下架后立刻查列表仍是旧状态），所以这里等一下再刷新，否则运营会以为没生效。
async function setListing(item, listing) {
  if (!item.id) return ElMessage.warning('当前商品缺少商品 ID')
  actionId.value = `${item.id}-${listing}`
  try {
    await youzanApi.setListing(item.id, listing)
    // 刷新后这一行会从当前 tab 消失（移去了另一个状态），提示里说清楚，否则像"操作把商品弄丢了"
    ElMessage.success(listing ? '已上架，商品已移入「出售中」' : '已下架，商品已移入「仓库中」')
    await new Promise((resolve) => setTimeout(resolve, 2000))
    await loadItems()
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    actionId.value = ''
  }
}

// 详情抽屉：列表接口往往不带全部字段，详情单独拉一次。
// 版式复用 ProductDetailView（与小红书/微信同一套），所以这里存**原始报文**而不是归一化后的行数据。
const detailVisible = ref(false)
const detailLoading = ref(false)
const detailRaw = ref(null)
const detailFallback = ref(null)
async function showDetail(item) {
  detailVisible.value = true
  detailLoading.value = true
  detailRaw.value = null
  detailFallback.value = item
  try {
    const data = await youzanApi.getProduct(item.id)
    detailRaw.value = data?.result ?? {}
  } catch (error) {
    detailRaw.value = {}
    ElMessage.warning(`详情字段拉取失败（以列表数据展示）：${error.message}`)
  } finally {
    detailLoading.value = false
  }
}

// —— 编辑抽屉（标题 / SKU 价格 / SKU 库存 / 上下架）——
// 为什么走中台接口而不是直连有赞服务：① 价格单位在"创建(分)"和"改价(元)"上是相反的，
// 换算必须由中台按适配器统一做；② 库存平台只支持"增量"，要设绝对值得先回读现值算差值；
// ③ item.update 会返回 is_success 但字段没变，中台已做回读校验并把结果放在 warnings 里。
const TITLE_MAX = 60                      // 有赞标题上限按 60 字保守取（★官方上限仍待核对）
const editVisible = ref(false)
const editLoading = ref(false)
const editSaving = ref(false)
const editForm = ref({ id: '', title: '', listing: true, skus: [], warnings: [] })
// 详情接口返回的原文：编辑表单只开放 4 个字段（标题/上下架/价格/库存），
// 其余信息（编码、类目、图片、更新时间）用只读区展示，运营不用再去有赞后台对照。
// 版式与微信/小红书编辑页统一：卡片分节 + 右上角序号 + label 置顶两列栅格。
const editRaw = ref({})
const editLoadError = ref('')

// 主图：有赞详情接口给 item_imgs 对象数组，兼容单图 pic_url（与 ProductDetailView 同一套取值）
const editMainImages = computed(() => {
  const fromImgs = (editRaw.value.item_imgs || []).map((one) => one?.url || one?.medium).filter(Boolean)
  return fromImgs.length ? fromImgs : [editRaw.value.pic_url].filter(Boolean)
})
// 有赞没有独立的详情图字段：详情图写在 desc 富文本里，只能抽 <img src>
const editDescImages = computed(() => [...String(editRaw.value.desc || '').matchAll(/<img[^>]+src=["']([^"']+)["']/gi)].map((one) => one[1]))
const editPreviewImages = computed(() => [...new Set([...editMainImages.value, ...editDescImages.value])])

const editPriceRange = computed(() => {
  const prices = editForm.value.skus.map((one) => Number(one.price)).filter((one) => Number.isFinite(one))
  if (!prices.length) return '—'
  const min = Math.min(...prices)
  const max = Math.max(...prices)
  return min === max ? `￥${min.toFixed(2)}` : `￥${min.toFixed(2)} ~ ￥${max.toFixed(2)}`
})
const editStockTotal = computed(() => editForm.value.skus.reduce((sum, one) => sum + (Number(one.quantity) || 0), 0))
const editCategoryId = computed(() => editRaw.value.cid || editRaw.value.category_id || '—')
const editUpdatedAt = computed(() => {
  const value = editRaw.value.edit_time || editRaw.value.updateTime
    || editRaw.value.update_time || editRaw.value.created_time
  if (!value) return '—'
  if (typeof value === 'string' && !/^\d+$/.test(value)) return value.replace('T', ' ')
  const date = new Date(Number(value) < 1e12 ? Number(value) * 1000 : Number(value))
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false })
})
// 「在有赞后台打开」：优先用详情返回的 detail_url，没有就按商品 ID 拼 H5 链接
const editDetailUrl = computed(() => editRaw.value.detail_url
  || (editForm.value.id ? `https://h5.youzan.com/v2/goods/${editForm.value.id}` : ''))

function openYouzanConsole() {
  if (editDetailUrl.value) window.open(editDetailUrl.value, '_blank', 'noopener')
}

function specTextOf(sku) {
  // 规格文本来自 properties_name_json（真机：{"k":"戒托颜色","v":"红色"} 的数组）
  try {
    const list = typeof sku.properties_name_json === 'string'
      ? JSON.parse(sku.properties_name_json || '[]') : (sku.properties_name_json || [])
    return (Array.isArray(list) ? list : []).map((one) => `${one?.k ?? ''}${one?.v ? `=${one.v}` : ''}`).filter(Boolean).join('，')
  } catch { return '' }
}

async function openEdit(item) {
  if (!item.id) return ElMessage.warning('当前商品缺少商品 ID')
  editVisible.value = true
  editLoading.value = true
  editLoadError.value = ''
  editRaw.value = {}
  editForm.value = { id: item.id, title: item.title || '', listing: true, skus: [], warnings: [] }
  try {
    const data = await youzanApi.getProduct(item.id)
    const raw = data?.result?.item || data?.result || {}
    editRaw.value = raw
    const skus = raw.skus || raw.sku_list || []
    editForm.value = {
      id: item.id,
      title: raw.title || item.title || '',
      // 详情接口**有** is_listing（列表接口没有），这里以详情为准
      listing: raw.is_listing === 1 || raw.is_listing === true,
      // 价格：平台存的是分，展示/提交都用元（中台负责按适配器换算）
      skus: skus.map((sku) => ({
        skuId: String(sku.sku_id ?? sku.id ?? ''),
        code: sku.item_no || sku.sku_no || '',
        spec: specTextOf(sku),
        price: Number((Number(sku.price ?? 0) / 100).toFixed(2)),
        quantity: Number(sku.quantity ?? sku.stock ?? 0),
      })),
      warnings: [],
    }
    if (!editForm.value.skus.length) ElMessage.warning('这个商品没有规格行（SKU），只能改标题与上下架')
  } catch (error) {
    // 详情拉不到也要能改标题/上下架：错误留在抽屉里显示（toast 一闪而过，运营不知道少了什么）
    editLoadError.value = error.message
  } finally {
    editLoading.value = false
  }
}

async function saveEdit() {
  const form = editForm.value
  if (!String(form.title || '').trim()) return ElMessage.warning('标题不能为空')
  if (String(form.title).trim().length > TITLE_MAX) {
    return ElMessage.warning(`标题 ${String(form.title).trim().length} 字，有赞上限按 ${TITLE_MAX} 字，请缩短后再保存`)
  }
  editSaving.value = true
  try {
    const data = await bulkApi.youzanEditItem({
      item_id: form.id,
      title: String(form.title).trim(),
      skus: form.skus.map((one) => ({ sku_id: one.skuId, price: one.price, quantity: one.quantity })),
      // 抽屉里的「上下架」开关以前只存在于表单、从来没提交过（改了不会生效）—— 补上
      listing: form.listing ? 1 : 0,
    })
    const result = data?.result || {}
    const changed = result.changed || []
    const warnings = result.warnings || []
    if (warnings.length) ElMessage.warning(`${changed.length ? `已提交 ${changed.join('、')}` : '未修改'}；但有 ${warnings.length} 项平台没生效：${warnings.join('；')}`)
    else ElMessage.success(changed.length ? `已保存：${changed.join('、')}` : '没有需要修改的内容')
    form.warnings = warnings
    await loadItems()                                   // 列表价格/库存同步刷新
    if (!warnings.length) editVisible.value = false
    else await openEdit({ id: form.id, title: form.title })   // 有没生效的项：回读真实值给运营看
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    editSaving.value = false
  }
}

onMounted(async () => {
  await checkHealth()
  if (serviceOnline.value && tokenConfigured.value) await loadItems()
})
watch(() => props.refreshTick, () => {
  if (serviceOnline.value && tokenConfigured.value && shopHasYouzan.value) void loadItems()
})
watch(pageSize, () => { page.value = 1; void loadItems() })
</script>

<template>
  <div class="youzan-view">
    <div class="page-heading">
      <div><h1>有赞商品</h1><p>查看有赞商品状态与上下架；发布走「批量发布商品」，单条临时发布用有赞官方后台<template v-if="serviceOnline && serviceMode === 'mock'">。<strong>当前为 mock 联调模式：列表为模拟数据（可能为空），不连有赞线上</strong></template></p></div>
    </div>
    <el-alert v-if="!serviceOnline" title="有赞服务未连接" description="请启动 youzan_api.py（默认 8030 端口）。它与微信/小红书服务相互独立。" type="warning" show-icon :closable="false" class="offline-alert" />
    <el-alert v-else-if="!shopHasYouzan" title="当前店铺未开通有赞" description="请先在店铺管理里配置这家店的有赞授权信息，或切换到已开通有赞的店铺。" type="warning" show-icon :closable="false" class="offline-alert" />
    <el-alert v-else-if="!tokenConfigured" :title="tokenError ? '有赞凭证查询失败' : '有赞凭证尚未取得'" :description="tokenError || '服务正常、店铺也配了 youzan，但还没有可用 access_token。在店铺管理页点「重取凭证」，或检查凭证是否正确。'" type="warning" show-icon :closable="false" class="offline-alert" />
    <!-- 页面主体不再按连通状态整块隐藏：没接通有赞时也照常渲染骨架（数字显示 '—'），
         否则运营看到的是一张"什么都没有"的页面，光靠上面那条 alert 很难判断是不是页面坏了。 -->
    <div class="youzan-body">
      <div class="stats-grid">
        <div class="stat-item"><span>商品总数</span><strong>{{ stats.total }}</strong><el-icon><Goods /></el-icon></div>
        <div class="stat-item"><span>出售中</span><strong>{{ stats.onsale }}</strong><el-icon class="green"><Top /></el-icon></div>
        <div class="stat-item"><span>仓库中</span><strong>{{ stats.inventory }}</strong><el-icon class="amber"><Bottom /></el-icon></div>
        <div class="stat-item"><span>当前页</span><strong>{{ stats.page }}</strong><el-icon><View /></el-icon></div>
      </div>
      <section class="content-panel">
        <ProductSearchBar
          v-model="searchForm"
          code-label="商家编码"
          code-placeholder="货号/规格编码，空格/逗号分隔"
          @search="page = 1"
          @reset="resetSearch"
        />
        <div class="panel-toolbar xhs-filter-toolbar">
          <nav class="xhs-status-tabs" aria-label="有赞商品状态筛选">
            <button v-for="option in stateTabs" :key="option.value" :class="{ active: stateTab === option.value }" @click="switchState(option.value)">{{ option.label }}</button>
          </nav>
          <div class="page-scope"><span>{{ listReady ? '全店统计已更新' : '全店统计未更新' }}</span></div>
        </div>
        <el-table v-loading="loading" :data="filteredItems" class="product-table" :empty-text="emptyText" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }">
          <el-table-column label="商品" min-width="320"><template #default="{ row }"><div class="product-cell"><el-image :src="itemImage(row)" fit="cover" class="product-thumb"><template #error><div class="image-fallback"><el-icon><Picture /></el-icon></div></template></el-image><div><EllipsisText tag="strong" :text="row.title" /><EllipsisText :text="`ID：${row.id || '--'}${row.itemNo ? ` · 编码：${row.itemNo}` : ''}`" /></div></div></template></el-table-column>
          <el-table-column label="售价" width="150" show-overflow-tooltip><template #default="{ row }"><strong class="price">{{ itemPrice(row) }}</strong></template></el-table-column>
          <el-table-column label="库存" width="110" show-overflow-tooltip><template #default="{ row }">{{ itemStock(row) }}</template></el-table-column>
          <el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="row.listing ? 'success' : 'warning'" effect="light" round>{{ row.listing ? '出售中' : '仓库中' }}</el-tag></template></el-table-column>
          <el-table-column label="操作" width="240" :fixed="isMobile ? false : 'right'"><template #default="{ row }"><el-button link type="primary" :disabled="!row.id" @click="showDetail(row)">详情</el-button><el-button link type="primary" :disabled="!row.id" @click="openEdit(row)">编辑</el-button><el-button v-if="!row.listing" link type="primary" :loading="actionId === `${row.id}-1`" @click="setListing(row, 1)">上架</el-button><el-button v-else link type="warning" :loading="actionId === `${row.id}-0`" @click="setListing(row, 0)">下架</el-button></template></el-table-column>
        </el-table>
        <div class="cursor-pagination">
          <el-pagination
            v-model:current-page="page"
            :page-size="pageSize"
            :page-sizes="[20, 50, 100]"
            :total="serverTotal"
            layout="total, sizes, prev, pager, next, jumper"
            @current-change="loadItems"
          />
          <span class="muted-copy">服务端分页（出售中/仓库中各自计数）</span>
        </div>
      </section>
      <el-drawer v-model="detailVisible" title="有赞商品详情" size="760px" class="product-detail-drawer">
        <div v-loading="detailLoading">
          <ProductDetailView v-if="detailRaw" platform="youzan" :data="detailRaw" />
          <p v-else-if="detailFallback" class="muted-copy">详情拉取失败，可稍后重试（列表行：{{ detailFallback.title }}）</p>
        </div>
      </el-drawer>

      <!-- 编辑抽屉：只开放"改得安全且已真机验证"的字段。
           类目与详情图不开放（类目改错要重新提交审核；详情图只能写进 desc 富文本，raw HTML 不该手改）。
           版式与微信/小红书编辑页对齐：卡片分节 + 右上角序号 + label 置顶两列栅格 + 吸底操作条。 -->
      <el-drawer v-model="editVisible" title="编辑有赞商品" size="860px">
        <div v-loading="editLoading" class="edit-form">
          <el-alert v-if="editLoadError" type="error" show-icon :closable="false" class="edit-tip">
            <template #title>读取有赞商品详情失败，下面只能改标题与上下架</template>
            {{ editLoadError }}
          </el-alert>

          <el-form label-position="top" class="product-form">
            <section class="form-section">
              <div class="section-heading">
                <div><h3>有赞商品信息</h3><p>标题与上下架会直接提交到有赞；编码、类目、图片请在有赞后台改</p></div>
                <span class="section-index">01</span>
              </div>
              <div class="form-grid form-grid-2">
                <el-form-item label="商品标题" required>
                  <el-input v-model="editForm.title" maxlength="90" show-word-limit placeholder="有赞标题上限按 60 字（保守值）" />
                  <div class="muted-copy">当前 {{ String(editForm.title || '').trim().length }} 字，超过 {{ TITLE_MAX }} 字保存会被拦下（有赞标题上限仍待官方核对）</div>
                </el-form-item>
                <el-form-item label="上下架">
                  <el-switch v-model="editForm.listing" active-text="出售中" inactive-text="仓库中" />
                  <div class="muted-copy">切换后商品会移到对应列表</div>
                </el-form-item>
              </div>
              <!-- 只读区：这些字段详情接口本来就返回，以前全丢掉，改价时看不到编码/类目/图片 -->
              <el-descriptions :column="2" border size="small" class="edit-readonly">
                <el-descriptions-item label="商品 ID">{{ editForm.id || '—' }}</el-descriptions-item>
                <el-descriptions-item label="商家编码">{{ editRaw.item_no || '—' }}</el-descriptions-item>
                <el-descriptions-item label="售价区间">{{ editPriceRange }}</el-descriptions-item>
                <el-descriptions-item label="库存合计">{{ editStockTotal }}</el-descriptions-item>
                <el-descriptions-item label="类目 ID">{{ editCategoryId }}</el-descriptions-item>
                <el-descriptions-item label="更新时间">{{ editUpdatedAt }}</el-descriptions-item>
              </el-descriptions>
            </section>

            <section class="form-section">
              <div class="section-heading">
                <div><h3>规格、价格与库存</h3><p>价格单位：元（平台内部按分存储，由中台换算）；库存填绝对值，平台只支持增量，中台会自己算差值</p></div>
                <span class="section-index">02</span>
              </div>
              <div class="sku-table-wrap">
                <el-table :data="editForm.skus" class="xhs-sku-table" empty-text="这个商品没有规格行（只能改标题与上下架）">
                  <el-table-column label="规格" min-width="200"><template #default="{ row }"><span class="sku-spec-value">{{ row.spec || '—' }}</span></template></el-table-column>
                  <el-table-column label="商家编码" min-width="140"><template #default="{ row }">{{ row.code || '—' }}</template></el-table-column>
                  <el-table-column label="售价（元）" width="150">
                    <template #default="{ row }"><el-input-number v-model="row.price" :min="0.01" :precision="2" :step="100" controls-position="right" size="small" /></template>
                  </el-table-column>
                  <el-table-column label="库存" width="140">
                    <template #default="{ row }"><el-input-number v-model="row.quantity" :min="0" :precision="0" :step="1" controls-position="right" size="small" /></template>
                  </el-table-column>
                </el-table>
              </div>
              <div class="sku-table-footer">
                <span class="muted-copy">共 {{ editForm.skus.length }} 个规格 · 售价区间 {{ editPriceRange }} · 库存合计 {{ editStockTotal }}</span>
              </div>
            </section>

            <section v-if="editMainImages.length || editDescImages.length" class="form-section">
              <div class="section-heading">
                <div><h3>商品图片</h3><p>这里只做对照，改图要回有赞后台（编辑接口不支持改图）</p></div>
                <span class="section-index">03</span>
              </div>
              <template v-if="editMainImages.length">
                <div class="field-label">商品主图 <span>{{ editMainImages.length }} 张 · 点击可放大</span></div>
                <div class="detail-image-grid">
                  <el-image v-for="(image, index) in editMainImages" :key="`${image}-main`" :src="image" :preview-src-list="editPreviewImages" :initial-index="index" fit="cover" lazy />
                </div>
              </template>
              <template v-if="editDescImages.length">
                <div class="field-label description-label">商品详情图 <span>{{ editDescImages.length }} 张 · 取自详情富文本</span></div>
                <div class="detail-image-grid">
                  <el-image v-for="(image, index) in editDescImages" :key="`${image}-desc`" :src="image" :preview-src-list="editPreviewImages" :initial-index="editMainImages.length + index" fit="cover" lazy />
                </div>
              </template>
            </section>

            <el-alert v-if="editForm.warnings.length" type="warning" show-icon :closable="false" class="edit-warnings">
              <template #title>以下内容平台没有生效（已回读确认，请对照后再保存）</template>
              <div v-for="(one, index) in editForm.warnings" :key="index">{{ one }}</div>
            </el-alert>

            <div class="form-actions edit-actions">
              <el-button v-if="editDetailUrl" link type="primary" :icon="View" @click="openYouzanConsole">在有赞后台打开</el-button>
              <span v-else></span>
              <div class="edit-actions__buttons">
                <el-button @click="editVisible = false">取消</el-button>
                <el-button type="primary" :icon="Check" :loading="editSaving" @click="saveEdit">保存到有赞</el-button>
              </div>
            </div>
          </el-form>
        </div>
      </el-drawer>
    </div>
  </div>
</template>

<style scoped>
/* 详情体由 ProductDetailView 提供（三平台同一套样式），本页不再自定义详情样式 */
/* 编辑抽屉的分节卡片 / 两列栅格 / 吸底操作条都走全局的 .product-form / .form-section / .form-actions，
   这里只补本页特有的间距和只读区，避免又长出一套"有赞专用样式" */
.edit-form { display: flex; flex-direction: column; }
.edit-tip { margin-bottom: 16px; }
.edit-readonly { margin-top: 2px; }
.edit-readonly :deep(.el-descriptions__label) { width: 96px; color: #667085; }
.edit-actions { justify-content: space-between; }
.edit-actions__buttons { display: flex; gap: 10px; }
.edit-warnings { margin-top: 8px; }
.edit-warnings :deep(.el-alert__content) { font-size: 12px; }
</style>
