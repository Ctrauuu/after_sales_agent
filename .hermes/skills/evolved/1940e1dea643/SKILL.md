---
name: 订单售后链路追踪分析
description: 输入订单号，调用 trace_order_timeline 实时聚合订单售后链路各来源节点（订单、售后、物流、仓库），输出售后时间线、SLA 状态、卡点定位与处理建议。适用于用户查询指定订单的售后链路、售后进度、卡点/异常定位。
---

# 订单售后链路追踪分析

输入订单号，调用 trace_order_timeline 实时聚合订单售后链路各来源节点（订单、售后、物流、仓库），输出售后时间线、SLA 状态、卡点定位与处理建议。适用于用户查询指定订单的售后链路、售后进度、卡点/异常定位。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-27T15:09:14.043917+00:00",
  "description": "输入订单号，调用 trace_order_timeline 实时聚合订单售后链路各来源节点（订单、售后、物流、仓库），输出售后时间线、SLA 状态、卡点定位与处理建议。适用于用户查询指定订单的售后链路、售后进度、卡点/异常定位。",
  "enabled": true,
  "name": "订单售后链路追踪分析",
  "output_template": "## 订单售后链路 ### 概览 - 订单：{order_id}，创建时间 {created_at}，订单状态 {order_status} - 涉及退单：{return_list} ### 售后时间线 | 时间 | 来源 | 退单 | 节点 | 说明 | SLA | |---|---|---|---|---|---| {timeline_rows} ### 当前卡点 - {bottleneck_node}：{bottleneck_detail}，状态 {sla_status} ### 结论与建议 1. {conclusion_1} 2. {conclusion_2} 3. {suggestion}",
  "required_mcp_tools": [
    "trace_order_timeline"
  ],
  "skill_id": "1940e1dea643",
  "trigger_patterns": [
    "查订单{order_id}售后链路",
    "查询订单{order_id}的售后进度",
    "订单{order_id}售后时间线",
    "{order_id}售后链路卡点",
    "查订单202406010001售后链路"
  ],
  "updated_at": "2026-08-27T15:09:14.043917+00:00",
  "usage_count": 1,
  "version": 1,
  "workflow": [
    {
      "output_key": "after_sales_timeline",
      "params": {
        "order_id": "{order_id}"
      },
      "step": 1,
      "tool": "trace_order_timeline"
    }
  ]
}
```
