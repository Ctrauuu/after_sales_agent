# NL2SQL 实现与代码阅读顺序

设计依据是 [`05b-NL2SQL可靠生成.md`](../../docs/05-技术重难点/05b-NL2SQL可靠生成.md)。
两个动态售后分析工具共用一条 Pipeline：

```text
自然语言问题或结构化统计参数
  -> 形成分析问题
  -> Schema-first System Prompt
  -> DeepSeek 候选 SELECT
  -> SQL 沙箱校验和修正
  -> 服务端 RBAC 注入
  -> EXPLAIN
  -> SELECT
  -> 响应脱敏
```

## 1. 哪些 MCP Tool 使用 NL2SQL

判断标准是：**SQL 结构能否由明确的 API 参数完整确定**。

| MCP Tool | 实现方式 | 原因 |
| --- | --- | --- |
| `query_return_stats_nl2sql` | 共用 NL2SQL Pipeline | 分组维度和统计组合会扩展，属于动态聚合 |
| `query_aftersale_nl2sql` | 共用 NL2SQL Pipeline | 承接复杂运营分析、组合指标和临时统计 |
| `query_sku_return_rate` | 固定参数化 SQL | SKU 退单率依赖订单-SKU 明细分母，必须保持口径稳定 |
| `search_orders` | 固定 SQL | 状态、品类、时间等筛选参数明确 |
| `get_order_detail` | 固定 SQL | 按订单 ID 查询，结构固定 |
| `get_aftersale_workflow` | 固定 SQL | 按退单 ID 查询固定流程字段 |
| `get_product_info` | 固定 SQL | 按 SKU 查询，结构固定 |
| `query_logistics` | 固定 SQL | 按退单 ID 查询物流轨迹 |
| `get_refund_status` | 固定 SQL | 按退单 ID 查询退款状态 |

“复杂运营数据分析”和“临时统计查询”仍由 `query_aftersale_nl2sql` 承接。`query_sku_return_rate`
是有明确分母和稳定返回结构的核心指标，不与 NL2SQL 重叠。

固定业务 API 不调用模型，因为固定 SQL 延迟更低、返回结构更稳定，也更容易测试。

### 1.1 Hermes 中的工具名

三个售后分析工具通过 `.hermes/plugins/suning-rbac-bridge` 注册到 `suning_business` toolset，Agent 调用时
使用裸名称 `query_return_stats_nl2sql`、`query_aftersale_nl2sql` 和 `query_sku_return_rate`。插件的 `schemas.py`、
`plugin.yaml`、MCP 的 `@mcp.tool` 名称以及数据库工具注册表必须完全一致；不要猜测 `mcp__`、点号或
冒号前缀。修改桥接 schema 后需要重启 Hermes gateway，已加载旧工具说明的会话还应重新开始会话。

`backend/tests/test_mcp_rbac_wiring.py` 会比较 MCP、桥接 schema 和插件清单中的工具名，防止只在其中
一层改名。

## 2. 推荐阅读顺序

### 第一步：售后分析 MCP 入口

先看 [`servers/aftersale.py`](../mcp_suning/servers/aftersale.py)：

1. `query_return_stats_nl2sql`
2. `query_aftersale_nl2sql`
3. `query_sku_return_rate`

`query_return_stats_nl2sql` 保留稳定的结构化参数接口，支持 `day`、`category`、`reason`、`region`、
`brand` 五个分组维度。它把授权后的统计参数转换成受控问题，再调用 Pipeline。

`query_aftersale_nl2sql` 直接接收自然语言，用于固定统计参数表达不了的组合维度、复杂运营分析和
临时统计。

`query_sku_return_rate` 接收日期、Top N 和品类参数，以订单创建时间为统计窗口，分母取订单-SKU
明细；退单条件保留在 `LEFT JOIN`，确保无退单订单仍进入分母。

### 第二步：共用 Pipeline 的创建位置

阅读 [`runtime.py`](runtime.py)。模块只创建一个 `nl2sql_pipeline`，统一配置：

- `DeepSeekSQLGenerator`
- `SQLValidator`
- `ReadOnlyExecutor`
- `max_attempts=2`

两个动态工具都引用这个对象，不再分别组装 Pipeline。当前售后 MCP 是一个进程，因此它们共享
同一个进程内实例；如果以后拆成不同进程，每个服务进程仍会有自己的实例。

### 第三步：Pipeline 主流程

阅读 [`pipeline.py`](pipeline.py) 的 `NL2SQLPipeline.run()`：

```text
build_schema_prompt
  -> generator(system_prompt, user_prompt)
  -> validator.validate_and_fix
  -> validator.inject_rbac
  -> executor.run
```

校验或数据库执行失败时，错误加入下一轮 User Prompt。默认最多两轮。

### 第四步：Schema 和 System Prompt

阅读 [`schema_registry.py`](schema_registry.py)：

1. `SCHEMA_REGISTRY`：模型允许使用的表和字段。
2. `JOIN_RELATIONS`：允许使用的固定关联。
3. `build_schema_prompt`：System Prompt 的规则。

Prompt 负责帮助模型正确生成 SQL，但最终安全边界仍是服务端校验和 RBAC。

### 第五步：模型调用

阅读 [`generator.py`](generator.py) 的 `DeepSeekSQLGenerator.__call__()`。

Schema 和规则进入 System Message，分析问题进入 User Message。API Key、HTTP、响应结构或空结果
错误会抛出 `RuntimeError`。

### 第六步：SQL 沙箱

阅读 [`sql_sandbox.py`](sql_sandbox.py)，建议顺序是：

1. `validate_and_fix`
2. `_clean`
3. `_used_tables`、`_join_error`、`_unknown_column`
4. `_aggregate_error`
5. `_insert_before_tail`
6. `inject_rbac`

这里完成 SQL 安全检查、默认时间、LIMIT 和 RBAC 注入。

### 第七步：EXPLAIN 和 SELECT

阅读 [`executor.py`](executor.py) 的 `ReadOnlyExecutor.run()`。它设置 5 秒执行上限，在同一连接上
先执行 EXPLAIN，再执行 SELECT。生产 MySQL 账号仍需只授予 SELECT 权限。

### 第八步：权限与测试

权限来源看 [`security/rbac.py`](../mcp_suning/security/rbac.py) 的：

1. `authorize_mcp_request`
2. `PermissionInterceptor.intercept`
3. `build_scope_clause`
4. `PermissionInterceptor.mask_sensitive_data`

行为验证看 [`test_nl2sql.py`](../tests/test_nl2sql.py)。测试包含两个动态工具共用 Pipeline、鉴权
顺序、Prompt、SQL 沙箱、RBAC、执行和重试。

## 3. 两个动态工具怎样进入 Pipeline

### 3.1 `query_return_stats_nl2sql`

接口保持结构化：

```python
query_return_stats_nl2sql(ctx, group_by, date_range_days, category)
```

执行顺序：

1. 用原始统计参数调用 `authorize_mcp_request()`。
2. 权限层收窄时间和品类，得到 `safe_filters`。
3. 根据 `group_by` 形成受控分析问题。
4. 把问题和 `safe_filters` 交给共用 Pipeline。
5. 只取 Pipeline 的 `rows`，保持原有列表返回类型。
6. 按 `data_scope` 脱敏。

例如参数：

```python
group_by="day"
date_range_days=14
category="空调"
```

会形成类似的受控问题：

```text
统计最近 14 天的退单数据，按退单申请日期分组；
返回 dimension_code、dimension_name、return_count、total_amount、total_amount_yuan，
只统计空调及其子品类，按 return_count 降序。
```

这不是让模型重新解释 API 含义，而是利用共用 Schema 和 Pipeline 动态生成不同分组 SQL。

### 3.2 `query_aftersale_nl2sql`

接口直接接收问题：

```python
query_aftersale_nl2sql(question, ctx)
```

它用于以下场景：

- 多个维度组合，例如“按区域和品牌比较近 30 天退单量”。
- 指标组合，例如同时分析退单数量、金额和平均金额。
- 复杂运营分析，例如“找出退单增长最快的品类”。
- 临时统计，例如运营临时提出、尚未固化成结构化 API 的问题。

如果某类临时统计逐渐变成稳定高频需求，可以再把它固化为明确参数的 MCP Tool；在固化之前由
`query_aftersale_nl2sql` 承接。

## 4. 共用 Pipeline 的实际流程

### 4.1 构造 Prompt

`build_schema_prompt()` 把五张表、固定 JOIN 和 SQL 规则组成 System Prompt。分析问题作为独立
User Message。

基础表和别名是：

```text
t_aftersale_return AS r
t_order_main AS o
t_product_sku AS s
```

品类和品牌分析分别使用 `t_product_category AS c`、`t_product_brand AS b`。

### 4.2 校验和修正

`validate_and_fix()` 依次：

1. 清理 Markdown 围栏和末尾分号。
2. 拒绝危险关键字、多语句、注释和非 SELECT。
3. 拒绝 `SELECT *`、`CROSS JOIN`。
4. 检查表、限定字段和固定 JOIN；允许已登记表显式声明的子查询别名，例如 `r2.create_time`。
5. 只允许聚合分析，拒绝标识和明细字段。
6. 要求 `WHERE`。
7. 没有时间条件时，以退单表最新数据时间为锚点补最近 30 天。
8. 没有 LIMIT 时补 500，超过 1000 时改成 1000。

默认时间条件：

```sql
AND r.create_time >= (
    SELECT MAX(create_time) - (30 * 86400)
    FROM t_aftersale_return
)
```

中文品类使用统一编码映射。例如“空调及其子品类”应生成：

```sql
AND (
    s.category_l3_code = 'C1-AC'
    OR s.category_l3_code LIKE 'C1-AC-%'
)
```

不能依赖 `c.category_name LIKE '%空调%'`，因为名称字段可能缺失或不规范。

### 4.3 注入 RBAC

候选 SQL 通过沙箱后，根据 `safe_filters` 参数化追加区域、城市、品类和角色最大时间范围。例如：

```sql
AND (o.region_code IN (:rbac_region_0))
AND r.create_time >= (
    SELECT MAX(create_time) - (:rbac_date_range_days * 86400)
    FROM t_aftersale_return
)
```

权限值单独放在 SQLAlchemy 参数中，不直接拼进 SQL。默认时间和 RBAC 最大时间都使用相同的
数据时间锚点，避免系统时钟晚于模拟数据时得到空结果。

### 4.4 EXPLAIN、执行和返回

执行器依次运行：

```sql
SET SESSION MAX_EXECUTION_TIME = 5000;
EXPLAIN <最终 SELECT>;
<最终 SELECT>;
```

Pipeline 内部返回 `sql`、`explain` 和 `rows`。两个 MCP Tool 都不对外暴露 EXPLAIN：

- `query_return_stats_nl2sql` 返回脱敏后的统计行列表。
- `query_aftersale_nl2sql` 返回 `success`、`question`、`sql`、`row_count` 和 `rows`。

## 5. 重试边界

| 情况 | 是否重试 |
| --- | --- |
| SQL 沙箱拒绝候选 SQL | 是，未达到 `max_attempts` 时 |
| EXPLAIN/SELECT 抛 `SQLAlchemyError` | 是，未达到 `max_attempts` 时 |
| DeepSeek 配置、HTTP 或响应错误 | 否 |
| 鉴权或 RBAC 参数错误 | 否 |
| 空自然语言问题 | 否 |

## 6. 当前边界

- 两个动态工具只支持售后退单聚合查询。
- 只支持 Schema Registry 中的固定表、别名和 JOIN。
- SQL 校验器基于正则，面向受限 SQL 子集。
- EXPLAIN 当前不分析成本并设置拒绝阈值。
- `ReadOnlyExecutor` 只执行校验后的 SELECT，数据库账号的 SELECT-only 权限由部署保证。

## 7. 修改后如何验证

```bash
cd backend
uv run pytest
```

代码、测试、本文件和 05b 设计文档需要同步修改。
