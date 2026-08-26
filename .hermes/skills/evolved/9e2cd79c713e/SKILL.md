---
name: generate_return_trend_chart
description: 当用户要求查看退单/退货趋势图时，实时查询指定时间窗口内按日聚合的退单数据，并生成趋势图发送到当前会话（桌面版+移动版）。
---

# generate_return_trend_chart

当用户要求查看退单/退货趋势图时，实时查询指定时间窗口内按日聚合的退单数据，并生成趋势图发送到当前会话（桌面版+移动版）。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-25T13:34:49.085891+00:00",
  "description": "当用户要求查看退单/退货趋势图时，实时查询指定时间窗口内按日聚合的退单数据，并生成趋势图发送到当前会话（桌面版+移动版）。",
  "enabled": true,
  "name": "generate_return_trend_chart",
  "output_template": "✅ 趋势图已生成并发送到当前会话（桌面版 + 移动版双图）。图表数据口径全部来自实时查询返回，未补零、未编造。日期与退单量如下：{return_daily_stats.detail}；窗口：{date_range_days}天，但数据源实际仅返回部分日期；形态：{return_daily_stats.pattern}。如需调整时间窗口、按其他维度或图表类型，随时说。",
  "required_mcp_tools": [
    "query_return_stats_nl2sql",
    "send_aftersale_chart"
  ],
  "skill_id": "9e2cd79c713e",
  "trigger_patterns": [
    "拉个退单趋势图",
    "退单趋势图",
    "退单趋势",
    "看下退单趋势"
  ],
  "updated_at": "2026-08-25T13:34:49.085891+00:00",
  "usage_count": 1,
  "version": 1,
  "workflow": [
    {
      "output_key": "return_daily_stats",
      "params": {
        "date_range_days": "{date_range_days}",
        "group_by": "day"
      },
      "step": 1,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "trend_chart_sent",
      "params": {
        "chart_type": "trend",
        "labels": "{return_daily_stats.labels}",
        "summary": "{return_daily_stats.summary}",
        "title": "{chart_title}",
        "values": "{return_daily_stats.values}"
      },
      "step": 2,
      "tool": "send_aftersale_chart"
    },
    {
      "output_key": "line_chart_sent",
      "params": {
        "chart_type": "line",
        "labels": "{return_daily_stats.labels}",
        "summary": "{return_daily_stats.summary}",
        "title": "{chart_title}",
        "values": "{return_daily_stats.values}"
      },
      "step": 3,
      "tool": "send_aftersale_chart"
    }
  ]
}
```
