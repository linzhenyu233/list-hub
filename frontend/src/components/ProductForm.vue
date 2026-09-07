<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Check, Delete, Plus, Search } from '@element-plus/icons-vue'
import { storeApi } from '../api'

const emit = defineEmits(['created', 'cancel', 'updated'])

const props = defineProps({
  productId: { type: String, default: '' },
  productData: { type: Object, default: null },
})
const isEdit = computed(() => !!props.productId)

const formRef = ref()
const saving = ref(false)
const searching = ref(false)
const uploadingIndex = ref(-1)
const categoryKeyword = ref('')
const categoryOptions = ref([])
const selectedCategory = ref(null)
const freightTemplates = ref([])
const loadingFreightTemplates = ref(false)
const attrDefs = ref([])
const attrValues = reactive({})
const specDefs = ref([])
const specValues = reactive({})
const _manualSpecInput = reactive({})

const form = reactive({
  title: '',
  short_title: '',
  out_product_id: '',
  brand_id: '2100000000',
  deliver_method: 0,
  seven_day_return: true,
  freight_insurance: false,
  freight_template_id: '',
  description: '',
  weight: '',
  video_url: '',
  attrs: [],
  head_imgs: ['', '', ''],
  desc_imgs: [''],
  skus: [{ out_sku_id: '', sku_code: '', sale_price_yuan: null, market_price_yuan: null, stock_num: 0 }],
})

const rules = {
  title: [{ required: true, message: '请输入商品标题', trigger: 'blur' }],
  out_product_id: [{ required: true, message: '请输入外部商品编码', trigger: 'blur' }],
  brand_id: [{ required: true, message: '请输入品牌 ID', trigger: 'blur' }],
}

const validHeadImages = computed(() => form.head_imgs.filter(Boolean))

function responseList(data, keys) {
  const result = data?.result || data?.data || data || {}
  for (const key of keys) if (Array.isArray(result[key])) return result[key]
  return Array.isArray(result) ? result : []
}

// 微信 API 的 attr.value 可能是字符串(单候选值)或逗号/分号分隔字符串，需规范化为数组，避免 v-for 拆成单字符
function attrOptions(v) {
  if (Array.isArray(v)) return v
  if (v == null || v === '') return []
  if (typeof v === 'string') {
    // 同时支持 , ， ; ； 四种分隔符
    if (/[,，;；]/.test(v)) return v.split(/[,，;；]/).map((s) => s.trim()).filter(Boolean)
    return [v]
  }
  return [v]
}

function optionId(item) { return item.id || item.template_id || item.templateId }
function optionName(item) { return item.name || item.template_name || item.templateName || String(optionId(item) || '') }

async function loadFreightTemplates() {
  loadingFreightTemplates.value = true
  try {
    const data = await storeApi.freightTemplates()
    freightTemplates.value = responseList(data, ['templates', 'freight_templates', 'list'])
  } catch (error) { ElMessage.warning(`读取微信运费模板失败：${error.message}`) }
  finally { loadingFreightTemplates.value = false }
}

onMounted(async () => {
  await loadFreightTemplates()
  if (props.productData) {
    const d = props.productData
    form.title = d.title || ''
    form.short_title = d.short_title || ''
    form.out_product_id = d.out_product_id || ''
    form.brand_id = d.brand_id || '2100000000'
    // 运费模板回填:微信商品详情用 express_info.template_id(非顶层 freight_template_id)
    form.freight_template_id = String(d.express_info?.template_id || d.freight_template_id || '')
    form.deliver_method = d.deliver_method ?? 0
    form.seven_day_return = d.extra_service?.seven_day_return !== 0
    form.freight_insurance = d.extra_service?.freight_insurance === 1
    form.description = d.desc_info?.detail || ''
    form.video_url = d.video_url || ''
    // 重量从 attrs 中取回
    const weightAttr = (d.attrs || []).find((a) => a.attr_key === '商品毛重')
    form.weight = weightAttr?.attr_value || ''
    form.head_imgs = d.head_imgs?.length ? [...d.head_imgs] : ['', '', '']
    while (form.head_imgs.length < 3) form.head_imgs.push('')
    form.desc_imgs = d.desc_info?.imgs?.length ? [...d.desc_info.imgs] : ['']
    form.skus = (d.skus || []).map((sku) => ({
      out_sku_id: sku.out_sku_id || '',
      sku_code: sku.sku_code || '',
      sale_price_yuan: sku.sale_price != null ? sku.sale_price / 100 : null,
      market_price_yuan: sku.market_price != null ? sku.market_price / 100 : null,
      stock_num: sku.stock_num || 0,
      _spec_attrs: sku.sku_attrs || [],
    }))
    if (!form.skus.length) form.skus = [{ out_sku_id: '', sku_code: '', sale_price_yuan: null, market_price_yuan: null, stock_num: 0 }]
    // 回填属性
    if (d.attrs?.length) {
      for (const a of d.attrs) { attrValues[a.attr_key] = a.attr_value }
    }
    // 回填类目(优先新类目树 cats_v2,兼容旧商品 cats)
    const respCats = d.cats_v2?.length ? d.cats_v2 : d.cats
    if (respCats?.length) {
      const lastCat = respCats[respCats.length - 1]
      selectedCategory.value = { cat_id: lastCat.cat_id, path: respCats.map((c) => c.name || c.cat_id).join(' > '), chain: respCats, leaf: true }
      await handleCategorySelect()
      // 重新回填属性值(handleCategorySelect会清空)
      if (d.attrs?.length) {
        for (const a of d.attrs) { attrValues[a.attr_key] = a.attr_value }
      }
    }
  }
})

function addSku() {
  form.skus.push({ out_sku_id: '', sku_code: '', sale_price_yuan: null, market_price_yuan: null, stock_num: 0 })
}

function removeSku(index) {
  if (form.skus.length === 1) return
  form.skus.splice(index, 1)
}

function addHeadImage() {
  if (form.head_imgs.length < 9) form.head_imgs.push('')
}

function addDescImage() {
  if (form.desc_imgs.length < 50) form.desc_imgs.push('')
}

async function uploadImage(index, type) {
  const source = type === 'head' ? form.head_imgs : form.desc_imgs
  if (!source[index]) return ElMessage.warning('请先填写图片公网地址')
  uploadingIndex.value = type === 'head' ? index : index + 100
  try {
    const data = await storeApi.uploadImage(source[index])
    source[index] = data.result
    ElMessage.success('图片已转存到微信')
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    uploadingIndex.value = -1
  }
}

async function searchCategories() {
  if (!categoryKeyword.value.trim()) return
  searching.value = true
  try {
    const data = await storeApi.searchCategories(categoryKeyword.value.trim())
    categoryOptions.value = (data.results || []).filter((item) => item.leaf)
    if (!categoryOptions.value.length) ElMessage.warning('未找到可发布的叶子类目')
    attrDefs.value = []
    specDefs.value = []
    Object.keys(attrValues).forEach((k) => delete attrValues[k])
    Object.keys(specValues).forEach((k) => delete specValues[k])
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    searching.value = false
  }
}

watch(attrDefs, () => {
  Object.keys(attrValues).forEach((k) => delete attrValues[k])
  attrDefs.value.forEach((a) => { attrValues[a.name] = a.type_v2 === 'select_many' ? [] : '' })
})

function generateSkusFromSpecs() {
  const dims = specDefs.value.filter((d) => (specValues[d.name] || []).length > 0)
  if (!dims.length) return
  let combos = [[]]
  for (const dim of dims) {
    combos = combos.flatMap((c) => (specValues[dim.name] || []).map((v) => [...c, { attr_key: dim.name, attr_value: v }]))
  }
  form.skus = combos.map((attrs, i) => ({
    out_sku_id: form.skus[i]?.out_sku_id || `SKU-${String(i + 1).padStart(3, '0')}`,
    sku_code: form.skus[i]?.sku_code || '',
    sale_price_yuan: form.skus[i]?.sale_price_yuan || null,
    market_price_yuan: form.skus[i]?.market_price_yuan || null,
    stock_num: form.skus[i]?.stock_num || 0,
    _spec_attrs: attrs,
  }))
}

function onSpecValuesChanged() { generateSkusFromSpecs() }

async function handleCategorySelect() {
  if (!selectedCategory.value) return
  try {
    const data = await storeApi.categoryDetail(selectedCategory.value.cat_id)
    const attr = data?.result || data || {}
    attrDefs.value = attr.product_attr_list || []
    specDefs.value = attr.sale_attr_list || []
    specDefs.value.forEach((d) => {
      if (!specValues[d.name]) specValues[d.name] = []
      _manualSpecInput[d.name] = ''
    })
  } catch (error) {
    ElMessage.warning(`加载类目属性失败：${error.message}`)
  }
}

async function submit() {
  try {
    await formRef.value.validate()
    if (!selectedCategory.value) return ElMessage.warning('请选择叶子类目')
    if (validHeadImages.value.length < 3) return ElMessage.warning('微信商品主图至少需要 3 张')
    if (form.skus.some((sku) => !sku.out_sku_id || sku.sale_price_yuan === null || sku.sale_price_yuan < 0.01)) {
      return ElMessage.warning('请完整填写 SKU 编码和价格')
    }

    saving.value = true
    const descImages = form.desc_imgs.filter(Boolean)
    const attrs = attrDefs.value
      .filter((a) => a.is_required || (attrValues[a.name] != null && attrValues[a.name] !== '' && (!Array.isArray(attrValues[a.name]) || attrValues[a.name].length > 0)))
      .map((a) => {
        const v = attrValues[a.name]
        return { attr_key: a.name, attr_value: Array.isArray(v) ? v.join(',') : String(v) }
      })
    // 重量作为属性传（微信 API 无原生重量字段）
    if (form.weight && !attrs.some((a) => a.attr_key === '商品毛重')) {
      attrs.push({ attr_key: '商品毛重', attr_value: String(form.weight) })
    }
    const product = {
      title: form.title,
      short_title: form.short_title || undefined,
      out_product_id: form.out_product_id,
      head_imgs: validHeadImages.value,
      // 类目链路必须用新类目树字段 cats_v2(店铺 B/C 端已全切新树);
      // 用旧字段 cats 装新树的父级 cat_id 会被微信判为类目错误(6600016)
      cats_v2: selectedCategory.value.chain.map((item) => ({ cat_id: item.cat_id })),
      brand_id: form.brand_id,
      // 运费模板必须放 express_info.template_id(官方字段);顶层 freight_template_id 会被微信忽略。
      // 后端 _prepare_for_submit 也会兜底迁移并补默认模板,这里直接按官方格式发送。
      express_info: form.freight_template_id
        ? { template_id: String(form.freight_template_id), ...(form.weight ? { weight: Number(form.weight) } : {}) }
        : undefined,
      attrs,
      deliver_method: form.deliver_method,
      extra_service: {
        seven_day_return: form.seven_day_return ? 1 : 0,
        freight_insurance: form.freight_insurance ? 1 : 0,
      },
      skus: form.skus.map((sku) => {
        const item = {
          out_sku_id: sku.out_sku_id,
          sale_price: Math.round(Number(sku.sale_price_yuan) * 100),
          stock_num: Number(sku.stock_num),
          sku_attrs: sku._spec_attrs || [],
        }
        if (sku.market_price_yuan != null && sku.market_price_yuan > 0) item.market_price = Math.round(Number(sku.market_price_yuan) * 100)
        if (sku.sku_code) item.sku_code = sku.sku_code
        return item
      }),
    }
    if (descImages.length || form.description) {
      product.desc_info = {}
      if (descImages.length) product.desc_info.imgs = descImages
      if (form.description) product.desc_info.detail = form.description
    }
    if (form.video_url) product.video_url = form.video_url
    if (isEdit.value) {
      await storeApi.updateProduct(props.productId, product)
      ElMessage.success('商品已更新')
      emit('updated', product)
    } else {
      const data = await storeApi.createProduct(product)
      ElMessage.success('商品草稿创建成功')
      emit('created', data.result)
    }
  } catch (error) {
    if (error?.message) ElMessage.error(error.message)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-form ref="formRef" :model="form" :rules="rules" label-position="top" class="product-form">
    <section class="form-section">
      <div class="section-heading">
        <div>
          <h3>基础信息</h3>
          <p>用于识别和展示商品的主要信息</p>
        </div>
        <span class="section-index">01</span>
      </div>
      <div class="form-grid form-grid-2">
        <el-form-item label="商品标题" prop="title">
          <el-input v-model="form.title" maxlength="60" show-word-limit placeholder="请输入商品标题" />
        </el-form-item>
        <el-form-item label="短标题">
          <el-input v-model="form.short_title" maxlength="20" show-word-limit placeholder="用于紧凑场景展示" />
        </el-form-item>
        <el-form-item label="外部商品编码" prop="out_product_id">
          <el-input v-model="form.out_product_id" placeholder="例如：SPU-2026-001" />
        </el-form-item>
        <el-form-item label="品牌 ID" prop="brand_id">
          <el-input v-model="form.brand_id" placeholder="无品牌可使用平台无品牌 ID" />
        </el-form-item>
        <el-form-item label="商品毛重（克）">
          <el-input v-model="form.weight" placeholder="例如：500（将作为属性传给平台）" />
        </el-form-item>
        <el-form-item label="商品视频链接">
          <el-input v-model="form.video_url" placeholder="https://example.com/product.mp4（可选）" />
        </el-form-item>
      </div>
      <el-form-item label="商品描述">
        <el-input v-model="form.description" type="textarea" :rows="3" placeholder="商品详情文字描述（可选，对应 desc_info.detail）" />
      </el-form-item>
      <el-form-item label="商品类目" required>
        <div class="category-search">
          <el-input v-model="categoryKeyword" placeholder="输入类目关键词，如珠宝 戒指" @keyup.enter="searchCategories">
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-button :loading="searching" @click="searchCategories">搜索类目</el-button>
        </div>
        <el-select v-model="selectedCategory" value-key="cat_id" filterable placeholder="从搜索结果中选择叶子类目" class="category-select" @change="handleCategorySelect">
          <el-option v-for="item in categoryOptions" :key="item.cat_id" :label="item.path" :value="item" />
        </el-select>
      </el-form-item>
    </section>

    <section class="form-section">
      <div class="section-heading">
        <div><h3>商品素材</h3><p>填写公网图片地址并转存至微信素材库</p></div>
        <span class="section-index">02</span>
      </div>
      <div class="field-label">商品主图 <span>至少 3 张，最多 9 张</span></div>
      <div class="url-list">
        <div v-for="(_, index) in form.head_imgs" :key="`head-${index}`" class="url-row">
          <span class="url-number">{{ index + 1 }}</span>
          <el-input v-model="form.head_imgs[index]" placeholder="https://example.com/product.jpg" />
          <el-button :loading="uploadingIndex === index" @click="uploadImage(index, 'head')">转存</el-button>
          <el-button v-if="form.head_imgs.length > 3" text type="danger" :icon="Delete" @click="form.head_imgs.splice(index, 1)" />
        </div>
        <el-button v-if="form.head_imgs.length < 9" text type="primary" :icon="Plus" @click="addHeadImage">添加主图</el-button>
      </div>
      <div class="field-label description-label">详情图片 <span>可选，最多 50 张</span></div>
      <div class="url-list">
        <div v-for="(_, index) in form.desc_imgs" :key="`desc-${index}`" class="url-row">
          <span class="url-number">{{ index + 1 }}</span>
          <el-input v-model="form.desc_imgs[index]" placeholder="https://example.com/detail.jpg" />
          <el-button :loading="uploadingIndex === index + 100" @click="uploadImage(index, 'desc')">转存</el-button>
          <el-button v-if="form.desc_imgs.length > 1" text type="danger" :icon="Delete" @click="form.desc_imgs.splice(index, 1)" />
        </div>
        <el-button v-if="form.desc_imgs.length < 50" text type="primary" :icon="Plus" @click="addDescImage">添加详情图</el-button>
      </div>
    </section>

    <section v-if="attrDefs.length" class="form-section">
      <div class="section-heading">
        <div><h3>商品属性</h3><p>根据所选类目自动加载，按类目要求填写</p></div>
        <span class="section-index">03</span>
      </div>
      <div class="form-grid form-grid-2">
        <el-form-item v-for="attr in attrDefs" :key="attr.name" :label="attr.name + (attr.is_required ? '（必填）' : '')" :required="attr.is_required">
          <el-select v-if="attr.type_v2 === 'select_one'" v-model="attrValues[attr.name]" clearable filterable :allow-create="attr.append_allowed" :placeholder="'请选择' + attr.name">
            <el-option v-for="v in attrOptions(attr.value)" :key="v" :label="v" :value="v" />
          </el-select>
          <el-select v-else-if="attr.type_v2 === 'select_many'" v-model="attrValues[attr.name]" multiple filterable :allow-create="attr.append_allowed" :placeholder="'请选择' + attr.name">
            <el-option v-for="v in attrOptions(attr.value)" :key="v" :label="v" :value="v" />
          </el-select>
          <el-input v-else v-model="attrValues[attr.name]" :placeholder="attr.hint || '请输入' + attr.name" />
        </el-form-item>
      </div>
    </section>

    <section v-if="specDefs.length" class="form-section">
      <div class="section-heading">
        <div><h3>销售规格</h3><p>选择规格值后自动生成 SKU 组合，再补充编码和价格</p></div>
        <span class="section-index">04</span>
      </div>
      <div v-for="dim in specDefs" :key="dim.name" class="spec-dimension">
        <div class="field-label">{{ dim.name }} <span v-if="dim.is_required">（必填）</span></div>
        <el-select v-if="attrOptions(dim.value).length > 0" v-model="specValues[dim.name]" multiple filterable :allow-create="dim.append_allowed" :placeholder="'选择' + dim.name + '值'" @change="onSpecValuesChanged">
          <el-option v-for="v in attrOptions(dim.value)" :key="v" :label="v" :value="v" />
        </el-select>
        <div v-else class="spec-manual-tags">
          <el-tag v-for="(v, vi) in (specValues[dim.name] || [])" :key="vi" closable @close="specValues[dim.name].splice(vi, 1); onSpecValuesChanged()">{{ v }}</el-tag>
          <el-input v-model="_manualSpecInput[dim.name]" :placeholder="'输入后回车添加'" style="width: 160px" @keyup.enter="(_v) => { if (_v) { (specValues[dim.name] = specValues[dim.name] || []); specValues[dim.name].push(_v); _manualSpecInput[dim.name] = ''; onSpecValuesChanged() } }" />
        </div>
      </div>
    </section>

    <section class="form-section">
      <div class="section-heading">
        <div><h3>销售规格</h3><p>价格以元录入，提交时自动转换为分</p></div>
        <span class="section-index">{{ specDefs.length ? '05' : '03' }}</span>
      </div>
      <div class="sku-table">
        <div class="sku-row sku-header sku-row-wide"><span>SKU 编码</span><span>商品条码</span><span>销售价（元）</span><span>划线价（元）</span><span>库存</span><span></span></div>
        <div v-for="(sku, index) in form.skus" :key="index" class="sku-row sku-row-wide">
          <el-input v-model="sku.out_sku_id" placeholder="SKU-001" />
          <el-input v-model="sku.sku_code" placeholder="商品条码（可选）" />
          <el-input-number v-model="sku.sale_price_yuan" :min="0.01" :precision="2" :controls="false" placeholder="0.00" />
          <el-input-number v-model="sku.market_price_yuan" :min="0" :precision="2" :controls="false" placeholder="划线价" />
          <el-input-number v-model="sku.stock_num" :min="0" :precision="0" controls-position="right" />
          <el-button text type="danger" :icon="Delete" :disabled="form.skus.length === 1" @click="removeSku(index)" />
        </div>
      </div>
      <el-button text type="primary" :icon="Plus" @click="addSku">添加 SKU</el-button>
    </section>

    <section class="form-section compact-section">
      <div class="section-heading">
        <div><h3>履约与服务</h3><p>设置发货及售后服务</p></div>
        <span class="section-index">{{ specDefs.length ? '06' : '04' }}</span>
      </div>
      <div class="switch-list">
        <div><span>运费模板</span><el-select v-model="form.freight_template_id" :loading="loadingFreightTemplates" clearable placeholder="选择微信运费模板"><el-option v-for="item in freightTemplates" :key="optionId(item)" :label="optionName(item)" :value="String(optionId(item))" /></el-select></div>
        <div><span>配送方式</span><el-select v-model="form.deliver_method"><el-option label="快递发货" :value="0" /></el-select></div>
        <div><span>七天无理由退货</span><el-switch v-model="form.seven_day_return" /></div>
        <div><span>运费险</span><el-switch v-model="form.freight_insurance" /></div>
      </div>
    </section>

    <div class="form-actions">
      <el-button @click="emit('cancel')">取消</el-button>
      <el-button type="primary" :loading="saving" :icon="Check" @click="submit">{{ isEdit ? '保存修改' : '创建商品草稿' }}</el-button>
    </div>
  </el-form>
</template>
