# 项目当前上下文

更新时间：2026-08-15

## 已完成

- Hermes、DeepSeek 与飞书 Gateway 正常运行
- MySQL 模拟业务库、用户绑定和角色权限数据已建立
- 订单、售后、商品、物流、支付五个只读 MCP 正常运行
- 项目桥接插件 `suning-rbac-bridge` 已安装并启用
- MCP 只接受短期 HMAC 身份凭证，不接受模型可填写的身份参数
- Redis 已用于身份凭证 `jti` 防重放
- 区域、城市、品类、时间范围、数据粒度和返回值脱敏已在 MCP 层强制执行
- 运营、区域经理、品控、VP、客服主管和未知用户的真实数据库验收已通过
- 三平台身份统一、私聊 30 分钟会话延续、短期上下文和用户级长期记忆已启用
- 三平台复杂分析 DAG 已启用；飞书和企微可发送图表，钉钉按 Gateway 能力降级为文字摘要

## 当前身份与权限链路

完整实现说明见 [MCP_RBAC_Implementation.md](MCP_RBAC_Implementation.md)。

```text
飞书 open_id / 企微 userid / 钉钉 unionid 或 userid
→ Hermes Gateway 请求级 ContextVar
→ suning-rbac-bridge
→ 30 秒 HMAC 身份凭证（绑定工具和 jti）
→ 私有 MCP tools/call._meta["suning/authn"]
→ 签名/时间/工具/Redis 重放校验
→ user_platform_binding
→ user_identity
→ user_role_permission
→ RBAC 范围求交
→ 参数化 SQL 行级过滤
→ 按 data_scope 返回或脱敏
```

## 当前运行端口

- 3306：MySQL
- 6380：Redis（容器内 6379）
- 8101：订单 MCP
- 8102：售后 MCP
- 8103：商品 MCP
- 8104：物流 MCP
- 8105：支付 MCP

## 安全边界

- 不修改 Hermes 源码
- 不新增外部身份绑定表，复用现有三张身份/RBAC 表
- 五个 MCP 地址继续运行，但在 Hermes 中为 `disabled`，模型不能直接调用
- 模型只看见桥接插件注册的八个苏宁业务工具
- 无签名、签名篡改、过期、错工具、重放、未知绑定和越权请求全部默认拒绝

## 本阶段边界

不修改 Hermes 源码、不新增统一用户中心或身份绑定表，也不迁移 Hermes 原生 transcript。
企微图表复用 Hermes 已连接的 AI Bot WebSocket（`WECOM_BOT_ID` / `WECOM_SECRET`），不依赖
企业应用凭据；钉钉当前会话 Webhook 不支持本地图片上传，因此保留文字降级。
