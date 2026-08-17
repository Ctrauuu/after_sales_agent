import { createRouter, createWebHistory } from 'vue-router'
import ConversationLogs from '../views/ConversationLogs.vue'
import Dashboard from '../views/Dashboard.vue'
import MCPServiceManagement from '../views/MCPServiceManagement.vue'
import SkillManagement from '../views/SkillManagement.vue'
import UserManagement from '../views/UserManagement.vue'

const routes = [
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', component: Dashboard },
  { path: '/users', component: UserManagement },
  { path: '/mcp-services', component: MCPServiceManagement },
  { path: '/conversation-logs', component: ConversationLogs },
  { path: '/skills', component: SkillManagement },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
