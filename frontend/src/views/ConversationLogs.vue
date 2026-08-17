<script setup>
import { Download, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import api from '../api'

const platformOptions = [
  { value: 'feishu', label: '飞书' },
  { value: 'wecom', label: '企微' },
  { value: 'dingtalk', label: '钉钉' },
]

const topicOptions = ['退单分析', '订单追踪', '售后政策', '经营分析', '其他']
const loading = ref(false)
const detailLoading = ref(false)
const exporting = ref(false)
const conversations = ref([])
const users = ref([])
const total = ref(0)
const detailVisible = ref(false)
const currentDetail = ref()
const expandedMessages = reactive({})

/**
 * 输入：可选基准日期 ``now``，默认使用当前时间。
 * 输出：最近七天的 ``[开始日期, 结束日期]`` 字符串数组。
 * 功能：初始化对话审计页的默认日期范围。
 */
function defaultDateRange(now = new Date()) {
  const start = new Date(now)
  start.setDate(start.getDate() - 6)
  return [formatDate(start), formatDate(now)]
}

/**
 * 输入：日期对象 ``date``。
 * 输出：本地 ``YYYY-MM-DD`` 日期字符串。
 * 功能：避免 UTC 转换导致审计查询日期偏移。
 */
function formatDate(date) {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

const filters = reactive({
  dates: defaultDateRange(),
  user_id: '',
  platforms: [],
  topic: '',
  keyword: '',
  page: 1,
  size: 10,
})

/**
 * 输入：无；读取当前审计筛选和分页状态。
 * 输出：符合后端日志接口约定的查询参数对象。
 * 功能：将日期范围与多平台选择转换成扁平 URL 参数。
 */
function conversationParams() {
  return {
    start_date: filters.dates?.[0] || '',
    end_date: filters.dates?.[1] || '',
    user_id: filters.user_id,
    platform: filters.platforms.join(','),
    topic: filters.topic,
    keyword: filters.keyword,
    page: filters.page,
    size: filters.size,
  }
}

/**
 * 输入：无；读取当前日志筛选参数。
 * 输出：完成日志列表请求的 Promise。
 * 功能：加载分页会话并同步接口提供的可筛选用户列表。
 */
async function loadConversations() {
  loading.value = true
  try {
    const result = await api.get('/logs/conversations', { params: conversationParams() })
    conversations.value = result.items || []
    total.value = result.total || 0
    if (result.users) users.value = result.users
  } finally {
    loading.value = false
  }
}

/**
 * 输入：无。
 * 输出：无；重置页码并自动重新加载日志。
 * 功能：响应日期、用户、平台、话题和关键词筛选条件变更。
 */
function filtersChanged() {
  filters.page = 1
  loadConversations()
}

/**
 * 输入：日志列表中的会话摘要 ``conversation``。
 * 输出：完成会话详情请求的 Promise。
 * 功能：打开右侧抽屉并加载完整多轮消息、MCP 调用链与汇总数据。
 */
async function openDetail(conversation) {
  detailVisible.value = true
  detailLoading.value = true
  currentDetail.value = undefined
  for (const key in expandedMessages) delete expandedMessages[key]
  try {
    currentDetail.value = await api.get(`/logs/conversations/${conversation.session_id}/detail`)
  } finally {
    detailLoading.value = false
  }
}

/**
 * 输入：消息唯一标识 ``messageId``。
 * 输出：无；反转对应消息的展开状态。
 * 功能：控制超过 200 字消息的全文展开与收起。
 */
function toggleMessage(messageId) {
  expandedMessages[messageId] = !expandedMessages[messageId]
}

/**
 * 输入：对话消息对象 ``message``。
 * 输出：完整内容或截断至 200 字的预览文本。
 * 功能：默认折叠长消息，同时保留短消息原文。
 */
function visibleMessageContent(message) {
  if (message.content.length <= 200 || expandedMessages[message.id]) return message.content
  return `${message.content.slice(0, 200)}…`
}

/**
 * 输入：平台编码 ``platform``。
 * 输出：平台中文名称。
 * 功能：统一日志表格与详情抽屉的平台文案。
 */
function platformLabel(platform) {
  if (platform === 'feishu') return '飞书'
  if (platform === 'wecom') return '企微'
  return '钉钉'
}

/**
 * 输入：平台编码 ``platform``。
 * 输出：Element Plus 标签类型。
 * 功能：将飞书、企微、钉钉映射为蓝、绿、橙色标签。
 */
function platformTagType(platform) {
  if (platform === 'wecom') return 'success'
  if (platform === 'dingtalk') return 'warning'
  return 'primary'
}

/**
 * 输入：话题名称 ``topic``。
 * 输出：Element Plus 标签类型。
 * 功能：为不同审计话题提供稳定且易区分的视觉颜色。
 */
function topicTagType(topic) {
  if (topic === '退单分析') return 'danger'
  if (topic === '订单追踪') return 'warning'
  if (topic === '售后政策') return 'success'
  return 'info'
}

/**
 * 输入：任意 CSV 字段值 ``value``。
 * 输出：符合 RFC 4180 常见兼容规则的双引号字段。
 * 功能：转义字段中的双引号，避免中文逗号和换行破坏导出列。
 */
function escapeCsv(value) {
  return `"${String(value ?? '').replaceAll('"', '""')}"`
}

/**
 * 输入：会话摘要数组 ``rows``。
 * 输出：带 UTF-8 BOM 的 CSV 文本。
 * 功能：将当前筛选结果转换为可由 Excel 直接打开的审计文件。
 */
function buildCsv(rows) {
  const lines = ['会话ID,用户姓名,平台,首条提问,话题类型,轮次数,MCP调用次数,总耗时,时间']
  for (const row of rows) {
    const fields = [row.session_id, row.user_name, platformLabel(row.platform), row.first_question, row.topic, row.turn_count, row.mcp_count, `${row.duration_ms}ms`, row.created_at]
    const escaped = []
    for (const field of fields) escaped.push(escapeCsv(field))
    lines.push(escaped.join(','))
  }
  return `\uFEFF${lines.join('\n')}`
}

/**
 * 输入：CSV 字符串 ``content``。
 * 输出：无；触发浏览器下载并释放临时对象 URL。
 * 功能：用原生 Blob API 保存筛选后的对话审计记录。
 */
function downloadCsv(content) {
  const blob = new Blob([content], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `对话日志-${formatDate(new Date())}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

/**
 * 输入：无；读取当前筛选条件和结果总量。
 * 输出：完成确认、全量查询与 CSV 下载的 Promise。
 * 功能：纯前端导出全部筛选结果，超过 5000 条时先提示预计耗时。
 */
async function exportConversations() {
  if (!total.value) {
    ElMessage.info('当前没有可导出的对话日志')
    return
  }
  if (total.value > 5000) {
    const seconds = Math.max(2, Math.ceil(total.value / 1200))
    try {
      await ElMessageBox.confirm(`导出数据量较大，预计需要 ${seconds} 秒，是否继续？`, '导出确认', { type: 'warning' })
    } catch {
      return
    }
  }
  exporting.value = true
  try {
    const params = conversationParams()
    params.page = 1
    params.size = total.value
    const result = await api.get('/logs/conversations', { params })
    downloadCsv(buildCsv(result.items || []))
    ElMessage.success('对话日志已导出')
  } finally {
    exporting.value = false
  }
}

onMounted(loadConversations)
</script>

<template>
  <section>
    <div class="page-heading">
      <div>
        <h1>对话日志审计</h1>
        <p>检索用户与 Agent 的完整对话、工具调用链与响应耗时</p>
      </div>
      <el-button :icon="Download" :loading="exporting" @click="exportConversations">导出 CSV</el-button>
    </div>

    <el-card class="page-card" shadow="never">
      <div class="filter-grid">
        <el-date-picker v-model="filters.dates" type="daterange" value-format="YYYY-MM-DD" range-separator="至" start-placeholder="开始日期" end-placeholder="结束日期" style="width: 250px" @change="filtersChanged" />
        <el-select v-model="filters.user_id" clearable filterable placeholder="全部用户" style="width: 150px" @change="filtersChanged"><el-option v-for="user in users" :key="user.id" :label="user.name" :value="user.id" /></el-select>
        <el-select v-model="filters.platforms" multiple collapse-tags clearable placeholder="全部平台" style="width: 180px" @change="filtersChanged"><el-option v-for="platform in platformOptions" :key="platform.value" :label="platform.label" :value="platform.value" /></el-select>
        <el-select v-model="filters.topic" clearable placeholder="全部话题" style="width: 150px" @change="filtersChanged"><el-option v-for="topic in topicOptions" :key="topic" :label="topic" :value="topic" /></el-select>
        <el-input v-model="filters.keyword" clearable placeholder="搜索提问或回复关键词" :prefix-icon="Search" class="keyword-input" @keyup.enter="filtersChanged" @clear="filtersChanged" />
        <el-button type="primary" @click="filtersChanged">搜索</el-button>
      </div>

      <el-skeleton v-if="loading" :rows="9" animated />
      <template v-else>
        <el-table :data="conversations">
          <el-table-column prop="session_id" label="会话 ID" width="155"><template #default="scope"><span class="mono session-id">{{ scope.row.session_id }}</span></template></el-table-column>
          <el-table-column prop="user_name" label="用户" width="100" />
          <el-table-column label="平台" width="90"><template #default="scope"><el-tag :type="platformTagType(scope.row.platform)" size="small" effect="light">{{ platformLabel(scope.row.platform) }}</el-tag></template></el-table-column>
          <el-table-column label="首条提问" min-width="245"><template #default="scope"><span :title="scope.row.first_question">{{ scope.row.first_question.length > 30 ? `${scope.row.first_question.slice(0, 30)}…` : scope.row.first_question }}</span></template></el-table-column>
          <el-table-column label="话题类型" width="110"><template #default="scope"><el-tag :type="topicTagType(scope.row.topic)" effect="plain" size="small">{{ scope.row.topic }}</el-tag></template></el-table-column>
          <el-table-column prop="turn_count" label="轮次" width="72" align="center" />
          <el-table-column prop="mcp_count" label="MCP 调用" width="95" align="center" />
          <el-table-column label="总耗时" width="95"><template #default="scope"><span :class="{ slow: scope.row.duration_ms > 5000 }">{{ (scope.row.duration_ms / 1000).toFixed(1) }}s</span></template></el-table-column>
          <el-table-column prop="created_at" label="时间" width="165" />
          <el-table-column label="操作" width="100" fixed="right"><template #default="scope"><el-button text type="primary" @click="openDetail(scope.row)">查看详情</el-button></template></el-table-column>
          <template #empty><el-empty class="empty-state" description="所选条件下暂无对话记录" /></template>
        </el-table>
        <div v-if="total" class="table-footer"><el-pagination v-model:current-page="filters.page" v-model:page-size="filters.size" background layout="total, sizes, prev, pager, next" :page-sizes="[10, 20, 50]" :total="total" @current-change="loadConversations" @size-change="filtersChanged" /></div>
      </template>
    </el-card>

    <el-drawer v-model="detailVisible" title="完整对话详情" size="60%">
      <el-skeleton v-if="detailLoading" :rows="12" animated />
      <template v-else-if="currentDetail">
        <div class="detail-header">
          <div><span class="mono">{{ currentDetail.session_id }}</span><h2>{{ currentDetail.user_name }} 的对话</h2></div>
          <el-tag :type="platformTagType(currentDetail.platform)">{{ platformLabel(currentDetail.platform) }}</el-tag>
        </div>
        <el-timeline class="conversation-timeline">
          <el-timeline-item v-for="message in currentDetail.messages" :key="message.id" :timestamp="message.created_at" placement="top" :type="message.sender === 'agent' ? 'primary' : 'success'">
            <div :class="['message-card', message.sender]">
              <div class="message-author"><el-avatar :size="30">{{ message.sender === 'agent' ? 'H' : currentDetail.user_name.slice(0, 1) }}</el-avatar><strong>{{ message.sender === 'agent' ? 'Hermes Agent' : currentDetail.user_name }}</strong><span>{{ message.duration_ms ? `${message.duration_ms}ms` : '' }}</span></div>
              <p>{{ visibleMessageContent(message) }}</p>
              <el-button v-if="message.content.length > 200" text type="primary" size="small" @click="toggleMessage(message.id)">{{ expandedMessages[message.id] ? '收起全文' : '展开全文' }}</el-button>
              <a v-if="message.chart_url" class="chart-link" :href="message.chart_url" target="_blank" rel="noopener">📊 [查看图表]</a>
              <div v-if="message.tool_calls?.length" class="tool-calls">
                <span class="tool-call-label">MCP 调用链</span>
                <div v-for="tool in message.tool_calls" :key="tool.name" class="tool-call"><span class="status-dot online" /><code>{{ tool.name }}</code><span>{{ tool.duration_ms }}ms</span></div>
              </div>
            </div>
          </el-timeline-item>
        </el-timeline>
        <el-descriptions class="detail-summary" title="会话汇总" :column="3" border>
          <el-descriptions-item label="总轮次">{{ currentDetail.summary.turn_count }}</el-descriptions-item>
          <el-descriptions-item label="MCP 调用次数">{{ currentDetail.summary.mcp_count }}</el-descriptions-item>
          <el-descriptions-item label="平均每条耗时">{{ currentDetail.summary.avg_duration_ms }}ms</el-descriptions-item>
        </el-descriptions>
      </template>
    </el-drawer>
  </section>
</template>

<style scoped>
.filter-grid { display: flex; align-items: center; gap: 10px; margin-bottom: 20px; }
.keyword-input { min-width: 190px; flex: 1; }
.session-id { color: #53627b; font-size: 11px; }
.slow { color: #dc4b4b; font-weight: 600; }
.detail-header { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 26px; padding: 15px 18px; border: 1px solid #e9edf3; border-radius: 12px; background: #fafbfc; }
.detail-header .mono { color: #8a95a7; font-size: 11px; }
.detail-header h2 { margin: 5px 0 0; color: #1e2940; font-size: 17px; }
.conversation-timeline { padding: 4px 8px 0; }
.message-card { max-width: 92%; padding: 15px 17px; border: 1px solid #e6ebf1; border-radius: 4px 13px 13px; background: white; box-shadow: 0 5px 16px rgb(28 39 60 / 4%); }
.message-card.agent { border-color: #ffd8c0; background: #fffaf7; }
.message-author { display: flex; align-items: center; gap: 9px; }
.message-author .el-avatar { color: #a94516; background: #ffe5d5; font-size: 11px; }
.message-author strong { color: #263149; font-size: 12px; }
.message-author span { margin-left: auto; color: #9aa3b3; font-size: 10px; }
.message-card p { margin: 13px 0 5px; color: #3c485e; font-size: 13px; line-height: 1.75; white-space: pre-wrap; }
.chart-link { display: block; margin-top: 10px; color: #ef651e; font-size: 12px; text-decoration: none; }
.tool-calls { display: grid; gap: 7px; margin-top: 13px; padding-top: 11px; border-top: 1px dashed #e4e8ee; }
.tool-call-label { color: #8792a5; font-size: 10px; font-weight: 700; letter-spacing: .4px; }
.tool-call { display: flex; align-items: center; gap: 8px; color: #6e7a8e; font-size: 11px; }
.tool-call code { color: #465570; }
.tool-call span:last-child { margin-left: auto; }
.detail-summary { margin-top: 8px; padding-top: 22px; border-top: 1px solid #e8edf3; }
</style>
