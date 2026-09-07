<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft,
  CircleCheck,
  CirclePlus,
  Collection,
  Connection,
  EditPen,
  Goods,
  Picture,
  Plus,
  Refresh,
  Remove,
  Setting,
  ShoppingBag,
  Warning,
  Notebook,
  Upload,
} from '@element-plus/icons-vue'
import { storeApi } from './api'
import ProductForm from './components/ProductForm.vue'
import XhsProductPanel from './components/XhsProductPanel.vue'
import XhsProductForm from './components/XhsProductForm.vue'
import BulkPublishPanel from './components/BulkPublishPanel.vue'
import CategoryAliasPanel from './components/CategoryAliasPanel.vue'
import ProductSearchBar from './components/ProductSearchBar.vue'
import ProductDetailView from './components/ProductDetailView.vue'
import EllipsisText from './components/EllipsisText.vue'

const activeView = ref('products')
const platform = ref('wechat')
const loading = ref(false)
const actionId = ref('')
const serviceOnline = ref(false)
const tokenTesting = ref(false)
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
}

const stats = computed(() => ({
  all: total.value || products.value.length,
  online: products.value.filter((item) => Number(item.status) === 5).length,
  offline: products.value.filter((item) => Number(item.status) === 11).length,
  draft: products.value.filter((item) => Number(item.status) === 0).length,
}))

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

// 将“多个以空格/逗号/分号分隔”的输入拆成 trim 后的数组
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

async function checkHealth() {
  try {
    await storeApi.health()
    serviceOnline.value = true
  } catch {
    serviceOnline.value = false
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

async function loadProducts(cursor = null, pushHistory = false) {
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
  } catch (error) {
    products.value = []
    ElMessage.error(error.message)
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
  pageHistory.value = []
  loadProducts()
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
    detailData.value = data.result?.data || data.result
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

function afterXhsCreated() {
  platform.value = 'xhs'
  activeView.value = 'xhs-products'
}

onMounted(async () => {
  await checkHealth()
  if (serviceOnline.value) await loadProducts()
})
</script>

<template>
  <el-container class="app-shell">
    <el-aside width="224px" class="sidebar">
      <div class="brand">
        <div class="brand-mark"><el-icon><ShoppingBag /></el-icon></div>
        <div><strong>微信小店</strong><span>运营工作台</span></div>
      </div>
      <el-menu :default-active="activeView" class="nav-menu" @select="activeView = $event">
        <el-menu-item index="products" @click="selectPlatform('wechat')"><el-icon><Goods /></el-icon><span>微信商品</span></el-menu-item>
        <el-menu-item index="xhs-products" @click="selectPlatform('xhs')"><el-icon><Notebook /></el-icon><span>小红书商品</span></el-menu-item>
        <el-menu-item index="create" @click="platform = 'wechat'"><el-icon><CirclePlus /></el-icon><span>发布微信商品</span></el-menu-item>
        <el-menu-item index="xhs-create" @click="platform = 'xhs'"><el-icon><CirclePlus /></el-icon><span>发布小红书商品</span></el-menu-item>
        <el-menu-item index="bulk-publish" @click="platform = 'bulk'"><el-icon><Upload /></el-icon><span>批量发布商品</span></el-menu-item>
        <el-menu-item index="category-aliases" @click="platform = 'bulk'"><el-icon><Collection /></el-icon><span>类目映射表</span></el-menu-item>
        <el-menu-item index="settings"><el-icon><Setting /></el-icon><span>接口设置</span></el-menu-item>
      </el-menu>
      <div class="sidebar-footer">
        <div :class="['service-dot', { online: serviceOnline }]" />
        <div><strong>{{ serviceOnline ? '服务运行正常' : '后端未连接' }}</strong><span>API · 127.0.0.1:8000</span></div>
      </div>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <div>
          <span class="breadcrumb">运营中心 /</span>
          <strong>{{ activeView === 'products' ? '微信商品' : activeView === 'xhs-products' ? '小红书商品' : activeView === 'xhs-create' ? '发布小红书商品' : activeView === 'create' ? '发布微信商品' : activeView === 'bulk-publish' ? '批量发布商品' : activeView === 'category-aliases' ? '类目映射表' : '接口设置' }}</strong>
        </div>
        <div class="topbar-actions">
          <el-tooltip content="刷新服务状态"><el-button circle :icon="Refresh" @click="checkHealth" /></el-tooltip>
          <div class="operator"><span>运营</span><div class="avatar">OP</div></div>
        </div>
      </el-header>

      <el-main class="main-content">
        <KeepAlive>
          <XhsProductPanel v-if="activeView === 'xhs-products'" @create="activeView = 'xhs-create'" />
        </KeepAlive>

        <template v-if="activeView === 'xhs-create'">
          <div class="page-heading"><div><h1>发布小红书商品</h1><p>小红书专属字段和接口，不会写入微信小店</p></div><el-button :icon="ArrowLeft" @click="activeView = 'xhs-products'">返回小红书商品</el-button></div>
          <XhsProductForm @created="afterXhsCreated" @cancel="activeView = 'xhs-products'" />
        </template>

        <KeepAlive>
          <BulkPublishPanel v-if="activeView === 'bulk-publish'" />
        </KeepAlive>

        <CategoryAliasPanel v-if="activeView === 'category-aliases'" />

        <template v-if="activeView === 'products'">
          <div class="page-heading">
            <div><h1>商品管理</h1><p>查看微信小店商品状态并完成上下架操作</p></div>
            <el-button type="primary" :icon="Plus" @click="activeView = 'create'">发布新商品</el-button>
          </div>

          <el-alert v-if="!serviceOnline" title="暂时无法连接后端服务" description="请先启动 FastAPI 服务，前端将通过 /api 代理访问 8000 端口。" type="warning" show-icon :closable="false" class="offline-alert" />

          <div class="stats-grid">
            <div class="stat-item"><span>商品总数</span><strong>{{ stats.all }}</strong><el-icon><Goods /></el-icon></div>
            <div class="stat-item"><span>销售中</span><strong>{{ stats.online }}</strong><el-icon class="green"><CircleCheck /></el-icon></div>
            <div class="stat-item"><span>未上架</span><strong>{{ stats.draft }}</strong><el-icon class="gray"><EditPen /></el-icon></div>
            <div class="stat-item"><span>已下架</span><strong>{{ stats.offline }}</strong><el-icon class="amber"><Remove /></el-icon></div>
          </div>

          <section class="content-panel">
            <ProductSearchBar
              v-model="searchForm"
              code-placeholder="商品编码/SKU编码/条码，空格/逗号分隔"
              @reset="resetSearch"
            />
            <div class="panel-toolbar">
              <el-segmented v-model="statusFilter" :options="[
                { label: '全部', value: '' }, { label: '未上架', value: 0 },
                { label: '销售中', value: 5 }, { label: '已下架', value: 11 },
              ]" @change="refreshFromStart" />
              <el-button :icon="Refresh" @click="refreshFromStart">刷新</el-button>
            </div>
            <el-table v-loading="loading" :data="filteredProducts" class="product-table" empty-text="暂无商品数据" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }">
              <el-table-column label="商品" min-width="310">
                <template #default="{ row }">
                  <div class="product-cell">
                    <el-image :src="productImage(row)" fit="cover" class="product-thumb">
                      <template #error><div class="image-fallback"><el-icon><Picture /></el-icon></div></template>
                    </el-image>
                    <div><EllipsisText tag="strong" :text="productTitle(row)" /><EllipsisText :text="`ID：${productId(row) || '--'}`" /></div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="价格" width="120" show-overflow-tooltip><template #default="{ row }"><strong class="price">{{ productPrice(row) }}</strong></template></el-table-column>
              <el-table-column label="库存" width="100" show-overflow-tooltip><template #default="{ row }">{{ productStock(row) }}</template></el-table-column>
              <el-table-column label="状态" width="120" show-overflow-tooltip>
                <template #default="{ row }"><el-tag :type="statusMap[row.status]?.type || 'info'" effect="light" round>{{ statusMap[row.status]?.label || `状态 ${row.status ?? '--'}` }}</el-tag></template>
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
              <span class="muted-copy">当前筛选显示 {{ filteredProducts.length }} 件</span>
            </div>
          </section>
        </template>

        <template v-if="activeView === 'create'">
          <div class="page-heading">
            <div><h1>发布新商品</h1><p>完成信息录入后先创建草稿，再从商品列表提交上架审核</p></div>
            <el-button :icon="ArrowLeft" @click="activeView = 'products'">返回商品列表</el-button>
          </div>
          <ProductForm @created="afterCreated" @cancel="activeView = 'products'" />
        </template>

        <template v-if="activeView === 'settings'">
          <div class="page-heading"><div><h1>接口设置</h1><p>检查前端与微信小店服务的连接状态</p></div></div>
          <section class="settings-panel">
            <div class="connection-status">
              <div :class="['status-icon', { online: serviceOnline }]"><el-icon><Connection /></el-icon></div>
              <div><h3>{{ serviceOnline ? 'FastAPI 服务已连接' : 'FastAPI 服务未连接' }}</h3><p>当前地址：{{ apiDisplay }}</p></div>
              <el-tag :type="serviceOnline ? 'success' : 'danger'" effect="light">{{ serviceOnline ? '正常' : '离线' }}</el-tag>
            </div>
            <el-divider />
            <div class="setting-row"><div><strong>微信接口凭证</strong><span>调用后端测试 AppID 和 AppSecret 是否可以正常换取 access_token</span></div><el-button type="primary" plain :loading="tokenTesting" :disabled="!serviceOnline" @click="testToken">测试凭证</el-button></div>
            <div class="security-note"><el-icon><Warning /></el-icon><div><strong>安全提醒</strong><p>AppSecret 应仅通过服务端环境变量配置，不应出现在前端、代码仓库或接口响应中。</p></div></div>
          </section>
        </template>
      </el-main>
    </el-container>

    <nav class="mobile-nav" aria-label="移动端主导航">
      <button :class="{ active: platform === 'wechat' && activeView === 'products' }" @click="selectPlatform('wechat')">
        <el-icon><Goods /></el-icon><span>微信</span>
      </button>
      <button :class="{ active: platform === 'xhs' && activeView === 'xhs-products' }" @click="selectPlatform('xhs')">
        <el-icon><Notebook /></el-icon><span>小红书</span>
      </button>
      <button :class="{ active: activeView === 'settings' }" @click="activeView = 'settings'">
        <el-icon><Setting /></el-icon><span>设置</span>
      </button>
    </nav>

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
