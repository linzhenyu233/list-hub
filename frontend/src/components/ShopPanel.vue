<script setup>
// 店铺管理页（P4）：给运营看的"哪些店能用、授权还剩多少天"。
//
// 设计原则（按运营反馈砍过两版）：
//   1. 卡片上默认只有「店名 + 微信状态 + 小红书状态」，其余按需出现：
//      微信只在"没检查过/异常"时给「检查」，小红书只在"快到期/异常"时给「续期」
//      —— 一切正常时页面上没有任何按钮可点，减少误操作；
//   2. 所有 id 与配置（AppID / 类目 / 运费模板 / 图片文件夹 / 备注）收进「技术信息」折叠；
//   3. 说人话：图片文件夹、未开通、未授权、快到期（联系技术同学）。
//
// 定位是只读巡检：新增/停用店铺要改 shops.json 并重启服务（新增还要重启后台发布进程）。
// 每个店单独传 shop_id：靠 shopContext.applyShopHeaders 的"显式头不覆盖"规则实现。
import { onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { bulkApi } from '../bulkApi'
import { xhsApi } from '../xhsApi'
import { storeApi } from '../api'

const props = defineProps({ refreshTick: { type: Number, default: 0 } })

const loading = ref(false)
const shops = ref([])
const xhsStates = reactive({})      // shop_id -> { loading, info, error }
const wechatStates = reactive({})   // shop_id -> { checking, checked, ok, message }
const renewing = reactive({})       // shop_id -> true 表示续期请求中

const hasWechat = (shop) => Boolean(shop.wechat?.appid)
const hasXhs = (shop) => Boolean(shop.xhs?.app_id)

async function loadShopsList() {
  loading.value = true
  try {
    const data = await bulkApi.shops()
    shops.value = data?.result || []
    await Promise.all(shops.value.filter(hasXhs).map((shop) => loadXhsToken(shop)))
  } catch (error) {
    shops.value = []
    ElMessage.error(`店铺列表加载失败：${error.message}`)
  } finally {
    loading.value = false
  }
}

async function loadXhsToken(shop) {
  xhsStates[shop.shop_id] = { loading: true, info: null, error: '' }
  try {
    const info = await xhsApi.tokenInfo(shop.shop_id)
    xhsStates[shop.shop_id] = { loading: false, info, error: '' }
  } catch (error) {
    xhsStates[shop.shop_id] = { loading: false, info: null, error: error.message }
  }
}

// ---- 一句话状态（运营看的结论） ----
function xhsState(shop) {
  if (!hasXhs(shop)) return { text: '未开通', type: 'info' }
  const state = xhsStates[shop.shop_id]
  if (!state || state.loading) return { text: '查询中…', type: 'info' }
  if (state.error) return { text: '查询失败', type: 'danger', hint: state.error, needAction: true }
  const info = state.info || {}
  if (!info.configured) return { text: '未授权', type: 'warning', hint: '需要重新授权，请联系技术同学', needAction: true }
  const days = info.remain_days
  if (typeof days !== 'number') return { text: '已授权', type: 'success' }
  return days < 2
    ? { text: `快到期（剩 ${days} 天）`, type: 'warning', hint: '建议点「续期」', needAction: true }
    : { text: `剩余 ${days} 天`, type: 'success' }
}

function wechatState(shop) {
  if (!hasWechat(shop)) return { text: '未开通', type: 'info' }
  const state = wechatStates[shop.shop_id]
  if (state?.checking) return { text: '检查中…', type: 'info' }
  if (!state?.checked) return { text: '未检查', type: 'info', hint: '点「检查」确认微信是否正常', needAction: true }
  return state.ok
    ? { text: '正常', type: 'success' }
    : { text: '异常', type: 'danger', hint: state.message, needAction: true }
}

async function checkWechatToken(shop) {
  wechatStates[shop.shop_id] = { checking: true, checked: false, ok: false, message: '' }
  try {
    const result = await storeApi.testToken(shop.shop_id)
    wechatStates[shop.shop_id] = {
      checking: false, checked: true, ok: true,
      message: `凭证正常（token 有效期 ${result.expires_in || '—'} 秒）`,
    }
    ElMessage.success('微信正常')
  } catch (error) {
    wechatStates[shop.shop_id] = { checking: false, checked: true, ok: false, message: error.message }
    ElMessage.error(error.message)
  }
}

async function renewXhsToken(shop) {
  renewing[shop.shop_id] = true
  try {
    const result = await xhsApi.tokenRefresh(shop.shop_id)
    ElMessage.success(result?.message || '已提交续期')
    await loadXhsToken(shop)
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    renewing[shop.shop_id] = false
  }
}

onMounted(loadShopsList)
watch(() => props.refreshTick, loadShopsList)
</script>

<template>
  <div class="shop-page">
    <div class="page-heading">
      <div>
        <h1>店铺管理</h1>
        <p>各店铺能不能用、授权还剩多少天</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="loadShopsList">刷新</el-button>
    </div>

    <section v-loading="loading" class="shop-list">
      <el-empty v-if="!shops.length && !loading" description="没有已启用的店铺，请联系技术同学" />

      <el-card v-for="shop in shops" :key="shop.shop_id" shadow="never" class="shop-card">
        <div class="shop-row">
          <strong class="shop-name">{{ shop.name }}</strong>
          <el-tag v-if="shop.is_default" type="success" effect="light" round size="small">默认店</el-tag>
          <span class="shop-status">
            <span class="status-item" :title="wechatState(shop).hint || ''">
              <span class="status-label">微信</span>
              <el-tag :type="wechatState(shop).type" effect="light" round>{{ wechatState(shop).text }}</el-tag>
              <el-button v-if="wechatState(shop).needAction" link type="primary" size="small" :loading="wechatStates[shop.shop_id]?.checking" @click="checkWechatToken(shop)">检查</el-button>
            </span>
            <span class="status-item" :title="xhsState(shop).hint || ''">
              <span class="status-label">小红书</span>
              <el-tag :type="xhsState(shop).type" effect="light" round>{{ xhsState(shop).text }}</el-tag>
              <el-button v-if="xhsState(shop).needAction" link type="primary" size="small" :loading="renewing[shop.shop_id]" @click="renewXhsToken(shop)">续期</el-button>
            </span>
          </span>
        </div>


      </el-card>
    </section>
  </div>
</template>

<style scoped>
.shop-list { display: flex; flex-direction: column; gap: 12px; }
.shop-card { border-radius: 7px; }
.shop-row { display: flex; align-items: center; flex-wrap: wrap; gap: 10px 18px; }
.shop-name { font-size: 16px; }
.shop-status { display: flex; align-items: center; flex-wrap: wrap; gap: 22px; margin-left: auto; }
.status-item { display: inline-flex; align-items: center; gap: 8px; }
.status-label { color: var(--muted); font-size: 13px; }
</style>
