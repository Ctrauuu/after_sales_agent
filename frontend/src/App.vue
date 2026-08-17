<script setup>
import {
  ChatLineSquare,
  Connection,
  MagicStick,
  Monitor,
  User,
} from '@element-plus/icons-vue'
</script>

<template>
  <el-container class="app-shell">
    <el-aside class="sidebar" width="232px">
      <div class="brand">
        <span class="brand-mark">H</span>
        <div>
          <strong>Hermes Agent</strong>
          <small>运营管理后台</small>
        </div>
      </div>

      <el-menu
        router
        :default-active="$route.path"
        class="nav-menu"
        background-color="transparent"
        text-color="#94a3b8"
        active-text-color="#ffffff"
      >
        <el-menu-item index="/dashboard">
          <el-icon><Monitor /></el-icon>
          <span>系统监控</span>
        </el-menu-item>
        <el-menu-item index="/users">
          <el-icon><User /></el-icon>
          <span>用户与权限</span>
        </el-menu-item>
        <el-menu-item index="/mcp-services">
          <el-icon><Connection /></el-icon>
          <span>MCP 服务</span>
        </el-menu-item>
        <el-menu-item index="/conversation-logs">
          <el-icon><ChatLineSquare /></el-icon>
          <span>对话日志</span>
        </el-menu-item>
        <el-menu-item index="/skills">
          <el-icon><MagicStick /></el-icon>
          <span>Skill 管理</span>
        </el-menu-item>
      </el-menu>

      <div class="sidebar-footer">
        <span class="online-dot" />
        <div>
          <strong>系统运行正常</strong>
          <small>生产环境 · 华东</small>
        </div>
      </div>
    </el-aside>

    <el-container class="content-shell">
      <el-header class="topbar">
        <div>
          <span class="environment-pill">ADMIN CONSOLE</span>
        </div>
        <div class="admin-profile">
          <div>
            <strong>系统管理员</strong>
            <small>admin@suning.com</small>
          </div>
          <el-avatar :size="36">管</el-avatar>
        </div>
      </el-header>
      <el-main class="page-container">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>
