<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Delete, Plus } from '@element-plus/icons-vue'
import { xhsApi } from '../xhsApi'

const emit = defineEmits(['created', 'cancel', 'updated'])

const props = defineProps({
  itemId: { type: String, default: '' },
  itemData: { type: Object, default: null },
})
const isEdit = computed(() => !!props.itemId)

const formRef = ref()
const saving = ref(false)
const loadingOptions = ref(false)
const uploading = ref('')
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

function generateSkusFromVariations() {
  const dims = varDefs.value.filter((d) => (Array.isArray(attrValues[d.id]) ? attrValues[d.id].length > 0 : false))
  if (!dims.length) return
  const cands = candidates
  let combos = [[]]
  for (const dim of dims) {
    const selectedIds = attrValues[dim.id] || []
    combos = combos.flatMap((c) => selectedIds.map((vid) => {
      const val = (cands[dim.id] || []).find((v) => v.valueId === vid)
      return [...c, { id: dim.id, name: dim.name, value: val?.valueName || '', valueId: vid }]
    }))
  }
  form.skuList = combos.map((variants, i) => ({
    erpCode: form.skuList[i]?.erpCode || `SKU-${String(i + 1).padStart(3, '0')}`,
    barcode: form.skuList[i]?.barcode || '',
    specImage: form.skuList[i]?.specImage || '',
    originalPriceYuan: form.skuList[i]?.originalPriceYuan || null,
    priceYuan: form.skuList[i]?.priceYuan || null,
    stock: form.skuList[i]?.stock || 0,
    logisticsPlanId: form.skuList[i]?.logisticsPlanId || '',
    deliveryHours: form.skuList[i]?.deliveryHours || 24,
    _variants: variants,
  }))
}

function onSpecSelectionChanged() { generateSkusFromVariations() }

async function uploadImage(list, index, key) {
  if (!list[index]) return ElMessage.warning('请先填写图片公网地址')
  uploading.value = `${key}-${index}`
  try {
    const data = await xhsApi.uploadMaterial(list[index])
    const result = data.result || {}
    list[index] = result.url || result.materialUrl || result.fileUrl || result
    ElMessage.success('素材已上传到小红书')
  } catch (error) { ElMessage.error(error.message) } finally { uploading.value = '' }
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
    form.brandId = d.brandId || ''
    form.categoryId = d.categoryId || ''
    form.shippingTemplateId = d.shippingTemplateId || ''
    form.shippingGrossWeight = d.shippingGrossWeight || 500
    form.freeReturn = d.freeReturn ?? '1'
    form.deliveryMode = d.deliveryMode || '0'
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
        logisticsPlanId: sku.logisticsPlanId || '',
        deliveryHours: sku.deliveryTime?.time ? Number(sku.deliveryTime.time) : 24,
        _variants: sku.variants || [],
      }))
    }
    // 加载类目属性和规格
    if (form.categoryId) {
      try {
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
        <el-form-item label="品牌" prop="brandId" :rules="[{ required: true, message: '请选择品牌' }]"><el-select v-model="form.brandId" :loading="loadingOptions" filterable placeholder="选择末级类目后加载"><el-option v-for="item in brands" :key="optionId(item)" :label="optionName(item)" :value="optionId(item)" /></el-select></el-form-item>
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
      <div class="section-heading"><div><h3>销售规格</h3><p>选择规格值后自动生成 SKU 组合</p></div><span class="section-index">03</span></div>
      <div v-for="dim in varDefs" :key="dim.id" class="spec-dimension">
        <div class="field-label">{{ dim.name }}</div>
        <el-select v-model="attrValues[dim.id]" multiple filterable :placeholder="'选择' + dim.name + '值'" @change="onSpecSelectionChanged">
          <el-option v-for="v in (candidates[dim.id] || [])" :key="v.valueId" :label="v.valueName" :value="v.valueId" />
        </el-select>
      </div>
    </section>

    <section class="form-section">
      <div class="section-heading"><div><h3>小红书素材</h3><p>图片通过小红书素材接口独立上传</p></div><span class="section-index">04</span></div>
      <div class="field-label">商品主图</div><div class="url-list"><div v-for="(_, index) in form.images" :key="index" class="url-row"><span class="url-number">{{ index + 1 }}</span><el-input v-model="form.images[index]" placeholder="图片公网 URL" /><el-button :loading="uploading === `main-${index}`" @click="uploadImage(form.images, index, 'main')">上传</el-button><el-button text type="danger" :icon="Delete" :disabled="form.images.length === 1" @click="form.images.splice(index, 1)" /></div><el-button text type="primary" :icon="Plus" @click="form.images.push('')">添加主图</el-button></div>
      <div class="field-label description-label">详情图</div><div class="url-list"><div v-for="(_, index) in form.imageDescriptions" :key="index" class="url-row"><span class="url-number">{{ index + 1 }}</span><el-input v-model="form.imageDescriptions[index]" placeholder="详情图片公网 URL" /><el-button :loading="uploading === `detail-${index}`" @click="uploadImage(form.imageDescriptions, index, 'detail')">上传</el-button><el-button text type="danger" :icon="Delete" :disabled="form.imageDescriptions.length === 1" @click="form.imageDescriptions.splice(index, 1)" /></div><el-button text type="primary" :icon="Plus" @click="form.imageDescriptions.push('')">添加详情图</el-button></div>
      <div class="form-grid form-grid-2" style="margin-top: 16px">
        <el-form-item label="商品视频链接"><el-input v-model="form.videoUrl" placeholder="https://.../video.mp4（可选）" /></el-form-item>
        <el-form-item label="透明图链接"><el-input v-model="form.transparentImage" placeholder="https://.../transparent.png（可选）" /></el-form-item>
      </div>
    </section>

    <section class="form-section">
      <div class="section-heading"><div><h3>小红书 SKU</h3><p>创建后需等待审核，审核通过才可按 SKU 上架</p></div><span class="section-index">05</span></div>
      <el-alert title="方案来源说明" description="“系统创建”表示由小红书平台自动生成，通常仅适用于虚拟商品或自动发货；普通实物商品请选择与店铺仓库、发货地址相匹配的商家物流方案。" type="info" show-icon :closable="false" class="logistics-tip" />
      <div class="xhs-sku-list"><div v-for="(sku, index) in form.skuList" :key="index" class="xhs-sku-card"><div class="sku-card-title"><strong>SKU {{ index + 1 }}</strong><el-button text type="danger" :icon="Delete" :disabled="form.skuList.length === 1" @click="form.skuList.splice(index, 1)" /></div><div class="form-grid form-grid-3"><el-form-item label="商家 SKU 编码"><el-input v-model="sku.erpCode" /></el-form-item><el-form-item label="商品条码"><el-input v-model="sku.barcode" placeholder="barcode（可选，特定品类必填）" /></el-form-item><el-form-item label="原价（元）"><el-input-number v-model="sku.originalPriceYuan" :min="0.01" :precision="2" :controls="false" /></el-form-item><el-form-item label="售价（元）"><el-input-number v-model="sku.priceYuan" :min="0.01" :precision="2" :controls="false" /></el-form-item><el-form-item label="库存"><el-input-number v-model="sku.stock" :min="0" /></el-form-item><el-form-item label="发货时效（小时）"><el-input-number v-model="sku.deliveryHours" :min="1" /></el-form-item><el-form-item label="物流方案"><el-select v-model="sku.logisticsPlanId" :loading="loadingOptions" filterable placeholder="请选择普通实物物流方案"><el-option v-for="item in logisticsPlans" :key="optionId(item)" :label="logisticsPlanLabel(item)" :value="optionId(item)" :disabled="isVirtualSystemPlan(item)"><div class="logistics-option"><span>{{ optionName(item) }}</span><el-tag size="small" :type="isSystemPlan(item) ? 'info' : 'primary'">{{ isSystemPlan(item) ? '系统创建' : '商家创建' }}</el-tag></div></el-option></el-select></el-form-item><el-form-item label="规格图链接"><el-input v-model="sku.specImage" placeholder="specImage（可选）" /></el-form-item></div></div></div>
      <el-button text type="primary" :icon="Plus" @click="addSku">添加 SKU</el-button>
    </section>
    <div class="form-actions"><el-button @click="emit('cancel')">取消</el-button><el-button type="primary" :loading="saving" :icon="Check" @click="submit">{{ isEdit ? '保存修改' : '创建小红书商品' }}</el-button></div>
  </el-form>
</template>
