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
  "enabled": true,
  "name": "return-order-reason-analysis",
  "output_template": "## {category}退单原因分布（实时查询 · 近{date_range_days}天） | 原因码 | 原因 | 退单量 | 占比 | |---|---|---|---| {reason_rows} | **合计** | | **{total_returns}** | 100% | **结论** 1. **首要原因**：**{top_reason_code} {top_reason_name}（{top_reason_count} 单，{top_reason_percent}）**；若存在同簇原因，合并后指出最大原因簇及占比。 2. **次因**：{secondary_reasons}。 3. 其他原因：{other_reasons}。 **提示**：{date_range_days} 天窗口实际覆盖 {actual_date_range}，样本 {total_returns} 单，占比为定性参考；{extra_notes}。",
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
    "请分析最近30天电视退单原因，按品类、原因和趋势给出结论。",
    "{category} 最近 {date_range_days} 天退单数量和原因怎么样",
    "{category}退货趋势分析",
    "最近 {date_range_days} 天退单情况",
    "最近电视退单数量和原因怎么样",
    "分析最近{date_range_days}天{category}退单原因",
    "{category}近{date_range_days}天退单分析",
    "{category}退单原因及趋势分析",
    "分析最近30天空调退单原因",
    "帮我看看最近的情况",
    "分析一下最近的售后情况",
    "最近退单情况怎么样",
    "售后退单分析",
    "请分析最近{date_range_days}天{category}退单原因，按原因、品类和趋势给出结论",
    "分析{category}最近{date_range_days}天的退货/退单原因分布、品类分布和趋势",
    "按原因、品类和趋势分析{category}退单情况",
    "请分析最近30天空调退单原因，按原因、品类和趋势给出结论。@Hermes\\_agent",
    "统计{category}近{date_range_days}天退货原因构成",
    "分析{category}退货按日变化趋势",
    "{category}近一个月退货原因及趋势分析",
    "退货原因构成并分析按日趋势",
    "请统计电视近一个月退货原因构成，并分析按日变化趋势。",
    "帮我分析近{date_range_days}天{category}退单原因",
    "分析{category}退货原因及占比",
    "按原因统计退单量",
    "退货原因多维分析",
    "帮我分析近7天空调退单原因，按原因分组输出退单量及占比。",
    "分析近{days}天退单趋势、主要品类、原因和退款金额",
    "退单分析",
    "退货/退款多维统计",
    "按日/品类/原因分析退单数据",
    "请分析近30天退单趋势、主要品类、原因和退款金额，并给出图表建议。",
    "{category}退单原因分布",
    "{category}的退单原因",
    "查询近{date_range_days}天{category}退单原因分布",
    "退单原因分布是什么",
    "按原因统计{category}退单",
    "空调退单原因分布"
  ],
  "updated_at": "2026-08-25T13:07:56.851959+00:00",
  "usage_count": 11,
  "version": 11,
  "workflow": [
    {
      "output_key": "return_stats",
      "params": {
        "category": "{category}",
        "date_range_days": "{date_range_days}",
        "group_by": "reason"
      },
      "step": 1,
      "tool": "query_return_stats_nl2sql"
    }
  ]
}
```
