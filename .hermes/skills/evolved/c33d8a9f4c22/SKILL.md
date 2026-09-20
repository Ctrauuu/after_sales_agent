---
name: generate_aftersale_trend_chart
description: 根据自然语言生成退单/退货趋势图：通过 NL2SQL 按时间粒度统计退单量，调用图表工具发送趋势图，并输出数据口径、窗口说明、形态描述和 RBAC 范围提示。
---

# generate_aftersale_trend_chart

根据自然语言生成退单/退货趋势图：通过 NL2SQL 按时间粒度统计退单量，调用图表工具发送趋势图，并输出数据口径、窗口说明、形态描述和 RBAC 范围提示。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-28T07:27:18.098528+00:00",
  "description": "根据自然语言生成退单/退货趋势图：通过 NL2SQL 按时间粒度统计退单量，调用图表工具发送趋势图，并输出数据口径、窗口说明、形态描述和 RBAC 范围提示。",
  "enabled": true,
  "name": "generate_aftersale_trend_chart",
  "output_template": "✅ 趋势图已生成并发送到当前会话。 **图表数据口径**（全部来自实时查询返回，未补零、未编造）： | 日期 | {metric}量 | |---|---| {rows} - **窗口**：近 {date_range_days} 天，数据源实际返回 {actual_days} 天有记录 - **形态**：{trend_shape} - ⚠️ 数据范围受 RBAC 限制，仅覆盖 {scope}，非全量 如需调整时间窗口、换维度或图表类型，随时说。",
  "required_mcp_tools": [
    "query_return_stats_nl2sql",
    "tool_describe",
    "send_aftersale_chart"
  ],
  "skill_id": "c33d8a9f4c22",
  "trigger_patterns": [
    "拉个退单趋势图",
    "拉个退货趋势图",
    "生成退单趋势",
    "近{days}天退单走势",
    "按{group_by}看退单趋势"
  ],
  "updated_at": "2026-08-28T07:27:18.098528+00:00",
  "usage_count": 1,
  "version": 1,
  "workflow": [
    {
      "output_key": "stats",
      "params": {
        "date_range_days": "{date_range_days}",
        "group_by": "{group_by}"
      },
      "step": 1,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "chart_tool_info",
      "params": {
        "name": "send_aftersale_chart"
      },
      "step": 2,
      "tool": "tool_describe"
    },
    {
      "output_key": "chart_result",
      "params": {
        "chart_type": "line",
        "data_kind": "trend",
        "labels": "{labels}",
        "summary": "{summary}",
        "title": "{title}",
        "values": "{values}",
        "ylabel": "{ylabel}"
      },
      "step": 3,
      "tool": "send_aftersale_chart"
    }
  ]
}
```
