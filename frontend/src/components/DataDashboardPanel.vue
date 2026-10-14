<script setup>
// 数据面板：按平台后台「数据概览」的样子看经营数字（支付金额 / 净支付 / 退款 / 子订单数 / 买家数 …）。
//
// 数据从哪来（页面必须能自证，不能让人猜）：
//   ① 平台自带数据概览（微信罗盘：成交金额/成交人数/成交订单数/退款金额/商品点击人数）—— 官方口径，优先用；
//   ② 订单接口聚合（两家平台都有）—— 兜底口径。
//   人数（支付买家数/下单人数）在官方口径缺失时用**订单里的买家标识当天去重**自算：
//   小红书没有罗盘，但订单接口带 xhsOpenId，所以这两个数能算出来（标识不落库，只落个数）。
//   流量（访客/点击）两家都拿不到——小红书开放平台完全没有这类接口，所以「支付转化率」
//   对小红书只能是"—"，不编数；能算的是「下单后转化率」（下单之后的转化）。
//   每个数字右上角的 ⓘ 里都写清了它用的哪套口径。
//
// 图上没有的"实时/周/月"：平台接口只提供**按日**数据，所以这里的 周/月 是我们把日数据归并出来的，
// "实时"给不了（接口层不存在），干脆不摆一个点不动的按钮。
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, ArrowRight, Download, InfoFilled, Refresh, Search, Setting } from '@element-plus/icons-vue'
import { bulkApi } from '../bulkApi'
import { shops } from '../shopContext'
import { useIsMobile } from '../useResponsive'

const isMobile = useIsMobile()
const loading = ref(false)
const syncing = ref(false)
// 手动同步的后台任务（POST /orders/sync 只登记任务，进度靠轮询 /orders/sync/status）
const syncJobId = ref('')
const syncJob = reactive({ status: '', done: 0, total: 0, synced: 0, failed: 0, current: '', message: '', range: '' })
let syncPollTimer = null
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
  { key: 'buyers', label: '支付买家数', kind: 'int-optional', tip: '微信取罗盘「成交人数」；小红书平台没开放人数接口，用订单里的买家标识**当天去重**自算（标识不落库）' },
  { key: 'conversion_rate', label: '支付转化率', kind: 'rate-optional', tip: '下单**之前**的转化，近似口径 = 支付买家数 ÷ 商品点击人数。小红书没有任何流量接口，分母拿不到 → 显示 —' },
  { key: 'created_buyers', label: '下单人数', kind: 'int-optional', tip: '按订单创建时间去重买家自算（含未支付/已取消）。两个平台都没有店铺级的官方「下单人数」接口——微信罗盘的下单人数只在商品级接口里' },
  { key: 'created_conversion_rate', label: '下单后转化率', kind: 'rate-optional', tip: '下单**之后**的转化 = 支付买家数 ÷ 下单人数。和上面「支付转化率」不是一回事，别混用' },
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

const pad2 = (n) => String(n).padStart(2, '0')

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
  // 库里存的是 UTC 时间（带 +00:00），页面必须按本地时区显示，否则会差 8 个小时
  const at = new Date(state.lastSyncedAt)
  const time = Number.isNaN(at.getTime())
    ? String(state.lastSyncedAt).slice(0, 16).replace('T', ' ')
    : `${at.getFullYear()}-${pad2(at.getMonth() + 1)}-${pad2(at.getDate())} ${pad2(at.getHours())}:${pad2(at.getMinutes())}`
  return `统计时间 ${time}（平台多为 T+1，当天数据次日才完整）`
})
// 同步按钮文案：跑起来后显示「同步中 12/120」，别让按钮看着像卡死
const syncButtonText = computed(() => {
  if (!syncing.value) return '同步数据'
  if (syncJob.status !== 'running' || !syncJob.total) return '同步中…'
  return `同步中 ${syncJob.done}/${syncJob.total}`
})
// 把「正在同步哪段区间」写出来：面板当前选的是别的区间时，用户看不到数字在动，
// 不知道该切到哪去看——这一行就是告诉他去哪儿看。
const syncHintText = computed(() => {
  if (!syncing.value) return ''
  const range = syncJob.range
  const scope = range && range.start && range.end ? ` ${range.start} ~ ${range.end}` : ''
  return `正在同步${scope}，已同步的会陆续显示，不用等全部跑完`
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

// ============ 商品明细（下方那块，按单个商品摊开）============
const productLoading = ref(false)
const productKeyword = ref('')
const productSort = ref('amount')
const productLimit = ref(20)
const productState = reactive({ rows: [], totals: {}, prevTotals: {}, productTotal: 0, statsNote: '' })

async function loadProducts(silent = false) {
  // 静默刷新（同步过程中每隔几秒刷一次）不要让商品表自己转圈，否则每隔几秒闪一下
  if (!silent) productLoading.value = true
  try {
    const start = toDay(range.value?.[0])
    const end = toDay(range.value?.[1])
    const data = await bulkApi.orderProductMetrics(start, end, '', panelShop.value,
                                                  productKeyword.value.trim(),
                                                  productSort.value, productLimit.value, 0)
    const result = data?.result || {}
    productState.rows = result.rows || []
    productState.totals = result.totals || {}
    productState.prevTotals = result.prev_totals || {}
    productState.productTotal = result.product_total || 0
    productState.statsNote = result.stats_note || ''
    await loadProductImages(productState.rows)
  } catch (error) {
    ElMessage.error(`商品维度读取失败：${error.message}`)
    productState.rows = []
    productState.statsNote = ''
  } finally {
    if (!silent) productLoading.value = false
  }
}
// ============ 交易明细（最下面那块，订单级）============
// 为什么不跟着上面的区间走：交易明细是**按单日**实时拉平台接口的（后端不落订单明细表），
// 跟着 28 天的区间会变成几十次平台请求、几百秒。所以它有自己的单日游标，
// 只共用面板的「店铺范围」，这样口径一致、耗时也可控。
const detail = reactive({
  day: ymd(new Date()), platform: '', keyword: '',
  page: 1, pageSize: 20, rows: [], total: 0, sumPaidAmount: 0,
  notes: [], loading: false, error: '', loaded: false,
})
// 明细里的时间戳后端已经转成**本地时区**秒级，直接格式化即可（不要再按 UTC 处理）
function orderTime(ts) {
  if (!ts) return '—'
  const at = new Date(Number(ts) * 1000)
  if (Number.isNaN(at.getTime())) return '—'
  return `${at.getFullYear()}-${pad2(at.getMonth() + 1)}-${pad2(at.getDate())} ${pad2(at.getHours())}:${pad2(at.getMinutes())}:${pad2(at.getSeconds())}`
}
const orderShopText = (row) => row.shop_name || row.shop_id || '—'
function shiftDetailDay(step) {
  const next = new Date(detail.day)
  if (Number.isNaN(next.getTime())) return
  next.setDate(next.getDate() + step)
  if (next > new Date()) return          // 明细是"某一天已发生的订单"，不让选未来
  detail.day = ymd(next)
  detail.page = 1
  loadDetails()
}
// 相对今天偏移 n 天（0=今天，-1=昨天，-7=上周同日）
function jumpDetailDay(offset) {
  const target = daysAgo(-offset)
  if (target > new Date()) return
  if (detail.day === ymd(target)) return   // 已经在这一天就别白拉一次接口
  detail.day = ymd(target)
  detail.page = 1
  loadDetails()
}
// 日历里禁掉未来日期：平台当天数据本来就要 T+1 才完整，选未来没意义
function disableFutureDay(date) {
  return date.getTime() > Date.now()
}
function onDetailDayPick(value) {
  if (!value) return
  detail.day = value
  detail.page = 1
  loadDetails()
}
async function loadDetails() {
  detail.loading = true
  detail.error = ''
  try {
    const data = await bulkApi.orderDetails(detail.day, detail.platform, panelShop.value,
                                             detail.keyword.trim(), detail.pageSize,
                                             (detail.page - 1) * detail.pageSize)
    const result = data?.result || {}
    detail.rows = result.rows || []
    detail.total = result.total || 0
    detail.sumPaidAmount = result.sum_paid_amount || 0
    detail.notes = result.notes || []
    detail.loaded = true
  } catch (error) {
    detail.error = error.message
    detail.rows = []
    detail.total = 0
  } finally {
    detail.loading = false
  }
}
// 商品成本/运费成本：平台订单接口没有这两个字段，本地也没有成本表，
// 所以先按示例做成「配置」入口，点开说明数据从哪来——别拿 0 冒充成本。
function costTip(kind) {
  ElMessageBox.alert(
    kind === '商品成本'
      ? '商品成本 = 各 SKU 的采购成本 × 数量。目前平台订单接口和本地货盘表都没有成本字段，'
        + '需要先在货盘表加「成本价」列，或接 ERP 的成本数据。'
      : '运费成本 = 每单实际承担的运费。平台订单接口只有「向买家收取的运费」（微信 freight），'
        + '不是我们的成本；真实成本要按物流结算单算，目前没有数据源。',
    kind, { confirmButtonText: '知道了' })
}
const productPlatformText = (platform) => platformText(platform)
// 商品级流量/转化：只有平台统计接口真的返回了(stats_source 非空)才显示数字。
// 判断必须用 visits_available，不能看数值是否为 0 —— 0 分不清"真的没人看"和"平台没这数据"。
const productVisits = (row) => (row.visits_available ? row.visits : null)
// 下单人数（罗盘 create_uv）与访客数共用同一个「平台有没有给数据」开关。
// 原「加购数」占位列已删除：加购发生在成单之前，罗盘商品级接口返回的 31 个字段里没有加购、
// 订单接口也只有成单后的数据，三条路都拿不到；换成平台确实提供的「下单人数」，
// 正好补上 点击 → 下单 → 支付 漏斗中间那一环。
const productCreateUv = (row) => (row.visits_available ? row.create_uv : null)
const productConversion = (row) => {
  if (!row.visits_available || row.conversion_rate === null || row.conversion_rate === undefined) return null
  return (row.conversion_rate * 100).toFixed(1) + '%'
}
const productImageCache = reactive({})
function productImageKey(row) { return String(row.platform) + '|' + String(row.product_id) }
function productImageUrl(row) { return productImageCache[productImageKey(row)] || '' }
async function loadProductImages(rows) {
  await Promise.all(rows.filter((row) => row.product_id).map(async (row) => {
    const key = productImageKey(row)
    if (productImageCache[key] !== undefined) return
    // 优先用订单里带回来的图（平台在订单里直接给了 SKU 主图）：
    // 无需再请求平台，也不会因为商品已下架/不在商品列表里而变成「无图」。
    if (row.image_url) { productImageCache[key] = row.image_url; return }
    try {
      const data = await bulkApi.orderProductImage(
        row.platform, row.product_id, panelShop.value, row.shop_id || '')
      productImageCache[key] = (data && data.result && data.result.url) || ''
    } catch (error) { productImageCache[key] = '' }
  }))
}
const productDelta = (row) => {
  if (row.delta === null || row.delta === undefined) return { text: '—', up: false, muted: true }
  return { text: `${Math.abs(row.delta * 100).toFixed(1)}%`, up: row.delta > 0, muted: false }
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

// 取数主体：load() 管整页 loading 遮罩与报错提示；同步过程中的「边跑边出数」直接调
// fetchPanel(true) 静默刷新——否则每隔几秒整页闪一次加载中，比不同步还难受。
async function fetchPanel(silent = false) {
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
  await loadProducts(silent)          // 下面的商品维度用同一区间/店铺范围，跟着一起刷新
}

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    await fetchPanel(false)
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
    ElMessage.success('已导出：汇总 + 各平台日销售 + 各平台交易清单 + 口径说明')
  } catch (error) {
    ElMessage.error(`导出失败：${error.message}`)
  } finally {
    exporting.value = false
  }
}

async function syncNow() {
  const start = toDay(range.value?.[0])
  const end = toDay(range.value?.[1])
  // 同步也只同步本页选的范围（all = 逐店同步，会慢一些）。
  // POST 现在只登记任务、立即返回，真正的进度靠轮询 /orders/sync/status
  syncing.value = true
  Object.assign(syncJob, { status: 'running', done: 0, total: 0, synced: 0, failed: 0, current: '', message: '' })
  syncLoadedDone = 0                 // 新任务从头开始边跑边出数
  syncLastRefreshAt = 0
  try {
    const data = await bulkApi.orderSync({ start, end, shop: panelShop.value })
    const result = data?.result || {}
    syncJobId.value = result.job_id || ''
    applySyncJob(result)
    if (result.status === 'running') startSyncPolling()
    else await finishSync(result)
  } catch (error) {
    ElMessage.error(`同步失败：${error.message}`)
    stopSyncPolling()
    syncing.value = false
  }
}

function applySyncJob(job) {
  if (!job || !job.job_id) return
  Object.assign(syncJob, {
    status: job.status || '', done: job.done || 0, total: job.total || 0,
    synced: job.synced || 0, failed: job.failed || 0, current: job.current || '',
    message: job.message || '', range: job.range || '',
  })
}

function startSyncPolling() {
  stopSyncPolling()
  syncPollTimer = setInterval(pollSyncJob, 1500)
}

function stopSyncPolling() {
  if (syncPollTimer) {
    clearInterval(syncPollTimer)
    syncPollTimer = null
  }
}

// 「边同步边出数」：每 SYNC_REFRESH_MS 毫秒静默刷一次面板，所以 100+ 天的同步
// 不用等全部跑完才看到数——已经落库的那部分先显示出来，数字会随进度往上走。
// 三个限制别去掉：
//   ① done 没变不刷（没新数据就别白拉一次接口）；
//   ② 3 秒节流（100 多步时按 1.5s 轮询直接刷会把接口打满）；
//   ③ 同一时刻只跑一次刷新（load 还没回来就不再发起）。
const SYNC_REFRESH_MS = 3000
let syncLoadedDone = 0
let syncLastRefreshAt = 0
let syncRefreshing = false

async function refreshPanelSilently(force = false) {
  if (syncRefreshing) return
  if (!force) {
    if (syncJob.done <= syncLoadedDone) return
    if (Date.now() - syncLastRefreshAt < SYNC_REFRESH_MS) return
  }
  syncRefreshing = true
  syncLastRefreshAt = Date.now()
  syncLoadedDone = Math.max(syncLoadedDone, syncJob.done)
  try {
    await fetchPanel(true)
  } catch { /* 静默刷新失败不打扰用户：结束时还会再整页刷一次 */ } finally {
    syncRefreshing = false
  }
}

async function pollSyncJob() {
  try {
    const data = await bulkApi.orderSyncStatus(syncJobId.value)
    const result = data?.result || {}
    // 服务重启过：任务没了，前端不能再傻等，直接按结束处理并刷新面板
    if (result.status === 'idle') {
      stopSyncPolling()
      await finishSync({ status: 'done', synced: 0, failed: 0 })
      return
    }
    applySyncJob(result)
    if (result.status !== 'running') {
      stopSyncPolling()
      await finishSync(result)
      return
    }
    await refreshPanelSilently()
  } catch (error) {
    stopSyncPolling()
    syncing.value = false
    ElMessage.error(`读取同步进度失败：${error.message}`)
  }
}

async function finishSync(result) {
  const synced = result.synced || 0
  const failed = result.failed || 0
  if (result.status === 'failed') {
    ElMessage.error(`同步失败：${result.message || '未知错误'}`)
  } else if (failed) {
    const first = (result.errors || [])[0]
    ElMessage.warning(`同步完成：成功 ${synced} 天、失败 ${failed} 天（${first?.error?.slice(0, 60) || ''}）`)
  } else if (result.range) {
    ElMessage.success(`同步完成：${synced} 条（${result.range.start} ~ ${result.range.end}）`)
  } else {
    ElMessage.success('同步已结束，数据已刷新')
  }
  await load()
  syncing.value = false
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
// 打开「自定义统计区间」弹窗。
// 每次都把日期框预填成**当前正在看的那段**：在自定义模式里再点一次「自定义」，
// 是想微调这段区间，而不是又跳回默认的近 7 天（原来预填逻辑只挂在粒度切换上，
// 结果第二次点按钮连弹窗都打不开）。
function openCustomRange() {
  customStart.value = range.value?.[0] || ymd(daysAgo(6))
  customEnd.value = range.value?.[1] || ymd(new Date())
  customVisible.value = true
}

// 切粒度时给一个"默认窗口"，每个窗口都对齐自然边界：
//   日 → 近 7 天（含今天）
//   周 → 近 4 个**完整**自然周（周一~周日；不含本周，因为本周还没过完，环比会假跌）
//   月 → 近 3 个**完整**自然月（不含本月，同理）
function applyGranularity(value) {
  granularity.value = value
  if (value === 'custom') {
    openCustomRange()
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
// 商品维度：排序/显示条数/搜索词变化时重拉（搜索做 350ms 防抖，别每敲一个字打一次接口）
watch([productSort, productLimit], loadProducts)
let productKeywordTimer = null
watch(productKeyword, () => {
  clearTimeout(productKeywordTimer)
  productKeywordTimer = setTimeout(loadProducts, 350)
})
// 交易明细：跟着店铺范围走（口径与上面一致）；订单号搜索同样 350ms 防抖，别每敲一个字打一次平台
watch(() => panelShop.value, () => { detail.page = 1; loadDetails() })
watch(() => detail.platform, () => { detail.page = 1; loadDetails() })
let detailKeywordTimer = null
watch(() => detail.keyword, () => {
  clearTimeout(detailKeywordTimer)
  detailKeywordTimer = setTimeout(() => { detail.page = 1; loadDetails() }, 350)
})

onMounted(async () => {
  if (!shops.value.length) {
    // 顶栏的店铺清单一般已加载；直接打开本页（如刷新后停在数据面板）时自己补一次
    try {
      const data = await bulkApi.shops()
      shops.value = data?.result || []
    } catch { /* 拉不到就只显示"全部店铺汇总" */ }
  }
  await load()
  await loadProducts()
  await loadDetails()      // 交易明细：按单日实时拉，第一次进页面就把当天明细取回来
  // 同步过程中刷新页面：接着轮询后台任务，按钮继续显示进度
  try {
    const data = await bulkApi.orderSyncStatus()
    const result = data?.result || {}
    if (result.status === 'running') {
      syncJobId.value = result.job_id || ''
      applySyncJob(result)
      syncing.value = true
      startSyncPolling()
    }
  } catch { /* 读不到就算了，不影响面板 */ }
})

onUnmounted(stopSyncPolling)
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
          <!-- 已经处于自定义模式时再点这个按钮，radio 不会发 update:model-value（选中态没变），
               弹窗就打不开了。所以额外挂一个 click：不管当前是什么模式，点它都开弹窗。 -->
          <el-radio-button value="custom" @click="openCustomRange">自定义</el-radio-button>
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
        <el-button type="primary" size="small" :icon="Refresh" :loading="syncing" @click="syncNow">{{ syncButtonText }}</el-button>
        <span v-if="syncing" class="dash-sync-hint">{{ syncHintText }}</span>
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

    <!-- 商品明细：把上面按"天/平台"的汇总，按单个商品摊开 -->
    <section class="dash-products">
      <div class="dash-head">
        <h1>商品明细</h1>
        <div class="dash-head-right">
          <el-input v-model="productKeyword" clearable size="small" :prefix-icon="Search"
                    placeholder="搜索商品名 / 商家编码 / 商品ID" style="width: 250px" />
          <el-select v-model="productSort" size="small" style="width: 132px">
            <el-option label="按支付金额" value="amount" />
            <el-option label="按支付件数" value="qty" />
            <el-option label="按订单数" value="orders" />
            <el-option label="按商品名" value="name" />
          </el-select>
          <span class="dash-time">共 {{ productState.productTotal }} 个商品</span>
        </div>
      </div>
      <p class="product-note">
        金额按「订单实付」等比分摊到商品（优惠、运费都摊过），所以各商品之和 = 上面的支付金额；
        退款仍按订单口径，未用平台商品级退款金额（口径不同，只作参考）。
      </p>
      <p v-if="productState.statsNote" class="product-note product-note-stats">
        {{ productState.statsNote }}
      </p>
      <el-table v-loading="productLoading" :data="productState.rows" size="small"
                empty-text="该区间还没有商品级数据，请先点右上角「同步数据」">
        <el-table-column label="图" width="64">
          <template #default="{ row }">
            <el-image v-if="productImageUrl(row)" :src="productImageUrl(row)" fit="cover"
                      class="p-thumb" :preview-src-list="[productImageUrl(row)]"
                      preview-teleported hide-on-click-modal />
            <span v-else class="p-thumb p-thumb-empty">无图</span>
          </template>
        </el-table-column>
        <el-table-column label="商品" min-width="280">
          <template #default="{ row }">
            <div class="p-cell">
              <el-tag size="small" effect="light" round
                      :type="row.platform === 'wechat' ? 'success' : 'danger'">{{ productPlatformText(row.platform) }}</el-tag>
              <span class="p-name" :title="row.product_name || row.product_id">
                {{ row.product_name || '（平台未返回商品名）' }}
              </span>
              <span v-if="row.sku_code" class="p-code">{{ row.sku_code }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="支付金额" width="130" align="right">
          <template #default="{ row }">{{ money(row.paid_amount) }}</template>
        </el-table-column>
        <el-table-column label="占比" width="160">
          <template #default="{ row }">
            <div class="p-share">
              <span class="p-bar"><i :style="{ width: Math.min(100, row.share * 300) + '%' }" /></span>
              <span class="p-share-text">{{ (row.share * 100).toFixed(1) }}%</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="支付件数" width="100" align="right">
          <template #default="{ row }">{{ row.paid_qty }}</template>
        </el-table-column>
        <el-table-column label="商品访客数" width="118" align="right">
          <template #header>
            <span class="col-head">
              商品访客数
              <el-tooltip placement="top" effect="light" content="微信=罗盘「商品点击人数」（平台没有「详情页访客数」这个口径）；小红书未开放统计接口，显示「—」；当天数据平台要 T+1 才有">
                <el-icon class="col-info"><InfoFilled /></el-icon>
              </el-tooltip>
            </span>
          </template>
          <template #default="{ row }">
            <span v-if="productVisits(row) !== null">{{ productVisits(row) }}</span>
            <span v-else class="col-na">—</span>
          </template>
        </el-table-column>
        <el-table-column label="下单人数" width="112" align="right">
          <template #header>
            <span class="col-head">
              下单人数
              <el-tooltip placement="top" effect="light" content="微信罗盘「下单人数」：点过商品又下了单的人数（下单未支付也算）；小红书未开放统计接口，显示「—」；当天数据平台要 T+1 才有">
                <el-icon class="col-info"><InfoFilled /></el-icon>
              </el-tooltip>
            </span>
          </template>
          <template #default="{ row }">
            <span v-if="productCreateUv(row) !== null">{{ productCreateUv(row) }}</span>
            <span v-else class="col-na">—</span>
          </template>
        </el-table-column>
        <el-table-column label="支付转化率" width="112" align="right">
          <template #header>
            <span class="col-head">
              支付转化率
              <el-tooltip placement="top" effect="light" content="成交人数 ÷ 商品点击人数（按平台口径自算，平台没给现成字段）；小红书未开放统计接口，显示「—」">
                <el-icon class="col-info"><InfoFilled /></el-icon>
              </el-tooltip>
            </span>
          </template>
          <template #default="{ row }">
            <span v-if="productConversion(row) !== null">{{ productConversion(row) }}</span>
            <span v-else class="col-na">—</span>
          </template>
        </el-table-column>
        <el-table-column label="订单数" width="90" align="right">
          <template #default="{ row }">{{ row.order_count }}</template>
        </el-table-column>
        <el-table-column label="较上一周期" width="120" align="right">
          <template #default="{ row }">
            <span :class="['p-delta', productDelta(row).up ? 'up' : 'down', { muted: productDelta(row).muted }]">
              <span class="caret">{{ productDelta(row).up ? '▲' : '▼' }}</span>{{ productDelta(row).text }}
            </span>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="productState.productTotal > productState.rows.length" class="p-more">
        <el-button size="small" text type="primary" @click="productLimit += 20">
          显示更多（已显示 {{ productState.rows.length }} / {{ productState.productTotal }}）
        </el-button>
      </div>
      <p v-if="productState.rows.length" class="product-sum">
        合计：{{ productState.totals.products }} 个商品 · 支付 {{ money(productState.totals.paid_amount) }} 元 ·
        {{ productState.totals.paid_qty }} 件<template v-if="productState.totals.visits"> ·
        访客 {{ productState.totals.visits }} 人 · 下单 {{ productState.totals.create_uv }} 人 ·
        成交 {{ productState.totals.buyers }} 人</template> ·
        上一周期 {{ money(productState.prevTotals.paid_amount) }} 元
      </p>
    </section>

    <!-- 交易明细：订单级明细。与上面同一个 .dash-page 内、紧邻其下，共用店铺范围；
         数据是按单日**实时**拉平台接口（后端不落订单明细表），有 5 分钟缓存。 -->
    <section class="dash-orders">
      <div class="dash-head">
        <h1>交易明细</h1>
        <div class="dash-head-right">
          <el-input v-model="detail.keyword" size="small" class="order-search"
                    placeholder="输入订单号" clearable :prefix-icon="Search" />
          <el-select v-model="detail.platform" size="small" class="platform-select">
            <el-option value="" label="全部平台" />
            <el-option value="wechat" label="微信小店" />
            <el-option value="xhs" label="小红书" />
          </el-select>
          <span class="dash-time">统计时间</span>
          <el-date-picker v-model="detail.day" type="date" value-format="YYYY-MM-DD"
                          format="YYYY-MM-DD" size="small" class="day-picker"
                          :clearable="false" :disabled-date="disableFutureDay"
                          placeholder="选择日期" @change="onDetailDayPick" />
          <el-button-group>
            <el-button size="small" :icon="ArrowLeft" @click="shiftDetailDay(-1)" title="前一天" />
            <el-button size="small" :icon="ArrowRight" @click="shiftDetailDay(1)" title="后一天" />
          </el-button-group>
          <!-- 只有前后按钮时翻一天要一直点，所以再给三个"一步到位"的跳转 -->
          <el-button-group class="day-quick">
            <el-button size="small" @click="jumpDetailDay(0)">今天</el-button>
            <el-button size="small" @click="jumpDetailDay(-1)">昨天</el-button>
            <el-button size="small" @click="jumpDetailDay(-7)">上周同日</el-button>
          </el-button-group>
        </div>
      </div>
      <el-alert v-if="detail.error" type="error" show-icon :closable="false" class="dash-alert"
                title="交易明细读取失败" :description="detail.error" />
      <p v-else-if="detail.notes.length" class="product-note">
        <span v-for="(note, index) in detail.notes" :key="index">{{ note }}<template v-if="index < detail.notes.length - 1">；</template></span>
      </p>
      <el-table v-loading="detail.loading" :data="detail.rows" size="small"
                empty-text="该日期没有订单明细">
        <!-- 列宽用 min-width：容器多出来的宽度会由 el-table 按比例分给各列，表格能撑满
             （写死 width 的话右侧会空一大块）。表头一律走 .col-head（已带 nowrap），
             中文不会在窄列里被压成"确认收/货金/额"竖排。 -->
        <el-table-column min-width="230" show-overflow-tooltip>
          <template #header><span class="col-head">订单编号</span></template>
          <template #default="{ row }">
            <div class="p-cell">
              <el-tag size="small" effect="plain">{{ platformText(row.platform) }}</el-tag>
              <span class="p-code">{{ row.order_no }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column min-width="170" show-overflow-tooltip>
          <template #header><span class="col-head">店铺</span></template>
          <template #default="{ row }"><span class="order-shop">{{ orderShopText(row) }}</span></template>
        </el-table-column>
        <el-table-column min-width="160">
          <template #header><span class="col-head">订单创建时间</span></template>
          <template #default="{ row }"><span class="order-time">{{ orderTime(row.create_ts) }}</span></template>
        </el-table-column>
        <el-table-column min-width="160">
          <template #header><span class="col-head">支付时间</span></template>
          <template #default="{ row }"><span class="order-time">{{ orderTime(row.pay_ts) }}</span></template>
        </el-table-column>
        <el-table-column min-width="110" align="right">
          <template #header><span class="col-head">实付金额</span></template>
          <template #default="{ row }">{{ money(row.paid_amount) }}</template>
        </el-table-column>
        <el-table-column min-width="120" align="right">
          <template #header><span class="col-head">确认收货金额</span></template>
          <template #default="{ row }">
            <span v-if="row.confirm_amount !== null && row.confirm_amount !== undefined">{{ money(row.confirm_amount) }}</span>
            <span v-else class="col-na">—</span>
          </template>
        </el-table-column>
        <el-table-column min-width="100" align="right">
          <template #header><span class="col-head">商品成本</span></template>
          <template #default><el-button link type="primary" size="small" @click="costTip('商品成本')">配置</el-button></template>
        </el-table-column>
        <el-table-column min-width="100" align="right">
          <template #header><span class="col-head">运费成本</span></template>
          <template #default><el-button link type="primary" size="small" @click="costTip('运费成本')">配置</el-button></template>
        </el-table-column>
      </el-table>
      <p class="product-note">
        确认收货金额：平台订单接口没有这个字段，用「已完成订单的实付金额」近似，未完成的显示 —；
        商品成本 / 运费成本：暂无数据源（订单接口与货盘表都没有成本字段，平台只回「向买家收取的运费」），
        点「配置」看说明。
      </p>
      <div class="order-foot">
        <el-pagination
          :current-page="detail.page"
          :page-size="detail.pageSize"
          :page-sizes="[20, 50, 100]"
          :total="detail.total"
          :disabled="detail.loading"
          layout="sizes, prev, pager, next"
          @current-change="(value) => { detail.page = value; loadDetails() }"
          @size-change="(value) => { detail.pageSize = value; detail.page = 1; loadDetails() }"
        />
        <span class="muted-copy">当日实付合计 {{ money(detail.sumPaidAmount) }} 元</span>
      </div>
    </section>

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
.dash-sync-hint { color: var(--gem-text-4); font-size: 12px; }
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
/* ===================== 商品明细（下方面板） ===================== */
.dash-products {
  display: flex; flex-direction: column; gap: var(--gem-space-2);
  padding: var(--gem-space-4);
  background: var(--gem-surface); border: 1px solid var(--gem-border);
  border-radius: var(--gem-radius-md); box-shadow: var(--gem-shadow-sm);
}
.dash-products .dash-head h1 { font-size: 16px; }
/* 交易明细：与商品维度同一张卡片的观感，只多加搜索框/页脚两块 */
.dash-orders {
  display: flex; flex-direction: column; gap: var(--gem-space-2);
  padding: var(--gem-space-4);
  background: var(--gem-surface); border: 1px solid var(--gem-border);
  border-radius: var(--gem-radius-md); box-shadow: var(--gem-shadow-sm);
}
.dash-orders .dash-head h1 { font-size: 16px; }
/* 表头一律不换行。中文在任意位置都能断行，列一被挤窄就会变成"确认收/货金/额"这种
   竖排，行高被撑到近 200px（实测踩到）。右侧三列用固定 width 保证放得下，
   屏幕不够宽时让表格横向滚动，也不要把表头压成竖排。 */
.dash-orders :deep(.el-table th .cell) { white-space: nowrap; }
.dash-orders :deep(.el-table th) { white-space: nowrap; }
.dash-orders .col-head { white-space: nowrap; }
.dash-orders .col-info { flex: 0 0 auto; }
.order-search { width: 190px; }
.day-picker { width: 140px; }
.platform-select { width: 120px; }
.order-shop, .order-time { color: var(--gem-text-2); font-size: 13px; font-variant-numeric: tabular-nums; white-space: nowrap; }
/* 订单号 22 位、店铺名又长：都按单行省略，别把行高撑成两行三行 */
.dash-orders .order-shop { overflow: hidden; text-overflow: ellipsis; }
.dash-orders .p-code { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.order-foot { display: flex; align-items: center; justify-content: space-between; gap: var(--gem-space-2); flex-wrap: wrap; }
.order-foot .muted-copy { color: var(--gem-text-3); font-size: 12px; font-variant-numeric: tabular-nums; }
.product-note { margin: 0; color: var(--gem-text-3); font-size: 12px; }
.product-note-stats { color: var(--gem-text-2); }
.product-sum { margin: var(--gem-space-2) 0 0; color: var(--gem-text-2); font-size: 12px; font-variant-numeric: tabular-nums; }
.p-thumb { width: 40px; height: 40px; border-radius: var(--gem-radius-sm); border: 1px solid var(--gem-border); background: var(--gem-surface-2); }
.p-thumb-empty { display: inline-flex; align-items: center; justify-content: center; color: var(--gem-text-4); font-size: 11px; }
.col-head { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
.col-info { color: var(--gem-text-4); font-size: 13px; cursor: help; }
.col-na { color: var(--gem-text-4); }
.p-cell { display: flex; align-items: center; gap: 8px; min-width: 0; }
.p-name { color: var(--gem-text); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.p-code { color: var(--gem-text-4); font-size: 11px; font-family: var(--gem-font-mono); flex-shrink: 0; }
.p-share { display: flex; align-items: center; gap: 8px; }
.p-bar { flex: 1; min-width: 40px; height: 6px; border-radius: var(--gem-radius-pill); background: var(--gem-surface-2); overflow: hidden; }
.p-bar > i { display: block; height: 100%; border-radius: var(--gem-radius-pill); background: var(--gem-brand); }
.p-share-text { color: var(--gem-text-3); font-size: 12px; font-variant-numeric: tabular-nums; min-width: 42px; text-align: right; }
.p-delta { display: inline-flex; align-items: center; gap: 3px; font-size: 12px; font-variant-numeric: tabular-nums; }
.p-delta.up { color: var(--gem-danger); }
.p-delta.down { color: var(--gem-success); }
.p-delta.muted { color: var(--gem-text-3); }
.p-delta .caret { font-size: 10px; }
.p-more { display: flex; justify-content: center; padding-top: var(--gem-space-1); }
.readme-title { margin: 0 0 8px; font-size: 13px; font-weight: 600; color: var(--gem-text); }
.readme-title:not(:first-child) { margin-top: var(--gem-space-4); }
.quick-ranges { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: var(--gem-space-3); }
.range-pickers { display: flex; align-items: center; gap: 8px; }
.range-sep { color: var(--gem-text-3); font-size: 13px; }
@media (max-width: 1400px) { .dash-body { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 1200px) { .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (prefers-reduced-motion: reduce) { .metric-card { transition: none; } }
</style>
