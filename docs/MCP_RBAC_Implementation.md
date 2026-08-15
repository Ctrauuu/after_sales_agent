# MCP RBAC 实现

本文只描述当前代码的必要安全链路。系统复用 Hermes Gateway，不在项目内维护
第二套 IM Gateway。

## 调用链

```text
IM 发送者
→ Hermes Gateway 请求级 ContextVar
→ suning-rbac-bridge 插件
→ 30 秒 HMAC 凭证
→ MCP tools/call._meta["suning/authn"]
→ 验签、工具绑定、有效期与 Redis 防重放
→ user_platform_binding 映射内部用户
→ user_identity + user_role_permission
→ 行级 SQL 条件与返回值脱敏
```

身份和业务参数走两条不同路径：模型只能填写查询条件；平台用户 ID、角色和权限
不会出现在工具 schema 中。

## 代码位置

```text
.hermes/plugins/suning-rbac-bridge/
├── __init__.py       # 注册模型可见工具
├── schemas.py        # 仅包含业务参数的 schema
└── bridge.py         # 读取 Hermes 身份、签名并调用 MCP

backend/mcp_suning/
├── auth_attestation.py  # 验签与防重放
├── auth_middleware.py   # 用户映射、授权范围、脱敏
├── order_server.py
├── aftersale_server.py
├── product_server.py
├── logistics_server.py
└── payment_server.py
```

## 为什么需要插件桥接

Hermes Gateway 知道当前消息的真实发送者，但普通 MCP 工具参数来自模型。若把
`user_id` 或 `role` 放进工具参数，模型或提示词注入就能伪造身份。插件运行在
Hermes 进程内，可以读取请求级身份，并通过私有 MCP `_meta` 传递签名凭证。

凭证包含：

- 平台与平台用户 ID；
- 私聊/群聊类型；
- 目标工具名；
- 签发和过期时间；
- 一次性 `jti`。

MCP 使用共享密钥验证 HMAC，并通过 Redis `SET NX` 消费 `jti`。生产环境 Redis
不可用时默认拒绝请求。

## 权限数据

权限只以现有数据库为准，不再维护一份硬编码角色策略或权限缓存：

- `user_platform_binding`：平台用户 ID 到 Hermes 内部用户；
- `user_identity`：员工、角色、用户级附加范围和启用状态；
- `user_role_permission`：角色区域、品类、最大查询天数和数据粒度。

角色范围与用户范围取交集。请求可进一步缩小范围，不能扩大范围。群聊最多返回
匿名明细；`aggregated` 角色只能调用统计工具。

不缓存权限的原因是当前规模无需为一次简单查表增加 Redis/本地双层缓存和失效
协议，同时权限撤销可以立即生效。Redis 只保留身份凭证防重放这一项必要职责。

## 业务工具约束

每个工具必须先授权，再访问业务表：

```python
_, safe_filters = authorize_mcp_request(ctx, "search_orders", filters)
scope_sql, scope_params = build_scope_clause(
    safe_filters,
    region_column="o.region_code",
    city_column="o.city_code",
    category_column="s.category_l3_code",
)
```

必须遵守以下规则：

1. 工具参数不得加入用户、工号、角色或权限字段。
2. 只能使用 `safe_filters` 生成 SQL 权限条件。
3. 权限值必须使用 SQLAlchemy 绑定参数。
4. 返回前按 `data_scope` 调用 `mask_sensitive_data()`。
5. 新增明细工具默认不允许 `aggregated` 角色访问。

## 配置

```dotenv
SUNING_MCP_BRIDGE_SECRET=至少32字节的随机密钥
SUNING_IDENTITY_ISSUER=suning-feishu-primary
REDIS_URL=redis://127.0.0.1:6380/0
SUNING_AUTHN_REQUIRE_REDIS=true
```

插件和五个 MCP 进程必须使用相同的桥接密钥与身份命名空间。私有 MCP 地址由
`SUNING_MCP_*_URL` 配置，默认使用本机 8101～8105 端口。

## 拒绝条件

以下情况统一默认拒绝：凭证缺失或篡改、凭证过期、目标工具不匹配、`jti` 重放、
未知平台绑定、用户禁用、角色不存在、必需范围为空、请求范围越权。

测试入口：

```bash
cd backend
uv run pytest
```
