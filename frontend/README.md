# Frontend

Hermes Agent 管理后台，使用 Vue 3、Vite、Element Plus 和 ECharts，实现系统监控、用户权限、MCP 服务、对话审计和 Skill 管理。

先启动真实管理 API：

```bash
cd ../backend
uv run uvicorn suning_hermes_agent.admin_api:app --host 127.0.0.1 --port 8080
```

再启动前端：

```bash
npm ci
npm run dev
```

开发服务器默认把 `/api` 代理到 `http://127.0.0.1:8080`。远端地址可通过
`VITE_ADMIN_API_TARGET` 修改；若后端配置了 `SUNING_ADMIN_API_TOKEN`，前端开发服务器使用同名环境变量由代理注入令牌，不会暴露给浏览器代码。

```bash
npm run build
```
