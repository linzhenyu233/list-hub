<script setup>
import { computed } from 'vue'
import { Picture } from '@element-plus/icons-vue'

const props = defineProps({
  platform: { type: String, required: true },
  data: { type: Object, default: () => ({}) },
})

// 三个平台共用这一套详情版式；差异只在取字段，不另写一套页面（视觉必须对齐）。
const isWechat = computed(() => props.platform === 'wechat')
const isYouzan = computed(() => props.platform === 'youzan')
const product = computed(() => {
  if (isWechat.value) return props.data?.product || props.data || {}
  // 有赞详情返回 {"item": {...}}（真机）；小红书是 itemInfo
  if (isYouzan.value) return props.data?.item || props.data || {}
  return props.data?.itemInfo || props.data?.item || props.data || {}
})
const skus = computed(() => {
  if (isWechat.value) return product.value.skus || []
  if (isYouzan.value) return product.value.skus || []
  return props.data?.skuInfos || product.value.skus || []
})
const title = computed(() => product.value.title || product.value.name || product.value.product_name || '未命名商品')
const mainImages = computed(() => {
  if (isYouzan.value) {
    // 有赞主图：item_imgs 是对象数组（取原图 url），另可能有 pic_url 单图
    const fromImgs = (product.value.item_imgs || []).map((one) => one?.url || one?.medium).filter(Boolean)
    return fromImgs.length ? fromImgs : [product.value.pic_url].filter(Boolean)
  }
  return product.value.head_imgs || product.value.images || []
})
// 有赞没有独立的「详情图」字段：详情图是写进 desc 富文本里的 <img src>
const descHtml = computed(() => (isYouzan.value ? String(product.value.desc || '') : ''))
const detailImages = computed(() => {
  if (isYouzan.value) return [...descHtml.value.matchAll(/<img[^>]+src=["']([^"']+)["']/gi)].map((m) => m[1])
  return product.value.desc_info?.imgs || product.value.imageDescriptions || []
})
const previewImages = computed(() => [...new Set([...mainImages.value, ...detailImages.value].filter(Boolean))])
const attributes = computed(() => product.value.attrs || product.value.attributes || product.value.item_props || [])

const statusInfo = computed(() => {
  if (isWechat.value) {
    if (Number(product.value.status) === 13) return { label: '审核中', type: 'warning' }
    return {
      0: { label: '未上架', type: 'info' },
      5: { label: '销售中', type: 'success' },
      11: { label: '已下架', type: 'warning' },
    }[Number(product.value.status)] || { label: '状态待确认', type: 'info' }
  }
  if (isYouzan.value) {
    // 有赞的上下架标记是商品级 is_listing（真机核对）
    return product.value.is_listing === 1 || product.value.is_listing === true
      ? { label: '出售中', type: 'success' }
      : { label: '仓库中', type: 'warning' }
  }
  if (!skus.value.length) return { label: '暂无规格', type: 'info' }
  return skus.value.some((sku) => sku.buyable === true || sku.buyable === 1 || sku.available === 1)
    ? { label: '销售中', type: 'success' }
    : { label: '未在售', type: 'warning' }
})

const merchantCode = computed(() => {
  if (isWechat.value) return product.value.out_product_id || product.value.spu_code || '--'
  if (isYouzan.value) return product.value.item_no || '--'
  return product.value.articleNo || product.value.article_no || '--'
})

function skuPrice(sku) {
  const value = Number(sku.sale_price ?? sku.price)
  return Number.isFinite(value) ? value / 100 : null
}

const priceText = computed(() => {
  const values = skus.value.map(skuPrice).filter((value) => value !== null)
  if (!values.length) return '--'
  const min = Math.min(...values)
  const max = Math.max(...values)
  return min === max ? `¥${min.toFixed(2)}` : `¥${min.toFixed(2)} - ¥${max.toFixed(2)}`
})

const stockTotal = computed(() => {
  const values = skus.value.map((sku) => Number(sku.stock_num ?? sku.stock)).filter(Number.isFinite)
  return values.length ? values.reduce((sum, value) => sum + value, 0) : '--'
})

const weightText = computed(() => {
  if (isYouzan.value) return '未填写'      // 有赞详情不返回商品重量（要另查物流/规格）
  const value = isWechat.value ? product.value.express_info?.weight : product.value.shippingGrossWeight
  return Number(value) > 0 ? `${value} 克` : '未填写'
})

const deliveryText = computed(() => {
  if (isYouzan.value) return '按店铺运费模板'
  const mode = isWechat.value ? product.value.deliver_method : product.value.deliveryMode
  if (mode === 0 || mode === '0') return '普通快递'
  if (mode === 1 || mode === '1') return '同城配送'
  return '按店铺设置'
})

const returnText = computed(() => {
  if (isYouzan.value) return '按平台设置'
  const value = isWechat.value ? product.value.extra_service?.seven_day_return : product.value.freeReturn
  if (value === 1 || value === '1' || value === true) return '支持七天无理由'
  if (value === 0 || value === '0' || value === false) return '不支持七天无理由'
  return '按平台设置'
})

const description = computed(() => {
  if (isYouzan.value) {
    // 有赞 desc 是富文本：把标签剥掉只留文字，图已单独在「商品详情图」区展示
    return String(product.value.desc || '')
      .replace(/<img[^>]*>/gi, '')
      .replace(/<[^>]+>/g, '')
      .replace(/&nbsp;/g, ' ')
      .trim()
  }
  return product.value.desc_info?.desc || product.value.desc_info?.detail || product.value.description || ''
})

const updatedAt = computed(() => {
  const value = product.value.edit_time || product.value.updateTime
    || product.value.update_time || product.value.created_time     // 有赞给的是 "YYYY-MM-DD HH:MM:SS" 字符串
  if (!value) return '--'
  if (typeof value === 'string' && !/^\d+$/.test(value)) {
    const date = new Date(value.replace(' ', 'T'))
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
  }
  const milliseconds = Number(value) < 100000000000 ? Number(value) * 1000 : Number(value)
  const date = new Date(milliseconds)
  return Number.isNaN(date.getTime()) ? '--' : date.toLocaleString('zh-CN', { hour12: false })
})

function attributeName(attribute) {
  return attribute.attr_key || attribute.name || '商品属性'
}

function attributeValue(attribute) {
  if (attribute.attr_value) return attribute.attr_value
  if (attribute.value) return attribute.value
  return (attribute.valueList || []).map((item) => item.value).filter(Boolean).join('、') || '--'
}

function skuCode(sku) {
  if (isWechat.value) return sku.out_sku_id || sku.sku_code || sku.bar_code || '--'
  if (isYouzan.value) return sku.item_no || '--'          // 有赞 SKU 编码字段是 item_no
  return sku.erpCode || sku.erp_code || sku.barcode || '--'
}

function skuSpecs(sku) {
  if (isYouzan.value) {
    // 有赞规格不是数组，而是每个 SKU 一个 JSON 字符串：[{"k":"尺寸","v":"2克拉",...}]
    try {
      const rows = JSON.parse(sku.properties_name_json || '[]')
      const text = (Array.isArray(rows) ? rows : [])
        .map((one) => (one.k && one.v ? `${one.k}：${one.v}` : one.v || one.k))
        .filter(Boolean).join('，')
      if (text) return text
    } catch { /* 解析失败就退回默认文案 */ }
    return '默认规格'
  }
  const values = isWechat.value ? sku.sku_attrs || [] : sku.variants || []
  return values.map((item) => {
    const name = item.attr_key || item.name
    const value = item.attr_value || item.value
    return name && value ? `${name}：${value}` : value || name
  }).filter(Boolean).join('，') || '默认规格'
}

function skuStatus(sku) {
  if (isWechat.value || isYouzan.value) {
    // 微信/有赞只有商品级上下架状态：SKU 统一跟随商品状态，避免出现"商品下架、规格却在售"的矛盾
    return statusInfo.value
  }
  return sku.buyable === true || sku.buyable === 1 || sku.available === 1
    ? { label: '销售中', type: 'success' }
    : { label: '未在售', type: 'warning' }
}

function formatSkuPrice(sku, field) {
  const value = Number(sku[field])
  return Number.isFinite(value) && value > 0 ? `¥${(value / 100).toFixed(2)}` : '--'
}
</script>

<template>
  <div class="operator-detail">
    <header class="operator-detail__header">
      <el-image :src="mainImages[0]" :preview-src-list="previewImages" fit="cover">
        <template #error><div class="image-fallback"><el-icon><Picture /></el-icon></div></template>
      </el-image>
      <div>
        <h3>{{ title }}</h3>
        <div class="operator-detail__meta">
          <el-tag :type="statusInfo.type" effect="light">{{ statusInfo.label }}</el-tag>
          <span>商品编码：{{ merchantCode }}</span>
        </div>
      </div>
    </header>

    <el-descriptions :column="2" border class="operator-detail__summary">
      <el-descriptions-item label="售价">{{ priceText }}</el-descriptions-item>
      <el-descriptions-item label="总库存">{{ stockTotal }}</el-descriptions-item>
      <el-descriptions-item label="规格数量">{{ skus.length }} 个</el-descriptions-item>
      <el-descriptions-item label="商品重量">{{ weightText }}</el-descriptions-item>
      <el-descriptions-item label="配送方式">{{ deliveryText }}</el-descriptions-item>
      <el-descriptions-item label="售后服务">{{ returnText }}</el-descriptions-item>
      <el-descriptions-item label="更新时间" :span="2">{{ updatedAt }}</el-descriptions-item>
    </el-descriptions>

    <section v-if="attributes.length" class="operator-detail__section">
      <h4>商品属性</h4>
      <dl class="attribute-list">
        <div v-for="(attribute, index) in attributes" :key="index">
          <dt>{{ attributeName(attribute) }}</dt>
          <dd>{{ attributeValue(attribute) }}</dd>
        </div>
      </dl>
    </section>

    <section class="operator-detail__section">
      <h4>规格、价格与库存</h4>
      <el-table :data="skus" empty-text="暂无规格信息" max-height="360" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }">
        <el-table-column label="规格" min-width="230" show-overflow-tooltip><template #default="{ row }">{{ skuSpecs(row) }}</template></el-table-column>
        <el-table-column label="商家编码" min-width="130" show-overflow-tooltip><template #default="{ row }">{{ skuCode(row) }}</template></el-table-column>
        <el-table-column label="售价" width="105"><template #default="{ row }"><strong class="price">{{ formatSkuPrice(row, isWechat ? 'sale_price' : 'price') }}</strong></template></el-table-column>
        <el-table-column label="划线价" width="105"><template #default="{ row }">{{ formatSkuPrice(row, isWechat ? 'market_price' : 'originalPrice') }}</template></el-table-column>
        <el-table-column label="库存" width="80"><template #default="{ row }">{{ row.stock_num ?? row.stock ?? '--' }}</template></el-table-column>
        <el-table-column label="状态" width="125"><template #default="{ row }"><el-tag :type="skuStatus(row).type" size="small">{{ skuStatus(row).label }}</el-tag></template></el-table-column>
      </el-table>
    </section>

    <section v-if="mainImages.length" class="operator-detail__section">
      <h4>商品主图 <span>{{ mainImages.length }} 张</span></h4>
      <div class="detail-image-grid">
        <el-image v-for="(image, index) in mainImages" :key="image" :src="image" :preview-src-list="previewImages" :initial-index="index" fit="cover" lazy />
      </div>
    </section>

    <section v-if="detailImages.length" class="operator-detail__section">
      <h4>商品详情图 <span>{{ detailImages.length }} 张</span></h4>
      <div class="detail-image-grid">
        <el-image v-for="(image, index) in detailImages" :key="`${image}-detail`" :src="image" :preview-src-list="previewImages" :initial-index="mainImages.length + index" fit="cover" lazy />
      </div>
    </section>

    <section v-if="description" class="operator-detail__section">
      <h4>商品描述</h4>
      <p class="product-description">{{ description }}</p>
    </section>
  </div>
</template>
