<script setup>
// 类目映射表维护:“内部类目 → 平台类目”对照关系的查看、批量导入、新增和删除。
// 独立页面从侧边栏进入;批量发布页核对类目时以弹窗方式打开本组件。
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Delete, Download, Plus } from '@element-plus/icons-vue'
import { bulkApi } from '../bulkApi'
import { xhsApi } from '../xhsApi'
import { storeApi } from '../api'

// el-dialog 首次打开才挂载内部组件,弹窗和独立页面两种模式都在挂载时拉列表即可
defineProps({ dialog: { type: Boolean, default: false } })

const aliasList = ref([])
const listLoading = ref(false)
const aliasFile = ref(null)
const aliasImporting = ref(false)
const aliasImportDialogVisible = ref(false)
const aliasImportResult = ref(null)
const aliasFormVisible = ref(false)
const aliasFormSaving = ref(false)
const aliasForm = ref({ internal_category: '', wechat: null, xhs: null })
const aliasWechatOptions = ref([])
const aliasWechatLoading = ref(false)
const aliasXhsLevels = ref([])
const aliasXhsSelected = ref([])

async function loadAliasList() {
  listLoading.value = true
  try { aliasList.value = (await bulkApi.categoryAliases()).result || [] } catch { aliasList.value = [] } finally { listLoading.value = false }
}

async function removeAlias(alias) {
  try {
    await bulkApi.deleteCategoryAlias(alias.id)
    ElMessage.success('已删除类目映射')
    void loadAliasList()
  } catch (error) { ElMessage.error(error.message) }
}

async function downloadAliasTemplate() {
  try {
    const response = await bulkApi.categoryAliasTemplate()
    const url = URL.createObjectURL(response)
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = '类目映射表模板.xlsx'; anchor.click(); URL.revokeObjectURL(url)
  } catch (error) { ElMessage.error(error.message) }
}

function readAsBase64(input) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '')
    reader.onerror = reject
    reader.readAsDataURL(input)
  })
}

async function importAliasFile() {
  if (!aliasFile.value) return ElMessage.warning('请先选择映射表 .xlsx 或 .csv 文件')
  aliasImporting.value = true
  try {
    const data = await bulkApi.importCategoryAliases({ filename: aliasFile.value.name, content_base64: await readAsBase64(aliasFile.value) })
    aliasImportResult.value = data.result || {}
    aliasImportDialogVisible.value = true
    aliasFile.value = null
    void loadAliasList()
  } catch (error) { ElMessage.error(error.message) } finally { aliasImporting.value = false }
}

function aliasReportType(message) {
  if (message === '成功') return 'success'
  if (!message || message === '未填写' || message.startsWith('跳过')) return 'info'
  return 'danger'
}

function openAliasForm() {
  aliasForm.value = { internal_category: '', wechat: null, xhs: null }
  aliasWechatOptions.value = []
  aliasXhsLevels.value = []
  aliasXhsSelected.value = []
  aliasFormVisible.value = true
}

async function searchAliasWechat(keyword) {
  if (!String(keyword || '').trim()) return
  aliasWechatLoading.value = true
  try {
    const data = await storeApi.searchCategories(String(keyword).trim())
    aliasWechatOptions.value = (data.results || []).filter((item) => item.leaf)
  } catch (error) { ElMessage.error(error.message) } finally { aliasWechatLoading.value = false }
}

function chooseAliasWechat(path) {
  const candidate = aliasWechatOptions.value.find((item) => item.path === path)
  aliasForm.value.wechat = candidate ? { category: candidate.path, chain: candidate.chain || [] } : null
}

async function loadAliasXhsLevel(level, parentId = null) {
  const data = await xhsApi.categories(parentId ? { category_id: parentId } : {})
  const result = data?.result || data || []
  const options = Array.isArray(result) ? result : (result.categoryV3s || result.categories || [])
  aliasXhsLevels.value = aliasXhsLevels.value.slice(0, level).concat([options])
  aliasXhsSelected.value = aliasXhsSelected.value.slice(0, level)
}

function chooseAliasXhs(level, value) {
  aliasXhsSelected.value = [...aliasXhsSelected.value.slice(0, level), value]
  const item = aliasXhsLevels.value[level].find((row) => String(row.id || row.categoryId) === String(value))
  if (!item) return
  if (!(item.isLeaf || item.leaf)) { aliasForm.value.xhs = null; void loadAliasXhsLevel(level + 1, value); return }
  const chain = aliasXhsSelected.value.map((id, index) => { const row = aliasXhsLevels.value[index].find((option) => String(option.id || option.categoryId) === String(id)); return { id, name: row?.name || '' } })
  aliasForm.value.xhs = { category: chain.map((row) => row.name).join(' > '), chain, category_id: value }
}

async function saveAliasForm() {
  const internal = aliasForm.value.internal_category.trim()
  if (!internal) return ElMessage.warning('请填写内部类目')
  if (!aliasForm.value.wechat && !aliasForm.value.xhs) return ElMessage.warning('请至少选择一个平台类目')
  aliasFormSaving.value = true
  try {
    await bulkApi.saveCategoryAlias({ internal_category: internal, wechat: aliasForm.value.wechat, xhs: aliasForm.value.xhs })
    ElMessage.success('映射已保存，批量发布重新点“立即匹配”即可生效')
    aliasFormVisible.value = false
    void loadAliasList()
  } catch (error) { ElMessage.error(error.message) } finally { aliasFormSaving.value = false }
}

onMounted(() => { void loadAliasList() })
</script>

<template>
  <div :class="{ 'alias-page': !dialog }">
    <template v-if="!dialog"><div class="page-heading"><div><h1>类目映射表</h1><p>维护“内部类目 → 平台类目”对照关系，批量发布时自动命中，无需重复选择</p></div></div></template>
    <section class="content-panel bulk-card">
      <h3>映射表维护</h3>
      <p class="muted-copy">商品多时建议提前整理对照表批量导入；平台类目路径必须写官方名称，系统自动反查类目ID并逐级校验。已确认的映射在后续批次自动命中，删除后恢复自动匹配。</p>
      <div class="form-actions">
        <el-button :icon="Download" @click="downloadAliasTemplate">下载映射模板</el-button>
        <input type="file" accept=".xlsx,.csv" @change="aliasFile = $event.target.files[0]" />
        <strong v-if="aliasFile">{{ aliasFile.name }}</strong>
        <el-button type="primary" :loading="aliasImporting" @click="importAliasFile">导入映射表</el-button>
        <el-button :icon="Plus" @click="openAliasForm">新增映射</el-button>
      </div>
    </section>
    <section class="content-panel bulk-card">
      <h3>已保存映射（{{ aliasList.length }}）</h3>
      <el-table v-loading="listLoading" :data="aliasList" max-height="420" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }" empty-text="暂无映射，可通过上方导入映射表或新增映射创建">
        <el-table-column prop="internal_category" label="内部类目" min-width="220" show-overflow-tooltip />
        <el-table-column label="微信类目" min-width="240" show-overflow-tooltip><template #default="{ row }"><el-tag v-if="row.wechat" type="success">{{ row.wechat.category }}</el-tag><span v-else class="muted-copy">未确认</span></template></el-table-column>
        <el-table-column label="小红书类目" min-width="240" show-overflow-tooltip><template #default="{ row }"><el-tag v-if="row.xhs" type="success">{{ row.xhs.category }}</el-tag><span v-else class="muted-copy">未确认</span></template></el-table-column>
        <el-table-column label="操作" width="90"><template #default="{ row }"><el-button size="small" type="danger" :icon="Delete" link @click="removeAlias(row)">删除</el-button></template></el-table-column>
      </el-table>
    </section>
    <el-dialog v-model="aliasImportDialogVisible" title="映射表导入结果" width="760px">
      <p class="muted-copy">成功 {{ aliasImportResult?.imported || 0 }} 行，失败 {{ aliasImportResult?.failed || 0 }} 行，跳过 {{ aliasImportResult?.skipped || 0 }} 行。成功行已存入映射表，失败行请按原因修正 Excel 后重新上传；批量发布重新点“立即匹配”即可生效。</p>
      <el-table :data="aliasImportResult?.report || []" max-height="380" :tooltip-options="{ effect: 'light', showAfter: 0, hideAfter: 0 }">
        <el-table-column prop="line" label="行" width="70" />
        <el-table-column prop="internal_category" label="内部类目" min-width="180" show-overflow-tooltip />
        <el-table-column label="微信侧" min-width="230" show-overflow-tooltip><template #default="{ row }"><el-tag v-if="row.wechat" :type="aliasReportType(row.wechat)">{{ row.wechat }}</el-tag><span v-else class="muted-copy">--</span></template></el-table-column>
        <el-table-column label="小红书侧" min-width="230" show-overflow-tooltip><template #default="{ row }"><el-tag v-if="row.xhs" :type="aliasReportType(row.xhs)">{{ row.xhs }}</el-tag><span v-else class="muted-copy">--</span></template></el-table-column>
      </el-table>
    </el-dialog>
    <el-dialog v-model="aliasFormVisible" title="新增类目映射" width="680px">
      <el-form label-width="100px">
        <el-form-item label="内部类目"><el-input v-model="aliasForm.internal_category" placeholder="与商品表内部类目一致，如：钻石 > 钻石首饰 > 项链" /></el-form-item>
        <el-form-item label="微信类目">
          <el-select :model-value="aliasForm.wechat?.category" filterable remote clearable reserve-keyword :remote-method="searchAliasWechat" :loading="aliasWechatLoading" placeholder="输入关键词搜索，选择官方叶子类目" style="width: 100%" @change="chooseAliasWechat">
            <el-option v-for="item in aliasWechatOptions" :key="item.path" :label="item.path" :value="item.path" />
          </el-select>
        </el-form-item>
        <el-form-item label="小红书类目">
          <el-button size="small" @click="loadAliasXhsLevel(0)">读取类目</el-button>
          <template v-for="(options, level) in aliasXhsLevels" :key="level"><el-select :model-value="aliasXhsSelected[level]" :placeholder="`第${level + 1}级类目`" @change="(value) => chooseAliasXhs(level, value)"><el-option v-for="item in options" :key="item.id || item.categoryId" :label="item.name" :value="item.id || item.categoryId" /></el-select></template>
        </el-form-item>
      </el-form>
      <template #footer><el-button @click="aliasFormVisible = false">取消</el-button><el-button type="primary" :loading="aliasFormSaving" @click="saveAliasForm">保存映射</el-button></template>
    </el-dialog>
  </div>
</template>
