<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Check, Download, Upload, Refresh, Right, Search } from '@element-plus/icons-vue'
import { bulkApi } from '../bulkApi'
import { xhsApi } from '../xhsApi'
import { storeApi } from '../api'
import CategoryAliasPanel from './CategoryAliasPanel.vue'
import ProductReviewDrawer from './ProductReviewDrawer.vue'

const step = ref(0)
const file = ref(null)
const folderFiles = ref([])
const commonDetailFiles = ref([])
const loading = ref(false)
const batch = ref(null)
const platforms = ref(['wechat', 'xhs'])
const mapping = ref({ mode: 'auto', products: {} })
const settingsLoading = ref(false)
const categoryMatching = ref(false)
const xhsCascade = ref({})
const aliasPanelVisible = ref(false)
const itemKeyword = ref('')
const itemErrorFilter = ref('all')
const itemPage = ref(1)
const itemPageSize = ref(20)
const platformSettings = ref({ xhsShipping: [], xhsLogistics: [], wechatBrand: '平台无品牌', wechatDelivery: '快递发货' })
const batchPlatformSettings = reactive({ wechatFreightId: '', xhsShippingId: '', xhsLogisticsId: '' })
const groupAttrDefs = reactive({})
const attrConfigLoading = ref(false)
const activeAttrGroups = reactive({})
const attrMatchStatus = reactive({})
const reviewVisible = ref(false)
const reviewProduct = ref(null)
const currentJob = ref(null)
const retrying = ref(false)
let jobTimer = null
function clearJobProgress() {
  if (jobTimer) { clearTimeout(jobTimer); jobTimer = null }
  currentJob.value = null
}
watch(step, (value) => { if (value < 3) clearJobProgress() })
const steps = ['导入文件', '校验与编辑', '平台映射', '确认发布']
const allItems = computed(() => batch.value?.items || [])
const items = computed(() => pagedItems.value)
const products = computed(() => batch.value?.products || [])
const validCount = computed(() => allItems.value.filter((item) => !(item.errors || []).length).length)
const errorCount = computed(() => allItems.value.length - validCount.value)
// 存在 SKU 间商品属性不一致提醒的商品数（不阻断发布，仅提醒）
const productWarningCount = computed(() => products.value.filter((product) => (product.warnings || []).length).length)
const filteredItems = computed(() => {
  const keyword = itemKeyword.value.trim().toLowerCase()
  return allItems.value.filter((item) => {
    const matchesKeyword = !keyword || [item.product_code, item.title, item.sku_code]
      .some((value) => String(value || '').toLowerCase().includes(keyword))
    const hasError = (item.errors || []).length > 0
    const matchesError = itemErrorFilter.value === 'all'
      || (itemErrorFilter.value === 'error' && hasError)
      || (itemErrorFilter.value === 'valid' && !hasError)
    return matchesKeyword && matchesError
  })
})
const pagedItems = computed(() => {
  const start = (itemPage.value - 1) * itemPageSize.value
  return filteredItems.value.slice(start, start + itemPageSize.value)
})
function resetItemPage() { itemPage.value = 1 }
const mappingRows = computed(() => products.value.map((product) => ({
  ...product,
  mapping: {
    ...(mapping.value.products?.[product.product_code] || {}),
    status: [mapping.value.products?.[product.product_code]?.status, mapping.value.products?.[product.product_code]?.xhs_status].filter(Boolean).join('；') || '待系统匹配',
  },
})))
const mappingGroups = computed(() => {
  const groups = new Map()
  for (const row of mappingRows.value) {
    const key = row.internal_category || '(未填写内部类目)'
    if (!groups.has(key)) groups.set(key, { internal_category: key, products: [], mapping: row.mapping })
    groups.get(key).products.push(row.product_code)
  }
  return [...groups.values()]
})
function confirmWechatCategory(group, value) {
  const candidate = group.mapping.wechat_candidates?.find((item) => item.path === value)
  if (!candidate) return
  for (const code of group.products) {
    const current = mapping.value.products[code] || {}
    mapping.value.products[code] = { ...current, wechat_category: candidate.path, wechat_category_chain: candidate.chain, status: '微信类目已人工确认' }
  }
  void saveGroupAlias(group.internal_category, { wechat: { category: candidate.path, chain: candidate.chain || [], attr_defaults: groupAttrDefs[group.internal_category]?.wechat?.defaults || {} } })
}
async function loadXhsLevel(group, level, parentId = null) {
  const data = await xhsApi.categories(parentId ? { category_id: parentId } : {})
  const result = data?.result || data || []
  const options = Array.isArray(result) ? result : (result.categoryV3s || result.categories || [])
  const state = xhsCascade.value[group.internal_category] || { levels: [[], [], []], selected: [] }
  state.levels[level] = options
  state.levels.splice(level + 1)
  state.selected.splice(level)
  xhsCascade.value = { ...xhsCascade.value, [group.internal_category]: state }
}
async function confirmXhsCategory(group, level, value) {
  const state = xhsCascade.value[group.internal_category]
  state.selected[level] = value
  const item = state.levels[level].find((row) => String(row.id || row.categoryId) === String(value))
  if (!item) return
  // 非叶子类目继续加载下一级,不写死 3 层,适配任意深度类目树
  if (!(item.isLeaf || item.leaf)) { await loadXhsLevel(group, level + 1, value); return }
  const chain = state.selected.slice(0, level + 1).map((id, index) => { const row = state.levels[index].find((option) => String(option.id || option.categoryId) === String(id)); return { id, name: row?.name || '' } })
  for (const code of group.products) mapping.value.products[code] = { ...(mapping.value.products[code] || {}), xhs_category: chain.map((row) => row.name).join(' > '), xhs_category_chain: chain, xhs_category_id: value, xhs_status: '小红书类目已人工确认' }
  xhsCascade.value = { ...xhsCascade.value, [group.internal_category]: state }
  void saveGroupAlias(group.internal_category, { xhs: { category: chain.map((row) => row.name).join(' > '), chain, category_id: value, attr_defaults: groupAttrDefs[group.internal_category]?.xhs?.defaults || {}, spec_map: groupAttrDefs[group.internal_category]?.xhs?.spec_map || {} } })
}

// 类目别名映射表:人工确认一次存表,后续批次自动命中不再重复选;后端合并微信/小红书两侧,这里只传本次确认的一侧即可

async function saveGroupAlias(internalCategory, payload) {
  try {
    await bulkApi.saveCategoryAlias({ internal_category: internalCategory, ...payload })
    ElMessage.success('已存为类目映射，下次同内部类目将自动命中')
  } catch { /* 别名保存失败不影响本次确认 */ }
}

function listFrom(data, keys) {
  const result = data?.result || data || {}
  for (const key of keys) if (Array.isArray(result[key])) return result[key]
  return Array.isArray(result) ? result : []
}

function optionName(item) { return item.name || item.templateName || item.template_name || item.planName || item.planInfoName || item.title || item.id || '--' }
function settingId(item) { return String(item.id || item.templateId || item.template_id || item.planInfoId || item.logisticsPlanId || '') }

async function loadPlatformSettings() {
  settingsLoading.value = true
  try {
    const [shipping, logistics, wechatFreight] = await Promise.all([xhsApi.shippingTemplates(), xhsApi.logisticsPlans(), storeApi.freightTemplates()])
    const wechatResult = wechatFreight?.result || wechatFreight?.data || wechatFreight || {}
    platformSettings.value = {
      ...platformSettings.value,
      wechatFreight: (wechatResult.templates || wechatResult.freight_templates || wechatResult.list || (Array.isArray(wechatResult) ? wechatResult : [])).filter((item) => item.is_valid !== false && !String(item.name || item.template_name || '').includes('测试')),
      xhsShipping: listFrom(shipping, ['templates', 'carriageTemplateList', 'list']).filter((item) => !optionName(item).includes('测试')),
      xhsLogistics: listFrom(logistics, ['logisticsPlans', 'logisticsList', 'plans', 'list']).filter((item) => item.isValid !== false),
    }
    if (!batchPlatformSettings.wechatFreightId && platformSettings.value.wechatFreight?.length === 1) batchPlatformSettings.wechatFreightId = settingId(platformSettings.value.wechatFreight[0])
    if (!batchPlatformSettings.xhsShippingId && platformSettings.value.xhsShipping.length === 1) batchPlatformSettings.xhsShippingId = settingId(platformSettings.value.xhsShipping[0])
    if (!batchPlatformSettings.xhsLogisticsId && platformSettings.value.xhsLogistics.length === 1) batchPlatformSettings.xhsLogisticsId = settingId(platformSettings.value.xhsLogistics[0])
  } catch (error) {
    ElMessage.warning(`读取小红书店铺设置失败：${error.message}`)
  } finally { settingsLoading.value = false }
}

async function autoMatchWechatCategories() {
  if (!products.value.length) return
  categoryMatching.value = true
  try {
    const mappings = { ...(mapping.value.products || {}) }
    // 先读类目别名映射表:命中直接应用,不再请求平台类目接口(映射表加载失败不阻塞自动匹配)
    const aliasMap = {}
    try {
      for (const alias of (await bulkApi.categoryAliases()).result || []) {
        aliasMap[alias.internal_category.trim()] = alias
      }
    } catch { /* ignore */ }
    // 从映射表恢复属性默认值：同类目下次自动带入
    for (const [key, alias] of Object.entries(aliasMap)) {
      if (alias?.wechat?.attr_defaults || alias?.xhs?.attr_defaults || alias?.xhs?.spec_map) {
        const cfg = ensureGroupConfig(key)
        if (alias.wechat?.attr_defaults) Object.assign(cfg.wechat.defaults, alias.wechat.attr_defaults)
        if (alias.xhs?.attr_defaults) Object.assign(cfg.xhs.defaults, alias.xhs.attr_defaults)
        if (alias.xhs?.spec_map) Object.assign(cfg.xhs.spec_map, alias.xhs.spec_map)
      }
    }
    let freightOptions = []
    try {
      const freight = await storeApi.freightTemplates()
      const result = freight?.result || freight?.data || freight || {}
      freightOptions = (result.templates || result.freight_templates || result.list || (Array.isArray(result) ? result : []))
        .filter((item) => item.is_valid !== false && !String(item.name || item.template_name || '').includes('测试'))
    } catch { freightOptions = [] }

    // 同一关键词只查一次后端(后端已有类目树缓存),避免每个商品重复全量搜索
    const searchCache = {}
    async function searchLocal(keyword) {
      if (!searchCache[keyword]) {
        try {
          const data = await storeApi.searchCategories(keyword)
          searchCache[keyword] = data.results || []
        } catch { searchCache[keyword] = [] }
      }
      return searchCache[keyword]
    }

    await Promise.all(products.value.map(async (product) => {
      const internal = String(product.internal_category || '').trim()
      if (!internal) {
        mappings[product.product_code] = { ...(mappings[product.product_code] || {}), status: '缺少内部类目' }
        return
      }
      try {
        const parts = internal.split('>').map((part) => part.trim()).filter(Boolean)
        const keyword = parts[parts.length - 1] || internal
        const candidates = (await searchLocal(keyword)).filter((item) => item.leaf)
        const current = mappings[product.product_code] || {}
        const freight = current.wechat_freight_template_id
          ? current
          : freightOptions.length === 1
            ? { ...current, wechat_freight_template_id: String(freightOptions[0].id || freightOptions[0].template_id || ''), wechat_freight_template: freightOptions[0].name || freightOptions[0].template_name || '' }
            : current
        // 历史映射只作为叶子类目提示，每次都用最新 cats_v2 候选刷新完整链路。
        const alias = aliasMap[internal]
        if (alias?.wechat?.category) {
          const savedChain = alias.wechat.chain || []
          const savedLeafId = String(savedChain[savedChain.length - 1]?.cat_id || '')
          const savedNames = savedChain.map((node) => String(node.name || '').trim())
          const refreshed = candidates.filter((item) => String(item.cat_id) === savedLeafId
            || (savedNames.length && (item.chain || []).map((node) => String(node.name || '').trim()).join('>') === savedNames.join('>')))
          if (refreshed.length === 1) {
            const latest = refreshed[0]
            mappings[product.product_code] = { ...freight, wechat_category: latest.path, wechat_category_chain: latest.chain || [], status: '微信类目已按最新 cats_v2 匹配' }
            if (JSON.stringify(savedChain) !== JSON.stringify(latest.chain || [])) {
              void saveGroupAlias(internal, { wechat: { ...alias.wechat, category: latest.path, chain: latest.chain || [] } })
            }
            return
          }
        }
        const exact = candidates.filter((item) => {
          const names = (item.chain || []).map((node) => String(node.name || '').trim())
          return parts.length <= names.length && parts.every((part, index) => names[names.length - parts.length + index] === part)
        })
        const match = exact.length === 1 ? exact[0] : null
        mappings[product.product_code] = {
          ...freight,
          wechat_category: match?.path || '',
          wechat_category_chain: match?.chain || [],
          wechat_candidates: candidates.map((item) => ({ path: item.path, chain: item.chain || [] })),
          status: match && freightOptions.length === 1 ? '微信配置已自动匹配' : match ? '微信类目已匹配，运费模板待确认' : candidates.length ? '微信类目待确认' : '微信类目匹配失败',
        }
      } catch {
        mappings[product.product_code] = { ...(mappings[product.product_code] || {}), status: '微信类目匹配失败' }
      }
    }))
    // 微信阶段完成先刷一次界面,即使小红书阶段慢也不会全程停在“待系统匹配”
    mapping.value = { ...mapping.value, products: mappings }
    await Promise.all(products.value.map(async (product) => {
      try {
        const alias = aliasMap[String(product.internal_category || '').trim()]
        if (alias?.xhs?.category_id) {
          mappings[product.product_code] = { ...(mappings[product.product_code] || {}), xhs_category: alias.xhs.category, xhs_category_chain: alias.xhs.chain || [], xhs_category_id: alias.xhs.category_id, xhs_status: '小红书类目已按映射表匹配' }
          return
        }
        mappings[product.product_code] = await autoMatchXhsCategory(product, mappings[product.product_code] || {})
      } catch { mappings[product.product_code] = { ...(mappings[product.product_code] || {}), xhs_status: '小红书类目匹配失败' } }
    }))
    const xhsBrandCache = {}
    await Promise.all(products.value.map(async (product) => {
      const current = mappings[product.product_code] || {}
      const categoryId = current.xhs_category_id
      let brandId = current.xhs_brand_id || ''
      if (categoryId && product.brand && !brandId) {
        try {
          // 与单独发布页保持一致：先读取该末级类目的完整店铺品牌列表，避免关键词查询漏品牌
          const cacheKey = String(categoryId)
          const brandData = xhsBrandCache[cacheKey] || await xhsApi.brands(categoryId)
          xhsBrandCache[cacheKey] = brandData
          const brands = listFrom(brandData, ['brands', 'brandList', 'list'])
          const normalizeBrand = (value) => String(value || '').replace(/[\s（）()·・]/g, '').toLowerCase()
          const targetBrand = normalizeBrand(product.brand)
          const exact = brands.filter((item) => normalizeBrand(optionName(item)) === targetBrand)
          if (exact.length === 1) brandId = settingId(exact[0])
        } catch { /* brand remains unconfirmed */ }
      }
      mappings[product.product_code] = { ...current, xhs_brand_id: brandId,
        xhs_shipping_template_id: current.xhs_shipping_template_id || batchPlatformSettings.xhsShippingId,
        xhs_logistics_plan_id: current.xhs_logistics_plan_id || batchPlatformSettings.xhsLogisticsId }
    }))
    mapping.value = { ...mapping.value, products: mappings }
    // 类目匹配完成后自动加载属性定义并匹配Excel属性值
    await loadGroupAttrDefs(true)
  } finally { categoryMatching.value = false }
}

async function autoMatchXhsCategory(product, mappingItem) {
  const parts = String(product.internal_category || '').split('>').map((part) => part.trim()).filter(Boolean)
  if (!parts.length) return { ...mappingItem, xhs_status: '缺少内部类目' }
  let parentId = null
  const chain = []
  for (const part of parts) {
    const data = await xhsApi.categories(parentId ? { category_id: parentId } : {})
    const result = data?.result || data || []
    const options = Array.isArray(result) ? result : (result.categoryV3s || result.categories || [])
    let matches = options.filter((item) => String(item.name || '').trim() === part)
    if (!matches.length) {
      // 精确匹配不上时做包含关系的模糊兜底,命中唯一才算自动匹配
      matches = options.filter((item) => {
        const name = String(item.name || '').trim()
        return name && (name.includes(part) || part.includes(name))
      })
    }
    if (matches.length !== 1) return { ...mappingItem, xhs_status: matches.length ? '小红书类目待确认' : '小红书类目匹配失败' }
    const selected = matches[0]
    chain.push({ id: selected.id || selected.categoryId, name: selected.name })
    parentId = selected.id || selected.categoryId
    if (!parentId) return { ...mappingItem, xhs_status: '小红书类目匹配失败' }
  }
  const leaf = chain[chain.length - 1]
  return { ...mappingItem, xhs_category: parts.join(' > '), xhs_category_chain: chain, xhs_category_id: leaf.id, xhs_status: '小红书类目已自动匹配' }
}

function getAttrGroupsNeedingConfig() {
  return mappingGroups.value.filter((g) => {
    const existing = groupAttrDefs[g.internal_category]
    if (existing?.wechat?.attr_defs?.length || existing?.xhs?.attr_defs?.length) return false
    return !!(g.mapping.wechat_category_chain?.length || g.mapping.xhs_category_id)
  })
}

async function loadGroupAttrDefs(silent = false) {
  const groups = getAttrGroupsNeedingConfig()
  if (!groups.length) { if (!silent) ElMessage.info('所有类目组已加载属性定义'); return }
  attrConfigLoading.value = true
  try {
    await Promise.all(groups.map(async (group) => {
      const config = { wechat: { attr_defs: [], defaults: {} }, xhs: { attr_defs: [], var_defs: [], defaults: {}, spec_map: {}, candidates: {} } }
      const wc = group.mapping.wechat_category_chain
      if (wc?.length) {
        try {
          const catId = String(wc[wc.length - 1].cat_id || '')
          if (catId) {
            const data = await storeApi.categoryDetail(catId)
            const attr = data?.result || data || {}
            config.wechat.attr_defs = attr.product_attr_list || []
            config.wechat.defaults = {};
            config.wechat.attr_defs.forEach((a) => { config.wechat.defaults[a.name] = a.type_v2 === 'select_many' ? [] : '' })
          }
        } catch (e) { /* wechat attr load failed */ }
      }
      const xid = group.mapping.xhs_category_id
      if (xid) {
        try {
          const [ad, vd] = await Promise.all([xhsApi.categoryAttributes(xid), xhsApi.categoryVariations(xid)])
          config.xhs.attr_defs = listFrom(ad, ['attributeV3s', 'attributes'])
          config.xhs.var_defs = listFrom(vd, ['variations'])
          config.xhs.defaults = {};
          config.xhs.attr_defs.forEach((a) => { config.xhs.defaults[a.id] = a.isMulti ? [] : '' })
          await Promise.all([...config.xhs.attr_defs, ...config.xhs.var_defs].map(async (d) => {
            try { const v = await xhsApi.attributeValues(xid, d.id || d.propertyId); config.xhs.candidates[d.id] = listFrom(v, ['attributeValueV3s', 'values']) } catch { config.xhs.candidates[d.id] = [] }
          }))
        } catch (e) { /* xhs attr load failed */ }
      }
      // 合并映射表已有的默认值(不被空值覆盖)
      const existing = groupAttrDefs[group.internal_category]
      if (existing?.wechat?.defaults) { for (const [k, v] of Object.entries(existing.wechat.defaults)) { if (v !== '' && !(Array.isArray(v) && !v.length)) config.wechat.defaults[k] = v } }
      if (existing?.xhs?.defaults) { for (const [k, v] of Object.entries(existing.xhs.defaults)) { if (v !== '' && !(Array.isArray(v) && !v.length)) config.xhs.defaults[k] = v } }
      if (existing?.xhs?.spec_map) Object.assign(config.xhs.spec_map, existing.xhs.spec_map)
      groupAttrDefs[group.internal_category] = config
      activeAttrGroups[group.internal_category] = true
    }))
    ElMessage.success(`已加载 ${groups.length} 个类目组的属性定义`)
    const result = autoMatchProductAttributes()
    if (result.matched + result.unmatched > 0) {
      ElMessage.success(`属性自动匹配：${result.matched} 个成功${result.unmatched ? `，${result.unmatched} 个待手动确认` : ''}`)
    }
  } finally { attrConfigLoading.value = false }
}

function getGroupAttrConfig(internalCategory) { return groupAttrDefs[internalCategory] || null }

// 微信 API 的 attr.value 可能是字符串或逗号/分号分隔字符串，需规范化为数组，避免 v-for 拆成单字符
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

function autoMatchProductAttributes() {
  let matched = 0, unmatched = 0
  for (const group of mappingGroups.value) {
    const config = groupAttrDefs[group.internal_category]
    if (!config) continue
    const groupProducts = products.value.filter((p) => (p.internal_category || '(未填写内部类目)') === group.internal_category)
    let gMatched = 0, gUnmatched = 0
    // 自动映射规格名 → 小红书规格维度（规格维度动态，按名称匹配）
    if (config.xhs.var_defs?.length && groupProducts.length) {
      const firstSku = groupProducts[0]?.skus?.[0]
      if (firstSku) {
        const specNames = (firstSku.specs || []).map((s) => s.name).filter(Boolean)
        for (const varDef of config.xhs.var_defs) {
          const hitName = specNames.find((n) => n === varDef.name || varDef.name.includes(n) || n.includes(varDef.name))
          if (hitName) config.xhs.spec_map[varDef.id] = hitName
        }
      }
    }
    // 每个商品的属性匹配
    for (const product of groupProducts) {
      const excelAttrs = product.attributes || {}
      const prodMapping = mapping.value.products?.[product.product_code] || {}
      const wxAttrs = { ...(prodMapping.wechat_attrs || {}) }
      const xhsAttrs = { ...(prodMapping.xhs_attrs || {}) }
      for (const [name, val] of Object.entries(excelAttrs)) {
        // 微信: 名称匹配(精确+模糊)，值直接用文本
        const wxDef = (config.wechat.attr_defs || []).find((d) => d.name === name)
          || (config.wechat.attr_defs || []).find((d) => d.name.includes(name) || name.includes(d.name))
        if (wxDef) { wxAttrs[wxDef.name] = val; gMatched++ } else { gUnmatched++ }
        // 小红书: 名称匹配(精确+模糊) + 候选值匹配 valueId
        const xhsDef = (config.xhs.attr_defs || []).find((d) => d.name === name)
          || (config.xhs.attr_defs || []).find((d) => d.name.includes(name) || name.includes(d.name))
        if (xhsDef) {
          const cands = config.xhs.candidates[xhsDef.id] || []
          const hit = cands.find((c) => c.valueName === val) || cands.find((c) => c.valueName.includes(val) || val.includes(c.valueName))
          if (hit) { xhsAttrs[xhsDef.id] = { propertyId: xhsDef.id, name: xhsDef.name, valueId: hit.valueId, value: hit.valueName }; gMatched++ }
          else if (xhsDef.inputType === 0 || xhsDef.customizable) { xhsAttrs[xhsDef.id] = { propertyId: xhsDef.id, name: xhsDef.name, valueId: '', value: val }; gMatched++ }
          else { gUnmatched++ }
        } else { gUnmatched++ }
      }
      // SKU 规格值 → 小红书 valueId
      const skuVariants = {}
      if (config.xhs.var_defs?.length) {
        for (const sku of (product.skus || [])) {
          const variants = []
          for (const varDef of config.xhs.var_defs) {
            const mapKey = config.xhs.spec_map[varDef.id]
            if (!mapKey) continue
            const specVal = resolveSpecValue(sku, mapKey)
            if (!specVal) continue
            const cands = config.xhs.candidates[varDef.id] || []
            const hit = cands.find((c) => c.valueName === specVal) || cands.find((c) => c.valueName.includes(specVal) || specVal.includes(c.valueName))
            variants.push(hit ? { id: varDef.id, name: varDef.name, value: hit.valueName, valueId: hit.valueId } : { id: varDef.id, name: varDef.name, value: specVal, valueId: '' })
          }
          if (variants.length) skuVariants[sku.sku_code] = variants
        }
      }
      // 更新组级默认显示(用第一个商品的值)
      if (product === groupProducts[0]) {
        Object.assign(config.wechat.defaults, wxAttrs)
        for (const [id, attr] of Object.entries(xhsAttrs)) { if (attr.valueId) config.xhs.defaults[id] = attr.valueId }
      }
      mapping.value.products[product.product_code] = { ...prodMapping, wechat_attrs: wxAttrs, xhs_attrs: xhsAttrs, xhs_sku_variants: skuVariants }
    }
    matched += gMatched; unmatched += gUnmatched
    attrMatchStatus[group.internal_category] = { matched: gMatched, unmatched: gUnmatched }
  }
  return { matched, unmatched }
}
function ensureGroupConfig(key) {
  if (!groupAttrDefs[key]) groupAttrDefs[key] = { wechat: { attr_defs: [], defaults: {} }, xhs: { attr_defs: [], var_defs: [], defaults: {}, spec_map: {}, candidates: {} } }
  return groupAttrDefs[key]
}
function setGroupWxAttr(key, name, value) { ensureGroupConfig(key).wechat.defaults[name] = value }
function setGroupXhsAttr(key, attrId, value) { ensureGroupConfig(key).xhs.defaults[attrId] = value }
function setGroupSpecMap(key, varId, col) { ensureGroupConfig(key).xhs.spec_map[varId] = col }
// 规格映射取值：兼容旧的 'specN_value' 列名与新的规格维度名称
function resolveSpecValue(sku, mapKey) {
  if (!mapKey) return ''
  const specs = sku?.specs || []
  const legacy = /^spec(\d+)_value$/.exec(mapKey)
  if (legacy) return specs[Number(legacy[1]) - 1]?.value || ''
  const hit = specs.find((s) => s.name === mapKey)
  return hit?.value || ''
}
// 某内部类目组下所有出现过的规格维度名称（按列序去重），用于规格映射下拉
function groupSpecDimensions(catKey) {
  const names = []
  for (const p of products.value) {
    if ((p.internal_category || '(未填写内部类目)') !== catKey) continue
    for (const sku of (p.skus || [])) {
      for (const sp of (sku.specs || [])) {
        if (sp.name && !names.includes(sp.name)) names.push(sp.name)
      }
    }
  }
  return names
}
const requiredAttrNames = computed(() => {
  const names = new Set()
  for (const config of Object.values(groupAttrDefs)) {
    for (const a of (config.wechat?.attr_defs || [])) if (a.is_required) names.add(a.name)
  }
  return [...names]
})
function getProductAttrValue(product, attrName) {
  const ov = mapping.value.products?.[product.product_code]?.wechat_attrs?.[attrName]
  if (ov !== undefined && ov !== '') return ov
  const key = product.internal_category
  return groupAttrDefs[key]?.wechat?.defaults?.[attrName] || ''
}

watch(step, (value) => {
  if (value === 2) {
    void loadPlatformSettings(); void autoMatchWechatCategories()
    if (mapping.value.attr_groups) Object.assign(groupAttrDefs, mapping.value.attr_groups)
  }
})

function readAsBase64(input) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '')
    reader.onerror = reject
    reader.readAsDataURL(input)
  })
}

async function readFolderFiles(fileList) {
  const files = Array.from(fileList || []).filter((item) => item.type?.startsWith('image/') || /\.(jpe?g|png|gif|webp)$/i.test(item.name))
  folderFiles.value = await Promise.all(files.map(async (item) => ({
    path: item.webkitRelativePath || item.name,
    name: item.name,
    content_base64: await readAsBase64(item),
  })))
}

async function readCommonDetailFiles(fileList) {
  const files = Array.from(fileList || []).filter((item) => item.type?.startsWith('image/') || /\.(jpe?g|png|gif|webp)$/i.test(item.name))
  commonDetailFiles.value = await Promise.all(files.map(async (item) => ({
    path: item.webkitRelativePath || item.name,
    name: item.name,
    content_base64: await readAsBase64(item),
  })))
}

async function downloadTemplate() {
  try {
    const response = await bulkApi.template()
    const url = URL.createObjectURL(response)
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = '商品批量发布模板.xlsx'; anchor.click(); URL.revokeObjectURL(url)
  } catch (error) { ElMessage.error(error.message) }
}

async function importFile() {
  clearJobProgress()
  if (!file.value) return ElMessage.warning('请先选择 .xlsx 或 .csv 文件')
  loading.value = true
  try {
    const data = await bulkApi.importBatch({ filename: file.value.name, content_base64: await readAsBase64(file.value), folder_files: folderFiles.value, common_detail_files: commonDetailFiles.value })
    batch.value = (await bulkApi.getBatch(data.batch_id)).result
    itemKeyword.value = ''
    itemErrorFilter.value = 'all'
    resetItemPage()
    step.value = 1
    ElMessage.success(`已导入 ${data.total} 行 SKU`)
  } catch (error) { ElMessage.error(error.message) } finally { loading.value = false }
}

async function validateBatch() {
  loading.value = true
  try { await bulkApi.validate(batch.value.id); batch.value = (await bulkApi.getBatch(batch.value.id)).result; ElMessage.success('校验完成'); step.value = 2 } catch (error) { ElMessage.error(error.message) } finally { loading.value = false }
}

async function saveAndNext() {
  loading.value = true
  try {
    await bulkApi.updateItems(batch.value.id, allItems.value)
    const productsMapping = Object.fromEntries(products.value.map((product) => [product.product_code, {
      ...(mapping.value.products?.[product.product_code] || {}),
      internal_category: product.internal_category,
      xhs_shipping_template_id: mapping.value.products?.[product.product_code]?.xhs_shipping_template_id || batchPlatformSettings.xhsShippingId,
      xhs_logistics_plan_id: mapping.value.products?.[product.product_code]?.xhs_logistics_plan_id || batchPlatformSettings.xhsLogisticsId,
      wechat_freight_template_id: mapping.value.products?.[product.product_code]?.wechat_freight_template_id || batchPlatformSettings.wechatFreightId,
      status: '待系统匹配',
    }]))
    for (const code of Object.keys(productsMapping)) {
      const previous = mapping.value.products?.[code]
      if (previous?.status) productsMapping[code].status = previous.status
    }
    const attrGroupsSnapshot = {}
    for (const [key, config] of Object.entries(groupAttrDefs)) {
      attrGroupsSnapshot[key] = { wechat: { attr_defs: config.wechat?.attr_defs || [], defaults: { ...(config.wechat?.defaults || {}) } }, xhs: { attr_defs: config.xhs?.attr_defs || [], var_defs: config.xhs?.var_defs || [], defaults: { ...(config.xhs?.defaults || {}) }, spec_map: { ...(config.xhs?.spec_map || {}) }, candidates: { ...(config.xhs?.candidates || {}) } } }
    }
    mapping.value = { mode: 'auto', products: productsMapping, attr_groups: attrGroupsSnapshot }
    await bulkApi.updateMappings(batch.value.id, mapping.value)
    step.value = 3
  } catch (error) { ElMessage.error(error.message) } finally { loading.value = false }
}

function openReview(product) {
  reviewProduct.value = product
  reviewVisible.value = true
}
function handleReviewSave(data) {
  const code = data.product_code
  const m = mapping.value.products?.[code] || {}
  mapping.value.products[code] = { ...m, wechat_attrs: data.wechat_attrs, xhs_attrs: data.xhs_attrs, wechat_title: data.wechat_title, xhs_title: data.xhs_title }
  // 同步更新 products 里的基础字段
  const prod = products.value.find((p) => p.product_code === code)
  if (prod) {
    prod.title = data.title; prod.brand = data.brand; prod.description = data.description
    // SKU 行内编辑回写：同步价格/库存/规格值，并重算汇总字段
    if (Array.isArray(data.skus)) {
      prod.skus = data.skus.map((sku) => ({ ...sku, specs: (sku.specs || []).map((s) => ({ ...s })) }))
      // 同步审核抽屉里自由增删的规格维度，保证下次打开与规格映射下拉一致
      if (Array.isArray(data.spec_dimensions)) prod.spec_dimensions = [...data.spec_dimensions]
      const prices = prod.skus.map((sku) => Number(sku.price)).filter(Number.isFinite)
      const stocks = prod.skus.map((sku) => Number(sku.stock)).filter(Number.isFinite)
      prod.price_min = prices.length ? Math.min(...prices) : null
      prod.price_max = prices.length ? Math.max(...prices) : null
      prod.stock_total = stocks.length ? stocks.reduce((sum, n) => sum + n, 0) : null
    }
  }
  for (const row of allItems.value.filter((item) => item.product_code === code)) {
    row.title = data.title
    row.brand = data.brand
    row.description = data.description
    row.weight = data.weight
    const sku = data.skus?.find((item) => item.sku_code === row.sku_code)
    if (sku) {
      row.price = sku.price
      row.original_price = sku.original_price
      row.stock = sku.stock
      row.specs = (sku.specs || []).map((spec) => ({ ...spec }))
    }
  }
}
function getReviewGroupConfig(product) {
  return groupAttrDefs[product?.internal_category] || null
}

function jobStatusText(status) {
  return ({ queued: '排队中', running: '发布中', success: '成功', failed: '失败', partial: '部分成功', completed: '已完成', completed_with_errors: '已完成（有失败）' })[status] || status
}
async function pollJob(jobId) {
  try {
    if (jobTimer) { clearTimeout(jobTimer); jobTimer = null }
    currentJob.value = (await bulkApi.job(jobId)).result
    if (!['completed', 'completed_with_errors'].includes(currentJob.value.status)) jobTimer = setTimeout(() => pollJob(jobId), 1500)
  } catch (error) { ElMessage.error(`读取发布进度失败：${error.message}`) }
}

async function retryFailedItems(itemIds = []) {
  if (!currentJob.value?.job_id || retrying.value) return
  retrying.value = true
  try {
    const result = await bulkApi.retry(currentJob.value.job_id, itemIds)
    ElMessage.success(result.message || '失败商品已重新排队')
    await pollJob(currentJob.value.job_id)
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    retrying.value = false
  }
}
onBeforeUnmount(() => { if (jobTimer) clearTimeout(jobTimer) })

async function publish() {
  if (errorCount.value) return ElMessage.warning('请先修正全部校验错误')
  loading.value = true
  try {
    await bulkApi.updateItems(batch.value.id, allItems.value)
    await bulkApi.updateMappings(batch.value.id, mapping.value)
    const result = await bulkApi.publish(batch.value.id, platforms.value)
    batch.value.status = '发布任务已创建'
    ElMessage.success(`已创建发布任务 ${result.job_id}`)
    void pollJob(result.job_id)
  } catch (error) { ElMessage.error(error.message) } finally { loading.value = false }
}
</script>

<template>
  <div class="bulk-view">
    <div class="page-heading"><div><h1>批量发布商品</h1><p>一次导入标准商品，分别映射并发布到微信和小红书</p></div><el-button :icon="Download" @click="downloadTemplate">下载模板</el-button></div>
    <el-steps :active="step" finish-status="success" class="bulk-steps"><el-step v-for="title in steps" :key="title" :title="title" /></el-steps>
    <div v-if="step === 1" class="bulk-filters">
      <el-input v-model="itemKeyword" clearable :prefix-icon="Search" placeholder="搜索商品编码、标题或 SKU" @clear="resetItemPage" @input="resetItemPage" />
      <el-select v-model="itemErrorFilter" style="width: 150px" @change="resetItemPage"><el-option label="全部记录" value="all" /><el-option label="仅看异常" value="error" /><el-option label="仅看通过" value="valid" /></el-select>
      <span class="muted-copy">匹配 {{ filteredItems.length }} 条</span>
    </div>
    <div v-if="step === 2" class="bulk-filters">
      <el-button type="primary" :loading="categoryMatching" @click="autoMatchWechatCategories">立即匹配微信/小红书类目</el-button>
      <el-button :loading="attrConfigLoading" @click="loadGroupAttrDefs()">重新加载属性</el-button>
      <el-button @click="aliasPanelVisible = true">核对类目映射表</el-button>
      <span class="muted-copy">系统按一级、二级、末级叶子类目逐级匹配</span>
    </div>
    <div v-if="step === 2" class="bulk-filters">
      <span>微信本批次设置</span>
      <el-select v-model="batchPlatformSettings.wechatFreightId" placeholder="选择微信运费模板" style="width:220px"><el-option v-for="item in (platformSettings.wechatFreight || [])" :key="settingId(item)" :label="optionName(item)" :value="settingId(item)" /></el-select>
      <span>小红书本批次设置</span>
      <el-select v-model="batchPlatformSettings.xhsShippingId" placeholder="选择运费模板" style="width:220px"><el-option v-for="item in platformSettings.xhsShipping" :key="settingId(item)" :label="optionName(item)" :value="settingId(item)" /></el-select>
      <el-select v-model="batchPlatformSettings.xhsLogisticsId" placeholder="选择物流方案" style="width:220px"><el-option v-for="item in platformSettings.xhsLogistics" :key="settingId(item)" :label="optionName(item)" :value="settingId(item)" /></el-select>
    </div>
    <div v-if="step === 2 && mappingGroups.some((group) => group.mapping.wechat_candidates?.length > 1)" class="mapping-confirm-list">
      <div v-for="group in mappingGroups.filter((item) => item.mapping.wechat_candidates?.length > 1)" :key="group.internal_category" class="mapping-confirm-row">
        <span>{{ group.internal_category }}（{{ group.products.length }} 个商品）</span>
        <el-select placeholder="选择微信叶子类目" @change="(value) => confirmWechatCategory(group, value)"><el-option v-for="candidate in group.mapping.wechat_candidates" :key="candidate.path" :label="candidate.path" :value="candidate.path" /></el-select>
      </div>
    </div>
    <div v-if="step === 2" class="mapping-confirm-list">
      <div v-for="group in mappingGroups.filter((item) => item.mapping.xhs_status && !item.mapping.xhs_status.includes('已自动') && !item.mapping.xhs_status.includes('已人工') && !item.mapping.xhs_status.includes('已按映射表'))" :key="`xhs-${group.internal_category}`" class="mapping-confirm-row">
        <span>小红书：{{ group.internal_category }}（{{ group.products.length }} 个商品）</span>
        <el-button size="small" @click="loadXhsLevel(group, 0)">读取类目</el-button>
        <template v-for="(options, level) in (xhsCascade[group.internal_category]?.levels || [])" :key="level"><el-select :model-value="xhsCascade[group.internal_category]?.selected?.[level]" :placeholder="`第${level + 1}级类目`" @change="(value) => confirmXhsCategory(group, level, value)"><el-option v-for="item in options" :key="item.id || item.categoryId" :label="item.name" :value="item.id || item.categoryId" /></el-select></template>
      </div>
    </div>
    <div v-if="step === 2 && Object.keys(groupAttrDefs).length" class="mapping-confirm-list">
      <div v-for="group in mappingGroups.filter((g) => groupAttrDefs[g.internal_category])" :key="`attr-${group.internal_category}`" class="attr-config-group">
        <div class="mapping-confirm-row" style="flex-wrap: wrap; gap: 8px">
          <strong>{{ group.internal_category }}</strong>
          <span class="muted-copy">（{{ group.products.length }} 个商品）</span>
          <el-tag v-if="attrMatchStatus[group.internal_category]" size="small" :type="attrMatchStatus[group.internal_category].unmatched ? 'warning' : 'success'">属性匹配 {{ attrMatchStatus[group.internal_category].matched }}✓{{ attrMatchStatus[group.internal_category].unmatched ? ` / ${attrMatchStatus[group.internal_category].unmatched}✗` : '' }}</el-tag>
          <el-button size="small" text @click="activeAttrGroups[group.internal_category] = !activeAttrGroups[group.internal_category]">{{ activeAttrGroups[group.internal_category] ? '收起' : '展开' }}</el-button>
        </div>
        <div v-if="activeAttrGroups[group.internal_category]" style="padding: 12px 0">
          <div v-if="groupAttrDefs[group.internal_category]?.wechat?.attr_defs?.length" style="margin-bottom: 16px">
            <h4 style="margin: 0 0 8px">微信商品属性 <small class="muted-copy">(已从 Excel “商品属性”列自动匹配，未匹配的可手动填写)</small></h4>
            <div class="form-grid form-grid-2">
              <div v-for="a in groupAttrDefs[group.internal_category].wechat.attr_defs" :key="a.name" class="attr-config-item">
                <label>{{ a.is_required ? '* ' : '' }}{{ a.name }}<small v-if="a.type_v2" class="muted-copy"> ({{ wxTypeLabel(a.type_v2) }})</small></label>
                <el-select v-if="a.type_v2 === 'select_many'" multiple :model-value="groupAttrDefs[group.internal_category].wechat.defaults[a.name]" placeholder="请选择" @update:model-value="(v) => setGroupWxAttr(group.internal_category, a.name, v)"><el-option v-for="opt in attrOptions(a.value)" :key="opt" :label="opt" :value="opt" /></el-select>
                <el-select v-else-if="a.type_v2 === 'select_one'" :model-value="groupAttrDefs[group.internal_category].wechat.defaults[a.name]" placeholder="请选择" @update:model-value="(v) => setGroupWxAttr(group.internal_category, a.name, v)"><el-option v-for="opt in attrOptions(a.value)" :key="opt" :label="opt" :value="opt" /></el-select>
                <el-input-number v-else-if="a.type_v2?.includes('integer') || a.type_v2?.includes('decimal')" :model-value="groupAttrDefs[group.internal_category].wechat.defaults[a.name]" @update:model-value="(v) => setGroupWxAttr(group.internal_category, a.name, v)" />
                <el-input v-else :model-value="groupAttrDefs[group.internal_category].wechat.defaults[a.name]" placeholder="请输入" @update:model-value="(v) => setGroupWxAttr(group.internal_category, a.name, v)" />
              </div>
            </div>
          </div>
          <div v-if="groupAttrDefs[group.internal_category]?.xhs?.attr_defs?.length" style="margin-bottom: 16px">
            <h4 style="margin: 0 0 8px">小红书商品属性 <small class="muted-copy">(已自动匹配属性值，未匹配的可手动选择)</small></h4>
            <div class="form-grid form-grid-2">
              <div v-for="a in groupAttrDefs[group.internal_category].xhs.attr_defs" :key="a.id" class="attr-config-item">
                <label>{{ a.isRequired ? '* ' : '' }}{{ a.name }}<small v-if="a.isMulti" class="muted-copy"> (多选)</small></label>
                <el-select v-if="a.isMulti" multiple :model-value="groupAttrDefs[group.internal_category].xhs.defaults[a.id]" placeholder="请选择" @update:model-value="(v) => setGroupXhsAttr(group.internal_category, a.id, v)"><el-option v-for="c in (groupAttrDefs[group.internal_category].xhs.candidates[a.id] || [])" :key="c.valueId" :label="c.valueName" :value="c.valueId" /></el-select>
                <el-select v-else-if="a.inputType === 1 && (groupAttrDefs[group.internal_category].xhs.candidates[a.id] || []).length" :model-value="groupAttrDefs[group.internal_category].xhs.defaults[a.id]" placeholder="请选择" @update:model-value="(v) => setGroupXhsAttr(group.internal_category, a.id, v)"><el-option v-for="c in (groupAttrDefs[group.internal_category].xhs.candidates[a.id] || [])" :key="c.valueId" :label="c.valueName" :value="c.valueId" /></el-select>
                <el-input v-else :model-value="groupAttrDefs[group.internal_category].xhs.defaults[a.id]" placeholder="请输入" @update:model-value="(v) => setGroupXhsAttr(group.internal_category, a.id, v)" />
              </div>
            </div>
          </div>
          <div v-if="groupAttrDefs[group.internal_category]?.xhs?.var_defs?.length">
            <h4 style="margin: 0 0 8px">小红书规格映射 <small class="muted-copy">(已自动按名称匹配，可手动调整)</small></h4>
            <p class="muted-copy" style="margin: 0 0 8px">Excel 规格列（规格1、规格2、规格3…可自由增减）→ 小红书规格维度，SKU 规格值已自动翻译为平台属性值</p>
            <div class="form-grid form-grid-2">
              <div v-for="v in groupAttrDefs[group.internal_category].xhs.var_defs" :key="v.id" class="attr-config-item">
                <label>{{ v.name }}</label>
                <el-select :model-value="groupAttrDefs[group.internal_category].xhs.spec_map[v.id] || ''" placeholder="不映射" @update:model-value="(val) => setGroupSpecMap(group.internal_category, v.id, val)"><el-option label="不映射" value="" /><el-option v-for="dim in groupSpecDimensions(group.internal_category)" :key="dim" :label="dim" :value="dim" /></el-select>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
    <section v-if="step === 0" class="content-panel bulk-card"><div class="bulk-drop"><el-icon><Upload /></el-icon><h3>选择商品 Excel</h3><p>每个 SKU 一行；.xlsx 支持主图和详情图列中的内嵌图片或公网图片链接</p><input type="file" accept=".xlsx,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv" @change="file = $event.target.files[0]" /><strong v-if="file">{{ file.name }}</strong></div><el-button type="primary" :loading="loading" @click="importFile">导入并校验 <el-icon><Right /></el-icon></el-button></section>
    <section v-else-if="step === 1" class="content-panel bulk-card"><div class="bulk-summary"><span>批次 {{ batch.id }}</span><el-tag type="success">可发布 {{ validCount }}</el-tag><el-tag type="danger">错误 {{ errorCount }}</el-tag></div><el-table :data="items" max-height="520" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }"><el-table-column prop="line" label="行" width="70" /><el-table-column prop="product_code" label="商品编码" width="150" show-overflow-tooltip /><el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip /><el-table-column prop="sku_code" label="SKU编码" width="150" show-overflow-tooltip /><el-table-column prop="price" label="售价" width="100" /><el-table-column prop="stock" label="库存" width="90" /><el-table-column label="校验结果" min-width="220" show-overflow-tooltip><template #default="{ row }"><el-tag v-if="row.errors?.length" type="danger">{{ row.errors.join('；') }}</el-tag><el-tag v-else type="success">通过</el-tag></template></el-table-column><template v-if="requiredAttrNames.length"><el-table-column v-for="attrName in requiredAttrNames" :key="attrName" :label="`*${attrName}`" width="160" show-overflow-tooltip><template #default="{ row }"><span class="muted-copy">{{ getProductAttrValue(row, attrName) || '继承组级默认' }}</span></template></el-table-column></template></el-table><div class="form-actions"><el-button @click="step = 0">重新导入</el-button><el-button type="primary" :loading="loading" @click="validateBatch">重新校验并继续</el-button></div></section>
    <section v-else-if="step === 2" class="content-panel bulk-card"><h3>平台映射</h3><p class="muted-copy">系统会按每个商品的 Excel 内部类目分别匹配微信和小红书类目，运营不需要填写全店统一类目。匹配不到的商品会单独标记。</p><el-table :data="mappingRows" max-height="520" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }"><el-table-column prop="product_code" label="商品编码" width="170" show-overflow-tooltip /><el-table-column prop="internal_category" label="Excel 内部类目" min-width="240" show-overflow-tooltip /><el-table-column label="微信类目" min-width="180" show-overflow-tooltip><template #default="{ row }"><el-tag type="info">{{ row.mapping.wechat_category || '待系统匹配' }}</el-tag></template></el-table-column><el-table-column label="小红书类目" min-width="180" show-overflow-tooltip><template #default="{ row }"><el-tag type="info">{{ row.mapping.xhs_category || '待系统匹配' }}</el-tag></template></el-table-column><el-table-column label="匹配状态" width="130" show-overflow-tooltip><template #default="{ row }"><el-tag type="warning">{{ row.mapping.status || '待处理' }}</el-tag></template></el-table-column><template v-if="requiredAttrNames.length"><el-table-column v-for="attrName in requiredAttrNames" :key="attrName" :label="`*${attrName}`" width="140" show-overflow-tooltip><template #default="{ row }"><span class="muted-copy">{{ getProductAttrValue(row, attrName) || '未配置' }}</span></template></el-table-column></template></el-table><div class="form-actions"><el-button @click="step = 1">返回</el-button><el-button type="primary" :loading="loading" @click="saveAndNext">保存自动映射并继续</el-button></div></section>
    <section v-else class="content-panel bulk-card"><h3>确认发布</h3><p>默认先创建微信草稿；小红书创建商品和 SKU 后进入审核，不自动上架。</p><el-alert v-if="productWarningCount" type="warning" :closable="false" show-icon :title="`${productWarningCount} 个商品存在 SKU 间属性不一致提醒`" description="商品属性是 SPU 级，各 SKU 行填了不同值时仅第一行生效。点“审核编辑”查看具体提醒，必要时把该差异提升为规格维度。" style="margin-bottom:12px" /><el-checkbox-group v-model="platforms"><el-checkbox label="wechat">微信</el-checkbox><el-checkbox label="xhs">小红书</el-checkbox></el-checkbox-group><el-alert v-if="errorCount" type="error" :closable="false" show-icon title="仍有校验错误，不能发布" /><div class="bulk-summary"><span>商品行数：{{ products.length }}</span><span>通过：{{ validCount }}</span><span>错误：{{ errorCount }}</span><el-tag v-if="batch.status">{{ batch.status }}</el-tag></div><el-table :data="products" max-height="360" style="margin-bottom:16px" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }"><el-table-column prop="product_code" label="商品编码" width="150" show-overflow-tooltip /><el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip /><el-table-column prop="internal_category" label="内部类目" min-width="180" show-overflow-tooltip /><el-table-column label="属性状态" width="130" show-overflow-tooltip><template #default="{ row }"><el-tag :type="(mapping.products?.[row.product_code]?.wechat_attrs && Object.keys(mapping.products[row.product_code].wechat_attrs).length) ? 'success' : 'info'" size="small">{{ Object.keys(mapping.products?.[row.product_code]?.wechat_attrs || {}).length + Object.keys(mapping.products?.[row.product_code]?.xhs_attrs || {}).length }} 项已配</el-tag></template></el-table-column><el-table-column label="SKU 提醒" width="110"><template #default="{ row }"><el-tooltip v-if="row.warnings?.length" placement="top"><template #content><div v-for="(w, i) in row.warnings" :key="i">{{ w }}</div></template><el-tag type="warning" size="small">{{ row.warnings.length }} 项不一致</el-tag></el-tooltip><span v-else class="muted-copy">--</span></template></el-table-column><el-table-column label="操作" width="100" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="openReview(row)">审核编辑</el-button></template></el-table-column></el-table><div class="form-actions"><el-button @click="step = 2">返回映射</el-button><el-button type="primary" :loading="loading" :disabled="!platforms.length || errorCount > 0" :icon="Check" @click="publish">创建批量发布任务</el-button></div></section>
    <section v-if="currentJob" class="content-panel bulk-card" style="margin-top:16px">
      <div class="bulk-summary"><strong>发布进度</strong><span>{{ currentJob.processed }}/{{ currentJob.total }}</span><el-tag>{{ jobStatusText(currentJob.status) }}</el-tag><el-button v-if="currentJob.items?.some((item) => item.status === 'failed')" size="small" type="warning" :loading="retrying" @click="retryFailedItems()">重试全部失败</el-button></div>
      <el-progress :percentage="currentJob.total ? Math.round(currentJob.processed * 100 / currentJob.total) : 0" />
      <el-table :data="currentJob.items" max-height="320" style="margin-top:12px" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }">
        <el-table-column prop="product_code" label="商品编码" show-overflow-tooltip />
        <el-table-column label="平台" width="100"><template #default="{ row }">{{ row.platform === 'wechat' ? '微信' : '小红书' }}</template></el-table-column>
        <el-table-column label="状态" width="130" show-overflow-tooltip><template #default="{ row }"><el-tag :type="row.status === 'success' ? 'success' : row.status === 'failed' || row.status === 'partial' ? 'danger' : 'info'">{{ jobStatusText(row.status) }}</el-tag></template></el-table-column>
        <el-table-column prop="error" label="失败原因" min-width="280" show-overflow-tooltip />
        <el-table-column label="操作" width="90" fixed="right"><template #default="{ row }"><el-button v-if="row.status === 'failed'" link type="primary" :loading="retrying" @click="retryFailedItems([row.id])">重试</el-button><span v-else class="muted-copy">--</span></template></el-table-column>
      </el-table>
    </section>
    <ProductReviewDrawer v-model:visible="reviewVisible" :product="reviewProduct" :product-mapping="mapping.products?.[reviewProduct?.product_code] || {}" :group-config="getReviewGroupConfig(reviewProduct)" :platforms="platforms" @save="handleReviewSave" />
    <div v-if="step === 1" class="bulk-pagination"><el-pagination v-model:current-page="itemPage" v-model:page-size="itemPageSize" :page-sizes="[20, 50, 100]" layout="total, sizes, prev, pager, next, jumper" :total="filteredItems.length" /></div>
    <el-dialog v-model="aliasPanelVisible" title="类目映射表" width="920px">
      <CategoryAliasPanel dialog />
    </el-dialog>
    <div v-if="step === 0" class="folder-upload" style="margin-top: 12px"><label class="el-button el-button--default"><span>选择商品图片文件夹</span><input type="file" webkitdirectory directory multiple accept="image/*" hidden @change="readFolderFiles($event.target.files)" /></label><span v-if="folderFiles.length" class="muted-copy">已选择 {{ folderFiles.length }} 张商品图片</span><label class="el-button el-button--default" style="margin-left: 8px"><span>选择通用详情图文件夹</span><input type="file" webkitdirectory directory multiple accept="image/*" hidden @change="readCommonDetailFiles($event.target.files)" /></label><span v-if="commonDetailFiles.length" class="muted-copy">已选择 {{ commonDetailFiles.length }} 张通用详情图</span><span v-if="!folderFiles.length && !commonDetailFiles.length" class="muted-copy">可选，系统会自动按商品编号匹配主图并追加通用详情图</span></div>
  </div>
</template>
