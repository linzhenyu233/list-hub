<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Delete, Plus } from '@element-plus/icons-vue'
import { xhsApi } from '../xhsApi'
import { currentShop } from '../shopContext'
import MediaGallery from './MediaGallery.vue'
import MediaUploader from './MediaUploader.vue'
import { longestOf, textWidth } from '../skuTableWidth'

const emit = defineEmits(['created', 'cancel', 'updated'])

const props = defineProps({
  itemId: { type: String, default: '' },
  itemData: { type: Object, default: null },
})
const isEdit = computed(() => !!props.itemId)

const formRef = ref()
const saving = ref(false)
const loadingOptions = ref(false)
const categoryLevels = ref([[]])
const selectedCategories = ref([])
const brands = ref([])
const shippingTemplates = ref([])
const logisticsPlans = ref([])
const attrDefs = ref([])
const attrValues = reactive({})
const varDefs = ref([])
const candidates = reactive({})

const form = reactive({
  name: '', subName: '', brandId: '', categoryId: '', shippingTemplateId: '', shippingGrossWeight: 500,
  articleNo: '', description: '', freeReturn: '1', deliveryMode: '0',
  images: [''], imageDescriptions: [''], videoUrl: '', transparentImage: '',
  skuList: [{ erpCode: '', barcode: '', specImage: '', originalPriceYuan: null, priceYuan: null, stock: 0, logisticsPlanId: '', deliveryHours: 24 }],
})

const listFrom = (data, keys) => {
  const result = data?.result || data || {}
  for (const key of keys) if (Array.isArray(result[key])) return result[key]
  return Array.isArray(result) ? result : []
}
const optionId = (item) => item.id || item.categoryId || item.brandId || item.templateId || item.planInfoId
const optionName = (item) => item.name || item.categoryName || item.brandName || item.templateName || item.planInfoName || item.planName || item.title || optionId(item)
const isLeaf = (item) => item.isLeaf === true || item.leaf === true || item.isLeaf === 1
const isSystemPlan = (item) => optionName(item).includes('系统创建')
const isVirtualSystemPlan = (item) => isSystemPlan(item) && /虚拟|电子资料|自动发货/.test(optionName(item))
const logisticsPlanLabel = (item) => `${isSystemPlan(item) ? '系统创建' : '商家创建'} · ${optionName(item)}`

// 品牌下拉选项：当前品牌常常**不在平台品牌库里**（店铺已授权/自有品牌不参与品牌库检索，
// 实测 brand_id=241795 翻遍该类目 320 个品牌都找不到），那样 el-select 匹配不到选项，
// 就会把裸 ID「241795」直接显示给运营。这里用「店铺配置里的品牌名」补一条可读的兜底选项。
const brandOptions = computed(() => {
  const list = [...brands.value]
  const id = String(form.brandId || '')
  if (!id || list.some((brand) => String(optionId(brand)) === id)) return list
  const conf = currentShop.value?.xhs || {}
  const label = String(conf.brand_id || '') === id && conf.brand_name
    ? `${conf.brand_name}（店铺默认品牌）`
    : `当前品牌（ID: ${id}，请重新选择）`
  list.unshift({ id, name: label })
  return list
})

async function loadCategories(parentId = null, level = 0) {
  const data = await xhsApi.categories(parentId ? { category_id: parentId } : {})
  categoryLevels.value[level] = listFrom(data, ['categoryV3s', 'categories'])
  categoryLevels.value.splice(level + 1)
}

async function selectCategory(value, level) {
  selectedCategories.value[level] = value
  selectedCategories.value.splice(level + 1)
  const item = categoryLevels.value[level].find((row) => String(optionId(row)) === String(value))
  form.categoryId = isLeaf(item) ? value : ''
  form.brandId = ''
  brands.value = []
  if (isLeaf(item)) {
    try { brands.value = listFrom(await xhsApi.brands(value), ['brands', 'brandList', 'list']) } catch (error) { ElMessage.warning(`品牌加载失败：${error.message}`) }
    try {
      const [attrData, varData] = await Promise.all([xhsApi.categoryAttributes(value), xhsApi.categoryVariations(value)])
      attrDefs.value = listFrom(attrData, ['attributeV3s', 'attributes'])
      varDefs.value = listFrom(varData, ['variations'])
      Object.keys(attrValues).forEach((k) => delete attrValues[k])
      attrDefs.value.forEach((a) => { attrValues[a.id] = a.isMulti ? [] : '' })
      // 默认只放平台标记必填的规格维度（最多 2 个），其余由运营点「添加规格」自己挑
      activeSpecIds.value = varDefs.value
        .filter((d) => d.isRequired)
        .slice(0, MAX_SPEC_DIMS)
        .map((d) => d.id)
      await Promise.all([...attrDefs.value, ...varDefs.value].map((a) => loadCandidatesFor(a.id)))
    } catch (error) { ElMessage.warning(`属性/规格加载失败：${error.message}`) }
  } else {
    try { await loadCategories(value, level + 1) } catch (error) { ElMessage.error(error.message) }
  }
}

async function loadOptions() {
  loadingOptions.value = true
  try {
    const [shipping, logistics] = await Promise.all([xhsApi.shippingTemplates(), xhsApi.logisticsPlans()])
    shippingTemplates.value = listFrom(shipping, ['templates', 'carriageTemplateList', 'list'])
      .filter((item) => !optionName(item).includes('测试'))
    logisticsPlans.value = listFrom(logistics, ['logisticsPlans', 'logisticsList', 'plans', 'list'])
      .filter((item) => item.isValid !== false)
    await loadCategories()
  } catch (error) {
    ElMessage.error(error.message)
  } finally { loadingOptions.value = false }
}

function addSku() { form.skuList.push({ erpCode: '', barcode: '', specImage: '', originalPriceYuan: null, priceYuan: null, stock: 0, logisticsPlanId: '', deliveryHours: 24 }) }

async function loadCandidatesFor(attrOrVarId) {
  try {
    const data = await xhsApi.attributeValues(form.categoryId, attrOrVarId)
    candidates[attrOrVarId] = listFrom(data, ['attributeValueV3s', 'values'])
  } catch (e) { candidates[attrOrVarId] = [] }
}

// ============ 规格维度：自己添加，最多 2 个 ============
// 小红书只支持 2 个规格维度，类目却会返回十几个（颜色分类/尺寸/款式/长度/规格/钻石净度/圈口…），
// 全部铺出来既乱又没用，所以改成从类目给的里面挑，最多挑 2 个。
const MAX_SPEC_DIMS = 2
const activeSpecIds = ref([])

const activeVarDefs = computed(() => activeSpecIds.value
  .map((id) => varDefs.value.find((d) => d.id === id))
  .filter(Boolean))

const availableVarDefs = computed(() => varDefs.value.filter((d) => !activeSpecIds.value.includes(d.id)))

function addSpecDim() {
  if (activeSpecIds.value.length >= MAX_SPEC_DIMS) {
    ElMessage.warning(`小红书最多 ${MAX_SPEC_DIMS} 个规格维度`)
    return
  }
  const next = availableVarDefs.value[0]
  if (!next) return
  if (!Array.isArray(attrValues[next.id])) attrValues[next.id] = []
  activeSpecIds.value.push(next.id)
}

function removeSpecDim(id) {
  activeSpecIds.value = activeSpecIds.value.filter((x) => x !== id)
  delete attrValues[id]
  generateSkusFromVariations()
}

function generateSkusFromVariations() {
  const dims = activeVarDefs.value.filter((d) => (Array.isArray(attrValues[d.id]) ? attrValues[d.id].length > 0 : false))
  if (!dims.length) return
  const cands = candidates
  let combos = [[]]
  for (const dim of dims) {
    const selectedIds = attrValues[dim.id] || []
    combos = combos.flatMap((c) => selectedIds.map((vid) => {
      // 下拉里选的是候选值的 valueId；手输的是文本。按 valueId 或中文名都匹配一次，
      // 这样"打字输入了候选里已有的颜色名"也会复用平台的官方 valueId，不会变成两条不同规格。
      const text = String(vid ?? '').trim()
      const hit = (cands[dim.id] || []).find((v) => v.valueId === vid || v.valueName === text)
      // 平台候选里没有的值：value 就是输入文本、不带 valueId
      // （批量发布那边找不到候选时也是这么发的，平台允许只给 value）。
      // ⚠️ 平台要的字段名是 value，valueName 只留着本地显示/兜底。
      const finalText = hit?.valueName || text
      return [...c, {
        id: dim.id,
        name: dim.name,
        valueId: hit?.valueId || '',
        value: finalText,
        valueName: finalText,
      }]
    }))
  }
  // 按规格组合匹配旧行（不是按下标）：改/删某个规格值时，剩下的行仍能对上自己那份价格与库存
  const existing = new Map()
  for (const sku of form.skuList) existing.set(specComboKey(sku._variants), sku)
  form.skuList = combos.map((variants, i) => {
    const previous = existing.get(specComboKey(variants))
    return {
      erpCode: previous?.erpCode || `SKU-${String(i + 1).padStart(3, '0')}`,
      barcode: previous?.barcode || '',
      specImage: previous?.specImage || '',
      originalPriceYuan: previous?.originalPriceYuan ?? null,
      priceYuan: previous?.priceYuan ?? null,
      stock: previous?.stock || 0,
      logisticsPlanId: previous?.logisticsPlanId || '',
      deliveryHours: previous?.deliveryHours || 24,
      _variants: variants,
    }
  })
}

// 规格组合指纹：把"规格名=值"排序拼接，用来把已填的价格/库存对应回同一组合
function specComboKey(variants) {
  return (variants || [])
    .map((v) => `${v.id || v.name}=${v.value || v.valueName || ''}`)
    .sort()
    .join('|')
}

function onSpecSelectionChanged() { generateSkusFromVariations() }

// SKU 表格：已添加且有值的规格维度各占一列（像小红书后台那样，一眼看出每行是哪个规格）
const activeSpecDims = computed(() => {
  const dims = activeVarDefs.value
    .filter((d) => Array.isArray(attrValues[d.id]) && attrValues[d.id].length > 0)
    .map((d) => ({ id: d.id, name: d.name }))
  // 兜底：类目规格没拉回来（或与商品对不上）时，用 SKU 自带的规格维度补齐，
  // 否则编辑老商品时左边一列规格都没有，只能靠编码/图片猜这是哪个颜色。
  for (const sku of form.skuList) {
    for (const variant of (sku._variants || [])) {
      if (!variant.name) continue
      if (!dims.some((d) => d.id === variant.id || d.name === variant.name)) {
        dims.push({ id: variant.id || variant.name, name: variant.name })
      }
    }
  }
  return dims
})

// 编辑已有商品时把 SKU 规格「还原」到界面上：
// 平台只在每个 SKU 的 variants 里存规格值，不回填的话「销售规格」是空的、
// 保存时 variantIds 也会变空（等于把商品的规格丢掉）。
function restoreSpecSelection() {
  for (const sku of form.skuList) {
    sku._variants = (sku._variants || []).map((variant) => {
      const dim = varDefs.value.find((d) => d.id === variant.id || d.name === variant.name)
      const value = variant.value || variant.valueName || ''
      const cands = (dim && candidates[dim.id]) || []
      // 平台可能只回 value（中文名）不给 valueId，用候选值按中文名反查补上
      const valueId = variant.valueId || cands.find((c) => c.valueName === value)?.valueId || ''
      return { id: variant.id, name: variant.name, value, valueName: value, valueId }
    })
  }
  // 还原因商品实际用到的规格维度（只放这些，不要把类目返回的十几个规格全摆出来）
  const usedNames = {}
  const usedIds = []
  for (const sku of form.skuList) {
    for (const variant of (sku._variants || [])) {
      const id = variant.id || variant.name
      if (!id) continue
      if (!usedNames[id]) usedNames[id] = variant.name || String(id)
      if (!usedIds.includes(id)) usedIds.push(id)
    }
  }
  for (const id of usedIds) {
    // 老商品可能有平台规格定义里已经没有的维度，补一条定义，保证页面上仍能看到
    if (!varDefs.value.some((d) => d.id === id)) {
      varDefs.value.push({ id, name: usedNames[id] })
    }
    const picked = []
    for (const sku of form.skuList) {
      for (const variant of (sku._variants || [])) {
        if ((variant.id || variant.name) !== id) continue
        // 有 valueId 用 valueId（下拉里的候选值）；没有就是自定义值，用文本本身回填
        const key = variant.valueId || variant.value
        if (key && !picked.includes(key)) picked.push(key)
      }
    }
    attrValues[id] = picked
  }
  activeSpecIds.value = usedIds.slice(0, MAX_SPEC_DIMS)
}

function specValueOf(row, dim) {
  const variant = (row._variants || []).find((item) => item.id === dim.id || item.name === dim.name)
  // 平台回填的规格值字段名不统一，valueName / value / valueId 三选一
  const value = variant ? (variant.valueName || variant.value || '') : ''
  return value || '—'
}

function removeSku(index) {
  if (form.skuList.length <= 1) return
  form.skuList.splice(index, 1)
}

// ============ SKU 表列宽：按字段长度自由调节（见 skuTableWidth.js） ============
const skuColumns = computed(() => {
  const columns = activeSpecDims.value.map((dim) => ({
    key: `spec:${dim.id}`,
    label: dim.name,
    type: 'spec',
    dim,
    width: textWidth(longestOf(form.skuList.map((sku) => specValueOf(sku, dim)), dim.name), 96),
  }))
  columns.push(
    { key: 'image', label: '规格图', type: 'image', width: 76 },
    { key: 'code', label: '商家 SKU 编码', type: 'code', width: textWidth(longestOf(form.skuList.map((sku) => sku.erpCode), 'SKU-001'), 120) },
    { key: 'barcode', label: '商品条码', type: 'barcode', width: textWidth(longestOf(form.skuList.map((sku) => sku.barcode), '可选'), 110) },
    { key: 'original', label: '原价（元）', type: 'original', width: 130 },
    { key: 'price', label: '售价（元）', type: 'price', width: 130 },
    { key: 'stock', label: '库存', type: 'stock', width: 110 },
    { key: 'delivery', label: '发货时效（小时）', type: 'delivery', width: 130 },
    { key: 'logistics', label: '物流方案', type: 'logistics', width: 220 },
    { key: 'actions', label: '操作', type: 'actions', width: 60 },
  )
  return columns
})

const skuTableWidth = computed(() => skuColumns.value.reduce((sum, column) => sum + column.width, 0))

// 素材：本地图片 → 小红书素材库。
// 旧流程要运营先在别处拿到公网图片地址、再点「上传」；现在直接调 /materials/upload-file，
// 由后端把本地图片传给小红书素材接口拿回素材 URL（后端会自动放大到 ≥1200）。
// 选图/换图/删除的交互都在 MediaGallery 组件里，这里只负责"把一张图传上去、返回地址"。
async function uploadXhsImage({ filename, contentBase64 }) {
  const data = await xhsApi.uploadMaterialFile(filename, contentBase64)
  const result = data?.result || {}
  return typeof result === 'string' ? result : (result.url || result.materialUrl || result.fileUrl || '')
}

async function submit() {
  try {
    await formRef.value.validate()
    if (!form.categoryId) return ElMessage.warning('请选择末级叶子类目')
    if (!form.images.some(Boolean)) return ElMessage.warning('请至少上传一张主图')
    if (form.skuList.some((sku) => !sku.erpCode || !sku.priceYuan || !sku.logisticsPlanId)) return ElMessage.warning('请完整填写 SKU 编码、价格和物流方案')
    saving.value = true
    const attributes = []
    for (const a of attrDefs.value) {
      const v = attrValues[a.id]
      if (a.isMulti && Array.isArray(v)) {
        for (const vid of v) {
          const val = (candidates[a.id] || []).find((c) => c.valueId === vid)
          attributes.push({ propertyId: a.id, name: a.name, valueId: vid, value: val?.valueName || '' })
        }
      } else if (v) {
        const val = (candidates[a.id] || []).find((c) => c.valueId === v)
        attributes.push({ propertyId: a.id, name: a.name, valueId: v, value: val?.valueName || '' })
      }
    }
    const variantIds = varDefs.value.filter((d) => (attrValues[d.id] || []).length > 0).map((d) => d.id)
    const item = {
      name: form.name, brandId: form.brandId, categoryId: form.categoryId, attributes,
      shippingTemplateId: form.shippingTemplateId, shippingGrossWeight: Number(form.shippingGrossWeight),
      variantIds, images: form.images.filter(Boolean), videoUrl: form.videoUrl || '', articleNo: form.articleNo,
      description: form.description, deliveryMode: form.deliveryMode || '0', freeReturn: form.freeReturn,
    }
    if (form.subName) item.subName = form.subName
    if (form.transparentImage) item.transparentImage = form.transparentImage
    const details = form.imageDescriptions.filter(Boolean)
    if (details.length) item.imageDescriptions = details

    if (isEdit.value) {
      // 编辑模式：更新商品信息
      const updatedFields = ['name', 'images', 'attributes', 'description', 'shippingTemplateId', 'shippingGrossWeight', 'freeReturn']
      if (form.articleNo) updatedFields.push('articleNo')
      await xhsApi.updateItem(props.itemId, { item, updated_fields: updatedFields })
      // 更新 SKU
      for (const sku of form.skuList) {
        if (sku._skuId) {
          await xhsApi.updateSku(sku._skuId, {
            sku: { price: Math.round(Number(sku.priceYuan) * 100), originalPrice: Math.round(Number(sku.originalPriceYuan || sku.priceYuan) * 100), stock: Number(sku.stock), logisticsPlanId: sku.logisticsPlanId },
            updated_fields: ['price', 'originalPrice', 'stock', 'logisticsPlanId'],
          })
        }
      }
      ElMessage.success('小红书商品已更新')
      emit('updated', item)
    } else {
      const sku_list = form.skuList.map((sku) => {
        const s = {
          ipq: 1, originalPrice: Math.round(Number(sku.originalPriceYuan || sku.priceYuan) * 100),
          price: Math.round(Number(sku.priceYuan) * 100), stock: Number(sku.stock),
          logisticsPlanId: sku.logisticsPlanId, erpCode: sku.erpCode, variants: sku._variants || [],
          deliveryTime: { time: String(sku.deliveryHours), type: 'RELATIVE_TIME_NEW' },
        }
        if (sku.barcode) s.barcode = sku.barcode
        if (sku.specImage) s.specImage = sku.specImage
        return s
      })
      const data = await xhsApi.createItemAndSku({ item, sku_list })
      const result = data.result || {}
      const skuErrors = result.skuErrors || []
      if (!data.ok || skuErrors.length) {
        const failedSkus = skuErrors.map((entry) => `${entry.sku || '未知 SKU'}：${entry.error}`).join('\n')
        await ElMessageBox.alert(
          `商品已经创建，请勿重复提交。\nSKU 成功 ${result.skuCount || 0}/${result.skuRequestedCount || sku_list.length} 个。\n\n失败原因：\n${failedSkus || '平台未返回具体原因'}`,
          data.partial ? '部分 SKU 创建失败' : 'SKU 创建失败',
          { type: 'warning', confirmButtonText: '返回商品列表' },
        )
        emit('created', result)
        return
      }
      ElMessage.success(`小红书商品及 ${result.skuCount || sku_list.length} 个 SKU 创建成功，已进入审核`)
      emit('created', result)
    }
  } catch (error) { if (error?.message) ElMessage.error(error.message) } finally { saving.value = false }
}

onMounted(async () => {
  await loadOptions()
  if (props.itemData) {
    // product.getItemInfo returns itemInfo/skuInfos at the top level; normalize it
    // before populating the same fields used by the create form.
    const raw = props.itemData
    const nested = raw.itemInfo || raw.data?.itemInfo || raw.product || raw.data?.product || raw.data || {}
    // Some gateway responses also include null placeholders at the top level.
    // Do not let those placeholders overwrite the real values in itemInfo.
    const d = { ...nested, ...raw }
    for (const key of Object.keys(nested)) {
      if (d[key] == null || d[key] === '') d[key] = nested[key]
    }
    if (!d.skus?.length && Array.isArray(raw.skuInfos)) d.skus = raw.skuInfos
    if (!d.images?.length && Array.isArray(raw.itemInfo?.images)) d.images = raw.itemInfo.images
    if (!d.imageDescriptions?.length && Array.isArray(raw.itemInfo?.imageDescriptions)) d.imageDescriptions = raw.itemInfo.imageDescriptions
    if (!d.attributes?.length && Array.isArray(raw.itemInfo?.attributes)) d.attributes = raw.itemInfo.attributes
    form.name = d.name || ''
    form.subName = d.subName || ''
    form.articleNo = d.articleNo || ''
    form.description = d.description || ''
    // ⚠️ 平台回填的数字型字段必须统一转成字符串：下拉选项的 value 都是字符串，
    //    数字与字符串不相等会让 el-select 匹配不到选项、直接把原始数字显示出来
    //    （运营看到的就是品牌「241795」、七天无理由「1」这种看不懂的值）。
    form.brandId = d.brandId == null || d.brandId === '' ? '' : String(d.brandId)
    form.categoryId = d.categoryId == null || d.categoryId === '' ? '' : String(d.categoryId)
    form.shippingTemplateId = d.shippingTemplateId == null || d.shippingTemplateId === '' ? '' : String(d.shippingTemplateId)
    form.shippingGrossWeight = d.shippingGrossWeight || 500
    form.freeReturn = d.freeReturn == null ? '1' : String(d.freeReturn)
    form.deliveryMode = d.deliveryMode == null ? '0' : String(d.deliveryMode)
    form.videoUrl = d.videoUrl || ''
    form.transparentImage = d.transparentImage || ''
    form.images = d.images?.length ? [...d.images] : ['']
    form.imageDescriptions = d.imageDescriptions?.length ? [...d.imageDescriptions] : ['']
    // SKU 回填
    const skus = d.skus || d.skuInfos || []
    if (skus.length) {
      form.skuList = skus.map((sku) => ({
        _skuId: sku.skuId || sku.id || '',
        erpCode: sku.erpCode || '',
        barcode: sku.barcode || '',
        specImage: sku.specImage || '',
        originalPriceYuan: sku.originalPrice != null ? sku.originalPrice / 100 : null,
        priceYuan: sku.price != null ? sku.price / 100 : null,
        stock: sku.stock || 0,
        logisticsPlanId: sku.logisticsPlanId == null ? '' : String(sku.logisticsPlanId),
        deliveryHours: sku.deliveryTime?.time ? Number(sku.deliveryTime.time) : 24,
        _variants: sku.variants || [],
      }))
    }
    // 加载类目属性和规格
    if (form.categoryId) {
      try {
        // 只取第一页：平台按相关性排序，店铺在用的品牌通常就在前面。
        // 万一不在（店铺自有品牌不参与品牌库检索），下面 brandOptions 会用店铺配置兜底显示可读名称。
        const [brandData, attrData, varData] = await Promise.all([
          xhsApi.brands(form.categoryId),
          xhsApi.categoryAttributes(form.categoryId),
          xhsApi.categoryVariations(form.categoryId),
        ])
        brands.value = listFrom(brandData, ['brands', 'brandList', 'list'])
        attrDefs.value = listFrom(attrData, ['attributeV3s', 'attributes'])
        varDefs.value = listFrom(varData, ['variations'])
        attrDefs.value.forEach((a) => { attrValues[a.id] = a.isMulti ? [] : '' })
        await Promise.all([...attrDefs.value, ...varDefs.value].map((a) => loadCandidatesFor(a.id)))
        // 回填属性值
        if (d.attributes?.length) {
          for (const attr of d.attributes) {
            const def = attrDefs.value.find((a) => a.id === attr.propertyId)
            if (def?.isMulti) {
              if (!Array.isArray(attrValues[def.id])) attrValues[def.id] = []
              attrValues[def.id].push(attr.valueId)
            } else if (def) {
              attrValues[def.id] = attr.valueId || attr.value || ''
            }
          }
        }
        // 回填 SKU 规格（商品详情只给了每个 SKU 的 variants，需要按维度聚合回来）
        restoreSpecSelection()
      } catch { /* ignore */ }
    }
  }
})
</script>

<template>
  <el-form ref="formRef" :model="form" label-position="top" class="product-form">
    <section class="form-section">
      <div class="section-heading"><div><h3>小红书商品信息</h3><p>字段只用于小红书，不会写入微信平台</p></div><span class="section-index">01</span></div>
      <div class="form-grid form-grid-2">
        <el-form-item label="商品名称" prop="name" :rules="[{ required: true, message: '请输入商品名称' }]"><el-input v-model="form.name" placeholder="请输入商品名称" /></el-form-item>
        <el-form-item label="短标题/副标题"><el-input v-model="form.subName" maxlength="20" show-word-limit placeholder="可选，展示在商品名下方" /></el-form-item>
        <el-form-item label="商品货号" prop="articleNo" :rules="[{ required: true, message: '请输入商品货号' }]"><el-input v-model="form.articleNo" /></el-form-item>
      </div>
      <el-form-item label="末级类目" required><div class="category-levels"><el-select v-for="(options, level) in categoryLevels" :key="level" :model-value="selectedCategories[level]" :loading="loadingOptions" placeholder="请选择类目" @change="selectCategory($event, level)"><el-option v-for="item in options" :key="optionId(item)" :label="optionName(item)" :value="optionId(item)" /></el-select></div></el-form-item>
      <div class="form-grid form-grid-2">
        <el-form-item label="品牌" prop="brandId" :rules="[{ required: true, message: '请选择品牌' }]"><el-select v-model="form.brandId" :loading="loadingOptions" filterable placeholder="选择末级类目后加载"><el-option v-for="item in brandOptions" :key="optionId(item)" :label="optionName(item)" :value="optionId(item)" /></el-select></el-form-item>
        <el-form-item label="运费模板" prop="shippingTemplateId" :rules="[{ required: true, message: '请选择运费模板' }]"><el-select v-model="form.shippingTemplateId" :loading="loadingOptions"><el-option v-for="item in shippingTemplates" :key="optionId(item)" :label="optionName(item)" :value="optionId(item)" /></el-select></el-form-item>
        <el-form-item label="商品毛重（克）"><el-input-number v-model="form.shippingGrossWeight" :min="1" /></el-form-item>
        <el-form-item label="七天无理由退货"><el-select v-model="form.freeReturn"><el-option label="支持" value="1" /><el-option label="不支持" value="0" /></el-select></el-form-item>
        <el-form-item label="配送方式"><el-select v-model="form.deliveryMode"><el-option label="普通物流" value="0" /><el-option label="同城配送" value="1" /></el-select></el-form-item>
      </div>
      <el-form-item label="商品描述"><el-input v-model="form.description" type="textarea" :rows="3" /></el-form-item>
    </section>

    <section v-if="attrDefs.length" class="form-section">
      <div class="section-heading"><div><h3>商品属性</h3><p>根据末级类目自动加载，珠宝等类目必填</p></div><span class="section-index">02</span></div>
      <div class="form-grid form-grid-2">
        <el-form-item v-for="a in attrDefs" :key="a.id" :label="a.name + (a.isRequired ? '（必填）' : '')" :required="a.isRequired">
          <el-select v-if="a.isMulti" v-model="attrValues[a.id]" multiple filterable :placeholder="'请选择' + a.name">
            <el-option v-for="v in (candidates[a.id] || [])" :key="v.valueId" :label="v.valueName" :value="v.valueId" />
          </el-select>
          <el-select v-else-if="(candidates[a.id] || []).length > 0" v-model="attrValues[a.id]" clearable filterable :placeholder="'请选择' + a.name">
            <el-option v-for="v in (candidates[a.id] || [])" :key="v.valueId" :label="v.valueName" :value="v.valueId" />
          </el-select>
          <el-input v-else v-model="attrValues[a.id]" :placeholder="'请输入' + a.name" />
        </el-form-item>
      </div>
    </section>

    <section v-if="varDefs.length" class="form-section">
      <div class="section-heading"><div><h3>销售规格</h3><p>小红书最多 2 个规格维度；从类目给的规格里挑要用的，平台候选没有的值可直接打字回车新增</p></div><span class="section-index">03</span></div>
      <div class="spec-rows">
        <div v-for="dim in activeVarDefs" :key="dim.id" class="spec-dimension">
          <div class="spec-dim-head">
            <span class="spec-dim-title">{{ dim.name }}<span v-if="dim.isRequired">（必填）</span></span>
            <el-button text type="danger" :icon="Delete" title="删除该规格" @click="removeSpecDim(dim.id)" />
          </div>
          <el-select
            v-model="attrValues[dim.id]"
            multiple
            filterable
            allow-create
            default-first-option
            :placeholder="`选择或直接输入${dim.name}值（输入后回车）`"
            @change="onSpecSelectionChanged"
          >
            <el-option v-for="v in (candidates[dim.id] || [])" :key="v.valueId" :label="v.valueName" :value="v.valueId" />
          </el-select>
        </div>
        <div class="spec-rows-footer">
          <el-tooltip :disabled="activeSpecIds.length < MAX_SPEC_DIMS" content="小红书最多 2 个规格维度" placement="top">
            <span class="spec-add-wrap">
              <el-button text type="primary" :icon="Plus" :disabled="activeSpecIds.length >= MAX_SPEC_DIMS || !availableVarDefs.length" @click="addSpecDim">
                {{ availableVarDefs.length ? `添加规格（${availableVarDefs[0].name}）` : '规格已全部添加' }}
              </el-button>
            </span>
          </el-tooltip>
          <span class="muted-copy">
            <template v-if="activeSpecIds.length">已添加 {{ activeSpecIds.length }}/{{ MAX_SPEC_DIMS }} 个规格；不填值会生成单个默认 SKU</template>
            <template v-else>不添加规格将生成单个默认 SKU</template>
          </span>
        </div>
      </div>
    </section>

    <section class="form-section">
      <div class="section-heading"><div><h3>小红书素材</h3><p>选择本地图片，直接上传到小红书素材库（不用再找公网地址）</p></div><span class="section-index">04</span></div>
      <div class="field-label">商品主图 <span>至少 1 张 · 第 1 张为首图</span></div>
      <MediaGallery v-model="form.images" :max="20" :upload="uploadXhsImage" />
      <div class="field-label description-label">详情图</div>
      <MediaGallery v-model="form.imageDescriptions" :max="50" :upload="uploadXhsImage" />
      <div class="form-grid form-grid-2" style="margin-top: 16px">
        <el-form-item label="商品视频链接"><el-input v-model="form.videoUrl" placeholder="https://.../video.mp4（可选）" /></el-form-item>
        <el-form-item label="透明图链接"><el-input v-model="form.transparentImage" placeholder="https://.../transparent.png（可选）" /></el-form-item>
      </div>
    </section>

    <section class="form-section">
      <div class="section-heading"><div><h3>小红书 SKU</h3><p>创建后需等待审核，审核通过才可按 SKU 上架</p></div><span class="section-index">05</span></div>
      <el-alert title="方案来源说明" description="“系统创建”表示由小红书平台自动生成，通常仅适用于虚拟商品或自动发货；普通实物商品请选择与店铺仓库、发货地址相匹配的商家物流方案。" type="info" show-icon :closable="false" class="logistics-tip" />
      <div class="sku-table-wrap">
        <el-table
          :key="activeSpecDims.map((d) => d.id).join('-')"
          :data="form.skuList"
          :style="{ width: `${skuTableWidth}px` }"
          size="small"
          border
          empty-text="未生成 SKU"
          class="xhs-sku-table"
        >
          <el-table-column v-for="col in skuColumns" :key="col.key" :label="col.label" :width="col.width" :align="col.type === 'image' || col.type === 'actions' ? 'center' : 'left'">
            <template #default="{ row, $index }">
              <span v-if="col.type === 'spec'" class="sku-spec-value" :title="specValueOf(row, col.dim)">{{ specValueOf(row, col.dim) }}</span>
              <MediaUploader v-else-if="col.type === 'image'" v-model="row.specImage" :upload="uploadXhsImage" placeholder="选图" compact />
              <el-input v-else-if="col.type === 'code'" v-model="row.erpCode" placeholder="必填" />
              <el-input v-else-if="col.type === 'barcode'" v-model="row.barcode" placeholder="可选" />
              <el-input-number v-else-if="col.type === 'original'" v-model="row.originalPriceYuan" :min="0.01" :precision="2" :controls="false" style="width: 100%" />
              <el-input-number v-else-if="col.type === 'price'" v-model="row.priceYuan" :min="0.01" :precision="2" :controls="false" style="width: 100%" />
              <el-input-number v-else-if="col.type === 'stock'" v-model="row.stock" :min="0" style="width: 100%" />
              <el-input-number v-else-if="col.type === 'delivery'" v-model="row.deliveryHours" :min="1" style="width: 100%" />
              <el-select v-else-if="col.type === 'logistics'" v-model="row.logisticsPlanId" :loading="loadingOptions" filterable placeholder="请选择普通实物物流方案" style="width: 100%">
                <el-option v-for="item in logisticsPlans" :key="optionId(item)" :label="logisticsPlanLabel(item)" :value="optionId(item)" :disabled="isVirtualSystemPlan(item)">
                  <div class="logistics-option"><span>{{ optionName(item) }}</span><el-tag size="small" :type="isSystemPlan(item) ? 'info' : 'primary'">{{ isSystemPlan(item) ? '系统创建' : '商家创建' }}</el-tag></div>
                </el-option>
              </el-select>
              <el-button v-else-if="col.type === 'actions'" text type="danger" :icon="Delete" :disabled="form.skuList.length === 1" @click="removeSku($index)" />
            </template>
          </el-table-column>
        </el-table>
      </div>
      <div class="sku-table-footer">
        <el-button text type="primary" :icon="Plus" @click="addSku">添加 SKU</el-button>
        <span class="muted-copy">共 {{ form.skuList.length }} 个 SKU；规格列由上面「销售规格」所选值自动生成</span>
      </div>
    </section>
    <div class="form-actions"><el-button @click="emit('cancel')">取消</el-button><el-button type="primary" :loading="saving" :icon="Check" @click="submit">{{ isEdit ? '保存修改' : '创建小红书商品' }}</el-button></div>
  </el-form>
</template>
