---
name: after_sales_order_timeline_trace
description: 查询指定订单的完整售后/退换货链路，实时聚合各数据源（退单、审核、取件、物流、仓库质检等），识别当前卡点、SLA 超时状态并给出处理建议。适用于订单售后链路排查。
---

# after_sales_order_timeline_trace

查询指定订单的完整售后/退换货链路，实时聚合各数据源（退单、审核、取件、物流、仓库质检等），识别当前卡点、SLA 超时状态并给出处理建议。适用于订单售后链路排查。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-25T13:33:10.039082+00:00",
  "description": "查询指定订单的完整售后/退换货链路，实时聚合各数据源（退单、审核、取件、物流、仓库质检等），识别当前卡点、SLA 超时状态并给出处理建议。适用于订单售后链路排查。",
  "enabled": true,
  "name": "after_sales_order_timeline_trace",
  "output_template": "## 订单售后链路 ### 概览 - 订单：{order_id}，创建时间 {created_at}，订单状态 {order_status} - 涉及退单：{return_records} ### 售后时间线 | 时间 | 来源 | 退单 | 节点 | 说明 | SLA | |---|---|---|---|---|---| {timeline_rows} ### 当前卡点 {current_bottleneck} ### 结论与建议 {conclusions_and_suggestions}",
  "required_mcp_tools": [
    "trace_order_timeline"
  ],
  "skill_id": "6c429f1d16fd",
  "trigger_patterns": [
    "查订单{order_id}售后链路",
    "查询{order_id}的售后/退换货链路",
    "{order_id} 售后进度/卡点",
    "trace order {order_id} after-sales timeline",
    "查订单202406010001售后链路",
    "查询{order_id}的售后时间线",
    "订单{order_id}售后链路追踪",
    "trace order after-sales timeline {order_id}",
    "查询订单{order_id}的售后时间线",
    "订单{order_id}售后/退单链路",
    "查一下{order_id}的售后进度"
  ],
  "updated_at": "2026-08-28T07:25:21.309594+00:00",
  "usage_count": 3,
  "version": 3,
  "workflow": [
    {
      "output_key": "order_timeline",
      "params": {
        "order_id": "{order_id}"
      },
      "step": 1,
      "tool": "trace_order_timeline"
    }
  ]
}
```
