<script setup>
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft,
  CircleCheck,
  CirclePlus,
  Collection,
  Connection,
  EditPen,
  Goods,
  More,
  Picture,
  Plus,
  Refresh,
  Remove,
  Setting,
  Shop,
  ShoppingBag,
  Warning,
  Notebook,
  Upload,
} from '@element-plus/icons-vue'
import { storeApi } from './api'
import { xhsApi } from './xhsApi'
// 视图级组件按需异步加载：仅当切换到对应标签时才下载并执行其代码块
// 首屏只保留商品列表所需的 ProductSearchBar / EllipsisText 同步引入
const ProductForm = defineAsyncComponent(() => import('./components/ProductForm.vue'))
const XhsProductPanel = defineAsyncComponent(() => import('./components/XhsProductPanel.vue'))
const XhsProductForm = defineAsyncComponent(() => import('./components/XhsProductForm.vue'))
const BulkPublishPanel = defineAsyncComponent(() => import('./components/BulkPublishPanel.vue'))
const CategoryAliasPanel = defineAsyncComponent(() => import('./components/CategoryAliasPanel.vue'))
const ShopPanel = defineAsyncComponent(() => import('./components/ShopPanel.vue'))
const ProductDetailView = defineAsyncComponent(() => import('./components/ProductDetailView.vue'))
const ProductSearchBar = defineAsyncComponent(() => import('./components/ProductSearchBar.vue'))
import EllipsisText from './components/EllipsisText.vue'
import {
  currentOperator,
  currentShopId,
  loadShops,
  setCurrentShop,
  setOperator,
  shops,
  shopsError,
  shopsLoaded,
} from './shopContext'

const activeView = ref('products')
const platform = ref('wechat')
const loading = ref(false)
const actionId = ref('')
const serviceOnline = ref(false)
const tokenTesting = ref(false)
const xhsOnline = ref(false)
const xhsChecking = ref(false)
const xhsTokenInfo = ref(null)
const xhsCode = ref('')
const xhsAuthorizing = ref(false)
const xhsRefreshing = ref(false)
const products = ref([])
const total = ref(0)
const statusFilter = ref('')
const nextKey = ref(null)
const pageHistory = ref([])
const pageSize = ref(20)
const detailVisible = ref(false)
const detailLoading = ref(false)
const detailData = ref(null)
const editVisible = ref(false)
const editLoading = ref(false)
const editProductId = ref('')
const editProductData = ref(null)
const apiDisplay = import.meta.env.VITE_API_BASE_URL || '/api（代理至 http://127.0.0.1:8000）'

// 搜索栏筛选条件（微信 API 不支持服务端筛选，在前端对已拉取列表做本地过滤）
const searchForm = ref({ ids: '', codes: '', keyword: '', minPrice: null, maxPrice: null, minStock: null, maxStock: null })

const statusMap = {
  0: { label: '未上架', type: 'info' },
  5: { label: '销售中', type: 'success' },
  11: { label: '已下架', type: 'warning' },
  13: { label: '审核中', type: 'warning' },
}

// 全店统计:微信接口支持按状态筛选,各状态只拉 1 条取接口返回的 total(代价很小)。
// 之前是「只统计当前这一页(最多20条)」,数字会明显偏小。
const stats = ref({ all: 0, online: 0, offline: 0, draft: 0, ready: false })
async function loadStats() {
  const totalOf = async (status) => {
    const params = { page_size: 1 }
    if (status !== undefined) params.status = status
    return unwrapProducts(await storeApi.listProducts(params)).total
  }
  try {
    const [all, draft, online, offline] = await Promise.all([totalOf(), totalOf(0), totalOf(5), totalOf(11)])
    stats.value = { all, draft, online, offline, ready: true }
  } catch {
    stats.value = { ...stats.value, ready: false }
  }
}

function unwrapProducts(data) {
  const result = data?.result || {}
  return {
    list: result.products || [],
    total: result.total || 0,
    nextKey: result.next_key || null,
  }
}

function productId(row) {
  return row.product_id || row.productId || row.id
}

function productTitle(row) {
  return row.title || row.product_name || row.name || '未命名商品'
}

function productImage(row) {
  return row.head_imgs?.[0] || row.head_img || row.cover_img || ''
}

function productPrice(row) {
  const price = row.skus?.[0]?.sale_price ?? row.sale_price ?? row.price
  return price === undefined || price === null ? '--' : `¥${(Number(price) / 100).toFixed(2)}`
}

function productStock(row) {
  if (Array.isArray(row.skus)) return row.skus.reduce((sum, sku) => sum + Number(sku.stock_num || 0), 0)
  return row.stock_num ?? '--'
}

// 将"多个以空格/逗号/分号分隔"的输入拆成 trim 后的数组
function splitTokens(str) {
  if (!str) return []
  return String(str).split(/[\s,，;；]+/).map((s) => s.trim()).filter(Boolean)
}
function productMinPriceYuan(row) {
  const prices = (row.skus || []).map((sku) => Number(sku.sale_price ?? sku.price)).filter(Number.isFinite)
  if (!prices.length) {
    const single = Number(row.sale_price ?? row.price)
    return Number.isFinite(single) ? single / 100 : null
  }
  return Math.min(...prices) / 100
}
function productStockNum(row) {
  if (Array.isArray(row.skus)) return row.skus.reduce((sum, sku) => sum + Number(sku.stock_num || 0), 0)
  const n = Number(row.stock_num)
  return Number.isFinite(n) ? n : null
}
function productCodes(row) {
  // 商品级编码 + SKU 级编码/条码
  const codes = [row.out_product_id]
  for (const sku of row.skus || []) {
    codes.push(sku.out_sku_id, sku.sku_code, sku.barcode)
  }
  return codes.filter(Boolean).map(String)
}

const filteredProducts = computed(() => {
  const f = searchForm.value
  const ids = splitTokens(f.ids).map(String)
  const codes = splitTokens(f.codes).map((s) => s.toLowerCase())
  const keywords = splitTokens(f.keyword).map((s) => s.toLowerCase())
  return products.value.filter((row) => {
    if (ids.length) {
      const pid = String(productId(row) ?? '')
      if (!ids.some((id) => pid && pid.includes(id))) return false
    }
    if (codes.length) {
      const allCodes = productCodes(row).map((c) => c.toLowerCase())
      if (!codes.some((c) => allCodes.some((ac) => ac.includes(c)))) return false
    }
    if (keywords.length) {
      const title = String(productTitle(row) || '').toLowerCase()
      if (!keywords.every((k) => title.includes(k))) return false
    }
    const price = productMinPriceYuan(row)
    if (f.minPrice != null && f.minPrice !== '' && (price == null || price < Number(f.minPrice))) return false
    if (f.maxPrice != null && f.maxPrice !== '' && (price == null || price > Number(f.maxPrice))) return false
    const stock = productStockNum(row)
    if (f.minStock != null && f.minStock !== '' && (stock == null || stock < Number(f.minStock))) return false
    if (f.maxStock != null && f.maxStock !== '' && (stock == null || stock > Number(f.maxStock))) return false
    return true
  })
})

function resetSearch() {
  searchForm.value = { ids: '', codes: '', keyword: '', minPrice: null, maxPrice: null, minStock: null, maxStock: null }
}

// silent=true 供页面自动探活;手动点刷新按钮时给明确反馈,否则点了像没反应
async function checkHealth(silent = true) {
  try {
    await storeApi.health()
    serviceOnline.value = true
    if (!silent) ElMessage.success('服务连接正常')
  } catch {
    serviceOnline.value = false
    if (!silent) ElMessage.error('无法连接服务，请检查后端服务是否已启动')
  }
}

async function testToken() {
  tokenTesting.value = true
  try {
    await storeApi.testToken()
    ElMessage.success('微信接口凭证有效')
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    tokenTesting.value = false
  }
}

// 微信接口慢:每一页都要重新「拉 ID 列表 + 逐个查详情」。翻回已经看过的页时直接复用缓存,
// 不再转圈;点顶栏刷新 / 上架下架后由 refreshFromStart 清缓存,保证能看到最新数据。
// 缓存键必须带 currentShopId:否则切店后会命中上一家店的缓存,列表串店(统计是真实请求、列表是缓存,两边对不上)。
const productPageCache = new Map()
const pageCacheKey = (cursor) => `${currentShopId.value || ''}|${statusFilter.value}|${pageSize.value}|${cursor || ''}`

async function loadProducts(cursor = null, pushHistory = false) {
  const cached = productPageCache.get(pageCacheKey(cursor))
  if (cached) {
    if (pushHistory) pageHistory.value.push(cursor)
    products.value = cached.list
    total.value = cached.total
    nextKey.value = cached.nextKey
    return true
  }
  loading.value = true
  try {
    const params = { page_size: pageSize.value }
    if (statusFilter.value !== '') params.status = statusFilter.value
    if (cursor) params.next_key = cursor
    const data = unwrapProducts(await storeApi.listProducts(params))
    if (pushHistory) pageHistory.value.push(cursor)
    products.value = data.list
    total.value = data.total
    nextKey.value = data.nextKey
    productPageCache.set(pageCacheKey(cursor), { list: data.list, total: data.total, nextKey: data.nextKey })
    return true
  } catch (error) {
    products.value = []
    ElMessage.error(error.message)
    return false
  } finally {
    loading.value = false
  }
}

function onPageSizeChange(size) {
  pageSize.value = size
  refreshFromStart()
}

// 微信是游标翻页，不支持跳任意页；只允许上一页/下一页
function onPageChange(targetPage) {
  const currentPage = pageHistory.value.length + 1
  if (targetPage === currentPage) return
  if (targetPage === currentPage - 1) {
    previousPage()
  } else if (targetPage === currentPage + 1 && nextKey.value) {
    loadProducts(nextKey.value, true)
  } else {
    ElMessage.warning('微信游标翻页不支持跳页，请逐页浏览')
  }
}

function refreshFromStart() {
  productPageCache.clear()   // 强制重拉:清掉分页缓存,避免看到旧数据
  pageHistory.value = []
  loadProducts()
}

// 切换状态筛选:不清缓存(缓存键含 status),切回看过的状态能秒开
function switchStatus() {
  pageHistory.value = []
  loadProducts()
}

// 顶栏圆圈刷新:以前只探活,所以只弹"服务连接正常"、列表不动,容易被误解为"没反应"。
// 现在探活通过后同时刷新当前页数据:微信列表在本组件刷新,小红书列表通过 refreshTick 通知子组件。
const globalRefreshTick = ref(0)
async function globalRefresh() {
  await checkHealth()
  if (!serviceOnline.value) { ElMessage.error('无法连接服务，请检查后端服务是否已启动'); return }
  globalRefreshTick.value += 1
  if (activeView.value === 'products') { await loadProducts(); void loadStats() }
  ElMessage.success('已刷新')
}

async function previousPage() {
  pageHistory.value.pop()
  const previousCursor = pageHistory.value.at(-1) || null
  await loadProducts(previousCursor)
}

async function showDetail(row) {
  detailVisible.value = true
  detailLoading.value = true
  detailData.value = null
  try {
    const data = await storeApi.getProduct(productId(row))
    const detail = data.result?.data || data.result || {}
    // 详情接口有时不返回商品状态，沿用列表中的状态，避免审核中被误显示为销售中
    detailData.value = { ...detail, product: { ...(detail.product || {}), status: detail.product?.status ?? detail.status ?? row.status } }
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    detailLoading.value = false
  }
}

async function runAction(row, action) {
  const id = productId(row)
  const actionText = { listing: '提交上架', delisting: '下架', delete: '永久删除' }[action]
  if (action === 'delete') {
    try {
      await ElMessageBox.confirm(`商品 ${id} 删除后不可恢复，确认继续？`, '永久删除商品', {
        confirmButtonText: '确认删除', cancelButtonText: '取消', type: 'warning',
      })
    } catch { return }
  }
  actionId.value = `${action}-${id}`
  try {
    if (action === 'listing') await storeApi.listingProduct(id)
    if (action === 'delisting') await storeApi.delistingProduct(id)
    if (action === 'delete') await storeApi.deleteProduct(id)
    ElMessage.success(`${actionText}操作已提交`)
    await refreshFromStart()
    void loadStats()
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    actionId.value = ''
  }
}

function afterCreated(id) {
  activeView.value = 'products'
  refreshFromStart()
  if (id) ElMessage.success(`平台商品 ID：${id}`)
}

async function openEditProduct(row) {
  editVisible.value = true
  editLoading.value = true
  editProductId.value = String(productId(row))
  editProductData.value = null
  try {
    const data = await storeApi.getProduct(productId(row))
    editProductData.value = data.result?.data?.product || data.result?.product || data.result
  } catch (error) {
    ElMessage.error(error.message)
    editVisible.value = false
  } finally {
    editLoading.value = false
  }
}

function afterProductUpdated() {
  editVisible.value = false
  refreshFromStart()
}

function selectPlatform(nextPlatform) {
  platform.value = nextPlatform
  activeView.value = nextPlatform === 'xhs' ? 'xhs-products' : 'products'
}

// ------------------------------------------------------------------
// 移动端导航：手机上侧栏整体隐藏，底部导航只放高频入口，
// 低频页面（发布 / 类目映射 / 店铺管理 / 接口设置）收进「更多」抽屉。
// goTo 与侧栏菜单保持同一套 platform 归属逻辑。
// ------------------------------------------------------------------
const moreNavVisible = ref(false)
const moreNavItems = [
  { view: 'create', label: '发布微信商品', icon: CirclePlus, platform: 'wechat' },
  { view: 'xhs-create', label: '发布小红书商品', icon: CirclePlus, platform: 'xhs' },
  { view: 'category-aliases', label: '类目映射', icon: Collection, platform: 'bulk' },
  { view: 'shops', label: '店铺管理', icon: Shop },
  { view: 'settings', label: '接口设置', icon: Setting },
]
const isMoreNavActive = computed(() => moreNavItems.some((item) => item.view === activeView.value))
function goTo(view) {
  if (view === 'products') return selectPlatform('wechat')
  if (view === 'xhs-products') return selectPlatform('xhs')
  if (view === 'bulk-publish' || view === 'category-aliases') platform.value = 'bulk'
  const item = moreNavItems.find((nav) => nav.view === view)
  if (item?.platform) platform.value = item.platform
  activeView.value = view
}

// 拆店后一家店只属于一个平台：当前视图与店铺平台不匹配时自动跳转，免得切到小红书店后还停在微信商品页要再手动换。
// 只管平台专属页（微信商品 / 小红书商品 / 发布小红书商品）；批量发布、类目映射、店铺管理、设置不限平台，不干预。
// 返回 'wechat' / 'xhs'（发生了跳转）或 ''（视图本来就匹配）。
function alignViewWithShop(shop) {
  if (!shop) return ''
  const isWechatOnly = Boolean(shop.wechat?.appid) && !shop.xhs?.app_id
  const isXhsOnly = Boolean(shop.xhs?.app_id) && !shop.wechat?.appid
  if (isWechatOnly && (activeView.value === 'xhs-products' || activeView.value === 'xhs-create')) {
    selectPlatform('wechat')
    return 'wechat'
  }
  if (isXhsOnly && activeView.value === 'products') {
    selectPlatform('xhs')
    return 'xhs'
  }
  return ''
}

function afterXhsCreated() {
  platform.value = 'xhs'
  activeView.value = 'xhs-products'
}

async function checkXhsStatus() {
  xhsChecking.value = true
  try {
    await xhsApi.health()
    xhsOnline.value = true
    try {
      xhsTokenInfo.value = await xhsApi.tokenInfo()
    } catch {
      xhsTokenInfo.value = null
    }
  } catch {
    xhsOnline.value = false
    xhsTokenInfo.value = null
  } finally {
    xhsChecking.value = false
  }
}

function xhsStatusText() {
  if (!xhsOnline.value) return '服务未连接（请先启动 xhs_api.py，默认 8010 端口）'
  const remain = xhsTokenInfo.value?.remain_days
  // 没有过期时间记录 = 服务端没存过 token,当前用的是代码里写死的旧 token(早已过期)
  if (remain === null || remain === undefined) return '未授权（当前用的是代码内置旧 token，请提交 code 授权）'
  if (remain <= 0) return '已过期，请点“一键续期”'
  return `授权有效，剩余 ${remain} 天`
}

async function authorizeXhs() {
  const code = xhsCode.value.trim()
  if (!code) return ElMessage.warning('请先粘贴授权回调里的 code')
  xhsAuthorizing.value = true
  try {
    await xhsApi.tokenByCode(code)
    ElMessage.success('小红书授权成功')
    xhsCode.value = ''
    await checkXhsStatus()
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    xhsAuthorizing.value = false
  }
}

async function refreshXhsToken() {
  xhsRefreshing.value = true
  try {
    const res = await xhsApi.tokenRefresh()
    // 小红书规则:accessToken 剩余>30分钟时不会换发新 token,后端返回 refreshed=false
    if (res?.refreshed === false) {
      ElMessage.info(res?.result?.message || 'accessToken 仍有效，暂不需要续期')
    } else {
      ElMessage.success('小红书授权已续期')
    }
    await checkXhsStatus()
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    xhsRefreshing.value = false
  }
}

// 进入连接设置页时自动刷新一次小红书授权状态
watch(activeView, (value) => {
  if (value === 'settings') void checkXhsStatus()
})

// ------------------------------------------------------------------
// 多店铺：顶栏店铺选择器 + 操作人留痕
// 切换店铺后，所有请求自动带上新的 X-Shop-Id（见 shopContext 的请求拦截器），
// 这里负责把"属于旧店铺的界面数据"清干净并重新拉取，避免看到串店的数据。
// ------------------------------------------------------------------
const shopSwitching = ref(false)
const shopIdProxy = computed({
  get: () => currentShopId.value,
  set: (value) => {
    if (!value || value === currentShopId.value) return
    setCurrentShop(value)
  },
})
const operatorProxy = computed({
  get: () => currentOperator.value,
  set: (value) => setOperator(String(value || '').trim()),
})

async function refreshShops() {
  shopSwitching.value = true
  try {
    await loadShops()
    if (shopsError.value) ElMessage.error(`店铺列表加载失败：${shopsError.value}`)
  } finally {
    shopSwitching.value = false
  }
}

watch(currentShopId, (value) => {
  const target = shops.value.find((item) => item.shop_id === value)
  const realigned = target ? alignViewWithShop(target) : ''
  // 旧店铺的商品列表/分页/详情/编辑态全部失效，清掉再刷新
  products.value = []
  total.value = 0
  pageHistory.value = []
  nextKey.value = null
  productPageCache.clear() // 分页缓存一并清掉：切店后强制重新拉取，不读上一家店的旧页
  detailVisible.value = false
  editVisible.value = false
  if (serviceOnline.value) void globalRefresh()
  if (target) {
    ElMessage.success(
      realigned === 'xhs' ? `已切换到「${target.name}」，该店是小红书店，已自动进入小红书商品页`
        : realigned === 'wechat' ? `已切换到「${target.name}」，该店是微信店，已自动回到微信商品页`
        : `已切换到「${target.name}」`,
    )
  }
})

onMounted(async () => {
  // 先取店铺清单：请求头要靠它，否则后续请求会落到后端默认店上
  await refreshShops()
  // 刷新页面后 activeView 固定回「微信商品」，若恢复的店铺是小红书店，这里静默对齐到小红书商品页
  alignViewWithShop(shops.value.find((item) => item.shop_id === currentShopId.value))
  await checkHealth()
  if (serviceOnline.value) { await loadProducts(); void loadStats() }
})
</script>

<template>
  <el-container class="app-shell">
    <el-aside width="224px" class="sidebar">
      <div class="brand">
        <div class="brand-mark"><el-icon><ShoppingBag /></el-icon></div>
        <div><strong>商品中台</strong><span>多平台运营</span></div>
      </div>
      <el-menu :default-active="activeView" class="nav-menu" @select="activeView = $event">
        <el-menu-item index="products" @click="selectPlatform('wechat')"><el-icon><Goods /></el-icon><span>微信商品</span></el-menu-item>
        <el-menu-item index="xhs-products" @click="selectPlatform('xhs')"><el-icon><Notebook /></el-icon><span>小红书商品</span></el-menu-item>
        <el-menu-item index="create" @click="platform = 'wechat'"><el-icon><CirclePlus /></el-icon><span>发布微信商品</span></el-menu-item>
        <el-menu-item index="xhs-create" @click="platform = 'xhs'"><el-icon><CirclePlus /></el-icon><span>发布小红书商品</span></el-menu-item>
        <el-menu-item index="bulk-publish" @click="platform = 'bulk'"><el-icon><Upload /></el-icon><span>批量发布商品</span></el-menu-item>
        <el-menu-item index="category-aliases" @click="platform = 'bulk'"><el-icon><Collection /></el-icon><span>类目映射</span></el-menu-item>
        <el-menu-item index="shops"><el-icon><Shop /></el-icon><span>店铺管理</span></el-menu-item>
        <el-menu-item index="settings"><el-icon><Setting /></el-icon><span>接口设置</span></el-menu-item>
      </el-menu>
      <div class="sidebar-footer">
        <div :class="['service-dot', { online: serviceOnline }]" />
        <div><strong>{{ serviceOnline ? '服务运行正常' : '服务暂时无法连接' }}</strong><span>平台服务</span></div>
      </div>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <div>
          <span class="breadcrumb">运营中心 /</span>
          <strong>{{ activeView === 'products' ? '微信商品' : activeView === 'xhs-products' ? '小红书商品' : activeView === 'xhs-create' ? '发布小红书商品' : activeView === 'create' ? '发布微信商品' : activeView === 'bulk-publish' ? '批量发布商品' : activeView === 'category-aliases' ? '类目映射' : activeView === 'shops' ? '店铺管理' : '连接设置' }}</strong>
        </div>
        <div class="topbar-actions">
          <el-select v-model="shopIdProxy" :loading="shopSwitching" placeholder="选择店铺"
                     class="shop-select" :disabled="!shops.length"
                     :title="shopsError ? `店铺列表加载失败：${shopsError}` : '当前店铺：批次/任务/图片缓存/类目映射都按它隔离'">
            <el-option v-for="item in shops" :key="item.shop_id" :label="item.name" :value="item.shop_id">
              <div class="shop-option">
                <span class="shop-option-name">{{ item.name }}</span>
                <span class="shop-option-tags">
                  <el-tag v-if="item.wechat?.appid" size="small" effect="light" round>微信</el-tag>
                  <el-tag v-if="item.xhs?.app_id" size="small" type="danger" effect="light" round>小红书</el-tag>
                </span>
              </div>
            </el-option>
          </el-select>
          <el-tooltip content="操作人：切换店铺/发布时记录，用于留痕">
            <el-input v-model="operatorProxy" placeholder="操作人" clearable maxlength="20" class="operator-input" />
          </el-tooltip>
          <el-tooltip content="刷新服务状态与当前列表"><el-button circle :icon="Refresh" @click="globalRefresh" /></el-tooltip>
          <div class="operator"><div class="avatar">OP</div></div>
        </div>
      </el-header>

      <el-main class="main-content">
        <!-- key 里带 currentShopId：切店时强制重建组件，避免保留上一家店的列表/分页状态；max=1 保证旧店铺实例被淘汰，不累积缓存 -->
        <KeepAlive :max="1">
          <XhsProductPanel v-if="activeView === 'xhs-products'" :key="`xhs-products-${currentShopId}`" :refresh-tick="globalRefreshTick" @create="activeView = 'xhs-create'" />
        </KeepAlive>

        <template v-if="activeView === 'xhs-create'">
          <div class="page-heading"><div><h1>发布小红书商品</h1><p>小红书专属字段和接口，不会写入微信平台</p></div><el-button :icon="ArrowLeft" @click="activeView = 'xhs-products'">返回小红书商品</el-button></div>
          <XhsProductForm @created="afterXhsCreated" @cancel="activeView = 'xhs-products'" />
        </template>

        <!-- key 里带 currentShopId：批次/任务/图片缓存都按店铺隔离，切店必须重建；max=1 保证旧店铺实例被淘汰，不累积缓存 -->
        <KeepAlive :max="1">
          <BulkPublishPanel v-if="activeView === 'bulk-publish'" :key="`bulk-publish-${currentShopId}`" @back="activeView = 'products'" />
        </KeepAlive>

        <CategoryAliasPanel v-if="activeView === 'category-aliases'" :key="`category-aliases-${currentShopId}`" />

        <template v-if="activeView === 'products'">
          <div class="page-heading">
            <div><h1>微信商品</h1><p>查看微信商品状态并完成上下架操作</p></div>
            <el-button type="primary" :icon="Plus" @click="activeView = 'create'">发布微信商品</el-button>
          </div>

          <el-alert v-if="!serviceOnline" title="暂时无法连接服务" description="请检查平台服务是否已启动。" type="warning" show-icon :closable="false" class="offline-alert" />

          <div class="stats-grid">
            <div class="stat-item"><span>商品总数</span><strong>{{ stats.ready ? stats.all : '—' }}</strong><el-icon><Goods /></el-icon></div>
            <div class="stat-item"><span>销售中</span><strong>{{ stats.ready ? stats.online : '—' }}</strong><el-icon class="green"><CircleCheck /></el-icon></div>
            <div class="stat-item"><span>未上架</span><strong>{{ stats.ready ? stats.draft : '—' }}</strong><el-icon class="gray"><EditPen /></el-icon></div>
            <div class="stat-item"><span>已下架</span><strong>{{ stats.ready ? stats.offline : '—' }}</strong><el-icon class="amber"><Remove /></el-icon></div>
          </div>

          <section class="content-panel">
            <ProductSearchBar
              v-model="searchForm"
              code-placeholder="商品编码/SKU编码/条码，空格或逗号分隔"
              @reset="resetSearch"
            />
            <div class="panel-toolbar">
              <el-segmented v-model="statusFilter" :options="[
                { label: '全部', value: '' }, { label: '未上架', value: 0 },
                { label: '销售中', value: 5 }, { label: '已下架', value: 11 },
              ]" @change="switchStatus" />
            </div>
            <el-table v-loading="loading" :data="filteredProducts" class="product-table" empty-text="暂无商品数据" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }">
              <el-table-column label="商品" min-width="310">
                <template #default="{ row }">
                  <div class="product-cell">
                    <el-image :src="productImage(row)" fit="cover" class="product-thumb">
                      <template #error><div class="image-fallback"><el-icon><Picture /></el-icon></div></template>
                    </el-image>
                    <div><EllipsisText tag="strong" :text="productTitle(row)" /><EllipsisText :text="productId(row)" /></div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="价格" width="120" show-overflow-tooltip><template #default="{ row }"><strong class="price">{{ productPrice(row) }}</strong></template></el-table-column>
              <el-table-column label="库存" width="100" show-overflow-tooltip><template #default="{ row }">{{ productStock(row) }}</template></el-table-column>
              <el-table-column label="状态" width="120" show-overflow-tooltip>
                <template #default="{ row }"><el-tag :type="statusMap[row.status]?.type || 'info'" effect="light" round>{{ statusMap[row.status]?.label || '状态待确认' }}</el-tag></template>
              </el-table-column>
              <el-table-column label="操作" width="250" fixed="right">
                <template #default="{ row }">
                  <el-button link type="primary" @click="showDetail(row)">详情</el-button>
                  <el-button link type="primary" @click="openEditProduct(row)">编辑</el-button>
                  <el-button v-if="Number(row.status) !== 5" link type="primary" :loading="actionId === `listing-${productId(row)}`" @click="runAction(row, 'listing')">上架</el-button>
                  <el-button v-else link type="warning" :loading="actionId === `delisting-${productId(row)}`" @click="runAction(row, 'delisting')">下架</el-button>
                  <el-button link type="danger" :loading="actionId === `delete-${productId(row)}`" @click="runAction(row, 'delete')">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
            <div class="cursor-pagination">
              <el-pagination
                :current-page="pageHistory.length + 1"
                :page-size="pageSize"
                :page-sizes="[10, 20, 30]"
                :total="total || products.length"
                layout="total, sizes, prev, pager, next"
                :disabled="loading"
                @size-change="onPageSizeChange"
                @current-change="onPageChange"
              />
              <span class="muted-copy">当前筛选显示 {{ filteredProducts.length }} 项</span>
            </div>
          </section>
        </template>

        <template v-if="activeView === 'create'">
          <div class="page-heading">
             <div><h1>发布微信商品</h1><p>完成信息录入后先创建草稿，再从商品列表提交上架审核</p></div>
            <el-button :icon="ArrowLeft" @click="activeView = 'products'">返回商品列表</el-button>
          </div>
          <ProductForm @created="afterCreated" @cancel="activeView = 'products'" />
        </template>

        <ShopPanel v-if="activeView === 'shops'" :refresh-tick="globalRefreshTick" />

        <template v-if="activeView === 'settings'">
          <div class="page-heading"><div><h1>连接设置</h1><p>查看平台服务的连接状态</p></div></div>
          <section class="settings-panel">
            <div class="connection-status">
              <div :class="['status-icon', { online: serviceOnline }]"><el-icon><Connection /></el-icon></div>
              <div><h3>平台服务连接状态</h3><p>连接地址：{{ apiDisplay }}</p></div>
              <el-tag :type="serviceOnline ? 'success' : 'danger'" effect="light">{{ serviceOnline ? '正常' : '离线' }}</el-tag>
            </div>
            <el-divider />
            <div class="setting-row"><div><strong>微信账号授权</strong><span>检查微信账号授权是否正常</span></div><el-button type="primary" plain :loading="tokenTesting" :disabled="!serviceOnline" @click="testToken">检查授权</el-button></div>
            <el-divider />
            <div class="setting-row">
              <div><strong>小红书账号授权</strong><span>{{ xhsStatusText() }}</span></div>
              <div class="xhs-auth-actions">
                <el-button plain :loading="xhsChecking" @click="checkXhsStatus">检查状态</el-button>
                <el-button type="primary" plain :loading="xhsRefreshing" :disabled="!xhsOnline || !xhsTokenInfo?.has_refresh_token" @click="refreshXhsToken">一键续期</el-button>
              </div>
            </div>
            <div class="setting-row xhs-code-row">
              <el-input v-model="xhsCode" placeholder="粘贴授权回调地址里的 code（形如 ?code=xxx，10 分钟内有效）" clearable />
              <el-button type="primary" :loading="xhsAuthorizing" :disabled="!xhsOnline" @click="authorizeXhs">提交授权</el-button>
            </div>
            <p class="muted-copy xhs-auth-tip">获取 code：浏览器打开授权链接（appId 换成应用 ID、redirectUri 换成回调地址），用店铺主账号登录后从回调地址复制 code。授权成功后 token 保存在服务端，快过期时会自动续期。</p>
            <p class="muted-copy">本页的授权/续期只作用于「顶栏当前选中的店铺」；想一次看清所有店铺的 token 有效期与凭证状态，走左侧「店铺管理」。</p>
            <div class="security-note"><el-icon><Warning /></el-icon><div><strong>安全提醒</strong><p>账号授权信息仅由服务端保存，不会显示在前端或返回给页面</p></div></div>
          </section>
        </template>
      </el-main>
    </el-container>

    <nav class="mobile-nav" aria-label="移动端主导航">
      <button :class="{ active: activeView === 'products' }" @click="goTo('products')">
        <el-icon><Goods /></el-icon><span>微信</span>
      </button>
      <button :class="{ active: activeView === 'xhs-products' }" @click="goTo('xhs-products')">
        <el-icon><Notebook /></el-icon><span>小红书</span>
      </button>
      <button :class="{ active: activeView === 'bulk-publish' }" @click="goTo('bulk-publish')">
        <el-icon><Upload /></el-icon><span>批量</span>
      </button>
      <button :class="{ active: isMoreNavActive }" @click="moreNavVisible = true">
        <el-icon><More /></el-icon><span>更多</span>
      </button>
    </nav>

    <!-- 移动端「更多」导航：收纳侧栏里的低频页面 -->
    <el-drawer v-model="moreNavVisible" direction="btt" size="auto" :with-header="false" class="more-nav-drawer">
      <div class="more-nav-grid">
        <button v-for="item in moreNavItems" :key="item.view" :class="{ active: activeView === item.view }"
                @click="goTo(item.view); moreNavVisible = false">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </button>
      </div>
    </el-drawer>

    <el-drawer v-model="detailVisible" title="微信商品详情" size="760px" class="product-detail-drawer">
      <div v-loading="detailLoading" class="detail-drawer">
        <ProductDetailView v-if="detailData" platform="wechat" :data="detailData" />
      </div>
    </el-drawer>

    <el-drawer v-model="editVisible" title="编辑微信商品" size="860px" destroy-on-close>
      <div v-loading="editLoading">
        <ProductForm v-if="editProductData" :product-id="editProductId" :product-data="editProductData" @updated="afterProductUpdated" @cancel="editVisible = false" />
      </div>
    </el-drawer>
  </el-container>
</template>
