# Suning Hermes Agent

苏宁售后场景的 Hermes Agent 项目。当前版本聚焦 IM 端只读业务查询，并已实现
结构化多轮上下文、跨系统订单全链路与用户级长期记忆。

## 项目结构

```text
.
├── backend/        # Python、FastMCP、数据库配置与后端测试
├── frontend/       # P2 管理后台占位页
├── packages/       # Python 3.11+ 公共上下文运行时
├── .hermes/        # Hermes RBAC、上下文/长期记忆 Hooks 与 Skill
└── docs/           # 需求、设计与运维文档
```

## 后端启动

```bash
cd backend
uv sync
uv run python -m mcp_suning.servers.order
```

后端从 `backend/.env` 读取数据库配置。首次配置时可以复制
`backend/.env.example`，再填写实际账号和密码。

Hermes 插件运行在独立 Python 3.11 虚拟环境中，部署时需要把公共上下文包及其
DashScope/Milvus 依赖安装到该环境，并把 Redis、MySQL、DeepSeek 和长期记忆配置加入
`~/.hermes/.env`。完整步骤与飞书验证输入参见
[`docs/09-QuickStart/09f-飞书上下文槽位测试案例集.md`](docs/09-QuickStart/09f-飞书上下文槽位测试案例集.md)。
长期记忆的数据流、配置和验收结果见
[`docs/longterm_memory.md`](docs/longterm_memory.md)。
订单、工单、物流和退款的实时聚合实现见
[`docs/跨系统订单全链路的实现.md`](docs/跨系统订单全链路的实现.md)。

运行后端测试：

```bash
cd backend
uv run pytest
```

## 前端占位页

```bash
cd frontend
npm ci
npm run dev
```

管理后台尚未进入实现阶段，因此只依赖 Vue 和 Vite。

## MySQL 启动

```bash
cd backend/infra/mysql
docker compose --env-file .env up -d
```

详细操作说明参见 `docs/backend-operations.md`。

## 开发约束

所有函数必须用 docstring 明确记录输入、输出和功能；新增实现也必须遵循。完整规则
见 `AGENTS.md`，后端测试会自动检查注释完整性。
