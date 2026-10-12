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
import { ArrowLeft, ArrowRight, Download, InfoFilled, Refresh, Setting } from '@element-plus/icons-vue'
import { bulkApi } from '../bulkApi'
import { shops } from '../shopContext'
import { useIsMobile } from '../useResponsive'

const isMobile = useIsMobile()
const loading = ref(false)
const syncing = ref(false)
const errorMsg = ref('')
const granularity = ref('day')
const customVisible = ref(false)
const pickerVisible = ref(false)          // 「自定义指标」弹窗的开关
// 面板自己的店铺范围（记住上次选择）：'all' = 全部店铺汇总，其余为 shop_id
const PANEL_SHOP_KEY = 'bulk.dashboard.shop'
const PANEL_SHOP_ALL = 'all'
const panelShop = ref(localStorage.getItem(PANEL_SHOP_KEY) || PANEL_SHOP_ALL)
// 自定义区间：开始/结束**两个独立日期框**（实测一个 daterange 选择器放在弹窗里，
// 第一次点击会被弹窗顶掉，两个框会变成同一个日期，选不出区间）
const customStart = ref('')
const customEnd = ref('')
const state = reactive({
  rows: [], totals: {}, prevTotals: {}, daily: [], prevDaily: [],
  platforms: [], statsPlatforms: [],
  configured: {}, lastSyncedAt: null, syncedDays: 0,
  range: { start: '', end: '' }, prevRange: { start: '', end: '' },
})

const PLATFORM_TEXT = { wechat: '微信小店', xhs: '小红书' }
const platformText = (platform) => PLATFORM_TEXT[platform] || platform

// 店铺下拉：第一项固定是"全部店铺汇总"，其余是各启用店铺（跟顶栏那份清单同源，但选择互不影响）
const shopOptions = computed(() => [
  { value: PANEL_SHOP_ALL, label: `全部店铺汇总（${shops.value.length || 0} 家）` },
  ...(shops.value || []).map((item) => ({ value: item.shop_id, label: item.name || item.shop_id })),
])
const panelShopLabel = computed(() => shopOptions.value
  .find((item) => item.value === panelShop.value)?.label || panelShop.value)
const scopeAll = computed(() => panelShop.value === PANEL_SHOP_ALL)

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
const METRIC_PREFS_KEY = 'bulk.dashboard.metrics'
const defaultCheckedKeys = () => METRICS.slice(0, DEFAULT_VISIBLE).map((metric) => metric.key)

// 勾选结果记住（刷新/第二天回来还是你选的那几张卡）
function loadCheckedKeys() {
  try {
    const saved = JSON.parse(localStorage.getItem(METRIC_PREFS_KEY) || 'null')
    if (Array.isArray(saved) && saved.length) {
      const valid = saved.filter((key) => METRICS.some((metric) => metric.key === key))
      if (valid.length) return valid
    }
  } catch { /* 存坏了就用默认 */ }
  return defaultCheckedKeys()
}

const checkedKeys = ref(loadCheckedKeys())
watch(checkedKeys, (list) => {
  try {
    localStorage.setItem(METRIC_PREFS_KEY, JSON.stringify(list))
  } catch { /* 无痕模式写不了就算了，不影响使用 */ }
})
// 面板显示哪些卡片 = 勾选了哪些。**没有隐藏状态**：勾上立即出现在面板上。
const visibleMetrics = computed(() => METRICS.filter((metric) => checkedKeys.value.includes(metric.key)))
const allChecked = computed(() => checkedKeys.value.length === METRICS.length)
function toggleAllMetrics() {
  checkedKeys.value = allChecked.value ? defaultCheckedKeys() : METRICS.map((metric) => metric.key)
}

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
function shiftDays(value, days) {
  const d = new Date(value)
  d.setDate(d.getDate() + days)
  return d
}
// 区间/日期一律走这里再发给后端：无论是 Date 对象、ISO 字符串还是 'YYYY-MM-DD'，
// 统一成 'YYYY-MM-DD'。/api 只认这个格式，传 Date 会被 axios 序列化成 ISO（踩过）。
function toDay(value) {
  if (!value) return ''
  if (typeof value === 'string') return value.slice(0, 10)
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? '' : ymd(d)
}
// 该日期所在自然周的周一（周日算上一周的末尾，避免"周"从周日开始）
function mondayOf(value) {
  const d = new Date(value)
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7))
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
// 区间文字要能自证"这是什么窗口"：选"周"就是 4 个整周，不是随便 28 天
const rangeText = computed(() => {
  const days = state.range.days || 0
  const suffixMap = { week: '（4 个整周）', month: '（3 个整月）' }
  const suffix = granularity.value === 'custom' || granularity.value === 'day'
    ? `（${days} 天）` : suffixMap[granularity.value] || `（${days} 天）`
  return `${state.range.start} ~ ${state.range.end}${suffix}`
})
// 「一个周期」怎么算：本周期 = 你选的这段区间；上一周期 = 紧邻在前、**天数相同**的一段。
// 涨跌幅 =（本周期 − 上一周期）÷ 上一周期（上期为 0 时不给百分比）。
// 窗口说明：选"周/月"时窗口是整周/整月（不含本周/本月），点日期旁的 ⓘ 就能看到
const WINDOW_NOTE = {
  day: '窗口 = 近 7 天（含今天）',
  week: '窗口 = 近 4 个完整自然周（周一 ~ 周日，不含本周）',
  month: '窗口 = 近 3 个完整自然月（不含本月）',
  custom: '窗口 = 你自己选的这段区间',
}
const periodTip = computed(() => {
  const days = state.range.days || 0
  return `${WINDOW_NOTE[granularity.value] || ''}；`
    + `本周期 ${state.range.start} ~ ${state.range.end}（${days} 天）；`
    + `上一周期 ${state.prevRange.start} ~ ${state.prevRange.end}（同样 ${days} 天，紧接在本周期之前）；`
    + '涨跌幅 =（本周期 − 上一周期）÷ 上一周期，上期为 0 时不显示百分比'
})
// 图例里只写"月-日"，省地方
const shortDate = (value) => String(value || '').slice(5)
// 页面上只留"同步失败"这种真问题；口径备注是给"有人质疑数字时"查出处用的，
// 撤进「口径说明」弹窗里（点开才有），不占版面、不打扰日常查看
const errorRows = computed(() => state.rows.filter((row) => row.sync_error))
const noteRows = computed(() => state.rows.filter((row) => String(row.notes || '').trim()))
const readmeVisible = ref(false)

// 趋势图：平台只给按天数据，所以横轴就是区间内的天；两条线 = 本周期 / 上一周期。
// 横轴刻度按天数自动取 6~8 个（原来只写首/中/尾三个，区间一长图表就空得没法看）。
const CHART_W = 320
const CHART_BASE = 132          // 0 值所在的 y（viewBox 顶部留 12 给最高点）
function dailyTotals(rows) {
  const bucket = new Map()
  ;(rows || []).forEach((row) => {
    bucket.set(row.stat_date, (bucket.get(row.stat_date) || 0) + Number(row.paid_amount || 0))
  })
  return bucket
}
function seriesOf(rangeStart, rangeEnd, bucket) {
  const days = []
  if (!rangeStart || !rangeEnd) return days
  const cursor = new Date(rangeStart)
  const last = new Date(rangeEnd)
  while (cursor <= last) {
    const key = ymd(cursor)
    days.push({ key, value: bucket.get(key) || 0 })
    cursor.setDate(cursor.getDate() + 1)
  }
  return days
}

const chart = computed(() => {
  const days = seriesOf(state.range.start, state.range.end, dailyTotals(state.daily))
  const prevDays = seriesOf(state.prevRange.start, state.prevRange.end, dailyTotals(state.prevDaily))
  const peak = Math.max(1, ...days.map((day) => day.value), ...prevDays.map((day) => day.value))
  const scaleY = (value) => CHART_BASE - (value / peak) * (CHART_BASE - 12)
  days.forEach((day, index) => {
    day.x = days.length > 1 ? index * (CHART_W / (days.length - 1)) : CHART_W / 2
    day.y = scaleY(day.value)
  })
  prevDays.forEach((day, index) => {
    day.x = prevDays.length > 1 ? index * (CHART_W / (prevDays.length - 1)) : CHART_W / 2
    day.y = scaleY(day.value)
  })
  // 刻度：最多 7 个（含首尾），太少会空、太多会挤
  const tickCount = Math.min(days.length, 7)
  const ticks = []
  for (let slot = 0; slot < tickCount; slot += 1) {
    const index = tickCount > 1 ? Math.round((slot * (days.length - 1)) / (tickCount - 1)) : 0
    const day = days[index]
    if (!day || ticks.some((tick) => tick.text === day.key.slice(5))) continue
    ticks.push({ text: day.key.slice(5), pct: days.length > 1 ? (index / (days.length - 1)) * 100 : 50 })
  }
  const sum = days.reduce((acc, day) => acc + day.value, 0)
  const avg = days.length ? sum / days.length : 0
  // 折线下方的渐变填充（多边形：左下角 → 沿折线 → 右下角 → 闭合）
  const area = days.length
    ? `0,${CHART_BASE} ${days.map((day) => `${day.x.toFixed(1)},${day.y.toFixed(1)}`).join(' ')} ${CHART_W},${CHART_BASE}`
    : ''
  return { days, prevDays, peak, ticks, sum, avg, area }
})

function pointsOf(series) {
  return (series || []).map((day) => `${day.x.toFixed(1)},${day.y.toFixed(1)}`).join(' ')
}

// 鼠标移到图上：显示那天的日期与金额（比只看一条线有用）
const hoverIndex = ref(-1)
function onChartMove(event) {
  const days = chart.value.days
  if (!days.length) return
  const box = event.currentTarget.getBoundingClientRect()
  const ratio = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width))
  hoverIndex.value = days.length === 1 ? 0 : Math.round(ratio * (days.length - 1))
}
const hoverInfo = computed(() => {
  const days = chart.value.days
  const day = days[hoverIndex.value]
  if (!day) return null
  // 两条线一起给：只看本周期的话，运营还得自己回头翻上一周期，等于没对比
  const prevDay = chart.value.prevDays[hoverIndex.value] || null
  const pct = days.length > 1 ? (hoverIndex.value / (days.length - 1)) * 100 : 50
  return {
    date: day.key, value: day.value,
    prevDate: prevDay ? prevDay.key : '', prevValue: prevDay ? prevDay.value : null,
    pct,
    // 提示框靠边时往里收，免得被裁掉
    tipLeft: Math.min(82, Math.max(18, pct)),
    yPct: (day.y / 160) * 100,
    prevYPct: prevDay ? (prevDay.y / 160) * 100 : null,
  }
})

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    const start = toDay(range.value?.[0])
    const end = toDay(range.value?.[1])
    // 「自定义」是界面概念（自选区间），后端粒度只有 日/周/月 —— 自定义时按「天」取数
    const apiGranularity = granularity.value === 'custom' ? 'day' : granularity.value
    const data = await bulkApi.orderMetrics(start, end, '', apiGranularity, panelShop.value)
    const result = data?.result || {}
    Object.assign(state, {
      rows: result.rows || [], totals: result.totals || {}, prevTotals: result.prev_totals || {},
      daily: result.daily || [], prevDaily: result.prev_daily || [], platforms: result.platforms || [],
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

// 导出 Excel：后端生成（含"按周期/按天/口径说明"三张表），这里只负责触发下载。
// 文件名用中文在前端拼，后端回的是 ASCII 名（避免 Content-Disposition 编码踩坑）。
const exporting = ref(false)
const GRANULARITY_FILE = { day: '按天', week: '按周', month: '按月', custom: '自定义区间' }
async function exportExcel() {
  exporting.value = true
  try {
    const start = toDay(range.value?.[0])
    const end = toDay(range.value?.[1])
    const apiGranularity = granularity.value === 'custom' ? 'day' : granularity.value
    const blob = await bulkApi.orderMetricsExport(start, end, '', apiGranularity, panelShop.value)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    const scopeText = scopeAll.value ? '全部店铺' : panelShopLabel.value
    link.href = url
    link.download = `数据概览_${scopeText}_${start}_${end}_${GRANULARITY_FILE[granularity.value] || apiGranularity}.xlsx`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    ElMessage.success('已导出，文件里含「按周期 / 按天 / 口径说明」三张表')
  } catch (error) {
    ElMessage.error(`导出失败：${error.message}`)
  } finally {
    exporting.value = false
  }
}

async function syncNow() {
  syncing.value = true
  try {
    const start = toDay(range.value?.[0])
    const end = toDay(range.value?.[1])
    // 同步也只同步本页选的范围（all = 逐店同步，会慢一些）
    const data = await bulkApi.orderSync({ start, end, shop: panelShop.value })
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

// 前后翻页：按当前粒度的"整段"平移（周=整周、月=整月、日/自定义=等长天数），
// 这样翻页后依然对齐自然周/自然月，不会出现"半周的桶"
function shiftRange(step) {
  const [start, end] = range.value || []
  if (!start || !end) return
  if (granularity.value === 'week') {
    // 注意要 ymd() 转成 'YYYY-MM-DD'：shiftDays 返回的是 Date，
    // 直接把 Date 塞进 range 会被 axios 序列化成 ISO（后端只认 YYYY-MM-DD，已踩过）
    range.value = [ymd(shiftDays(start, step * 28)), ymd(shiftDays(end, step * 28))]
    return
  }
  if (granularity.value === 'month') {
    const s = new Date(start)
    s.setMonth(s.getMonth() + step * 3)
    const e = new Date(end)
    e.setMonth(e.getMonth() + step * 3)
    range.value = [ymd(new Date(s.getFullYear(), s.getMonth(), 1)),
                   ymd(new Date(e.getFullYear(), e.getMonth() + 1, 0))]
    return
  }
  const span = Math.round((new Date(end) - new Date(start)) / 86400000) + 1
  range.value = [ymd(shiftDays(start, step * span)), ymd(shiftDays(end, step * span))]
}
// 切粒度时给一个"默认窗口"，每个窗口都对齐自然边界：
//   日 → 近 7 天（含今天）
//   周 → 近 4 个**完整**自然周（周一~周日；不含本周，因为本周还没过完，环比会假跌）
//   月 → 近 3 个**完整**自然月（不含本月，同理）
function applyGranularity(value) {
  granularity.value = value
  if (value === 'custom') {
    customStart.value = range.value?.[0] || ymd(daysAgo(6))
    customEnd.value = range.value?.[1] || ymd(new Date())
    customVisible.value = true
    return
  }
  if (value === 'day') {
    range.value = [ymd(daysAgo(6)), ymd(new Date())]
    return
  }
  if (value === 'week') {
    const lastSunday = shiftDays(mondayOf(new Date()), -1)
    range.value = [ymd(shiftDays(lastSunday, -27)), ymd(lastSunday)]
    return
  }
  if (value === 'month') {
    const today = new Date()
    const lastMonthEnd = new Date(today.getFullYear(), today.getMonth(), 0)
    const start = new Date(lastMonthEnd.getFullYear(), lastMonthEnd.getMonth() - 2, 1)
    range.value = [ymd(start), ymd(lastMonthEnd)]
  }
}
function applyCustomRange() {
  const start = toDay(customStart.value)
  const end = toDay(customEnd.value)
  if (!start || !end) return ElMessage.warning('请分别选择开始日期与结束日期')
  if (start > end) return ElMessage.warning('开始日期不能晚于结束日期')
  range.value = [start, end]
  customVisible.value = false
  granularity.value = 'custom'
}
// 快捷区间：省得每次都手点两个日期框
function quickRange(kind) {
  const today = new Date()
  if (kind === 'today') {
    customStart.value = ymd(today)
    customEnd.value = ymd(today)
  } else if (kind === 'yesterday') {
    const day = daysAgo(1)
    customStart.value = ymd(day)
    customEnd.value = ymd(day)
  } else {
    const span = kind === '30d' ? 30 : 7
    customStart.value = ymd(daysAgo(span - 1))
    customEnd.value = ymd(today)
  }
}
function resetMetrics() {
  checkedKeys.value = defaultCheckedKeys()
}

// 面板**自己的**店铺范围：'all' = 全部店铺汇总。
// 刻意不 watch(currentShopId)：顶栏切店铺不影响本页，本页切换也不动顶栏——
// 面板只把这个值当查询参数(shop=)传给后端，不动中台的"当前店铺"。
watch(panelShop, (value) => {
  try {
    localStorage.setItem(PANEL_SHOP_KEY, value)
  } catch { /* 无痕模式忽略 */ }
  load()
})
watch(() => range.value, load)

onMounted(async () => {
  if (!shops.value.length) {
    // 顶栏的店铺清单一般已加载；直接打开本页（如刷新后停在数据面板）时自己补一次
    try {
      const data = await bulkApi.shops()
      shops.value = data?.result || []
    } catch { /* 拉不到就只显示"全部店铺汇总" */ }
  }
  await load()
})
</script>

<template>
  <div class="dash-page" v-loading="loading">
    <div class="dash-head">
      <h1>数据概览</h1>
      <div class="dash-head-right">
        <span class="dash-time">{{ updatedText }}</span>
        <!-- 本页专属的店铺范围：只影响数据面板，不动顶栏的"当前店铺" -->
        <el-tooltip placement="bottom" effect="light"
                    content="只影响本页：可选全部店铺汇总或单独一家店；顶栏切换店铺不会改变这里的选择">
          <span class="dash-scope-label">统计范围</span>
        </el-tooltip>
        <el-select v-model="panelShop" size="small" class="scope-select">
          <el-option v-for="item in shopOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
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
        <el-tooltip placement="bottom" effect="light" :content="periodTip">
          <span class="dash-range">{{ rangeText }}</span>
        </el-tooltip>
        <el-button size="small" :icon="Setting" @click="pickerVisible = true">自定义指标</el-button>
        <el-button size="small" @click="toggleAllMetrics">{{ allChecked ? '只看核心指标' : '全部指标' }}</el-button>
        <el-button link size="small" :icon="InfoFilled" @click="readmeVisible = true">口径说明</el-button>
        <el-button size="small" :icon="Download" :loading="exporting" @click="exportExcel">导出</el-button>
        <el-button type="primary" size="small" :icon="Refresh" :loading="syncing" @click="syncNow">同步数据</el-button>
      </div>
    </div>

    <el-alert v-if="errorMsg" type="error" show-icon :closable="false" class="dash-alert"
              title="数据读取失败" :description="errorMsg" />
    <el-alert v-else-if="!state.platforms.length" type="warning" show-icon :closable="false" class="dash-alert"
              :title="`所选统计范围（${panelShopLabel}）没有可用的订单数据源`"
              description="该范围里的店铺未配置订单平台凭证，或平台尚未开通订单能力。可先把上方「统计范围」选成「全部店铺汇总」；配置类问题联系技术同学。" />
    <el-alert v-if="errorRows.length" type="error" show-icon :closable="false" class="dash-alert"
              :title="`有 ${errorRows.length} 天的数据同步失败`"
              :description="errorRows.slice(0, 3).map((row) => `${row.label}：${row.sync_error}`).join('；')" />


    <div class="dash-body">
      <section class="metric-grid">
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
          <span class="chart-legend muted">
            <i class="dot dot-prev" />上一周期 {{ shortDate(state.prevRange.start) }} ~ {{ shortDate(state.prevRange.end) }}
          </span>
        </div>
        <div class="chart-plot" @mousemove="onChartMove" @mouseleave="hoverIndex = -1">
          <svg viewBox="0 0 320 160" preserveAspectRatio="none" class="chart-svg" role="img"
               aria-label="支付金额按天趋势">
            <defs>
              <!-- 渐变用 CSS 的 stop-color 定义（SVG 属性不认 var()，写在 class 上） -->
              <linearGradient id="dashAreaFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" class="area-stop top" />
                <stop offset="100%" class="area-stop bottom" />
              </linearGradient>
            </defs>
            <line v-for="line in [12, 52, 92, 132]" :key="line" x1="0" :y1="line" x2="320" :y2="line" class="chart-grid" />
            <polygon v-if="chart.area" :points="chart.area" class="chart-area" />
            <polyline v-if="chart.prevDays.length" :points="pointsOf(chart.prevDays)" class="chart-line prev" />
            <polyline v-if="chart.days.length" :points="pointsOf(chart.days)" class="chart-line now" />
          </svg>
          <template v-if="hoverInfo">
            <i class="chart-guide" :style="{ left: hoverInfo.pct + '%' }" />
            <i v-if="hoverInfo.prevYPct !== null" class="chart-dot prev"
               :style="{ left: hoverInfo.pct + '%', top: hoverInfo.prevYPct + '%' }" />
            <i class="chart-dot" :style="{ left: hoverInfo.pct + '%', top: hoverInfo.yPct + '%' }" />
            <div class="chart-tip" :style="{ left: hoverInfo.tipLeft + '%' }">
              <span class="tip-line now">本周期 {{ hoverInfo.date }}：{{ money(hoverInfo.value) }} 元</span>
              <span v-if="hoverInfo.prevValue !== null" class="tip-line prev">
                上一周期 {{ hoverInfo.prevDate }}：{{ money(hoverInfo.prevValue) }} 元
              </span>
            </div>
          </template>
        </div>
        <!-- 日期刻度：放在绘图区与「合计/日均/峰值」之间（日期在合计数据上方） -->
        <div class="chart-axis">
          <span v-for="tick in chart.ticks" :key="tick.text" class="chart-tick"
                :style="{ left: tick.pct + '%' }">{{ tick.text }}</span>
        </div>
        <!-- 图表卡与三行指标卡等高：多出来的高度给折线区（而不是把卡片撑高或撑开行间距） -->
        <div class="chart-summary">
          <div class="summary-cell"><span>本区间合计</span><strong>{{ money(chart.sum) }}</strong></div>
          <div class="summary-cell"><span>日均</span><strong>{{ money(chart.avg) }}</strong></div>
          <div class="summary-cell"><span>峰值</span><strong>{{ money(chart.peak) }}</strong></div>
        </div>

      </aside>
    </div>

    <el-dialog v-model="pickerVisible" title="自定义指标" width="440px" append-to-body>
      <p class="dialog-tip">勾选后立即显示在面板上（默认展示前 8 个平台后台同款指标），选择会被记住。</p>
      <el-checkbox-group v-model="checkedKeys">
        <el-checkbox v-for="metric in METRICS" :key="metric.key" :value="metric.key">{{ metric.label }}</el-checkbox>
      </el-checkbox-group>
      <template #footer>
        <el-button @click="resetMetrics">恢复默认</el-button>
        <el-button type="primary" @click="pickerVisible = false">完成</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="customVisible" title="自定义统计区间" width="420px" append-to-body>
      <div class="quick-ranges">
        <el-button size="small" @click="quickRange('today')">今天</el-button>
        <el-button size="small" @click="quickRange('yesterday')">昨天</el-button>
        <el-button size="small" @click="quickRange('7d')">近 7 天</el-button>
        <el-button size="small" @click="quickRange('30d')">近 30 天</el-button>
      </div>
      <div class="range-pickers">
        <el-date-picker v-model="customStart" type="date" value-format="YYYY-MM-DD"
                        placeholder="开始日期" style="width: 152px" />
        <span class="range-sep">至</span>
        <el-date-picker v-model="customEnd" type="date" value-format="YYYY-MM-DD"
                        placeholder="结束日期" style="width: 152px" />
      </div>
      <p class="dialog-tip">开始与结束分开选（不会互相跟着变）；也可以先用上面的快捷区间。</p>
      <template #footer>
        <el-button @click="customVisible = false">取消</el-button>
        <el-button type="primary" @click="applyCustomRange">确定</el-button>
      </template>
    </el-dialog>

    <!-- 口径说明：平时不占版面；有人质疑某个数字时点这里查出处 -->
    <el-dialog v-model="readmeVisible" title="口径说明" width="760px" append-to-body>
      <h4 class="readme-title">指标口径与来源</h4>
      <el-table :data="METRICS" size="small" max-height="34vh">
        <el-table-column prop="label" label="指标" width="190" />
        <el-table-column prop="tip" label="口径 / 来源" />
      </el-table>
      <h4 class="readme-title">本区间备注（{{ noteRows.length }} 条）</h4>
      <el-table v-if="noteRows.length" :data="noteRows" size="small" max-height="30vh">
        <el-table-column prop="label" label="周期" width="150" />
        <el-table-column prop="notes" label="备注" />
      </el-table>
      <p v-else class="dialog-tip">本区间的数据没有额外备注。</p>
      <p class="dialog-tip">备注不是错误：只是说明这些天的数字是怎么取到的（同一句已自动去重）。需要处理的问题会在页面上用红色提示标出。</p>
    </el-dialog>
  </div>
</template>

<style scoped>
.dash-page { display: flex; flex-direction: column; gap: var(--gem-space-4); }
.dash-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: var(--gem-space-3); }
.dash-head h1 { margin: 0; font-size: 20px; font-weight: 600; color: var(--gem-text); }
.dash-head-right { display: flex; align-items: center; flex-wrap: wrap; gap: var(--gem-space-2); }
.dash-time { color: var(--gem-text-3); font-size: 12px; }
.dash-scope-label { color: var(--gem-text-3); font-size: 12px; cursor: help; }
.scope-select { width: 210px; }
.dash-range { color: var(--gem-text-2); font-size: 12px; font-variant-numeric: tabular-nums; }
.dash-alert { border-radius: var(--gem-radius-sm); }
/* align-items: stretch —— 让右侧图表与左侧三行指标卡**等高**：
   之前是 start，图表只有一行高，下面空一大块，看着不平衡。 */
.dash-body { display: grid; grid-template-columns: minmax(0, 1fr) 380px; gap: var(--gem-space-4); align-items: stretch; }
/* align-content: space-between —— 卡高贴合内容，多出来的高度分到"行与行之间"，
   这样右侧图表仍与左侧整块**上下对齐**，而卡内不会下半截空着。
   （默认的 stretch 会把高度匀给每一行 → 卡内出现大片空白；
     start 则让左侧整块变矮 → 图表底边对不上。） */
.metric-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--gem-space-3); align-content: space-between; }
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
.chart-head { display: flex; align-items: center; flex-wrap: wrap; gap: var(--gem-space-4); font-size: 12px; }
.chart-legend { display: inline-flex; align-items: center; gap: 6px; color: var(--gem-text-2); }
.chart-legend.muted { color: var(--gem-text-3); }
.dot { width: 8px; height: 8px; border-radius: var(--gem-radius-pill); display: inline-block; }
.dot-now { background: var(--gem-brand); }
.dot-prev { background: var(--gem-border-strong); }
.chart-svg { position: absolute; inset: 0; width: 100%; height: 100%; }
/* 绘图区**固定在一个舒服的比例**：不跟着卡片高度无限拉长（线会变得又瘦又难看）；
   卡片多出来的高度交给下面的"合计/日均/峰值"。 */
/* 折线区自然吃掉图表卡的剩余高度（不再有逐日明细撑着）：
   左右两列高度接近，指标卡行间距基本维持 12px，图表底边也能和卡片对齐。 */
.chart-plot { position: relative; flex: 1 1 auto; min-height: 180px; max-height: 320px; }
.chart-grid { stroke: var(--gem-border); stroke-width: .5; }
.chart-area { fill: url(#dashAreaFill); }
.area-stop.top { stop-color: var(--gem-brand); stop-opacity: .20; }
.area-stop.bottom { stop-color: var(--gem-brand); stop-opacity: 0; }
.chart-line { fill: none; stroke-width: 2; vector-effect: non-scaling-stroke; stroke-linejoin: round; stroke-linecap: round; }
.chart-line.now { stroke: var(--gem-brand); stroke-width: 2.2; }
.chart-line.prev { stroke: var(--gem-border-strong); stroke-dasharray: 4 3; }
.chart-guide { position: absolute; top: 0; bottom: 0; width: 1px; background: var(--gem-border-strong); pointer-events: none; }
.chart-dot {
  position: absolute; width: 7px; height: 7px; margin: -4px 0 0 -4px; border-radius: var(--gem-radius-pill);
  background: var(--gem-surface); border: 2px solid var(--gem-brand); pointer-events: none;
}
.chart-dot.prev { border-color: var(--gem-border-strong); }
.chart-tip {
  position: absolute; top: -4px; transform: translateX(-50%); padding: 4px 8px; white-space: nowrap;
  display: flex; flex-direction: column; gap: 2px;
  background: var(--gem-surface); border: 1px solid var(--gem-border-strong); border-radius: var(--gem-radius-sm);
  box-shadow: var(--gem-shadow-sm); color: var(--gem-text-2); font-size: 12px;
  font-variant-numeric: tabular-nums; pointer-events: none;
}
.tip-line.now { color: var(--gem-text); }
.tip-line.prev { color: var(--gem-text-3); }
.chart-axis { position: relative; height: 16px; margin-top: 6px; color: var(--gem-text-3); font-size: 11px; }
/* 逐日明细：吃掉卡片里剩下的高度；天数多时内部滚动，不会把卡片撑高 */
/* .chart-days（逐日明细）已移除：它把图表撑高，导致左侧行间距被撑开 */

/* 汇总行：吃掉图表卡里多出来的高度，比把折线拉长好看也更有用 */
.chart-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--gem-space-2); margin-top: auto; padding-top: var(--gem-space-2); border-top: 1px solid var(--gem-border); }
.summary-cell { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.summary-cell span { color: var(--gem-text-3); font-size: 11px; }
.summary-cell strong { color: var(--gem-text); font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums; overflow: hidden; text-overflow: ellipsis; }
.chart-tick { position: absolute; top: 0; transform: translateX(-50%); white-space: nowrap; }
.dialog-tip { margin: 8px 0 0; color: var(--gem-text-3); font-size: 12px; }
.readme-title { margin: 0 0 8px; font-size: 13px; font-weight: 600; color: var(--gem-text); }
.readme-title:not(:first-child) { margin-top: var(--gem-space-4); }
.quick-ranges { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: var(--gem-space-3); }
.range-pickers { display: flex; align-items: center; gap: 8px; }
.range-sep { color: var(--gem-text-3); font-size: 13px; }
@media (max-width: 1400px) { .dash-body { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 1200px) { .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (prefers-reduced-motion: reduce) { .metric-card { transition: none; } }
</style>
