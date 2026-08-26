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
  "output_template": "订单 {order_id} 售后链路已实时聚合（partial={timeline_result.partial}，source_failures={timeline_result.source_failures}）。 ## 订单概览 {timeline_result.order_overview} ## 售后时间线 {timeline_result.timeline_table} ## 当前卡点 {timeline_result.bottleneck} ## 结论与建议 {timeline_result.suggestions}",
  "required_mcp_tools": [
    "trace_order_timeline"
  ],
  "skill_id": "6c429f1d16fd",
  "trigger_patterns": [
    "查订单{order_id}售后链路",
    "查询{order_id}的售后/退换货链路",
    "{order_id} 售后进度/卡点",
    "trace order {order_id} after-sales timeline",
    "查订单202406010001售后链路"
  ],
  "updated_at": "2026-08-25T13:33:10.039082+00:00",
  "usage_count": 1,
  "version": 1,
  "workflow": [
    {
      "output_key": "timeline_result",
      "params": {
        "order_id": "{order_id}"
      },
      "step": 1,
      "tool": "trace_order_timeline"
    }
  ]
}
```
