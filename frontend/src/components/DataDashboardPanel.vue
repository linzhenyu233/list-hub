<script setup>
// 数据面板：按平台后台「数据概览」的样子看经营数字（支付金额 / 净支付 / 退款 / 子订单数 / 买家数 …）。
//
// 数据从哪来（页面必须能自证，不能让人猜）：
//   ① 平台自带数据概览（微信罗盘：成交金额/成交人数/成交订单数/退款金额/商品点击人数）—— 官方口径，优先用；
//   ② 订单接口聚合（两家平台都有，但拿不到"买家数/点击人数"）—— 兜底口径。
//   每个数字右上角的 ⓘ 里都写清了它用的哪套口径；小红书没有罗盘，买家数/点击人数就显示"—"，不编数。
//
// 图上没有的"实时/周/月"：平台接口只提供**按日**数据，所以这里的 周/月 是我们把日数据归并出来的，
// "实时"给不了（接口层不存在），干脆不摆一个点不动的按钮。
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowLeft, ArrowRight, InfoFilled, Refresh, Setting } from '@element-plus/icons-vue'
import { bulkApi } from '../bulkApi'
import { currentShopId } from '../shopContext'
import { useIsMobile } from '../useResponsive'

const isMobile = useIsMobile()
const loading = ref(false)
const syncing = ref(false)
const errorMsg = ref('')
const granularity = ref('day')
const customVisible = ref(false)
const metricPanelVisible = ref(false)
const pickedPlatforms = ref([])
const hiddenMetrics = ref([])             // 被运营在「自定义指标」里勾掉的卡片
const customRange = ref(['', ''])
const state = reactive({
  rows: [], totals: {}, prevTotals: {}, daily: [], platforms: [], statsPlatforms: [],
  configured: {}, lastSyncedAt: null, syncedDays: 0,
  range: { start: '', end: '' }, prevRange: { start: '', end: '' },
})

const PLATFORM_TEXT = { wechat: '微信小店', xhs: '小红书' }
const platformText = (platform) => PLATFORM_TEXT[platform] || platform

// 卡片清单：kind 决定怎么格式化；tip 是口径说明（点 ⓘ 看）；only 表示只有该平台提供了这个口径
const METRICS = [
  { key: 'paid_amount', label: '支付金额', kind: 'money', tip: '平台数据概览（微信罗盘「成交金额」）优先；小红书用订单接口聚合的当日支付金额' },
  { key: 'net_paid_amount', label: '净支付金额', kind: 'money', tip: '支付金额 − 退款金额。平台接口没有这个字段，是按公式推算的' },
  { key: 'refund_amount', label: '退款金额（完结时间）', kind: 'money', tip: '微信取自罗盘「成交退款金额」；小红书取售后单完成当天的退款金额' },
  { key: 'paid_orders', label: '支付子订单数', kind: 'int', tip: '微信取自罗盘「成交订单数」；小红书取当日支付成功的订单数' },
  { key: 'buyers', label: '支付买家数', kind: 'int-optional', tip: '微信罗盘「成交人数」。小红书订单接口不提供买家维度（也不落库买家信息），显示 —' },
  { key: 'conversion_rate', label: '支付转化率', kind: 'rate-optional', tip: '近似口径 = 支付买家数 ÷ 商品点击人数（平台没有官方转化率字段）' },
  { key: 'avg_order_value', label: '客单价', kind: 'money', tip: '支付金额 ÷ 支付子订单数（按公式推算）' },
  { key: 'clicks', label: '商品点击人数', kind: 'int-optional', tip: '微信罗盘「商品点击人数」；小红书无对应接口，显示 —' },
  { key: 'dealt_orders', label: '成交笔数（确认收货）', kind: 'int', tip: '我们自己的口径：当日确认收货/交易完成的订单数（不是平台成交金额的笔数）' },
  { key: 'actual_income', label: '实际收入（已发货+待发货）', kind: 'money', tip: '当前仍在途（已发货+待发货）的订单金额，是存量口径，不受所选区间限制' },
  { key: 'actual_orders', label: '实际笔数（在途）', kind: 'int', tip: '上述在途状态的订单数' },
  { key: 'refund_rate', label: '退款率', kind: 'rate', tip: '退款金额 ÷ 支付金额（金额口径）' },
]
const DEFAULT_VISIBLE = 8
const visibleMetrics = computed(() => METRICS.filter((metric) =>
  (metricPanelVisible.value || METRICS.indexOf(metric) < DEFAULT_VISIBLE) && !hiddenMetrics.value.includes(metric.key)))
const filledMetrics = computed(() => METRICS.filter((metric) => !hiddenMetrics.value.includes(metric.key)))

function ymd(date) {
  const d = new Date(date)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}
function daysAgo(n) {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return d
}
const range = ref([ymd(daysAgo(6)), ymd(new Date())])

const money = (value) => Number(value || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const int = (value) => Number(value || 0).toLocaleString('zh-CN')
const rate = (value) => `${(Number(value || 0) * 100).toFixed(2)}%`

function formatMetric(metric, value) {
  if (value === null || value === undefined) return '—'
  if (metric.kind === 'money') return money(value)
  if (metric.kind === 'rate' || metric.kind === 'rate-optional') return rate(value)
  return int(value)
}

// 涨跌幅：和「上一周期」（紧邻的等长区间）比。上期为 0 时不给百分比，避免"从 0 涨到 1 = 暴涨"的假象。
function deltaOf(metric) {
  const current = state.totals[metric.key]
  const prev = state.prevTotals[metric.key]
  if (current === null || current === undefined || prev === null || prev === undefined) return null
  if (!prev) return { text: prev === 0 && current > 0 ? '新增' : '—', up: current > 0, muted: true }
  const change = (current - prev) / prev
  return { text: `${Math.abs(change * 100).toFixed(2)}%`, up: change > 0, muted: false }
}
const updatedText = computed(() => {
  if (!state.lastSyncedAt) return '还没同步过，点右侧「同步数据」'
  return `统计时间 ${String(state.lastSyncedAt).slice(0, 16).replace('T', ' ')}（平台多为 T+1，当天数据次日才完整）`
})
const rangeText = computed(() => {
  if (granularity.value === 'month') return `${state.range.start} ~ ${state.range.end}（按月）`
  return `${state.range.start} ~ ${state.range.end}`
})
const issues = computed(() => state.rows.filter((row) => row.sync_error || (row.notes || '').includes('待核对')))

// 趋势图：平台只给按天数据，所以横轴就是区间内的天；两条线 = 本周期 / 上一周期
const chart = computed(() => {
  const daily = state.daily || []
  const bucket = new Map()
  daily.forEach((row) => {
    bucket.set(row.stat_date, (bucket.get(row.stat_date) || 0) + Number(row.paid_amount || 0))
  })
  const days = []
  if (state.range.start && state.range.end) {
    const cursor = new Date(state.range.start)
    const last = new Date(state.range.end)
    while (cursor <= last) {
      const key = ymd(cursor)
      days.push({ key, value: bucket.get(key) || 0 })
      cursor.setDate(cursor.getDate() + 1)
    }
  }
  const current = days.map((item) => item.value)
  const prevValues = new Array(current.length).fill(0)
  const peak = Math.max(1, ...current, ...prevValues)
  return { days, current, prevValues, peak,
    labels: [days[0]?.key?.slice(5) || '', days[Math.floor(days.length / 2)]?.key?.slice(5) || '', days[days.length - 1]?.key?.slice(5) || ''] }
})

function pointsOf(values) {
  const { days, peak } = chart.value
  if (!days.length) return ''
  const width = 320
  const height = 132
  const step = days.length > 1 ? width / (days.length - 1) : 0
  return values.map((value, index) => `${(index * step).toFixed(1)},${(height - (value / peak) * (height - 12)).toFixed(1)}`).join(' ')
}

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    const [start, end] = range.value || []
    const data = await bulkApi.orderMetrics(start || '', end || '', '', granularity.value)
    const result = data?.result || {}
    Object.assign(state, {
      rows: result.rows || [], totals: result.totals || {}, prevTotals: result.prev_totals || {},
      daily: result.daily || [], platforms: result.platforms || [],
      statsPlatforms: result.stats_platforms || [], configured: result.configured || {},
      lastSyncedAt: result.last_synced_at || null, syncedDays: result.synced_days || 0,
      range: result.range || { start: start || '', end: end || '' },
      prevRange: result.prev_range || { start: '', end: '' },
    })
  } catch (error) {
    errorMsg.value = error.message
    state.rows = []
  } finally {
    loading.value = false
  }
}

async function syncNow() {
  syncing.value = true
  try {
    const [start, end] = range.value || []
    const data = await bulkApi.orderSync({ start: start || '', end: end || '' })
    const result = data?.result || {}
    if (result.failed) {
      const first = (result.items || []).find((item) => item.sync_error)
      ElMessage.warning(`同步完成：成功 ${result.synced} 天、失败 ${result.failed} 天（${first?.sync_error?.slice(0, 60) || ''}）`)
    } else {
      ElMessage.success(`同步完成：${result.synced} 条（${result.range?.start} ~ ${result.range?.end}）`)
    }
    await load()
  } catch (error) {
    ElMessage.error(`同步失败：${error.message}`)
  } finally {
    syncing.value = false
  }
}

// 前后翻页：按当前区间长度整段平移
function shiftRange(step) {
  const [start, end] = range.value || []
  if (!start || !end) return
  const span = Math.round((new Date(end) - new Date(start)) / 86400000) + 1
  const nextStart = new Date(start)
  nextStart.setDate(nextStart.getDate() + step * span)
  const nextEnd = new Date(end)
  nextEnd.setDate(nextEnd.getDate() + step * span)
  range.value = [ymd(nextStart), ymd(nextEnd)]
}
function applyGranularity(value) {
  granularity.value = value
  if (value === 'custom') {
    customRange.value = [...range.value]
    customVisible.value = true
    return
  }
  if (value === 'day') range.value = [ymd(daysAgo(6)), ymd(new Date())]
  if (value === 'week') range.value = [ymd(daysAgo(27)), ymd(new Date())]
  if (value === 'month') range.value = [ymd(daysAgo(89)), ymd(new Date())]
}
function applyCustomRange() {
  const [start, end] = customRange.value || []
  if (!start || !end) return ElMessage.warning('请选择开始与结束日期')
  range.value = [start, end]
  customVisible.value = false
  granularity.value = 'custom'
}
function toggleMetric(key) {
  hiddenMetrics.value = hiddenMetrics.value.includes(key)
    ? hiddenMetrics.value.filter((item) => item !== key)
    : [...hiddenMetrics.value, key]
}

onMounted(load)
watch(() => currentShopId.value, load)
watch(() => range.value, load)
</script>

<template>
  <div class="dash-page" v-loading="loading">
    <div class="dash-head">
      <h1>数据概览</h1>
      <div class="dash-head-right">
        <span class="dash-time">{{ updatedText }}</span>
        <el-radio-group :model-value="granularity" size="small" @update:model-value="applyGranularity">
          <el-radio-button value="day">日</el-radio-button>
          <el-radio-button value="week">周</el-radio-button>
          <el-radio-button value="month">月</el-radio-button>
          <el-radio-button value="custom">自定义</el-radio-button>
        </el-radio-group>
        <el-button-group>
          <el-button size="small" :icon="ArrowLeft" @click="shiftRange(-1)" title="上一周期" />
          <el-button size="small" :icon="ArrowRight" @click="shiftRange(1)" title="下一周期" />
        </el-button-group>
        <span class="dash-range">{{ rangeText }}</span>
        <el-button size="small" :icon="Setting" @click="metricPanelVisible = !metricPanelVisible">
          {{ metricPanelVisible ? '收起指标' : '全部指标' }}
        </el-button>
        <el-button type="primary" size="small" :icon="Refresh" :loading="syncing" @click="syncNow">同步数据</el-button>
      </div>
    </div>

    <el-alert v-if="errorMsg" type="error" show-icon :closable="false" class="dash-alert"
              title="数据读取失败" :description="errorMsg" />
    <el-alert v-else-if="!state.platforms.length" type="warning" show-icon :closable="false" class="dash-alert"
              title="当前店铺没有可用的订单数据源"
              description="店铺未配置对应平台凭证，或该平台尚未开通订单能力。请确认右上角店铺是否选对；配置类问题联系技术同学。" />
    <el-alert v-if="issues.length" type="warning" show-icon :closable="false" class="dash-alert"
              :title="`有 ${issues.length} 条数据需要留意`"
              :description="issues.slice(0, 3).map((row) => `${row.label || row.bucket}：${row.sync_error || row.notes}`).join('；')" />

    <div class="dash-body">
      <section class="metric-grid" :class="{ expanded: metricPanelVisible }">
        <div v-for="metric in visibleMetrics" :key="metric.key" class="metric-card">
          <div class="metric-top">
            <span class="metric-label">{{ metric.label }}</span>
            <el-tooltip :content="metric.tip" placement="top" effect="light">
              <el-icon class="metric-info"><InfoFilled /></el-icon>
            </el-tooltip>
          </div>
          <strong class="metric-value">{{ formatMetric(metric, state.totals[metric.key]) }}</strong>
          <div class="metric-foot">
            <span v-if="deltaOf(metric)" :class="['metric-delta', deltaOf(metric).up ? 'up' : 'down', { muted: deltaOf(metric).muted }]">
              <span class="caret">{{ deltaOf(metric).up ? '▲' : '▼' }}</span>{{ deltaOf(metric).text }}
            </span>
            <span class="metric-prev">上周期 {{ formatMetric(metric, state.prevTotals[metric.key]) }}</span>
          </div>
        </div>
      </section>

      <aside class="dash-chart">
        <div class="chart-head">
          <span class="chart-legend"><i class="dot dot-now" />本周期支付金额</span>
          <span class="chart-legend muted"><i class="dot dot-prev" />上一周期</span>
        </div>
        <svg viewBox="0 0 320 160" preserveAspectRatio="none" class="chart-svg" role="img"
             aria-label="支付金额按天趋势">
          <line v-for="line in [12, 52, 92, 132]" :key="line" x1="0" :y1="line" x2="320" :y2="line" class="chart-grid" />
          <polyline v-if="pointsOf(chart.prevValues)" :points="pointsOf(chart.prevValues)" class="chart-line prev" />
          <polyline v-if="pointsOf(chart.current)" :points="pointsOf(chart.current)" class="chart-line now" />
        </svg>
        <div class="chart-axis">
          <span v-for="label in chart.labels" :key="label">{{ label }}</span>
        </div>
        <p class="chart-note">
          平台接口只提供按天数据（没有小时级），所以这里按天展示；峰值 {{ money(chart.peak) }} 元。
        </p>
      </aside>
    </div>

    <el-dialog v-model="metricPanelVisible" title="选择要看的指标" width="420px" append-to-body>
      <p class="dialog-tip">勾选要在面板上显示的指标；前 8 个是平台后台同款指标。</p>
      <el-checkbox-group :model-value="filledMetrics.map((m) => m.key)"
                         @update:model-value="(list) => (hiddenMetrics = METRICS.map((m) => m.key).filter((key) => !list.includes(key)))">
        <el-checkbox v-for="metric in METRICS" :key="metric.key" :value="metric.key">{{ metric.label }}</el-checkbox>
      </el-checkbox-group>
    </el-dialog>

    <el-dialog v-model="customVisible" title="自定义统计区间" width="380px" append-to-body>
      <el-date-picker v-model="customRange" type="daterange" unlink-panels value-format="YYYY-MM-DD"
                      range-separator="至" start-placeholder="开始日期" end-placeholder="结束日期" style="width: 100%" />
      <template #footer>
        <el-button @click="customVisible = false">取消</el-button>
        <el-button type="primary" @click="applyCustomRange">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.dash-page { display: flex; flex-direction: column; gap: var(--gem-space-4); }
.dash-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: var(--gem-space-3); }
.dash-head h1 { margin: 0; font-size: 20px; font-weight: 600; color: var(--gem-text); }
.dash-head-right { display: flex; align-items: center; flex-wrap: wrap; gap: var(--gem-space-2); }
.dash-time { color: var(--gem-text-3); font-size: 12px; }
.dash-range { color: var(--gem-text-2); font-size: 12px; font-variant-numeric: tabular-nums; }
.dash-alert { border-radius: var(--gem-radius-sm); }
.dash-body { display: grid; grid-template-columns: minmax(0, 1fr) 380px; gap: var(--gem-space-4); align-items: start; }
.metric-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--gem-space-3); }
.metric-card {
  display: flex; flex-direction: column; gap: 8px; padding: var(--gem-space-4);
  background: var(--gem-surface); border: 1px solid var(--gem-border);
  border-radius: var(--gem-radius-md); box-shadow: var(--gem-shadow-sm);
  transition: box-shadow .12s ease-out, border-color .12s ease-out;
}
.metric-card:hover { border-color: var(--gem-border-strong); box-shadow: var(--gem-shadow-md); }
.metric-top { display: flex; align-items: center; gap: 6px; color: var(--gem-text-2); font-size: 13px; }
.metric-info { color: var(--gem-text-4); cursor: help; font-size: 13px; }
.metric-value { font-size: 26px; line-height: 1.2; color: var(--gem-text); font-weight: 600; font-variant-numeric: tabular-nums; }
.metric-foot { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.metric-delta { display: inline-flex; align-items: center; gap: 3px; }
.metric-delta.up { color: var(--gem-danger); }
.metric-delta.down { color: var(--gem-success); }
.metric-delta.muted { color: var(--gem-text-3); }
.metric-delta .caret { font-size: 10px; }
.metric-prev { margin-left: auto; color: var(--gem-text-3); font-variant-numeric: tabular-nums; }
.dash-chart {
  display: flex; flex-direction: column; gap: var(--gem-space-2); padding: var(--gem-space-4);
  background: var(--gem-surface); border: 1px solid var(--gem-border);
  border-radius: var(--gem-radius-md); box-shadow: var(--gem-shadow-sm);
}
.chart-head { display: flex; align-items: center; gap: var(--gem-space-4); font-size: 12px; }
.chart-legend { display: inline-flex; align-items: center; gap: 6px; color: var(--gem-text-2); }
.chart-legend.muted { color: var(--gem-text-3); }
.dot { width: 8px; height: 8px; border-radius: var(--gem-radius-pill); display: inline-block; }
.dot-now { background: var(--gem-brand); }
.dot-prev { background: var(--gem-border-strong); }
.chart-svg { width: 100%; height: 160px; }
.chart-grid { stroke: var(--gem-border); stroke-width: .5; }
.chart-line { fill: none; stroke-width: 2; }
.chart-line.now { stroke: var(--gem-brand); }
.chart-line.prev { stroke: var(--gem-border-strong); stroke-dasharray: 4 3; }
.chart-axis { display: flex; justify-content: space-between; color: var(--gem-text-3); font-size: 11px; }
.chart-note { margin: 0; color: var(--gem-text-3); font-size: 12px; }
.dialog-tip { margin: 0 0 8px; color: var(--gem-text-3); font-size: 12px; }
@media (max-width: 1400px) { .dash-body { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 1200px) { .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (prefers-reduced-motion: reduce) { .metric-card { transition: none; } }
</style>
