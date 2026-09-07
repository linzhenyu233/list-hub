<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  visible: { type: Boolean, default: false },
  product: { type: Object, default: null },
  productMapping: { type: Object, default: () => ({}) },
  groupConfig: { type: Object, default: null },
  platforms: { type: Array, default: () => ['wechat', 'xhs'] },
})
const emit = defineEmits(['update:visible', 'save'])

const showWechat = computed(() => props.platforms.includes('wechat'))
const showXhs = computed(() => props.platforms.includes('xhs'))

const editForm = reactive({
  title: '', wechat_title: '', xhs_title: '', brand: '', description: '',
  weight: '', wechat_attrs: {}, xhs_attrs: {}, skus: [],
})

watch(() => [props.visible, props.product], () => {
  if (!props.visible || !props.product) return
  const p = props.product
  const m = props.productMapping || {}
  editForm.title = p.title || ''
  editForm.wechat_title = m.wechat_title || p.wechat_title || ''
  editForm.xhs_title = m.xhs_title || p.xhs_title || ''
  editForm.brand = p.brand || ''
  editForm.description = p.description || ''
  editForm.weight = p.weight || ''
  editForm.wechat_attrs = { ...(m.wechat_attrs || {}) }
  editForm.xhs_attrs = { ...(m.xhs_attrs || {}) }
  editForm.skus = (p.skus || []).map((sku) => ({ ...sku, specs: (sku.specs || []).map((s) => ({ ...s })) }))
  // 初始化可编辑的规格维度：优先用后端汇总的 spec_dimensions，回退从 SKU 收集
  specDims.value = Array.isArray(p.spec_dimensions) && p.spec_dimensions.length
    ? [...p.spec_dimensions]
    : collectSpecNames(editForm.skus)
  alignSkuSpecs()
}, { immediate: true })

const wxAttrDefs = computed(() => props.groupConfig?.wechat?.attr_defs || [])
const xhsAttrDefs = computed(() => props.groupConfig?.xhs?.attr_defs || [])
const xhsCandidates = computed(() => props.groupConfig?.xhs?.candidates || {})
const productWarnings = computed(() => props.product?.warnings || [])
const skuCount = computed(() => editForm.skus.length)

// 规格维度改为可编辑状态：支持在审核抽屉里自由增删规格列，无需回 Excel 改
const specDims = ref([])
// 商品属性区块默认折叠（同一系列很少所有属性都相同，按需展开）
const activeAttrPanels = ref([])

function collectSpecNames(skus) {
  const names = []
  for (const sku of skus) {
    for (const sp of (sku.specs || [])) {
      if (sp.name && !names.includes(sp.name)) names.push(sp.name)
    }
  }
  return names
}

// 把每个 SKU 的 specs 对齐到当前维度（按下标补齐/裁剪，保留已填的值）
function alignSkuSpecs() {
  for (const sku of editForm.skus) {
    const old = Array.isArray(sku.specs) ? sku.specs : []
    sku.specs = specDims.value.map((name, i) => ({ name, value: old[i]?.value ?? '' }))
  }
}

const specDimensionHint = computed(() => {
  const named = specDims.value.filter((d) => d && d.trim())
  return named.length
    ? `共 ${named.length} 个规格维度：${named.join('、')}`
    : '未设置规格维度，点右上角“添加规格维度”新增'
})

// SKU 行内编辑：价格/库存转数字，规格值保留文本
function updateSkuField(index, field, value) {
  editForm.skus[index][field] = value
}
// 规格值行内编辑：按维度下标写入对应 SKU 的 specs
function updateSkuSpec(index, specIndex, value) {
  const sku = editForm.skus[index]
  if (!Array.isArray(sku.specs)) sku.specs = []
  if (!sku.specs[specIndex]) sku.specs[specIndex] = { name: specDims.value[specIndex] || '', value: '' }
  sku.specs[specIndex].value = value
}
// 重命名规格维度：同步更新所有 SKU 对应列的 name
function renameSpecDim(di, name) {
  specDims.value[di] = name
  for (const sku of editForm.skus) {
    if (sku.specs?.[di]) sku.specs[di].name = name
  }
}
// 新增一个规格维度列（名称待填），每个 SKU 补一个空值
function addSpecDim() {
  specDims.value.push('')
  for (const sku of editForm.skus) {
    if (!Array.isArray(sku.specs)) sku.specs = []
    sku.specs.push({ name: '', value: '' })
  }
}
// 删除某个规格维度列
function removeSpecDim(di) {
  specDims.value.splice(di, 1)
  for (const sku of editForm.skus) {
    if (Array.isArray(sku.specs)) sku.specs.splice(di, 1)
  }
}

// 微信 API 的 attr.value 可能是字符串或逗号/分号分隔字符串，需规范化为数组
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

function getWxAttrValue(name) { return editForm.wechat_attrs[name] ?? '' }
function setWxAttrValue(name, val) { editForm.wechat_attrs[name] = val }

function getXhsAttrValue(id) {
  const attr = editForm.xhs_attrs[id]
  if (!attr) return ''
  return attr.valueId || attr.value || ''
}
function setXhsAttrValue(id, val) {
  const def = xhsAttrDefs.value.find((d) => d.id === id)
  if (!def) return
  if (def.isMulti) {
    // multi-select: val is array of valueIds
    editForm.xhs_attrs[id] = val.map((vid) => {
      const c = (xhsCandidates.value[id] || []).find((cv) => cv.valueId === vid)
      return { propertyId: id, name: def.name, valueId: vid, value: c?.valueName || '' }
    })
  } else {
    const c = (xhsCandidates.value[id] || []).find((cv) => cv.valueId === val)
    editForm.xhs_attrs[id] = { propertyId: id, name: def.name, valueId: val, value: c?.valueName || val }
  }
}
function getXhsMultiValues(id) {
  const attr = editForm.xhs_attrs[id]
  if (Array.isArray(attr)) return attr.map((a) => a.valueId).filter(Boolean)
  if (attr?.valueId) return [attr.valueId]
  return []
}

function handleSave() {
  // 规格维度校验：不允许留空名称
  if (specDims.value.some((d) => !d || !d.trim())) {
    ElMessage.warning('存在未命名的规格维度，请填写名称或删除该列')
    return
  }
  // SKU 行内编辑校验：售价必须 > 0，库存不能为负
  for (const sku of editForm.skus) {
    const price = Number(sku.price)
    const stock = Number(sku.stock)
    if (!Number.isFinite(price) || price <= 0) {
      ElMessage.warning(`SKU ${sku.sku_code || ''} 的售价必须大于 0`)
      return
    }
    if (!Number.isFinite(stock) || stock < 0) {
      ElMessage.warning(`SKU ${sku.sku_code || ''} 的库存不能为负数`)
      return
    }
  }
  emit('save', {
    product_code: props.product.product_code,
    title: editForm.title,
    wechat_title: editForm.wechat_title,
    xhs_title: editForm.xhs_title,
    brand: editForm.brand,
    description: editForm.description,
    weight: editForm.weight,
    wechat_attrs: { ...editForm.wechat_attrs },
    xhs_attrs: { ...editForm.xhs_attrs },
    skus: editForm.skus.map((sku) => ({ ...sku, specs: (sku.specs || []).map((s) => ({ ...s })) })),
    spec_dimensions: specDims.value.map((d) => d.trim()).filter(Boolean),
  })
  ElMessage.success('商品信息已更新')
  emit('update:visible', false)
}
</script>

<template>
  <el-drawer :model-value="visible" title="商品审核 / 编辑" size="720px" @update:model-value="emit('update:visible', $event)">
    <template v-if="product">
      <div class="review-product-header">
        <h3>{{ product.title || '未命名商品' }}</h3>
        <span class="muted-copy">编码：{{ product.product_code }} · 类目：{{ product.internal_category }}</span>
      </div>

      <!-- SKU 间属性不一致告警 -->
      <el-alert
        v-for="(warning, index) in productWarnings"
        :key="index"
        :title="warning"
        type="warning"
        show-icon
        :closable="false"
        class="review-warning"
      />

      <!-- 基础信息 -->
      <section class="review-section">
        <h4>基础信息</h4>
        <div class="form-grid form-grid-2">
          <el-form-item label="通用标题"><el-input v-model="editForm.title" /></el-form-item>
          <el-form-item v-if="showWechat" label="微信标题"><el-input v-model="editForm.wechat_title" placeholder="留空则用通用标题" /></el-form-item>
          <el-form-item v-if="showXhs" label="小红书标题"><el-input v-model="editForm.xhs_title" placeholder="留空则用通用标题" /></el-form-item>
          <el-form-item label="品牌"><el-input v-model="editForm.brand" /></el-form-item>
          <el-form-item label="重量(g)"><el-input v-model="editForm.weight" /></el-form-item>
        </div>
        <el-form-item label="描述"><el-input v-model="editForm.description" type="textarea" :rows="2" /></el-form-item>
      </section>

      <!-- 商品属性（SPU 级，默认折叠，按需展开） -->
      <el-collapse
        v-if="(showWechat && wxAttrDefs.length) || (showXhs && xhsAttrDefs.length)"
        v-model="activeAttrPanels"
        class="review-section attr-collapse"
      >
        <el-collapse-item v-if="showWechat && wxAttrDefs.length" name="wechat">
          <template #title>
            <span class="collapse-title">微信商品属性<small class="muted-copy">（{{ wxAttrDefs.length }} 个属性定义 · SPU 级，应用到全部 {{ skuCount }} 个 SKU）</small></span>
          </template>
          <p class="muted-copy spu-hint">以下为商品级（SPU）属性，保存后将统一应用到全部 {{ skuCount }} 个 SKU；若某个属性各 SKU 实际不同，请在下方 SKU 表里用规格维度区分。</p>
          <div class="form-grid form-grid-2">
            <div v-for="a in wxAttrDefs" :key="a.name" class="attr-config-item">
              <label>{{ a.is_required ? '* ' : '' }}{{ a.name }}<small v-if="a.type_v2" class="muted-copy"> ({{ wxTypeLabel(a.type_v2) }})</small></label>
              <el-select v-if="a.type_v2 === 'select_many'" multiple :model-value="getWxAttrValue(a.name)" placeholder="请选择" @update:model-value="(v) => setWxAttrValue(a.name, v)">
                <el-option v-for="opt in attrOptions(a.value)" :key="opt" :label="opt" :value="opt" />
              </el-select>
              <el-select v-else-if="a.type_v2 === 'select_one'" :model-value="getWxAttrValue(a.name)" clearable placeholder="请选择" @update:model-value="(v) => setWxAttrValue(a.name, v)">
                <el-option v-for="opt in attrOptions(a.value)" :key="opt" :label="opt" :value="opt" />
              </el-select>
              <el-input-number v-else-if="a.type_v2?.includes('integer') || a.type_v2?.includes('decimal')" :model-value="getWxAttrValue(a.name)" @update:model-value="(v) => setWxAttrValue(a.name, v)" />
              <el-input v-else :model-value="getWxAttrValue(a.name)" placeholder="请输入" @update:model-value="(v) => setWxAttrValue(a.name, v)" />
            </div>
          </div>
        </el-collapse-item>
        <el-collapse-item v-if="showXhs && xhsAttrDefs.length" name="xhs">
          <template #title>
            <span class="collapse-title">小红书商品属性<small class="muted-copy">（{{ xhsAttrDefs.length }} 个属性定义 · SPU 级，应用到全部 {{ skuCount }} 个 SKU）</small></span>
          </template>
          <div class="form-grid form-grid-2">
            <div v-for="a in xhsAttrDefs" :key="a.id" class="attr-config-item">
              <label>{{ a.isRequired ? '* ' : '' }}{{ a.name }}<small v-if="a.isMulti" class="muted-copy"> (多选)</small></label>
              <el-select v-if="a.isMulti" multiple :model-value="getXhsMultiValues(a.id)" placeholder="请选择" @update:model-value="(v) => setXhsAttrValue(a.id, v)">
                <el-option v-for="c in (xhsCandidates[a.id] || [])" :key="c.valueId" :label="c.valueName" :value="c.valueId" />
              </el-select>
              <el-select v-else-if="a.inputType === 1 && (xhsCandidates[a.id] || []).length" :model-value="getXhsAttrValue(a.id)" clearable placeholder="请选择" @update:model-value="(v) => setXhsAttrValue(a.id, v)">
                <el-option v-for="c in (xhsCandidates[a.id] || [])" :key="c.valueId" :label="c.valueName" :value="c.valueId" />
              </el-select>
              <el-input v-else :model-value="getXhsAttrValue(a.id)" placeholder="请输入" @update:model-value="(v) => setXhsAttrValue(a.id, v)" />
            </div>
          </div>
        </el-collapse-item>
      </el-collapse>

      <!-- SKU 信息（行内可编辑，规格维度可自由增删） -->
      <section v-if="editForm.skus.length" class="review-section">
        <div class="sku-section-head">
          <h4>SKU 信息（{{ editForm.skus.length }} 个）<small class="muted-copy">　售价/库存/规格值可直接修改，仅影响当前 SKU</small></h4>
          <el-button size="small" @click="addSpecDim">+ 添加规格维度</el-button>
        </div>
        <el-table :data="editForm.skus" size="small" max-height="320" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }">
          <el-table-column prop="sku_code" label="SKU编码" width="130" show-overflow-tooltip />
          <el-table-column v-for="(dim, di) in specDims" :key="di" width="180">
            <template #header>
              <div class="spec-dim-head">
                <el-input :model-value="dim" size="small" placeholder="规格名称" @update:model-value="(v) => renameSpecDim(di, v)" />
                <el-button size="small" text type="danger" @click="removeSpecDim(di)">删除</el-button>
              </div>
            </template>
            <template #default="{ row, $index }">
              <el-input :model-value="row.specs?.[di]?.value" size="small" :placeholder="dim || '规格值'" @update:model-value="(v) => updateSkuSpec($index, di, v)" />
            </template>
          </el-table-column>
          <el-table-column label="售价(元)" width="120">
            <template #default="{ row, $index }">
              <el-input-number :model-value="Number(row.price) || 0" size="small" :min="0.01" :precision="2" :controls="false" class="sku-num" @update:model-value="(v) => updateSkuField($index, 'price', v)" />
            </template>
          </el-table-column>
          <el-table-column label="库存" width="100">
            <template #default="{ row, $index }">
              <el-input-number :model-value="Number(row.stock) || 0" size="small" :min="0" :precision="0" :controls="false" class="sku-num" @update:model-value="(v) => updateSkuField($index, 'stock', v)" />
            </template>
          </el-table-column>
        </el-table>
        <p class="muted-copy sku-dim-hint">规格名称：{{ specDimensionHint }}</p>
      </section>

      <div class="form-actions">
        <el-button @click="emit('update:visible', false)">关闭</el-button>
        <el-button type="primary" @click="handleSave">保存修改</el-button>
      </div>
    </template>
  </el-drawer>
</template>

<style scoped>
.review-product-header { margin-bottom: 20px; }
.review-product-header h3 { margin: 0 0 4px; font-size: 18px; }
.review-section { margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid #edf0f3; }
.review-section:last-of-type { border-bottom: none; }
.review-section h4 { margin: 0 0 12px; font-size: 15px; }
.review-section h4 small { font-weight: 400; }
.review-warning { margin-bottom: 12px; }
.spu-hint { margin: -6px 0 12px; font-size: 12px; line-height: 1.5; }
.sku-dim-hint { margin: 8px 0 0; font-size: 12px; }
.sku-num { width: 100%; }
.sku-section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.sku-section-head h4 { margin: 0; }
.spec-dim-head { display: flex; align-items: center; gap: 4px; }
.attr-collapse { border-top: none; }
.attr-collapse :deep(.el-collapse-item__header) { font-size: 15px; }
.collapse-title small { font-weight: 400; margin-left: 4px; }
</style>
