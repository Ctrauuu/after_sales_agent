---
name: query_return_order_count_by_date_range
description: 查询最近指定天数内的退单量，按天汇总退单数、计算合计与峰值日期，用于售后退单趋势的快速分析。
---

# query_return_order_count_by_date_range

查询最近指定天数内的退单量，按天汇总退单数、计算合计与峰值日期，用于售后退单趋势的快速分析。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-25T12:25:58.861270+00:00",
  "description": "查询最近指定天数内的退单量，按天汇总退单数、计算合计与峰值日期，用于售后退单趋势的快速分析。",
  "enabled": true,
  "name": "query_return_order_count_by_date_range",
  "output_template": "## 最近 {days} 天退单量（实时查询） **近 {days} 天退单合计：{total} 单** | 日期 | 退单量 | |---|---:| {rows} **提示**：{days} 天窗口实际仅返回 {returned_days} 天数据，其余 {missing_days} 天数据源未返回记录；{peak_date} 为单日峰值（{peak_value} 单）。如需按品类/原因拆解或看趋势图，可继续查询。",
  "required_mcp_tools": [
    "query_return_stats_nl2sql"
  ],
  "skill_id": "c324237fb896",
  "trigger_patterns": [
    "最近{days}天内退单量",
    "近{days}天退单量",
    "查询近{days}天退货量",
    "{days}天内的退单量",
    "最近7天内退单量",
    "查询近{days}天退单量",
    "查询近{days}天退单统计",
    "{days}天内退单量趋势",
    "查询近7天退单量"
  ],
  "updated_at": "2026-08-25T13:05:23.757095+00:00",
  "usage_count": 2,
  "version": 2,
  "workflow": [
    {
      "output_key": "return_stats",
      "params": {
        "date_range_days": "{days}",
        "group_by": "day"
      },
      "step": 1,
      "tool": "query_return_stats_nl2sql"
    }
  ]
}
```
