<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { CircleCheck, Clock, Goods, Picture, Plus, Refresh, Remove } from '@element-plus/icons-vue'
import { xhsApi } from '../xhsApi'
import XhsProductForm from './XhsProductForm.vue'
import ProductSearchBar from './ProductSearchBar.vue'
import ProductDetailView from './ProductDetailView.vue'
import EllipsisText from './EllipsisText.vue'

const emit = defineEmits(['create'])
const loading = ref(false)
const refreshing = ref(false)
const statusLoaded = ref(false)
const summaryLoading = ref(false)
const summaryReady = ref(false)
const loadVersion = ref(0)
const actionId = ref('')
const serviceOnline = ref(false)
const tokenConfigured = ref(false)
const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)
const editVisible = ref(false)
const editLoading = ref(false)
const editItemId = ref('')
const editItemData = ref(null)
const stateFilter = ref('all')

// 搜索栏筛选条件（小红书 API 不支持服务端筛选，在前端对已拉取列表做本地过滤）
const searchForm = ref({ ids: '', codes: '', keyword: '', minPrice: null, maxPrice: null, minStock: null, maxStock: null })

function splitTokens(str) {
  if (!str) return []
  return String(str).split(/[\s,，;；]+/).map((s) => s.trim()).filter(Boolean)
}
function resetSearch() {
  searchForm.value = { ids: '', codes: '', keyword: '', minPrice: null, maxPrice: null, minStock: null, maxStock: null }
  page.value = 1
}

const itemId = (item) => item.itemId || item.id || item.item_id
const itemName = (item) => item.name || item.title || '未命名商品'
const itemImage = (item) => item.images?.[0] || item.image || ''
const itemSkus = (item) => item.skus || item.skuInfos || []
const skuId = (sku) => sku.skuId || sku.id || sku.sku_id
const buyable = (sku) => sku.buyable === true || sku.buyable === 1 || sku.available === 1
const itemPrice = (item) => {
  const prices = itemSkus(item).map((sku) => Number(sku.price)).filter(Number.isFinite)
  if (!prices.length) return '--'
  const min = Math.min(...prices)
  const max = Math.max(...prices)
  return min === max ? `¥${(min / 100).toFixed(2)}` : `¥${(min / 100).toFixed(2)}～${(max / 100).toFixed(2)}`
}
const itemStock = (item) => {
  const stocks = itemSkus(item).map((sku) => Number(sku.stock)).filter(Number.isFinite)
  return stocks.length ? stocks.reduce((sum, stock) => sum + stock, 0) : '--'
}
const itemState = (item) => {
  const skus = itemSkus(item)
  if (!statusLoaded.value && !skus.length) return 'loading'
  if (!skus.length) return 'no_sku'
  return skus.some(buyable) ? 'buyable' : 'unavailable'
}

// 搜索栏匹配需要的辅助函数（依赖 itemSkus，必须在 itemSkus 之后定义）
function itemArticleNo(item) {
  return item.articleNo || item.article_no || item.productCode || ''
}
function itemCodes(item) {
  const codes = [itemArticleNo(item)]
  for (const sku of itemSkus(item)) {
    codes.push(sku.erpCode, sku.erp_code, sku.barcode, sku.skuCode, sku.sku_code)
  }
  return codes.filter(Boolean).map(String)
}
function itemMinPriceYuan(item) {
  const prices = itemSkus(item).map((sku) => Number(sku.price)).filter(Number.isFinite)
  if (!prices.length) return null
  return Math.min(...prices) / 100
}
function itemStockNum(item) {
  const stocks = itemSkus(item).map((sku) => Number(sku.stock)).filter(Number.isFinite)
  if (!stocks.length) return null
  return stocks.reduce((sum, n) => sum + n, 0)
}
function matchSearchForm(item) {
  const f = searchForm.value
  const ids = splitTokens(f.ids).map(String)
  const codes = splitTokens(f.codes).map((s) => s.toLowerCase())
  const keywords = splitTokens(f.keyword).map((s) => s.toLowerCase())
  if (ids.length) {
    const iid = String(itemId(item) ?? '')
    if (!ids.some((id) => iid && iid.includes(id))) return false
  }
  if (codes.length) {
    const allCodes = itemCodes(item).map((c) => c.toLowerCase())
    if (!codes.some((c) => allCodes.some((ac) => ac.includes(c)))) return false
  }
  if (keywords.length) {
    const title = String(itemName(item) || '').toLowerCase()
    if (!keywords.every((k) => title.includes(k))) return false
  }
  const price = itemMinPriceYuan(item)
  if (f.minPrice != null && f.minPrice !== '' && (price == null || price < Number(f.minPrice))) return false
  if (f.maxPrice != null && f.maxPrice !== '' && (price == null || price > Number(f.maxPrice))) return false
  const stock = itemStockNum(item)
  if (f.minStock != null && f.minStock !== '' && (stock == null || stock < Number(f.minStock))) return false
  if (f.maxStock != null && f.maxStock !== '' && (stock == null || stock > Number(f.maxStock))) return false
  return true
}
const stats = computed(() => ({
  total: total.value || items.value.length,
  buyable: summaryReady.value ? items.value.filter((item) => itemState(item) === 'buyable').length : '—',
  unavailable: summaryReady.value ? items.value.filter((item) => itemState(item) === 'unavailable').length : '—',
  noSku: summaryReady.value ? items.value.filter((item) => itemState(item) === 'no_sku').length : '—',
}))
const filteredItems = computed(() => {
  const byState = stateFilter.value === 'all'
    ? items.value
    : items.value.filter((item) => itemState(item) === stateFilter.value)
  return byState.filter(matchSearchForm)
})
const pagedItems = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredItems.value.slice(start, start + pageSize.value)
})
const filterOptions = computed(() => [
  { label: `全部（${stats.value.total}）`, value: 'all' },
  { label: `售卖中（${stats.value.buyable}）`, value: 'buyable' },
  { label: `审核中/不可售（${stats.value.unavailable}）`, value: 'unavailable' },
  { label: `无 SKU（${stats.value.noSku}）`, value: 'no_sku' },
])

function unwrapList(data) {
  const result = data?.result || {}
  return result.itemDetailV3s || result.items || result.itemList || result.data || []
}

async function loadItems() {
  const version = ++loadVersion.value
  loading.value = true
  refreshing.value = true
  summaryLoading.value = true
  summaryReady.value = false
  try {
    const firstPage = await xhsApi.listItems({ page_no: 1, page_size: pageSize.value })
    const firstItems = unwrapList(firstPage)
    if (version !== loadVersion.value) return
    total.value = firstPage.result?.total || firstPage.result?.totalCount || firstItems.length
    items.value = firstItems
    page.value = 1
    statusLoaded.value = false
    loading.value = false
    const statuses = await fetchStatuses(firstItems, 1)
    if (version !== loadVersion.value) return
    mergeStatuses(items.value, statuses)
    statusLoaded.value = true
    void loadFullCatalog(version, items.value.slice())
  } catch (error) {
    items.value = []
    if (version === loadVersion.value) summaryLoading.value = false
    ElMessage.error(error.message)
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

async function fetchStatuses(targetItems, concurrency = 2) {
  const ids = targetItems.map(itemId).filter(Boolean)
  const batches = []
  for (let index = 0; index < ids.length; index += 20) batches.push(ids.slice(index, index + 20))
  let nextBatch = 0
  const statuses = []
  async function worker() {
    while (nextBatch < batches.length) {
      const batch = batches[nextBatch++]
      const data = await xhsApi.itemStatus(batch)
      statuses.push(...(data.result || []))
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, batches.length) }, worker))
  return statuses
}

function mergeStatuses(targetItems, statuses) {
  const byId = new Map(statuses.map((status) => [String(status.itemId), status]))
  for (const item of targetItems) {
    const status = byId.get(String(itemId(item)))
    if (status) item.skus = status.skus || []
  }
}

async function loadFullCatalog(version, currentItems) {
  summaryLoading.value = true
  try {
    const firstPage = await xhsApi.listItems({ page_no: 1, page_size: 100 })
    const firstItems = unwrapList(firstPage)
    const storeTotal = firstPage.result?.total || firstPage.result?.totalCount || firstItems.length
    const pageCount = Math.ceil(storeTotal / 100)
    const remainingPages = pageCount > 1
      ? await Promise.all(Array.from({ length: pageCount - 1 }, (_, index) => xhsApi.listItems({ page_no: index + 2, page_size: 100 })))
      : []
    const allItems = [...firstItems, ...remainingPages.flatMap(unwrapList)]
    const currentById = new Map(currentItems.map((item) => [String(itemId(item)), item]))
    for (const item of allItems) {
      const current = currentById.get(String(itemId(item)))
      if (current?.skus) item.skus = current.skus
    }
    const unresolved = allItems.filter((item) => !Array.isArray(item.skus))
    const statuses = await fetchStatuses(unresolved, 2)
    mergeStatuses(allItems, statuses)
    if (version !== loadVersion.value) return
    items.value = allItems
    total.value = Math.max(storeTotal, allItems.length)
    summaryReady.value = true
  } catch (error) {
    if (version === loadVersion.value) ElMessage.warning(`全店统计更新失败：${error.message}`)
  } finally {
    if (version === loadVersion.value) summaryLoading.value = false
  }
}

async function checkHealth() {
  try {
    await xhsApi.health()
    const token = await xhsApi.tokenInfo()
    serviceOnline.value = true
    tokenConfigured.value = Boolean(token.configured)
  } catch {
    serviceOnline.value = false
    tokenConfigured.value = false
  }
}

function statusText(item) {
  const skus = itemSkus(item)
  if (!statusLoaded.value && !skus.length) return { label: '状态查询中', type: 'info' }
  if (skus.some(buyable)) return { label: '售卖中', type: 'success' }
  if (skus.length) return { label: '审核中/不可售', type: 'warning' }
  return { label: '未创建 SKU', type: 'info' }
}

async function setAvailable(sku, available) {
  const id = skuId(sku)
  if (!id) return ElMessage.warning('当前商品缺少 SKU ID')
  if (available === 1 && !buyable(sku)) {
    return ElMessage.warning('该 SKU 尚未审核通过，暂不能上架')
  }
  actionId.value = `${id}-${available}`
  try {
    await xhsApi.setSkuAvailable(id, available)
    ElMessage.success(available ? 'SKU 已提交上架' : 'SKU 已下架')
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    actionId.value = ''
  }
}

async function setItemAvailable(item, available) {
  const skus = available === 1 ? itemSkus(item).filter(buyable) : itemSkus(item)
  if (!skus.length) return ElMessage.warning(available ? '商品暂无审核通过、可以上架的规格' : '商品暂无可操作的规格')
  const id = itemId(item)
  actionId.value = `${id}-${available}`
  try {
    await Promise.all(skus.map((sku) => xhsApi.setSkuAvailable(skuId(sku), available)))
    ElMessage.success(available ? '商品已提交上架' : '商品已提交下架')
    await loadItems()
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    actionId.value = ''
  }
}

async function showDetail(item) {
  detailVisible.value = true
  detailLoading.value = true
  try {
    const data = await xhsApi.getItem(itemId(item))
    detail.value = data.result
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    detailLoading.value = false
  }
}

async function openEditItem(item) {
  editVisible.value = true
  editLoading.value = true
  editItemId.value = String(itemId(item))
  editItemData.value = null
  try {
    const data = await xhsApi.getItem(itemId(item))
    editItemData.value = data.result
  } catch (error) {
    ElMessage.error(error.message)
    editVisible.value = false
  } finally {
    editLoading.value = false
  }
}

function afterItemUpdated() {
  editVisible.value = false
  loadItems()
}

function onPageChange(nextPage) {
  page.value = nextPage
}

function onXhsPageSizeChange(size) {
  pageSize.value = size
  page.value = 1
}

function selectState(value) {
  stateFilter.value = value
  page.value = 1
}

onMounted(async () => {
  await checkHealth()
  if (serviceOnline.value) await loadItems()
})
</script>

<template>
  <div class="xhs-view">
    <div class="page-heading">
      <div><h1>小红书商品</h1><p>独立管理小红书商品与 SKU 上下架，审核状态不会与微信小店混用</p></div>
      <div class="heading-actions"><el-button :icon="Refresh" :loading="refreshing" @click="loadItems">刷新状态</el-button><el-button type="primary" :icon="Plus" @click="emit('create')">发布小红书商品</el-button></div>
    </div>
    <el-alert v-if="!serviceOnline" title="小红书服务未连接" description="请启动 xhs_api.py（默认 8010 端口）。它与微信小店 FastAPI 服务相互独立。" type="warning" show-icon :closable="false" class="offline-alert" />
    <el-alert v-else-if="!tokenConfigured" title="小红书尚未配置有效授权令牌" description="请先完成小红书 OAuth 授权或配置 accessToken。" type="warning" show-icon :closable="false" class="offline-alert" />
    <div class="stats-grid">
      <div class="stat-item"><span>商品总数</span><strong>{{ stats.total }}</strong><el-icon><Goods /></el-icon></div>
      <div class="stat-item"><span>售卖中</span><strong>{{ stats.buyable }}</strong><el-icon class="green"><CircleCheck /></el-icon></div>
      <div class="stat-item"><span>审核中 / 不可售</span><strong>{{ stats.unavailable }}</strong><el-icon class="amber"><Clock /></el-icon></div>
      <div class="stat-item"><span>无 SKU</span><strong>{{ stats.noSku }}</strong><el-icon class="gray"><Remove /></el-icon></div>
    </div>
    <section class="content-panel">
      <ProductSearchBar
        v-model="searchForm"
        code-label="商家编码"
        code-placeholder="货号/erpCode/条码，空格/逗号分隔"
        @search="page = 1"
        @reset="resetSearch"
      />
      <div class="panel-toolbar xhs-filter-toolbar"><nav class="xhs-status-tabs" aria-label="小红书商品状态筛选"><button v-for="option in filterOptions" :key="option.value" :class="{ active: stateFilter === option.value }" @click="selectState(option.value)">{{ option.label }}</button></nav><div class="page-scope"><span>{{ summaryLoading ? '全店统计后台更新中' : '全店统计已更新' }}</span><el-button :icon="Refresh" @click="loadItems">刷新列表</el-button></div></div>
      <el-table v-loading="loading" :data="pagedItems" class="product-table" empty-text="当前分类暂无小红书商品" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }">
        <el-table-column label="商品" min-width="320"><template #default="{ row }"><div class="product-cell"><el-image :src="itemImage(row)" fit="cover" class="product-thumb"><template #error><div class="image-fallback"><el-icon><Picture /></el-icon></div></template></el-image><div><EllipsisText tag="strong" :text="itemName(row)" /><EllipsisText :text="`ID：${itemId(row) || '--'}`" /></div></div></template></el-table-column>
        <el-table-column label="售价" width="150" show-overflow-tooltip><template #default="{ row }"><strong class="price">{{ itemPrice(row) }}</strong></template></el-table-column>
        <el-table-column label="库存" width="110" show-overflow-tooltip><template #default="{ row }">{{ itemStock(row) }}</template></el-table-column>
        <el-table-column label="平台状态" width="160" show-overflow-tooltip><template #default="{ row }"><el-tag :type="statusText(row).type" effect="light" round>{{ statusText(row).label }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="260" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="showDetail(row)">详情</el-button><el-button link type="primary" @click="openEditItem(row)">编辑</el-button><el-button link type="primary" :loading="actionId === `${itemId(row)}-1`" @click="setItemAvailable(row, 1)">上架</el-button><el-button link type="warning" :loading="actionId === `${itemId(row)}-0`" @click="setItemAvailable(row, 0)">下架</el-button></template></el-table-column>
      </el-table>
      <div class="cursor-pagination">
        <el-pagination
          :current-page="page"
          :page-size="pageSize"
          :page-sizes="[20, 50, 100]"
          :total="filteredItems.length"
          :disabled="summaryLoading"
          layout="total, sizes, prev, pager, next, jumper"
          @current-change="onPageChange"
          @size-change="onXhsPageSizeChange"
        />
        <span class="muted-copy">{{ summaryLoading ? '全店数据后台更新中' : `第 ${page} 页` }}</span>
      </div>
    </section>
    <el-drawer v-model="detailVisible" title="小红书商品详情" size="760px" class="product-detail-drawer"><div v-loading="detailLoading"><ProductDetailView v-if="detail" platform="xhs" :data="detail" /></div></el-drawer>
    <el-drawer v-model="editVisible" title="编辑小红书商品" size="860px" destroy-on-close>
      <div v-loading="editLoading">
        <XhsProductForm v-if="editItemData" :item-id="editItemId" :item-data="editItemData" @updated="afterItemUpdated" @cancel="editVisible = false" />
      </div>
    </el-drawer>
  </div>
</template>
