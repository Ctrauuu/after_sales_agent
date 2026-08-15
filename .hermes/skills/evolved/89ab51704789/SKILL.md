---
name: return-order-reason-analysis
description: 复用 query_return_stats_nl2sql 按品类、原因、日趋势三个维度分析指定品类在指定时间窗口内的退单原因，并输出结构化结论与数据局限提示。适用于任意品类和任意近 N 天退单分析。
---

# return-order-reason-analysis

复用 query_return_stats_nl2sql 按品类、原因、日趋势三个维度分析指定品类在指定时间窗口内的退单原因，并输出结构化结论与数据局限提示。适用于任意品类和任意近 N 天退单分析。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-15T13:45:36.838831+00:00",
  "description": "复用 query_return_stats_nl2sql 按品类、原因、日趋势三个维度分析指定品类在指定时间窗口内的退单原因，并输出结构化结论与数据局限提示。适用于任意品类和任意近 N 天退单分析。",
  "name": "return-order-reason-analysis",
  "output_template": "按工作流完成实时查询（4 个维度均已实际调用 `query_return_stats_nl2sql`）： ## {category} / 最近 {date_range_days} 天退单分析 **① 按品类构成** {{result_category}} **② 按退单原因** {{result_reason}} **③ 按日趋势（{category}）** {{result_trend}} **④ 全品类对照趋势（近 {date_range_days} 天）** {{result_all_trend}} --- ### 结论 1. **原因**：基于 {{result_reason}} 中的原因占比给出结构性描述；若样本量过小，需提示无统计意义，不建议据此启动质量溯源或赔付决策。 2. **趋势**：基于 {{result_trend}} 和 {{result_all_trend}} 判断是否存在尖峰或波动，并给出该品类在全部退单中的占比。 3. **数据覆盖提示**：核对返回日期跨度是否满足 {date_range_days} 天窗口；若实际覆盖天数不足，需说明为数据源未同步覆盖，并明确结论仅限定性参考。",
  "required_mcp_tools": [
    "query_return_stats_nl2sql"
  ],
  "skill_id": "89ab51704789",
  "trigger_patterns": [
    "分析{category}最近{date_range_days}天退单原因",
    "按品类、原因和趋势分析退单",
    "查询{category}退单统计",
    "退货原因分析",
    "分析最近{days}天{category}退单原因",
    "退货原因分析 按品类/原因/趋势",
    "请分析{category}最近{days}天退单结构",
    "退货趋势分析 {category} {days}天",
    "请分析最近{date_range_days}天{category}退单原因",
    "分析{category}退货原因、品类和趋势",
    "{category}退单原因分析",
    "最近{date_range_days}天退货原因趋势",
    "退货/退单多维度分析",
    "请分析最近30天电视退单原因，按品类、原因和趋势给出结论。"
  ],
  "updated_at": "2026-08-15T13:54:41.089036+00:00",
  "usage_count": 3,
  "version": 3,
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
      "output_key": "result_all_trend",
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
