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
  "output_template": "## {category} / 最近 {date_range_days} 天退单分析如下 ### ① 按品类构成 {category_stats} ### ② 按退单原因 {reason_stats} ### ③ 按日趋势（{category}） {trend_stats} ### 全品类对照趋势 {baseline_trend_stats} ### 结论 1. **原因**：{category}近{date_range_days}天退单 {total_orders} 单，主要原因为 {top_reason}，占比 {top_reason_ratio}。若样本量过小（如 <5 单），仅作个案线索留存，不建议据此启动质量溯源或供应商赔付决策。 2. **趋势**：{category}按日趋势 {trend_conclusion}，全品类对照峰值 {baseline_peak_date}（{baseline_peak_value} 单/日），{category}当日为 {category_value_on_peak} 单，与全品类波动 {overlap_conclusion}。 3. **数据覆盖提示**：{data_coverage_note}；金额字段如脱敏，结论仅限定性参考，不宜外推为完整周期结论。",
  "required_mcp_tools": [
    "query_return_stats_nl2sql"
  ],
  "skill_id": "621ba6dc5b05",
  "trigger_patterns": [
    "分析最近{date_range_days}天{category}退单原因",
    "分析{category}退货/退单原因，按品类、原因和趋势",
    "{category}退单归因分析",
    "为什么{category}最近退货多",
    "请分析最近30天电视退单原因，按品类、原因和趋势给出结论。"
  ],
  "updated_at": "2026-08-15T13:56:22.724388+00:00",
  "usage_count": 1,
  "version": 1,
  "workflow": [
    {
      "output_key": "reason_stats",
      "params": {
        "category": "{category}",
        "date_range_days": "{date_range_days}",
        "group_by": "reason"
      },
      "step": 1,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "category_stats",
      "params": {
        "category": "{category}",
        "date_range_days": "{date_range_days}",
        "group_by": "category"
      },
      "step": 2,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "trend_stats",
      "params": {
        "category": "{category}",
        "date_range_days": "{date_range_days}",
        "group_by": "day"
      },
      "step": 3,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "baseline_trend_stats",
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
