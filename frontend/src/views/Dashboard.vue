<script setup>
import {
  ChatDotRound,
  Connection,
  DataAnalysis,
  Refresh,
  Timer,
  User,
} from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'
import EChart from '../components/EChart.vue'

const router = useRouter()
const loading = ref(true)
const refreshing = ref(false)
const countdown = ref(30)
const alerts = ref([])
const alertPage = ref(1)
const alertPageSize = 8
let refreshTimer

const metrics = ref([
  { key: 'conversations', label: '今日对话总数', value: 0, change: 0, icon: ChatDotRound, tone: 'orange', path: '/conversation-logs' },
  { key: 'active_users', label: '活跃用户数', value: 0, change: 0, icon: User, tone: 'blue', path: '/users' },
  { key: 'mcp_calls', label: 'MCP 调用总量', value: 0, change: 0, icon: Connection, tone: 'purple', path: '/mcp-services' },
  { key: 'avg_response_time', label: '平均响应时间', value: 0, suffix: '秒', change: 0, icon: Timer, tone: 'green', path: '/conversation-logs' },
])

const trendOption = ref({
  color: ['#ff6a1a'],
  tooltip: { trigger: 'axis' },
  grid: { left: 38, right: 20, top: 28, bottom: 28 },
  xAxis: { type: 'category', boundaryGap: false, data: [], axisLine: { lineStyle: { color: '#e6eaf0' } }, axisLabel: { color: '#8791a4' } },
  yAxis: { type: 'value', splitLine: { lineStyle: { color: '#eef1f6', type: 'dashed' } }, axisLabel: { color: '#8791a4' } },
  series: [{ type: 'line', smooth: true, symbol: 'circle', symbolSize: 7, data: [], areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(255,106,26,.24)' }, { offset: 1, color: 'rgba(255,106,26,0)' }] } }, lineStyle: { width: 3 } }],
})

const distributionOption = ref({
  color: ['#ff6a1a', '#4f7cff', '#8b5cf6', '#1db68a', '#f3b33d'],
  tooltip: { trigger: 'item', formatter: '{b}<br/>{c} 次（{d}%）' },
  legend: { bottom: 0, itemWidth: 9, itemHeight: 9, textStyle: { color: '#6f7b90' } },
  series: [{ type: 'pie', radius: ['47%', '70%'], center: ['50%', '43%'], avoidLabelOverlap: true, label: { show: false }, emphasis: { label: { show: true, fontSize: 15, fontWeight: 700 } }, data: [] }],
})

/**
 * 输入：无；读取浏览器本地日期。
 * 输出：``YYYY-MM-DD`` 格式的本地日期字符串。
 * 功能：为当天趋势和 MCP 分布接口生成日期参数。
 */
function today() {
  const now = new Date()
  const year = now.getFullYear()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

/**
 * 输入：后端四项指标对象 ``overview``。
 * 输出：无；原地更新页面统计卡片。
 * 功能：兼容数值或 ``{ value, change }`` 两种指标响应结构。
 */
function applyOverview(overview) {
  for (const metric of metrics.value) {
    const source = overview[metric.key]
    metric.value = typeof source === 'object' ? source.value : (source ?? 0)
    metric.change = typeof source === 'object' ? (source.change ?? 0) : (overview[`${metric.key}_change`] ?? 0)
  }
}

/**
 * 输入：按小时返回的趋势数组 ``trend``。
 * 输出：无；更新折线图横轴与序列。
 * 功能：将后端小时统计转换为 ECharts 可消费的数据。
 */
function applyTrend(trend) {
  const hours = []
  const values = []
  for (const item of trend) {
    hours.push(item.hour)
    values.push(item.count)
  }
  trendOption.value.xAxis.data = hours
  trendOption.value.series[0].data = values
}

/**
 * 输入：按 MCP 服务返回的调用量数组 ``distribution``。
 * 输出：无；更新环形图数据，并排除调用量为零的服务。
 * 功能：按提示词要求隐藏无调用量扇区。
 */
function applyDistribution(distribution) {
  const data = []
  for (const item of distribution) {
    if (item.value > 0) data.push({ name: item.name, value: item.value })
  }
  distributionOption.value.series[0].data = data
}

/**
 * 输入：布尔值 ``silent`` 表示是否静默处理本轮失败。
 * 输出：完成四个仪表盘请求的 Promise；失败时保留既有数据。
 * 功能：并发刷新指标、趋势、MCP 分布和最近 20 条告警。
 */
async function loadDashboard(silent = false) {
  if (silent) refreshing.value = true
  else loading.value = true
  try {
    const date = today()
    const results = await Promise.all([
      api.get('/dashboard/overview', { silent }),
      api.get('/dashboard/trend', { params: { date }, silent }),
      api.get('/dashboard/mcp-distribution', { params: { date }, silent }),
      api.get('/dashboard/alerts', { params: { limit: 20 }, silent }),
    ])
    applyOverview(results[0])
    applyTrend(results[1].items || results[1])
    applyDistribution(results[2].items || results[2])
    alerts.value = results[3].items || results[3]
    countdown.value = 30
  } catch {
    // 自动刷新按页面提示静默保留上次成功数据。
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

/**
 * 输入：无；读取当前倒计时。
 * 输出：无；每秒递减，并在归零时静默刷新仪表盘。
 * 功能：驱动 30 秒自动刷新和页面倒计时展示。
 */
function tickCountdown() {
  if (refreshing.value) return
  countdown.value -= 1
  if (countdown.value <= 0) loadDashboard(true)
}

/**
 * 输入：指标卡片 ``metric``，包含目标路由 ``path``。
 * 输出：路由跳转 Promise。
 * 功能：将统计卡片点击操作导航到对应管理详情页。
 */
function openMetric(metric) {
  return router.push(metric.path)
}

/**
 * 输入：Element Plus 表格行上下文 ``rowContext``。
 * 输出：error 告警对应的高亮类名，否则为空字符串。
 * 功能：为严重告警整行增加浅红色视觉提示。
 */
function alertRowClass(rowContext) {
  return rowContext.row.level === 'error' ? 'error-alert-row' : ''
}

/**
 * 输入：告警级别 ``level``。
 * 输出：Element Plus 标签类型。
 * 功能：统一 info、warning、error 的状态色映射。
 */
function alertTagType(level) {
  if (level === 'error') return 'danger'
  if (level === 'warning') return 'warning'
  return 'info'
}

/**
 * 输入：无；读取当前告警页码和每页数量。
 * 输出：本页需要展示的告警数组。
 * 功能：对最近 20 条告警做纯前端分页。
 */
function pagedAlerts() {
  const start = (alertPage.value - 1) * alertPageSize
  return alerts.value.slice(start, start + alertPageSize)
}

/**
 * 输入：无。
 * 输出：无；加载首屏并启动一秒定时器。
 * 功能：初始化仪表盘数据和自动刷新机制。
 */
function mountDashboard() {
  loadDashboard()
  refreshTimer = window.setInterval(tickCountdown, 1000)
}

/**
 * 输入：无；读取当前自动刷新定时器。
 * 输出：无；停止页面计时。
 * 功能：卸载仪表盘时避免后台定时器泄漏。
 */
function unmountDashboard() {
  window.clearInterval(refreshTimer)
}

const visibleAlerts = computed(pagedAlerts)

onMounted(mountDashboard)
onBeforeUnmount(unmountDashboard)
</script>

<template>
  <section>
    <div class="page-heading">
      <div>
        <h1>系统监控</h1>
        <p>实时掌握 Hermes Agent、MCP 数据网关与用户访问状态</p>
      </div>
      <div class="refresh-status">
        <el-icon :class="{ spinning: refreshing }"><Refresh /></el-icon>
        <span>{{ refreshing ? '正在刷新' : `${countdown} 秒后刷新` }}</span>
        <el-button text type="primary" @click="loadDashboard(false)">立即刷新</el-button>
      </div>
    </div>

    <el-skeleton v-if="loading" :rows="10" animated />

    <template v-else>
      <el-row :gutter="18" class="metrics-row">
        <el-col v-for="metric in metrics" :key="metric.key" :span="6">
          <el-card class="metric-card" shadow="never" @click="openMetric(metric)">
            <div class="metric-topline">
              <span :class="['metric-icon', metric.tone]"><el-icon><component :is="metric.icon" /></el-icon></span>
              <el-icon class="metric-link"><DataAnalysis /></el-icon>
            </div>
            <div class="metric-label">{{ metric.label }}</div>
            <div class="metric-value">{{ metric.value }}<small v-if="metric.suffix"> {{ metric.suffix }}</small></div>
            <div :class="['metric-change', metric.change > 0 ? 'up' : metric.change < 0 ? 'down' : 'flat']">
              <template v-if="metric.change !== 0">{{ metric.change > 0 ? '↑' : '↓' }} {{ Math.abs(metric.change) }}%</template>
              <template v-else>—</template>
              <span>较昨日</span>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="18" class="charts-row">
        <el-col :span="14">
          <el-card class="chart-card" shadow="never">
            <template #header>
              <div class="card-header"><div><strong>今日对话趋势</strong><small>按小时统计会话数量</small></div><el-tag effect="plain">今日</el-tag></div>
            </template>
            <EChart :option="trendOption" />
          </el-card>
        </el-col>
        <el-col :span="10">
          <el-card class="chart-card" shadow="never">
            <template #header>
              <div class="card-header"><div><strong>MCP 调用分布</strong><small>各业务服务调用占比</small></div><el-tag effect="plain">实时</el-tag></div>
            </template>
            <EChart :option="distributionOption" />
          </el-card>
        </el-col>
      </el-row>

      <el-card class="page-card alert-card" shadow="never">
        <template #header>
          <div class="card-header">
            <div><strong>最近告警事件</strong><small>最近 20 条系统运行事件</small></div>
            <el-tag type="success" effect="light">当前健康</el-tag>
          </div>
        </template>
        <el-table v-if="alerts.length" :data="visibleAlerts" :row-class-name="alertRowClass">
          <el-table-column prop="time" label="时间" width="180" />
          <el-table-column label="级别" width="110">
            <template #default="scope"><el-tag :type="alertTagType(scope.row.level)" effect="light">{{ scope.row.level }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="description" label="事件描述" min-width="420" />
          <el-table-column label="状态" width="120">
            <template #default="scope"><span :class="['resolution', scope.row.status === '已解决' ? 'resolved' : 'pending']"><i />{{ scope.row.status }}</span></template>
          </el-table-column>
        </el-table>
        <el-empty v-else class="healthy-empty" description="暂无告警事件，系统运行正常" />
        <div v-if="alerts.length > alertPageSize" class="table-footer">
          <el-pagination v-model:current-page="alertPage" background layout="prev, pager, next" :page-size="alertPageSize" :total="alerts.length" />
        </div>
      </el-card>
    </template>
  </section>
</template>

<style scoped>
.refresh-status { display: flex; align-items: center; gap: 8px; color: #7c8799; font-size: 12px; }
.spinning { animation: spin 1s linear infinite; }
.metrics-row { margin-bottom: 18px; }
.metric-card { cursor: pointer; transition: transform .2s, box-shadow .2s; }
.metric-card:hover { transform: translateY(-3px); box-shadow: 0 14px 34px rgb(23 32 51 / 9%) !important; }
.metric-topline { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.metric-icon { display: grid; width: 42px; height: 42px; place-items: center; border-radius: 12px; font-size: 20px; }
.metric-icon.orange { color: #ff6a1a; background: #fff0e7; }
.metric-icon.blue { color: #4577ee; background: #edf3ff; }
.metric-icon.purple { color: #8357df; background: #f3efff; }
.metric-icon.green { color: #16a577; background: #eaf9f3; }
.metric-link { color: #c4cad4; }
.metric-label { color: #748096; font-size: 12px; }
.metric-value { margin: 7px 0 8px; color: #141d31; font-size: 29px; font-weight: 750; letter-spacing: -.8px; }
.metric-value small { font-size: 14px; font-weight: 500; }
.metric-change { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; }
.metric-change span { color: #9aa3b2; font-weight: 400; }
.metric-change.up { color: #12a371; }
.metric-change.down { color: #df4a4a; }
.metric-change.flat { color: #9aa3b2; }
.charts-row { margin-bottom: 18px; }
.card-header { display: flex; align-items: center; justify-content: space-between; }
.card-header strong, .card-header small { display: block; }
.card-header strong { color: #202a40; font-size: 14px; }
.card-header small { margin-top: 5px; color: #8b95a6; font-size: 11px; font-weight: 400; }
.chart-card :deep(.el-card__body) { height: 326px; padding: 10px 18px 18px; }
.alert-card { margin-top: 0; }
.resolution { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; }
.resolution i { width: 7px; height: 7px; border-radius: 50%; }
.resolution.resolved { color: #17956d; }
.resolution.resolved i { background: #1bb57f; }
.resolution.pending { color: #d78824; }
.resolution.pending i { background: #e6a23c; }
.healthy-empty { --el-empty-description-margin-top: 8px; color: #19a777; }
:deep(.error-alert-row td.el-table__cell) { background: #fff2f2 !important; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
