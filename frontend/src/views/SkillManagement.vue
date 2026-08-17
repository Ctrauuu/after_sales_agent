<script setup>
import { Delete, Edit, MagicStick, Plus, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import api from '../api'
import EChart from '../components/EChart.vue'

const loading = ref(false)
const detailLoading = ref(false)
const submitting = ref(false)
const skills = ref([])
const total = ref(0)
const detailVisible = ref(false)
const editVisible = ref(false)
const currentSkill = ref()
const showAllSteps = ref(false)
const editPatterns = ref([])
const filters = reactive({ type: '', status: '', keyword: '', page: 1, size: 12 })

const scoreOption = ref({
  color: ['#ff6a1a'],
  tooltip: { trigger: 'axis', formatter: '{b}<br/>质量评分：{c} 分' },
  grid: { left: 35, right: 16, top: 24, bottom: 28 },
  xAxis: { type: 'category', boundaryGap: false, data: [], axisLabel: { color: '#8a94a6' }, axisLine: { lineStyle: { color: '#e7ebf1' } } },
  yAxis: { type: 'value', min: 60, max: 100, splitLine: { lineStyle: { color: '#eef1f5', type: 'dashed' } }, axisLabel: { color: '#8a94a6' } },
  series: [{ type: 'line', smooth: true, symbolSize: 7, data: [], lineStyle: { width: 3 }, areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(255,106,26,.22)' }, { offset: 1, color: 'rgba(255,106,26,0)' }] } } }],
})

/**
 * 输入：无；读取技能筛选和分页状态。
 * 输出：完成技能列表请求的 Promise。
 * 功能：加载自动与手动 Skill 卡片数据。
 */
async function loadSkills() {
  loading.value = true
  try {
    const result = await api.get('/skills', { params: filters })
    skills.value = result.items || []
    total.value = result.total || 0
  } finally {
    loading.value = false
  }
}

/**
 * 输入：无。
 * 输出：无；将分页重置到第一页后重新加载技能。
 * 功能：响应类型、状态和名称筛选条件。
 */
function applyFilters() {
  filters.page = 1
  loadSkills()
}

/**
 * 输入：技能卡片摘要 ``skill``。
 * 输出：完成技能详情请求的 Promise。
 * 功能：打开抽屉并加载版本、触发模式、工作流和评分趋势。
 */
async function openSkill(skill) {
  detailVisible.value = true
  detailLoading.value = true
  currentSkill.value = undefined
  showAllSteps.value = false
  try {
    const result = await api.get(`/skills/${skill.id}`)
    currentSkill.value = result
    applyScoreTrend(result.score_trend || [])
  } finally {
    detailLoading.value = false
  }
}

/**
 * 输入：质量评分趋势数组 ``trend``。
 * 输出：无；更新技能详情折线图配置。
 * 功能：把日期与质量分转换为 ECharts 序列。
 */
function applyScoreTrend(trend) {
  const dates = []
  const scores = []
  for (const point of trend) {
    dates.push(point.date)
    scores.push(point.score)
  }
  scoreOption.value.xAxis.data = dates
  scoreOption.value.series[0].data = scores
}

/**
 * 输入：无；读取当前技能步骤与展开状态。
 * 输出：完整步骤或前八个步骤。
 * 功能：当工作流超过八步时按提示词提供折叠展示。
 */
function displayedSteps() {
  const steps = currentSkill.value?.workflow_steps || []
  if (steps.length <= 8 || showAllSteps.value) return steps
  return steps.slice(0, 8)
}

const visibleSteps = computed(displayedSteps)

/**
 * 输入：技能标识 ``skillId`` 与目标启用状态 ``enabled``。
 * 输出：无；同步卡片列表和详情对象中的状态。
 * 功能：确保从卡片或抽屉操作开关时两处界面保持一致。
 */
function syncSkillStatus(skillId, enabled) {
  for (const item of skills.value) {
    if (item.id === skillId) item.enabled = enabled
  }
  if (currentSkill.value?.id === skillId) currentSkill.value.enabled = enabled
}

/**
 * 输入：技能对象 ``skill``，其 ``enabled`` 已被开关修改。
 * 输出：完成状态请求的 Promise。
 * 功能：无需二次确认地启用或禁用技能，请求失败时回滚。
 */
async function toggleSkill(skill) {
  const target = skill.enabled
  try {
    await api.put(`/skills/${skill.id}/status`, { enabled: target })
    syncSkillStatus(skill.id, target)
    ElMessage.success(`Skill 已${target ? '启用' : '禁用'}`)
  } catch {
    syncSkillStatus(skill.id, !target)
  }
}

/**
 * 输入：无；隐式读取当前技能的完整触发模式。
 * 输出：无；复制触发模式并打开编辑对话框。
 * 功能：避免编辑输入直接修改详情抽屉中的原始数据。
 */
function openPatternEditor() {
  editPatterns.value = [...currentSkill.value.trigger_patterns]
  editVisible.value = true
}

/**
 * 输入：无。
 * 输出：无；向触发模式编辑器追加一个空输入项。
 * 功能：允许管理员新增技能触发语句。
 */
function addPattern() {
  editPatterns.value.push('')
}

/**
 * 输入：待删除触发模式的数组索引 ``index``。
 * 输出：无；原地删除指定输入项。
 * 功能：支持管理员移除不再适用的触发语句。
 */
function removePattern(index) {
  editPatterns.value.splice(index, 1)
}

/**
 * 输入：无；读取触发模式编辑数组。
 * 输出：完成技能更新请求的 Promise。
 * 功能：去除空白模式并保存，至少保留一条有效触发语句。
 */
async function savePatterns() {
  const patterns = []
  for (const pattern of editPatterns.value) {
    const normalized = pattern.trim()
    if (normalized) patterns.push(normalized)
  }
  if (!patterns.length) {
    ElMessage.warning('至少保留一条触发模式')
    return
  }
  submitting.value = true
  try {
    const updated = await api.put(`/skills/${currentSkill.value.id}`, { trigger_patterns: patterns })
    currentSkill.value.trigger_patterns = updated.trigger_patterns
    editVisible.value = false
    ElMessage.success('触发模式已更新')
    await loadSkills()
  } finally {
    submitting.value = false
  }
}

/**
 * 输入：技能对象 ``skill``。
 * 输出：完成二次确认和删除请求的 Promise。
 * 功能：只允许删除手动创建的 Skill，自动 Skill 始终保留。
 */
async function deleteSkill(skill) {
  if (skill.type === 'auto') return
  try {
    await ElMessageBox.confirm(`确认删除 Skill“${skill.name}”吗？删除后不可恢复。`, '删除 Skill', { type: 'warning' })
    await api.delete(`/skills/${skill.id}`)
    detailVisible.value = false
    ElMessage.success('Skill 已删除')
    await loadSkills()
  } catch {
    // 用户取消或请求失败时保留技能。
  }
}

/**
 * 输入：技能类型 ``type``。
 * 输出：自动或手动创建的中文标签。
 * 功能：统一技能卡片与抽屉中的类型文案。
 */
function typeLabel(type) {
  return type === 'auto' ? '自动创建' : '手动创建'
}

onMounted(loadSkills)
</script>

<template>
  <section>
    <div class="page-heading">
      <div>
        <h1>Skill 管理</h1>
        <p>管理 Agent 从复杂分析任务中沉淀的可复用工作流</p>
      </div>
      <div class="skill-summary"><span><MagicStick /> 已沉淀</span><strong>{{ total }}</strong><small>个技能</small></div>
    </div>

    <el-card class="page-card filter-card" shadow="never">
      <div class="toolbar">
        <el-select v-model="filters.type" clearable placeholder="全部类型" style="width: 160px" @change="applyFilters"><el-option label="自动创建" value="auto" /><el-option label="手动创建" value="manual" /></el-select>
        <el-select v-model="filters.status" clearable placeholder="全部状态" style="width: 150px" @change="applyFilters"><el-option label="已启用" value="enabled" /><el-option label="已禁用" value="disabled" /></el-select>
        <el-input v-model="filters.keyword" clearable placeholder="搜索技能名称" :prefix-icon="Search" style="width: 250px" @keyup.enter="applyFilters" @clear="applyFilters" />
        <el-button @click="applyFilters">查询</el-button>
      </div>
    </el-card>

    <el-skeleton v-if="loading" :rows="12" animated />
    <template v-else-if="skills.length">
      <div class="skill-grid">
        <el-card v-for="skill in skills" :key="skill.id" :class="['skill-card', skill.type]" shadow="never" @click="openSkill(skill)">
          <div class="skill-card-top">
            <el-tag :type="skill.type === 'auto' ? 'primary' : 'info'" size="small" effect="light">{{ typeLabel(skill.type) }}</el-tag>
            <el-badge :value="`v${skill.version}`" type="info" />
          </div>
          <h2>{{ skill.name }}</h2>
          <p class="description">{{ skill.description }}</p>
          <div class="trigger-preview">
            <span class="label">触发模式</span>
            <div v-for="pattern in skill.trigger_patterns.slice(0, 2)" :key="pattern">“{{ pattern }}”</div>
          </div>
          <div class="quality-line"><span>平均质量评分</span><strong>{{ skill.quality_score }}</strong></div>
          <el-progress :percentage="skill.quality_score" :show-text="false" :stroke-width="5" color="#ff6a1a" />
          <div class="skill-card-footer">
            <span>已使用 <strong>{{ skill.usage_count }}</strong> 次</span>
            <div @click.stop><el-switch v-model="skill.enabled" @change="toggleSkill(skill)" /></div>
          </div>
        </el-card>
      </div>
      <div class="table-footer"><el-pagination v-model:current-page="filters.page" v-model:page-size="filters.size" background layout="total, sizes, prev, pager, next" :page-sizes="[8, 12, 24]" :total="total" @current-change="loadSkills" @size-change="applyFilters" /></div>
    </template>
    <el-empty v-else class="page-card skill-empty" description="尚无沉淀的技能。Agent 会在完成复杂分析任务后自动创建技能" />

    <el-drawer v-model="detailVisible" size="50%" title="Skill 详情">
      <el-skeleton v-if="detailLoading" :rows="14" animated />
      <template v-else-if="currentSkill">
        <div class="skill-detail-heading">
          <div><div class="title-row"><h2>{{ currentSkill.name }}</h2><el-tag :type="currentSkill.type === 'auto' ? 'primary' : 'info'">{{ typeLabel(currentSkill.type) }}</el-tag><el-tag effect="plain">v{{ currentSkill.version }}</el-tag></div><p>{{ currentSkill.description }}</p></div>
          <el-switch v-model="currentSkill.enabled" inline-prompt active-text="启" inactive-text="禁" @change="toggleSkill(currentSkill)" />
        </div>

        <div class="drawer-section">
          <h3 class="section-title"><span>完整触发模式</span><el-button text type="primary" :icon="Edit" @click="openPatternEditor">编辑</el-button></h3>
          <div class="pattern-list"><el-tag v-for="pattern in currentSkill.trigger_patterns" :key="pattern" effect="plain">{{ pattern }}</el-tag></div>
        </div>

        <div class="drawer-section">
          <h3 class="section-title">工作流步骤</h3>
          <el-steps direction="vertical" :active="visibleSteps.length" finish-status="success" class="workflow-steps">
            <el-step v-for="step in visibleSteps" :key="step.order" :title="`${step.order}. ${step.tool}`" :description="step.description" />
          </el-steps>
          <el-button v-if="currentSkill.workflow_steps.length > 8" text type="primary" @click="showAllSteps = !showAllSteps">{{ showAllSteps ? '收起后续步骤' : `展开全部 ${currentSkill.workflow_steps.length} 步` }}</el-button>
        </div>

        <div class="drawer-section">
          <h3 class="section-title">输出模板预览</h3>
          <pre class="template-preview">{{ currentSkill.output_template }}</pre>
        </div>

        <div class="drawer-section score-section">
          <h3 class="section-title"><span>使用统计</span><span class="statistics">{{ currentSkill.usage_count }} 次使用 · 平均 {{ currentSkill.quality_score }} 分</span></h3>
          <div class="score-chart"><EChart :option="scoreOption" /></div>
        </div>

        <div class="drawer-section">
          <h3 class="section-title">版本历史</h3>
          <el-timeline><el-timeline-item v-for="version in currentSkill.version_history" :key="version.version" :timestamp="version.created_at" placement="top"><strong>v{{ version.version }}</strong><p class="muted">{{ version.note }}</p></el-timeline-item></el-timeline>
        </div>

        <div class="detail-actions">
          <el-tooltip :disabled="currentSkill.type !== 'auto'" content="自动创建的技能不可删除，可选择禁用" placement="top">
            <span><el-button type="danger" plain :icon="Delete" :disabled="currentSkill.type === 'auto'" @click="deleteSkill(currentSkill)">删除技能</el-button></span>
          </el-tooltip>
        </div>
      </template>
    </el-drawer>

    <el-dialog v-model="editVisible" title="编辑触发模式" width="580px">
      <p class="edit-help">Agent 会在用户提问匹配以下语句时优先调用该 Skill。</p>
      <div class="pattern-editor">
        <div v-for="(pattern, index) in editPatterns" :key="index" class="pattern-input"><el-input v-model="editPatterns[index]" placeholder="输入一条触发语句" /><el-button text type="danger" :icon="Delete" @click="removePattern(index)" /></div>
      </div>
      <el-button text type="primary" :icon="Plus" @click="addPattern">添加触发语句</el-button>
      <template #footer><el-button @click="editVisible = false">取消</el-button><el-button type="primary" :loading="submitting" @click="savePatterns">保存修改</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.skill-summary { display: flex; align-items: baseline; gap: 5px; color: #7a8598; }
.skill-summary span { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.skill-summary span svg { width: 14px; }
.skill-summary strong { margin-left: 7px; color: #ff6818; font-size: 24px; }
.skill-summary small { font-size: 11px; }
.filter-card { margin-bottom: 18px; }
.filter-card :deep(.el-card__body) { padding-bottom: 2px; }
.skill-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.skill-card { position: relative; overflow: hidden; border-top: 3px solid #8792a5 !important; cursor: pointer; transition: transform .2s, box-shadow .2s; }
.skill-card.auto { border-top-color: #4f7cff !important; }
.skill-card:hover { transform: translateY(-3px); box-shadow: 0 13px 28px rgb(26 38 61 / 8%) !important; }
.skill-card-top { display: flex; align-items: center; justify-content: space-between; min-height: 25px; }
.skill-card h2 { margin: 15px 0 8px; color: #212c43; font-size: 16px; }
.description { height: 40px; margin: 0; overflow: hidden; color: #798499; font-size: 12px; line-height: 1.65; }
.trigger-preview { min-height: 74px; margin: 15px 0; padding: 10px 11px; border-radius: 9px; color: #647087; font-size: 10px; line-height: 1.7; background: #f7f9fc; }
.trigger-preview .label { display: block; margin-bottom: 3px; color: #9aa3b3; font-weight: 700; }
.quality-line { display: flex; justify-content: space-between; margin-bottom: 6px; color: #7d8799; font-size: 10px; }
.quality-line strong { color: #e96520; }
.skill-card-footer { display: flex; align-items: center; justify-content: space-between; margin-top: 15px; padding-top: 13px; border-top: 1px solid #edf0f4; color: #8993a5; font-size: 10px; }
.skill-card-footer strong { color: #46536b; }
.skill-empty { padding: 80px 0; }
.skill-detail-heading { display: flex; align-items: flex-start; justify-content: space-between; padding-bottom: 20px; border-bottom: 1px solid #e8edf4; }
.title-row { display: flex; align-items: center; gap: 8px; }
.title-row h2 { margin: 0; color: #202b42; font-size: 20px; }
.skill-detail-heading p { margin: 9px 30px 0 0; color: #778297; font-size: 12px; line-height: 1.7; }
.pattern-list { display: flex; flex-wrap: wrap; gap: 8px; }
.workflow-steps { max-height: 620px; }
.workflow-steps :deep(.el-step__description) { padding-bottom: 16px; color: #7d879a; font-size: 11px; }
.template-preview { margin: 0; padding: 15px; overflow: auto; border: 1px solid #e8edf3; border-radius: 10px; color: #41506a; font-size: 11px; line-height: 1.65; background: #f8fafc; white-space: pre-wrap; }
.statistics { color: #8791a3; font-size: 11px; font-weight: 400; }
.score-chart { height: 260px; }
.score-chart :deep(.echart) { min-height: 260px; }
.drawer-section :deep(.el-timeline) { padding-left: 4px; }
.drawer-section :deep(.el-timeline-item__content strong) { color: #38465f; font-size: 12px; }
.drawer-section :deep(.el-timeline-item__content p) { margin: 5px 0; font-size: 11px; }
.detail-actions { display: flex; justify-content: flex-end; padding: 22px 0; }
.edit-help { margin: -4px 0 16px; color: #7d8798; font-size: 12px; }
.pattern-editor { display: grid; gap: 10px; margin-bottom: 7px; }
.pattern-input { display: flex; align-items: center; gap: 4px; }
@media (max-width: 1500px) { .skill-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
</style>
