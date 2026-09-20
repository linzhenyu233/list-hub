<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Delete, Plus, Search } from '@element-plus/icons-vue'
import { storeApi } from '../api'
import { currentShop } from '../shopContext'
import MediaGallery from './MediaGallery.vue'
import MediaUploader from './MediaUploader.vue'

const emit = defineEmits(['created', 'cancel', 'updated'])

const props = defineProps({
  productId: { type: String, default: '' },
  productData: { type: Object, default: null },
})
const isEdit = computed(() => !!props.productId)

const formRef = ref()
const saving = ref(false)
const searching = ref(false)
const categoryKeyword = ref('')
const categoryOptions = ref([])
const selectedCategory = ref(null)
const freightTemplates = ref([])
const loadingFreightTemplates = ref(false)
// 品牌的显示名：按 brand_id 调一次接口拿（微信有按 ID 精确查询的接口，不需要翻品牌库）
const brandName = ref('')
const brandEnName = ref('')
const loadingBrandName = ref(false)
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
  brand_id: [
    { required: true, message: '请选择品牌', trigger: 'change' },
    // 品牌框显示的是名称，但值必须是数字 ID（允许运营直接输入新的品牌 ID）
    { pattern: /^\d+$/, message: '品牌必须是数字 ID；无品牌请选「无品牌（平台默认）」', trigger: 'change' },
  ],
}

// 微信"无品牌"占位值（官方规定：无品牌填 2100000000）
const WECHAT_DEFAULT_BRAND_ID = '2100000000'

// 把品牌 ID 换成品牌名显示：微信有 /channels/ec/brand/get 可以**按 ID 精确查询**，一次请求即可。
// （平台公共品牌库 /channels/ec/brand/all 是上万条、每页只给 10 条，翻页既慢又找不到店铺自有品牌，
//   所以那个接口不能用来做下拉。）
async function loadBrandName() {
  const id = String(form.brand_id || '').trim()
  brandName.value = ''
  brandEnName.value = ''
  if (!id || id === WECHAT_DEFAULT_BRAND_ID) return
  loadingBrandName.value = true
  try {
    const data = await storeApi.brandDetail(id)
    const result = data?.result || data || {}
    brandName.value = result.name || ''
    brandEnName.value = result.en_name || ''
  } catch {
    brandName.value = ''   // 查不到就走兜底文案，不打断编辑
  } finally {
    loadingBrandName.value = false
  }
}

// 品牌选择框：**框里给运营看品牌名，实际提交的仍是 brand_id**。
// 微信没有可用的品牌列表（平台库上万条、每页只给 10 条），所以选项就两项：
// 本商品在用的品牌（按 ID 查出名称）+ 无品牌占位值；要换成别的品牌时可直接输入品牌 ID。
const brandOptions = computed(() => {
  const list = [{ id: WECHAT_DEFAULT_BRAND_ID, name: '无品牌（平台默认）' }]
  const id = String(form.brand_id || '').trim()
  if (id && id !== WECHAT_DEFAULT_BRAND_ID) {
    const label = loadingBrandName.value
      ? `${id}（查询中…）`
      : (brandName.value || `${id}（未查到名称，请核对）`)
    list.unshift({ id, name: label })
  }
  return list
})

const brandHint = computed(() => {
  const id = String(form.brand_id || '').trim()
  if (!id) return '请选择品牌；无品牌选「无品牌（平台默认）」'
  if (id === WECHAT_DEFAULT_BRAND_ID) return '当前：无品牌（微信平台默认占位）'
  if (loadingBrandName.value) return '正在查询品牌名称…'
  if (brandEnName.value) return `英文名：${brandEnName.value}（品牌 ID：${id}）`
  if (brandName.value) return `品牌 ID：${id}`
  const conf = currentShop.value?.wechat || {}
  if (String(conf.brand_id || '') === id && conf.brand_name) return `当前：${conf.brand_name}（店铺配置的品牌）`
  return `品牌 ID：${id} —— 具体名称可在微信小店后台的品牌列表里核对`
})

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
    void loadBrandName()   // 把品牌 ID 换成品牌名显示
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
      thumb_img: sku.thumb_img || '',
      sale_price_yuan: sku.sale_price != null ? sku.sale_price / 100 : null,
      market_price_yuan: sku.market_price != null ? sku.market_price / 100 : null,
      stock_num: sku.stock_num || 0,
      _spec_attrs: sku.sku_attrs || [],
    }))
    if (!form.skus.length) form.skus = [{ out_sku_id: '', sku_code: '', thumb_img: '', sale_price_yuan: null, market_price_yuan: null, stock_num: 0 }]
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
      // 再把 SKU 上的规格维度/值还原到规格区（平台只存在 sku_attrs 里）
      restoreSpecSelection()
    }
  }
})

function addSku() {
  form.skus.push({ out_sku_id: '', sku_code: '', thumb_img: '', sale_price_yuan: null, market_price_yuan: null, stock_num: 0 })
}

function removeSku(index) {
  if (form.skus.length === 1) return
  form.skus.splice(index, 1)
}

// 商品素材：本地图片 → 微信素材库。
// 旧流程要运营先在别处拿到公网图片地址、再点「转存」；现在直接调微信二进制上传接口
// (/images/upload-file)，拿回来的就是发品要填的图片地址，全程不用手写 URL。
// 选图/换图/删除的交互都在 MediaGallery 组件里，这里只负责"把一张图传上去、返回地址"。
async function uploadWechatImage({ filename, contentBase64 }) {
  const data = await storeApi.uploadImageFile(filename, contentBase64)
  const result = data?.result
  return typeof result === 'string' ? result : (result?.img_url || result?.url || '')
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
    activeSpecNames.value = []
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

// ============ 规格维度：像微信小店后台那样自己添加/删除维度 ============
// 平台给了该类目可用的规格定义(sale_attr_list)，运营从里面挑要用的，
// 不再一次性把用不到的维度都铺在页面上。
const activeSpecNames = ref([])

// 微信小店后台最多 4 个规格维度（颜色/尺码/材质…），超过平台不认，这里就卡住不让加
const MAX_SPEC_DIMS = 4

const activeSpecDefs = computed(() => activeSpecNames.value
  .map((name) => specDefs.value.find((d) => d.name === name))
  .filter(Boolean))

const availableSpecDefs = computed(() => specDefs.value
  .filter((d) => !activeSpecNames.value.includes(d.name)))

// SKU 表里，已填了值的规格维度各占一列
const activeSpecDims = computed(() => activeSpecDefs.value
  .filter((d) => (specValues[d.name] || []).length > 0))

// ============ 规格值配图（对齐微信后台的「配图」） ============
// 同一颜色的各个尺码共用一张图：在规格值上配一次，自动套到所有用到该值的 SKU。
// 一行只允许一个维度开配图，避免"颜色图"和"尺码图"互相覆盖。
const specValueImages = reactive({})
const imageSpecName = ref('')

function setImageSpec(name, on) {
  imageSpecName.value = on ? name : ''
  if (on) {
    if (!specValueImages[name]) specValueImages[name] = {}
    // 重新打开时把已配过的图立即套回 SKU（中间可能改过规格值）
    for (const value of Object.keys(specValueImages[name])) applySpecValueImage(name, value)
  }
}

function applySpecValueImage(name, value) {
  const url = specValueImages[name]?.[value] || ''
  for (const sku of form.skus) {
    if (specValueOf(sku, name) === value) sku.thumb_img = url
  }
}

// 规格值被删掉时，顺手清掉它配过的图
function pruneSpecValueImages() {
  for (const name of Object.keys(specValueImages)) {
    for (const value of Object.keys(specValueImages[name])) {
      if (!(specValues[name] || []).includes(value)) delete specValueImages[name][value]
    }
  }
}

// 某个 SKU 在指定规格维度上的值
function specValueOf(sku, name) {
  const attr = (sku._spec_attrs || []).find((a) => (a.attr_key || a.name) === name)
  return (attr && (attr.attr_value ?? attr.value)) || '—'
}

function generateSkusFromSpecs() {
  const dims = activeSpecDefs.value.filter((d) => (specValues[d.name] || []).length > 0)
  if (!dims.length) {
    // 规格全删掉 → 只留一个默认 SKU（与后台一致），价格等沿用第一行
    if (form.skus.length > 1) form.skus = [form.skus[0]]
    return
  }
  let combos = [[]]
  for (const dim of dims) {
    combos = combos.flatMap((c) => (specValues[dim.name] || []).map((v) => [...c, { attr_key: dim.name, attr_value: v }]))
  }
  form.skus = combos.map((attrs, i) => {
    // 开了配图的维度：按规格值取那张图；否则沿用该行原来传的图，别被重新组合清掉
    const imageKey = imageSpecName.value
    const imageValue = imageKey ? (attrs.find((a) => a.attr_key === imageKey)?.attr_value || '') : ''
    const thumbImg = (imageKey && specValueImages[imageKey]?.[imageValue]) || form.skus[i]?.thumb_img || ''
    return {
      out_sku_id: form.skus[i]?.out_sku_id || `SKU-${String(i + 1).padStart(3, '0')}`,
      sku_code: form.skus[i]?.sku_code || '',
      thumb_img: thumbImg,
      sale_price_yuan: form.skus[i]?.sale_price_yuan || null,
      market_price_yuan: form.skus[i]?.market_price_yuan || null,
      stock_num: form.skus[i]?.stock_num || 0,
      _spec_attrs: attrs,
    }
  })
}

function onSpecValuesChanged() {
  cleanSpecValues()
  pruneSpecValueImages()
  generateSkusFromSpecs()
}

// 没有候选值的规格（比如"钻石"）用输入框手填，回车加入
function addManualSpecValue(name) {
  const value = String(_manualSpecInput[name] || '').trim()
  if (!value) return
  if (!Array.isArray(specValues[name])) specValues[name] = []
  if (!specValues[name].includes(value)) specValues[name].push(value)
  _manualSpecInput[name] = ''
  onSpecValuesChanged()
}

// 兜底：清掉历史误操作留下的脏值（旧代码把回车事件对象当成了规格值）
function cleanSpecValues() {
  for (const name of Object.keys(specValues)) {
    const list = specValues[name]
    if (!Array.isArray(list)) continue
    const cleaned = list.filter((v) => typeof v === 'string' && v.trim() && !/^\[object .+\]$/.test(v.trim()))
    if (cleaned.length !== list.length) specValues[name] = cleaned
  }
}

// 新增规格：优先用类目还没用上的规格名；平台给的都用完了就允许自己起名字。
// 微信的 sku_attrs.attr_key 是自由文本，所以自定义规格名能发出去；
// 但平台对类目规格有校验，万一提交被拒，换成类目提供的规格名即可。
function addSpecDim() {
  if (activeSpecNames.value.length >= MAX_SPEC_DIMS) {
    ElMessage.warning(`微信小店最多 ${MAX_SPEC_DIMS} 个规格`)
    return
  }
  const next = availableSpecDefs.value[0]
  if (next) {
    if (!Array.isArray(specValues[next.name])) specValues[next.name] = []
    activeSpecNames.value.push(next.name)
    return
  }
  void promptCustomSpecDim()
}

async function promptCustomSpecDim() {
  let name = ''
  try {
    const { value } = await ElMessageBox.prompt('输入规格名，例如：颜色、尺寸', '自定义规格', {
      confirmButtonText: '添加',
      cancelButtonText: '取消',
      inputPlaceholder: '规格名（最多 40 个字符）',
      inputValidator: (text) => {
        const trimmed = String(text || '').trim()
        if (!trimmed) return '请填写规格名'
        if (trimmed.length > 40) return '规格名最多 40 个字符'
        if (specDefs.value.some((d) => d.name === trimmed)) return '该规格名已存在'
        return true
      },
    })
    name = String(value || '').trim()
  } catch {
    return
  }
  if (!name) return
  specDefs.value.push({ name, value: [], append_allowed: true })
  specValues[name] = []
  _manualSpecInput[name] = ''
  activeSpecNames.value.push(name)
}

function removeSpecDim(name) {
  activeSpecNames.value = activeSpecNames.value.filter((n) => n !== name)
  delete specValues[name]
  delete specValueImages[name]
  if (imageSpecName.value === name) imageSpecName.value = ''
  generateSkusFromSpecs()
}

// 换规格名：已选的值照搬过去（值本身与维度名无关，不用重填）
function renameSpecDim(oldName, newName) {
  if (!newName || newName === oldName || activeSpecNames.value.includes(newName)) return
  const index = activeSpecNames.value.indexOf(oldName)
  if (index < 0) return
  activeSpecNames.value.splice(index, 1, newName)
  specValues[newName] = specValues[oldName] || []
  delete specValues[oldName]
}

// 编辑已有商品：平台只在每个 SKU 的 sku_attrs 里存规格值。
// 不回填的话规格区是空的、SKU 表也长不出规格列，保存还会把商品的规格丢掉。
function restoreSpecSelection() {
  const names = []
  const values = {}
  for (const sku of form.skus) {
    for (const attr of (sku._spec_attrs || [])) {
      const key = attr.attr_key || attr.name
      const value = attr.attr_value ?? attr.value
      if (!key || value == null || value === '') continue
      if (!names.includes(key)) names.push(key)
      if (!values[key]) values[key] = []
      if (!values[key].includes(String(value))) values[key].push(String(value))
    }
  }
  if (!names.length) return
  for (const name of names) {
    // 老商品可能有平台规格定义里已经没有的维度，补一条定义，保证页面上仍能看到
    if (!specDefs.value.some((d) => d.name === name)) {
      specDefs.value.push({ name, value: [], append_allowed: true })
    }
    specValues[name] = values[name]
  }
  activeSpecNames.value = names

  // 如果某个维度满足"同值同图"，说明它是当初配图的那个维度，把图和开关一起还原
  for (const name of names) {
    const map = {}
    let consistent = true
    for (const sku of form.skus) {
      const value = specValueOf(sku, name)
      const image = sku.thumb_img || ''
      if (!image || value === '—') continue
      if (map[value] && map[value] !== image) { consistent = false; break }
      map[value] = image
    }
    if (consistent && Object.keys(map).length) {
      specValueImages[name] = map
      if (!imageSpecName.value) imageSpecName.value = name
    }
  }
}

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
    // 切换类目后丢掉不属于该类目的旧维度；默认只放平台标记为必填的维度，
    // 其余由运营点「新增规格」按需添加（编辑时会再由 restoreSpecSelection 还原实际用的维度）
    activeSpecNames.value = activeSpecNames.value.filter((n) => specDefs.value.some((d) => d.name === n))
    if (!activeSpecNames.value.length) {
      activeSpecNames.value = specDefs.value.filter((d) => d.is_required).map((d) => d.name)
    }
    cleanSpecValues()
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
        // SKU 规格图：微信只接受图片上传接口返回的 img_url(mmecimage.cn)，否则报 10020035
        if (sku.thumb_img) item.thumb_img = sku.thumb_img
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
        <el-form-item label="品牌" prop="brand_id">
          <el-select v-model="form.brand_id" filterable allow-create default-first-option style="width: 100%" placeholder="选择品牌；要换其它品牌可直接输入品牌 ID" @change="loadBrandName">
            <el-option v-for="item in brandOptions" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
          <div class="muted-copy" style="font-size: 12px; line-height: 1.5; margin-top: 4px">{{ brandHint }}</div>
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
        <div><h3>商品素材</h3><p>选择本地图片，直接上传到微信素材库（不用再找公网地址）</p></div>
        <span class="section-index">02</span>
      </div>
      <div class="field-label">商品主图 <span>至少 3 张，最多 9 张 · 第 1 张为首图</span></div>
      <MediaGallery v-model="form.head_imgs" :max="9" :upload="uploadWechatImage" />
      <div class="field-label description-label">详情图片 <span>可选，最多 50 张</span></div>
      <MediaGallery v-model="form.desc_imgs" :max="50" :upload="uploadWechatImage" />
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

    <section class="form-section">
      <div class="section-heading">
        <div><h3>规格和库存价格</h3><p>先添加颜色、尺码等规格并填值，系统按规格组合生成对应 SKU，再逐个填价格、库存与编码</p></div>
        <span class="section-index">{{ attrDefs.length ? '04' : '03' }}</span>
      </div>
      <div class="spec-rows">
        <div v-for="dim in activeSpecDefs" :key="dim.name" class="spec-dimension">
          <div class="spec-dim-head">
            <el-select class="spec-name-select" :model-value="dim.name" @change="(name) => renameSpecDim(dim.name, name)">
              <el-option v-for="d in specDefs" :key="d.name" :label="d.name + (d.is_required ? '（必填）' : '')" :value="d.name" :disabled="activeSpecNames.includes(d.name) && d.name !== dim.name" />
            </el-select>
            <div class="spec-dim-tools">
              <el-switch :model-value="imageSpecName === dim.name" @change="(on) => setImageSpec(dim.name, on)" />
              <span title="开：按规格值配图（同色各尺码共用一张图）">配图</span>
              <el-button text type="danger" :icon="Delete" title="删除该规格" @click="removeSpecDim(dim.name)" />
            </div>
          </div>
          <el-select v-if="attrOptions(dim.value).length > 0" v-model="specValues[dim.name]" multiple filterable allow-create default-first-option :placeholder="`选择或直接输入${dim.name}值（输入后回车）`" @change="onSpecValuesChanged">
            <el-option v-for="v in attrOptions(dim.value)" :key="v" :label="v" :value="v" />
          </el-select>
          <div v-else class="spec-manual-tags">
            <el-tag v-for="(v, vi) in (specValues[dim.name] || [])" :key="vi" closable @close="specValues[dim.name].splice(vi, 1); onSpecValuesChanged()">{{ v }}</el-tag>
            <el-input v-model="_manualSpecInput[dim.name]" placeholder="输入后回车添加" style="width: 260px" @keyup.enter="addManualSpecValue(dim.name)" />
          </div>
          <div v-if="imageSpecName === dim.name" class="spec-value-images">
            <div v-for="v in (specValues[dim.name] || [])" :key="v" class="spec-value-image">
              <MediaUploader v-model="specValueImages[dim.name][v]" :upload="uploadWechatImage" placeholder="选图" compact @update:model-value="applySpecValueImage(dim.name, v)" />
              <span class="spec-value-image__label" :title="v">{{ v }}</span>
            </div>
            <span v-if="!(specValues[dim.name] || []).length" class="muted-copy">先填{{ dim.name }}的值，再逐个配图</span>
          </div>
        </div>
        <div class="spec-rows-footer">
          <el-tooltip :disabled="activeSpecNames.length < MAX_SPEC_DIMS" content="微信小店最多 4 个规格" placement="top">
            <span class="spec-add-wrap">
              <el-button text type="primary" :icon="Plus" :disabled="activeSpecNames.length >= MAX_SPEC_DIMS" @click="addSpecDim">
                {{ availableSpecDefs.length ? `新增规格（${availableSpecDefs[0].name}）` : '自定义规格' }}
              </el-button>
            </span>
          </el-tooltip>
          <span class="muted-copy">
            <template v-if="activeSpecNames.length">已添加 {{ activeSpecNames.length }}/{{ MAX_SPEC_DIMS }} 个规格</template>
            <template v-else>{{ specDefs.length ? '不添加规格将生成单个默认 SKU' : '该类目没有预置规格，可点「自定义规格」自己加' }}</template>
          </span>
        </div>
      </div>

      <div class="field-label description-label">价格与库存</div>
      <el-table :key="activeSpecDims.map((d) => d.name).join('-')" :data="form.skus" size="small" border empty-text="未生成 SKU" class="wx-sku-table">
        <el-table-column v-for="dim in activeSpecDims" :key="dim.name" :label="dim.name" min-width="110" show-overflow-tooltip>
          <template #default="{ row }"><span class="sku-spec-value">{{ specValueOf(row, dim.name) }}</span></template>
        </el-table-column>
        <el-table-column label="规格图" width="86" align="center">
          <template #default="{ row }"><MediaUploader v-model="row.thumb_img" :upload="uploadWechatImage" placeholder="选图" compact /></template>
        </el-table-column>
        <el-table-column label="销售价（元）" width="140"><template #default="{ row }"><el-input-number v-model="row.sale_price_yuan" :min="0.01" :precision="2" :controls="false" placeholder="0.00" style="width: 100%" /></template></el-table-column>
        <el-table-column label="划线价（元）" width="140"><template #default="{ row }"><el-input-number v-model="row.market_price_yuan" :min="0" :precision="2" :controls="false" placeholder="划线价" style="width: 100%" /></template></el-table-column>
        <el-table-column label="库存" width="130"><template #default="{ row }"><el-input-number v-model="row.stock_num" :min="0" :precision="0" style="width: 100%" /></template></el-table-column>
        <el-table-column label="SKU 编码" width="180"><template #default="{ row }"><el-input v-model="row.out_sku_id" placeholder="SKU-001" /></template></el-table-column>
        <el-table-column label="商品条码" width="170"><template #default="{ row }"><el-input v-model="row.sku_code" placeholder="可选" /></template></el-table-column>
        <el-table-column label="操作" width="60" align="center" fixed="right">
          <template #default="{ $index }"><el-button text type="danger" :icon="Delete" :disabled="form.skus.length === 1" @click="removeSku($index)" /></template>
        </el-table-column>
      </el-table>
      <div class="sku-table-footer">
        <el-button v-if="!activeSpecDims.length" text type="primary" :icon="Plus" @click="addSku">添加 SKU</el-button>
        <span class="muted-copy">共 {{ form.skus.length }} 个 SKU；规格列由上面所选规格值自动生成，价格以元录入，提交时自动转换为分</span>
      </div>
    </section>

    <section class="form-section compact-section">
      <div class="section-heading">
        <div><h3>履约与服务</h3><p>设置发货及售后服务</p></div>
        <span class="section-index">{{ attrDefs.length ? '05' : '04' }}</span>
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
