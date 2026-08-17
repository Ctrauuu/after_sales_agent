---
name: return_reason_analysis
description: 按品类、原因和日趋势分析指定时间窗口内商品退单/退货情况，并与全品类基线对照，判断是否存在质量或趋势风险
---

# return_reason_analysis

按品类、原因和日趋势分析指定时间窗口内商品退单/退货情况，并与全品类基线对照，判断是否存在质量或趋势风险

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-15T13:56:22.724388+00:00",
  "description": "按品类、原因和日趋势分析指定时间窗口内商品退单/退货情况，并与全品类基线对照，判断是否存在质量或趋势风险",
  "name": "return_reason_analysis",
  "output_template": "## {category} / 最近 {date_range_days} 天退货分析 ### ① 按品类构成 {按品类构成表格} ### ② 按退货原因 {按退货原因表格} ### ③ 按日趋势（{category}） {按日趋势表格} ### ④ 全品类对照趋势 {全品类对照趋势表格} ### 结论 1. **原因**：{主要退货原因及占比/原因簇分析} 2. **趋势**：{峰值日及与全品类峰值重叠情况} 3. **数据覆盖提示**：{实际覆盖日期/缺失日期/字段脱敏等说明}",
  "required_mcp_tools": [
    "query_return_stats_nl2sql"
  ],
  "skill_id": "621ba6dc5b05",
  "trigger_patterns": [
    "分析最近{date_range_days}天{category}退单原因",
    "分析{category}退货/退单原因，按品类、原因和趋势",
    "{category}退单归因分析",
    "为什么{category}最近退货多",
    "请分析最近30天电视退单原因，按品类、原因和趋势给出结论。",
    "{category}近{date_range_days}天退货原因构成",
    "分析{category}退货原因和按日趋势",
    "统计{category}退货情况",
    "{category}退货按日变化趋势",
    "请统计电视近一个月退货原因构成，并分析按日变化趋势。",
    "查最近{days}天{category}退单情况",
    "分析{category}近{days}天退货",
    "{category}退货原因分析",
    "最近{days}天{category}退货趋势",
    "查最近7天空调退单情况@Hermes\\_agent",
    "查最近{date_range_days}天{category}退单情况",
    "{category}退单分析",
    "{category}退货原因与趋势",
    "最近{date_range_days}天退货趋势"
  ],
  "updated_at": "2026-08-16T08:31:54.195645+00:00",
  "usage_count": 4,
  "version": 4,
  "workflow": [
    {
      "output_key": "result_reason",
      "params": {
        "category": "{category}",
        "date_range_days": "{date_range_days}",
        "group_by": "reason"
      },
      "step": 1,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "result_category",
      "params": {
        "category": "{category}",
        "date_range_days": "{date_range_days}",
        "group_by": "category"
      },
      "step": 2,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "result_trend",
      "params": {
        "category": "{category}",
        "date_range_days": "{date_range_days}",
        "group_by": "day"
      },
      "step": 3,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "result_all_category_trend",
      "params": {
        "date_range_days": "{date_range_days}",
        "group_by": "day"
      },
      "step": 4,
      "tool": "query_return_stats_nl2sql"
    }
  ]
}
```
