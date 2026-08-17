<script setup>
import { Connection, Edit, Plus, Refresh, Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import api from '../api'

const loading = ref(false)
const toolsLoading = ref(false)
const submitting = ref(false)
const servers = ref([])
const tools = ref([])
const selectedServer = ref()
const keyword = ref('')
const dialogVisible = ref(false)
const editingId = ref()
const formRef = ref()
const heartbeatLoading = reactive({})
const toolLoading = reactive({})
const serviceForm = reactive({ name: '', host: '', port: 8101 })

/**
 * 输入：Element Plus 校验规则、主机值 ``value`` 与完成回调 ``callback``。
 * 输出：无；通过回调返回地址格式校验结果。
 * 功能：接受 IPv4、localhost 或域名，可带 http/https 协议但不允许路径和端口。
 */
function validateHost(rule, value, callback) {
  const hostPattern = /^(https?:\/\/)?(localhost|(?:\d{1,3}\.){3}\d{1,3}|[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?)$/
  if (!hostPattern.test(value)) callback(new Error('请输入有效主机地址，如 127.0.0.1'))
  else callback()
}

/**
 * 输入：Element Plus 校验规则、端口 ``value`` 与完成回调 ``callback``。
 * 输出：无；通过回调返回端口范围校验结果。
 * 功能：限制注册服务端口为 1 至 65535 的整数。
 */
function validatePort(rule, value, callback) {
  if (!Number.isInteger(Number(value)) || Number(value) < 1 || Number(value) > 65535) callback(new Error('端口必须为 1-65535 的整数'))
  else callback()
}

const serviceRules = {
  name: [{ required: true, message: '请输入服务名称', trigger: 'blur' }],
  host: [{ required: true, validator: validateHost, trigger: 'blur' }],
  port: [{ required: true, validator: validatePort, trigger: 'blur' }],
}

/**
 * 输入：无；读取服务搜索关键词。
 * 输出：名称或地址命中的 MCP 服务数组。
 * 功能：在左侧卡片列表即时筛选已注册服务。
 */
function filterServers() {
  const query = keyword.value.trim().toLowerCase()
  if (!query) return servers.value
  const matches = []
  for (const server of servers.value) {
    if (server.name.toLowerCase().includes(query) || server.address.toLowerCase().includes(query)) matches.push(server)
  }
  return matches
}

const visibleServers = computed(filterServers)

/**
 * 输入：无。
 * 输出：完成服务列表请求的 Promise。
 * 功能：加载全部 MCP Server，并在首次进入时选中第一项。
 */
async function loadServers() {
  loading.value = true
  try {
    const result = await api.get('/mcp/servers')
    servers.value = result.items || result
    if (!servers.value.length) {
      selectedServer.value = undefined
      tools.value = []
      return
    }
    let nextSelected = servers.value[0]
    if (selectedServer.value) {
      for (const server of servers.value) {
        if (server.id === selectedServer.value.id) nextSelected = server
      }
    }
    await selectServer(nextSelected)
  } finally {
    loading.value = false
  }
}

/**
 * 输入：被点击的 MCP 服务 ``server``。
 * 输出：完成工具列表请求的 Promise。
 * 功能：切换左侧选中服务并加载该服务注册的工具。
 */
async function selectServer(server) {
  selectedServer.value = server
  toolsLoading.value = true
  try {
    const result = await api.get(`/mcp/servers/${server.id}/tools`)
    tools.value = result.items || result
  } finally {
    toolsLoading.value = false
  }
}

/**
 * 输入：无。
 * 输出：无；重置表单并打开服务注册对话框。
 * 功能：准备手动注册 MCP 服务所需的名称、主机和端口字段。
 */
function openCreateDialog() {
  editingId.value = undefined
  Object.assign(serviceForm, { name: '', host: '', port: 8101 })
  dialogVisible.value = true
}

/**
 * 输入：待编辑 MCP 服务 ``server``。
 * 输出：无；回填服务表单并打开编辑对话框。
 * 功能：允许管理员修改已注册服务名称和连接位置。
 */
function openEditDialog(server) {
  editingId.value = server.id
  Object.assign(serviceForm, { name: server.name, host: server.host, port: server.port })
  dialogVisible.value = true
}

/**
 * 输入：服务注册或编辑表单的隐式响应式数据。
 * 输出：完成保存请求的 Promise。
 * 功能：校验 host 与 port 后注册或更新 MCP 服务。
 */
async function submitService() {
  try {
    await formRef.value.validate()
  } catch {
    return
  }
  submitting.value = true
  try {
    if (editingId.value) await api.put(`/mcp/servers/${editingId.value}`, serviceForm)
    else await api.post('/mcp/servers', serviceForm)
    ElMessage.success(editingId.value ? '服务信息已更新' : '服务注册成功')
    dialogVisible.value = false
    await loadServers()
  } finally {
    submitting.value = false
  }
}

/**
 * 输入：待检测 MCP 服务 ``server`` 和可选点击事件 ``event``。
 * 输出：完成最长 30 秒心跳请求的 Promise。
 * 功能：手动更新服务健康状态；超时后标记为黄色疑离线状态。
 */
async function checkHeartbeat(server, event) {
  event?.stopPropagation()
  heartbeatLoading[server.id] = true
  try {
    const updated = await api.post(`/mcp/servers/${server.id}/heartbeat`, {}, { timeout: 30000, silent: true })
    Object.assign(server, updated)
    ElMessage.success(`${server.name} 心跳正常`)
  } catch {
    server.status = 'degraded'
    server.last_heartbeat = '30 秒前'
    ElMessage.warning(`${server.name} 心跳超时，已标记为疑离线`)
  } finally {
    heartbeatLoading[server.id] = false
  }
}

/**
 * 输入：工具对象 ``tool``，其 ``enabled`` 已被开关修改。
 * 输出：完成工具状态切换的 Promise。
 * 功能：即时启用或禁用 MCP 工具，请求失败时回滚开关状态。
 */
async function toggleTool(tool) {
  const target = tool.enabled
  toolLoading[tool.id] = true
  try {
    await api.put(`/mcp/tools/${tool.id}/toggle`, { enabled: target })
    ElMessage.success(`${tool.name} 已${target ? '启用' : '禁用'}`)
  } catch {
    tool.enabled = !target
  } finally {
    toolLoading[tool.id] = false
  }
}

/**
 * 输入：服务状态编码 ``status``。
 * 输出：对应的中文状态文案。
 * 功能：统一在线、离线和降级状态展示。
 */
function statusLabel(status) {
  if (status === 'online') return '在线'
  if (status === 'offline') return '离线'
  return '降级'
}

/**
 * 输入：服务状态编码 ``status``。
 * 输出：Element Plus 标签类型。
 * 功能：将在线、离线、降级映射为绿、红、黄状态色。
 */
function statusTagType(status) {
  if (status === 'online') return 'success'
  if (status === 'offline') return 'danger'
  return 'warning'
}

/**
 * 输入：工具参数 Schema 对象 ``schema``。
 * 输出：带两空格缩进的 JSON 字符串。
 * 功能：在折叠浮层中可读地展示 MCP 工具参数定义。
 */
function formatSchema(schema) {
  return JSON.stringify(schema, null, 2)
}

onMounted(loadServers)
</script>

<template>
  <section>
    <div class="page-heading">
      <div>
        <h1>MCP 服务管理</h1>
        <p>管理业务数据网关、工具开放状态与服务健康心跳</p>
      </div>
      <el-button type="primary" :icon="Plus" @click="openCreateDialog">注册服务</el-button>
    </div>

    <el-card class="page-card mcp-shell" shadow="never">
      <div class="mcp-layout">
        <aside class="server-panel">
          <el-input v-model="keyword" clearable placeholder="搜索服务名称或地址" :prefix-icon="Search" />
          <el-scrollbar class="server-scroll">
            <el-skeleton v-if="loading" :rows="8" animated />
            <div v-else-if="visibleServers.length" class="server-list">
              <article
                v-for="server in visibleServers"
                :key="server.id"
                :class="['server-card', { selected: selectedServer?.id === server.id, offline: server.status === 'offline' }]"
                @click="selectServer(server)"
              >
                <div class="server-title">
                  <div><span :class="['status-dot', server.status]" /><strong>{{ server.name }}</strong></div>
                  <el-tag :type="statusTagType(server.status)" size="small" effect="light">{{ statusLabel(server.status) }}</el-tag>
                </div>
                <div class="server-address mono">{{ server.address }}</div>
                <div class="server-meta"><span>{{ server.tool_count }} 个工具</span><span>最后心跳：{{ server.last_heartbeat }}</span></div>
                <div class="server-actions">
                  <el-button text size="small" :icon="Edit" @click.stop="openEditDialog(server)">编辑</el-button>
                  <el-button text type="primary" size="small" :icon="Refresh" :loading="heartbeatLoading[server.id]" @click="checkHeartbeat(server, $event)">检测</el-button>
                </div>
              </article>
            </div>
            <el-empty v-else :image-size="90" description="暂无注册的 MCP 服务，点击上方按钮注册" />
          </el-scrollbar>
        </aside>

        <main class="tool-panel">
          <div v-if="selectedServer" class="tool-heading">
            <div><span class="tool-icon"><Connection /></span><div><h2>{{ selectedServer.name }}</h2><p>{{ selectedServer.address }} · {{ selectedServer.tool_count }} 个工具</p></div></div>
            <el-tag :type="statusTagType(selectedServer.status)" effect="light"><span :class="['status-dot', selectedServer.status]" />{{ statusLabel(selectedServer.status) }}</el-tag>
          </div>
          <el-table v-if="selectedServer" v-loading="toolsLoading" :data="tools">
            <el-table-column prop="name" label="工具名称" min-width="190"><template #default="scope"><span class="mono tool-name">{{ scope.row.name }}</span></template></el-table-column>
            <el-table-column prop="description" label="描述" min-width="300" show-overflow-tooltip />
            <el-table-column label="参数 Schema" width="130">
              <template #default="scope">
                <el-popover placement="left" :width="430" trigger="click">
                  <template #reference><el-button text type="primary">展开查看</el-button></template>
                  <pre class="schema-view">{{ formatSchema(scope.row.schema) }}</pre>
                </el-popover>
              </template>
            </el-table-column>
            <el-table-column label="启用" width="100" align="center">
              <template #default="scope"><el-switch v-model="scope.row.enabled" :loading="toolLoading[scope.row.id]" @change="toggleTool(scope.row)" /></template>
            </el-table-column>
            <template #empty><el-empty description="该服务暂未注册工具" /></template>
          </el-table>
          <el-empty v-else description="请先选择一个 MCP 服务" />
        </main>
      </div>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="editingId ? '编辑 MCP 服务' : '注册 MCP 服务'" width="520px">
      <el-form ref="formRef" :model="serviceForm" :rules="serviceRules" label-position="top">
        <el-form-item label="服务名称" prop="name"><el-input v-model="serviceForm.name" placeholder="如 订单数据服务" /></el-form-item>
        <el-row :gutter="16">
          <el-col :span="16"><el-form-item label="主机地址" prop="host"><el-input v-model="serviceForm.host" placeholder="127.0.0.1" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="端口" prop="port"><el-input-number v-model="serviceForm.port" :min="1" :max="65535" controls-position="right" style="width: 100%" /></el-form-item></el-col>
        </el-row>
        <el-alert type="info" :closable="false" show-icon title="服务注册地址将自动拼接为 host:port/mcp" />
      </el-form>
      <template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" :loading="submitting" @click="submitService">{{ editingId ? '保存修改' : '确认注册' }}</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.mcp-shell :deep(.el-card__body) { padding: 0; }
.mcp-layout { display: grid; grid-template-columns: 365px minmax(0, 1fr); min-height: 680px; }
.server-panel { padding: 20px 18px; border-right: 1px solid #e9edf3; background: #fbfcfe; }
.server-scroll { height: 610px; margin-top: 16px; padding-right: 5px; }
.server-list { display: grid; gap: 11px; }
.server-card { padding: 16px; border: 1px solid #e4e9f0; border-radius: 12px; background: white; cursor: pointer; transition: border-color .18s, box-shadow .18s, transform .18s; }
.server-card:hover { transform: translateY(-1px); border-color: #ffc3a1; }
.server-card.selected { border-color: #ff7b35; box-shadow: 0 8px 20px rgb(255 106 26 / 10%); }
.server-card.offline { opacity: .55; filter: grayscale(.2); }
.server-title, .server-title > div, .server-meta, .server-actions { display: flex; align-items: center; }
.server-title { justify-content: space-between; }
.server-title strong { color: #263149; font-size: 13px; }
.server-address { margin: 12px 0; color: #69758b; font-size: 11px; }
.server-meta { justify-content: space-between; color: #929baa; font-size: 10px; }
.server-actions { justify-content: flex-end; margin: 10px -8px -8px 0; }
.tool-panel { min-width: 0; padding: 24px; }
.tool-heading { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; padding-bottom: 20px; border-bottom: 1px solid #edf0f4; }
.tool-heading > div, .tool-heading > div > div { display: flex; align-items: center; }
.tool-heading h2 { margin: 0 0 6px; color: #202a40; font-size: 17px; }
.tool-heading p { margin: 0; color: #8690a1; font-size: 11px; }
.tool-heading > div > div { display: block; }
.tool-icon { display: grid; width: 40px; height: 40px; margin-right: 12px; place-items: center; border-radius: 11px; color: #ff6818; background: #ffede2; }
.tool-name { color: #34435f; font-size: 12px; font-weight: 600; }
.schema-view { max-height: 420px; margin: 0; overflow: auto; color: #39465d; font-size: 11px; line-height: 1.6; white-space: pre-wrap; }
</style>
