<script setup>
import { Plus, Refresh, Search, UserFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import api from '../api'

const regions = ref(['华东', '华南', '华北', '西南'])
const categories = ref(['大家电', '3C'])
const granularities = ['完整明细', '脱敏明细', '仅汇总']
const platformOptions = [
  { value: 'feishu', label: '飞书' },
  { value: 'wecom', label: '企微' },
  { value: 'dingtalk', label: '钉钉' },
]

const loading = ref(false)
const loadFailed = ref(false)
const submitting = ref(false)
const bindingSubmitting = ref(false)
const users = ref([])
const roles = ref([])
const total = ref(0)
const currentUser = ref()
const detailVisible = ref(false)
const userDialogVisible = ref(false)
const bindingDialogVisible = ref(false)
const editingId = ref()
const userFormRef = ref()
const bindingFormRef = ref()

const filters = reactive({ role: '', status: '', keyword: '', page: 1, size: 10 })
const userForm = reactive({ employee_id: '', name: '', role: '', regions: [], categories: [], granularity: '脱敏明细' })
const bindingForm = reactive({ platform: '', user_id: '' })

/**
 * 输入：Element Plus 校验规则、工号值 ``value`` 与完成回调 ``callback``。
 * 输出：无；通过回调返回校验结果。
 * 功能：要求员工工号由 4 至 20 位字母、数字、下划线或连字符组成。
 */
function validateEmployeeId(rule, value, callback) {
  if (!/^[A-Za-z0-9_-]{4,20}$/.test(value)) callback(new Error('请输入 4-20 位有效工号'))
  else callback()
}

const userRules = {
  employee_id: [{ required: true, validator: validateEmployeeId, trigger: 'blur' }],
  name: [{ required: true, message: '请输入姓名', trigger: 'blur' }],
  role: [{ required: true, message: '请选择角色', trigger: 'change' }],
  regions: [{ required: true, type: 'array', min: 1, message: '至少选择一个区域', trigger: 'change' }],
  categories: [{ required: true, type: 'array', min: 1, message: '至少选择一个品类', trigger: 'change' }],
  granularity: [{ required: true, message: '请选择数据粒度', trigger: 'change' }],
}

const bindingRules = {
  platform: [{ required: true, message: '请选择 IM 平台', trigger: 'change' }],
  user_id: [{ required: true, min: 3, message: '请输入有效的平台用户 ID', trigger: 'blur' }],
}

/**
 * 输入：无；读取当前筛选与分页状态。
 * 输出：完成用户列表请求的 Promise。
 * 功能：加载用户列表并在网络失败时展示可重试警告条。
 */
async function loadUsers() {
  loading.value = true
  loadFailed.value = false
  try {
    const result = await api.get('/users', { params: filters, silent: true })
    users.value = result.items || []
    total.value = result.total || 0
  } catch {
    loadFailed.value = true
  } finally {
    loading.value = false
  }
}

/**
 * 输入：无。
 * 输出：完成角色列表请求的 Promise。
 * 功能：从后端加载用户表单和筛选器共用的角色选项。
 */
async function loadRoles() {
  try {
    const result = await api.get('/roles', { silent: true })
    roles.value = result.items || result
    if (result.regions) regions.value = result.regions
    if (result.categories) categories.value = result.categories
  } catch {
    roles.value = []
  }
}

/**
 * 输入：无。
 * 输出：无；将筛选分页重置至第一页并重新加载。
 * 功能：响应角色、状态或关键词筛选操作。
 */
function applyFilters() {
  filters.page = 1
  loadUsers()
}

/**
 * 输入：无。
 * 输出：无；清空用户表单为新增状态。
 * 功能：打开添加用户对话框并设置默认数据粒度。
 */
function openAddUser() {
  editingId.value = undefined
  Object.assign(userForm, { employee_id: '', name: '', role: '', regions: [], categories: [], granularity: '脱敏明细' })
  userDialogVisible.value = true
}

/**
 * 输入：待编辑用户对象 ``user``。
 * 输出：无；回填表单并打开编辑对话框。
 * 功能：复制数组字段，避免表单编辑直接污染表格数据。
 */
function openEditUser(user) {
  editingId.value = user.id
  Object.assign(userForm, {
    employee_id: user.employee_id,
    name: user.name,
    role: user.role,
    regions: [...user.regions],
    categories: [...user.categories],
    granularity: user.granularity,
  })
  userDialogVisible.value = true
}

/**
 * 输入：新增或编辑表单的隐式响应式数据。
 * 输出：完成保存请求的 Promise。
 * 功能：校验并创建或更新用户，单独处理重复工号冲突。
 */
async function submitUser() {
  try {
    await userFormRef.value.validate()
  } catch {
    return
  }
  submitting.value = true
  try {
    let saved
    if (editingId.value) saved = await api.put(`/users/${editingId.value}`, userForm, { silent: true })
    else saved = await api.post('/users', userForm, { silent: true })
    if (currentUser.value?.id === editingId.value) Object.assign(currentUser.value, saved)
    ElMessage.success(editingId.value ? '用户信息已更新' : '用户已添加')
    userDialogVisible.value = false
    await loadUsers()
  } catch (error) {
    if (error.response?.status === 409) ElMessage.error('该工号已存在')
    else ElMessage.error(error.response?.data?.message || '保存失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}

/**
 * 输入：用户表格行 ``user``。
 * 输出：无；设置当前用户并展开详情抽屉。
 * 功能：展示角色、权限范围和平台绑定信息。
 */
function openUserDetail(user) {
  currentUser.value = user
  detailVisible.value = true
}

/**
 * 输入：用户对象 ``user``，其 ``enabled`` 已被开关预先修改。
 * 输出：完成状态切换或回滚的 Promise。
 * 功能：二次确认后启用或禁用用户，取消和失败时恢复开关状态。
 */
async function toggleUserStatus(user) {
  const target = user.enabled
  try {
    await ElMessageBox.confirm(`确认${target ? '启用' : '禁用'}用户“${user.name}”吗？`, '状态确认', { type: 'warning' })
    await api.put(`/users/${user.id}/status`, { enabled: target })
    ElMessage.success(`用户已${target ? '启用' : '禁用'}`)
  } catch {
    user.enabled = !target
  }
}

/**
 * 输入：无；隐式使用详情抽屉中的当前用户。
 * 输出：无；清空绑定表单并打开平台绑定对话框。
 * 功能：为当前用户发起新的飞书、企微或钉钉账号绑定。
 */
function openBindingDialog() {
  Object.assign(bindingForm, { platform: '', user_id: '' })
  bindingDialogVisible.value = true
}

/**
 * 输入：平台绑定表单的隐式响应式数据。
 * 输出：完成绑定请求的 Promise。
 * 功能：校验平台用户 ID 并刷新当前用户的绑定标签。
 */
async function submitBinding() {
  try {
    await bindingFormRef.value.validate()
  } catch {
    return
  }
  bindingSubmitting.value = true
  try {
    const updated = await api.post(`/users/${currentUser.value.id}/bindings`, bindingForm, { silent: true })
    currentUser.value.bindings = updated.bindings
    bindingDialogVisible.value = false
    ElMessage.success('平台绑定成功')
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '平台用户 ID 无效')
  } finally {
    bindingSubmitting.value = false
  }
}

/**
 * 输入：待解绑的平台编码 ``platform``。
 * 输出：完成确认及解绑请求的 Promise。
 * 功能：删除当前用户的平台身份绑定并同步列表数据。
 */
async function removeBinding(platform) {
  try {
    await ElMessageBox.confirm(`确认删除${platformLabel(platform)}绑定吗？`, '删除绑定', { type: 'warning' })
    const updated = await api.delete(`/users/${currentUser.value.id}/bindings/${platform}`)
    currentUser.value.bindings = updated.bindings
    ElMessage.success('平台绑定已删除')
  } catch {
    // 用户取消确认或请求失败时保留现状。
  }
}

/**
 * 输入：平台编码 ``platform``。
 * 输出：对应的中文平台名称。
 * 功能：统一表格、抽屉和确认框的平台文案。
 */
function platformLabel(platform) {
  if (platform === 'feishu') return '飞书'
  if (platform === 'wecom') return '企微'
  return '钉钉'
}

/**
 * 输入：平台编码 ``platform``。
 * 输出：Element Plus 标签类型。
 * 功能：将飞书、企微、钉钉分别显示为蓝、绿、橙色标签。
 */
function platformTagType(platform) {
  if (platform === 'wecom') return 'success'
  if (platform === 'dingtalk') return 'warning'
  return 'primary'
}

/**
 * 输入：无。
 * 输出：并发完成用户与角色首屏请求的 Promise。
 * 功能：初始化用户权限管理页。
 */
async function mountUsers() {
  await Promise.all([loadRoles(), loadUsers()])
}

onMounted(mountUsers)
</script>

<template>
  <section>
    <div class="page-heading">
      <div>
        <h1>用户与权限管理</h1>
        <p>管理账号、IM 平台身份绑定与业务数据访问范围</p>
      </div>
      <el-button type="primary" :icon="Plus" @click="openAddUser">添加用户</el-button>
    </div>

    <el-alert v-if="loadFailed" class="warning-strip" type="warning" :closable="false" show-icon>
      <template #title>加载失败，请点击重试</template>
      <el-button text type="warning" :icon="Refresh" @click="loadUsers">重试</el-button>
    </el-alert>

    <el-card class="page-card" shadow="never">
      <div class="toolbar">
        <el-select v-model="filters.role" clearable placeholder="全部角色" style="width: 180px" @change="applyFilters">
          <el-option v-for="role in roles" :key="role.value" :label="role.label" :value="role.value" />
        </el-select>
        <el-select v-model="filters.status" clearable placeholder="全部状态" style="width: 140px" @change="applyFilters">
          <el-option label="已启用" value="enabled" />
          <el-option label="已禁用" value="disabled" />
        </el-select>
        <el-input v-model="filters.keyword" clearable placeholder="搜索员工工号或姓名" :prefix-icon="Search" style="width: 250px" @keyup.enter="applyFilters" @clear="applyFilters" />
        <el-button @click="applyFilters">查询</el-button>
        <div class="toolbar-spacer" />
        <span class="muted result-count">共 {{ total }} 位用户</span>
      </div>

      <el-table v-loading="loading" :data="users" row-class-name="clickable-row" @row-click="openUserDetail">
        <el-table-column prop="employee_id" label="员工工号" width="145"><template #default="scope"><span class="mono employee-id">{{ scope.row.employee_id }}</span></template></el-table-column>
        <el-table-column prop="name" label="姓名" width="120">
          <template #default="scope"><div class="user-cell"><el-avatar :size="30">{{ scope.row.name.slice(0, 1) }}</el-avatar><strong>{{ scope.row.name }}</strong></div></template>
        </el-table-column>
        <el-table-column prop="role_label" label="角色" min-width="135" />
        <el-table-column label="已绑平台" min-width="190">
          <template #default="scope">
            <el-space v-if="scope.row.bindings.length" :size="5" wrap>
              <el-tag v-for="binding in scope.row.bindings" :key="binding.platform" :type="platformTagType(binding.platform)" effect="light" size="small">{{ platformLabel(binding.platform) }}</el-tag>
            </el-space>
            <span v-else class="muted">未绑定</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="scope"><div @click.stop><el-switch v-model="scope.row.enabled" inline-prompt active-text="启" inactive-text="禁" @change="toggleUserStatus(scope.row)" /></div></template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="175" />
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="scope"><el-button text type="primary" @click.stop="openEditUser(scope.row)">编辑</el-button></template>
        </el-table-column>
        <template #empty><el-empty class="empty-state" description="暂无用户，点击上方按钮添加" /></template>
      </el-table>

      <div v-if="total" class="table-footer">
        <el-pagination v-model:current-page="filters.page" v-model:page-size="filters.size" background layout="total, sizes, prev, pager, next" :page-sizes="[10, 20, 50]" :total="total" @current-change="loadUsers" @size-change="applyFilters" />
      </div>
    </el-card>

    <el-dialog v-model="userDialogVisible" :title="editingId ? '编辑用户' : '添加用户'" width="620px" destroy-on-close>
      <el-form ref="userFormRef" :model="userForm" :rules="userRules" label-position="top">
        <el-row :gutter="18">
          <el-col :span="12"><el-form-item label="员工工号" prop="employee_id"><el-input v-model="userForm.employee_id" :disabled="Boolean(editingId)" placeholder="如 SN20240018" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="姓名" prop="name"><el-input v-model="userForm.name" placeholder="请输入员工姓名" /></el-form-item></el-col>
        </el-row>
        <el-row :gutter="18">
          <el-col :span="12"><el-form-item label="角色" prop="role"><el-select v-model="userForm.role" placeholder="选择业务角色" style="width: 100%"><el-option v-for="role in roles" :key="role.value" :label="role.label" :value="role.value" /></el-select></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="数据粒度" prop="granularity"><el-select v-model="userForm.granularity" style="width: 100%"><el-option v-for="item in granularities" :key="item" :label="item" :value="item" /></el-select></el-form-item></el-col>
        </el-row>
        <el-form-item label="区域权限" prop="regions"><el-checkbox-group v-model="userForm.regions"><el-checkbox v-for="item in regions" :key="item" :value="item">{{ item }}</el-checkbox></el-checkbox-group></el-form-item>
        <el-form-item label="品类权限" prop="categories"><el-checkbox-group v-model="userForm.categories"><el-checkbox v-for="item in categories" :key="item" :value="item">{{ item }}</el-checkbox></el-checkbox-group></el-form-item>
      </el-form>
      <template #footer><el-button @click="userDialogVisible = false">取消</el-button><el-button type="primary" :loading="submitting" @click="submitUser">{{ editingId ? '保存修改' : '确认添加' }}</el-button></template>
    </el-dialog>

    <el-drawer v-model="detailVisible" size="460px" :title="currentUser ? `${currentUser.name} · 用户详情` : '用户详情'">
      <template v-if="currentUser">
        <div class="profile-summary">
          <el-avatar :size="52"><UserFilled /></el-avatar>
          <div><strong>{{ currentUser.name }}</strong><span class="mono">{{ currentUser.employee_id }}</span></div>
          <el-tag :type="currentUser.enabled ? 'success' : 'info'">{{ currentUser.enabled ? '已启用' : '已禁用' }}</el-tag>
        </div>
        <div class="drawer-section">
          <h3 class="section-title">账号权限</h3>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="角色">{{ currentUser.role_label }}</el-descriptions-item>
            <el-descriptions-item label="数据粒度">{{ currentUser.granularity }}</el-descriptions-item>
            <el-descriptions-item label="区域范围"><el-tag v-for="item in currentUser.regions" :key="item" class="scope-tag" effect="plain">{{ item }}</el-tag></el-descriptions-item>
            <el-descriptions-item label="品类范围"><el-tag v-for="item in currentUser.categories" :key="item" class="scope-tag" effect="plain">{{ item }}</el-tag></el-descriptions-item>
          </el-descriptions>
        </div>
        <div class="drawer-section">
          <h3 class="section-title"><span>IM 平台绑定</span><el-button type="primary" text :icon="Plus" @click="openBindingDialog">绑定平台</el-button></h3>
          <div v-if="currentUser.bindings.length" class="binding-list">
            <div v-for="binding in currentUser.bindings" :key="binding.platform" class="binding-item">
              <div><el-tag :type="platformTagType(binding.platform)" effect="light">{{ platformLabel(binding.platform) }}</el-tag><span class="mono">{{ binding.user_id }}</span></div>
              <el-button text type="danger" @click="removeBinding(binding.platform)">删除绑定</el-button>
            </div>
          </div>
          <el-empty v-else :image-size="70" description="暂未绑定 IM 平台" />
        </div>
        <div class="drawer-actions"><el-button type="primary" plain @click="openEditUser(currentUser)">编辑用户信息</el-button></div>
      </template>
    </el-drawer>

    <el-dialog v-model="bindingDialogVisible" title="绑定 IM 平台" width="480px">
      <el-form ref="bindingFormRef" :model="bindingForm" :rules="bindingRules" label-position="top">
        <el-form-item label="平台" prop="platform"><el-select v-model="bindingForm.platform" placeholder="选择平台" style="width: 100%"><el-option v-for="platform in platformOptions" :key="platform.value" :label="platform.label" :value="platform.value" /></el-select></el-form-item>
        <el-form-item label="平台用户 ID" prop="user_id"><el-input v-model="bindingForm.user_id" placeholder="请输入平台侧唯一用户 ID" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="bindingDialogVisible = false">取消</el-button><el-button type="primary" :loading="bindingSubmitting" @click="submitBinding">确认绑定</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.result-count { font-size: 12px; }
.employee-id { color: #44516a; font-size: 12px; }
.user-cell { display: flex; align-items: center; gap: 9px; }
.user-cell .el-avatar { color: #ae4819; background: #ffeadc; font-size: 12px; }
.user-cell strong { font-size: 13px; }
.profile-summary { display: flex; align-items: center; gap: 13px; padding-bottom: 20px; border-bottom: 1px solid #e8edf4; }
.profile-summary .el-avatar { color: #9e4318; background: #ffe4d3; }
.profile-summary > div { flex: 1; }
.profile-summary strong, .profile-summary span { display: block; }
.profile-summary strong { margin-bottom: 5px; font-size: 17px; }
.profile-summary span { color: #8490a3; font-size: 12px; }
.scope-tag { margin: 2px 5px 2px 0; }
.binding-list { display: grid; gap: 10px; }
.binding-item { display: flex; align-items: center; justify-content: space-between; padding: 12px; border: 1px solid #e9edf3; border-radius: 10px; background: #fafbfc; }
.binding-item > div { display: flex; align-items: center; gap: 10px; }
.binding-item .mono { color: #68748a; font-size: 11px; }
.drawer-actions { padding-top: 18px; }
</style>
