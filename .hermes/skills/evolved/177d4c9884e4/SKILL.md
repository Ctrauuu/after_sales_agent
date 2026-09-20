---
name: return_refund_multi_dimension_analysis
description: 当用户要求分析退单/退货的趋势、主要品类、原因和退款金额并给出图表建议时，自动并联查询退单统计与售后退款明细，生成多维分析结论。
---

# return_refund_multi_dimension_analysis

当用户要求分析退单/退货的趋势、主要品类、原因和退款金额并给出图表建议时，自动并联查询退单统计与售后退款明细，生成多维分析结论。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-16T08:41:22.620073+00:00",
  "description": "当用户要求分析退单/退货的趋势、主要品类、原因和退款金额并给出图表建议时，自动并联查询退单统计与售后退款明细，生成多维分析结论。",
  "enabled": true,
  "name": "return_refund_multi_dimension_analysis",
  "output_template": "## 近 {date_range_days} 天{category}品类退单原因分布（实时查询 · 合计 {total_orders} 单） | 原因编码 | 原因 | 退单量 | 占比 | |---|---|---:|---:| {reason_rows} **原因归因要点**： 1. **首要原因**：{primary_reason}（{primary_count} 单，{primary_pct}%） 2. **主要原因簇**：{cluster_analysis} 3. **其他**：{other_analysis} **结论**：{conclusion} > 数据口径：{data_scope_note}",
  "required_mcp_tools": [
    "query_return_stats_nl2sql"
  ],
  "skill_id": "177d4c9884e4",
  "trigger_patterns": [
    "分析近{days}天退单趋势、主要品类、原因和退款金额",
    "退货退款多维分析",
    "请分析退货趋势、品类、原因和退款金额并给出图表建议",
    "退单原因和品类分布分析",
    "请分析近30天退单趋势、主要品类、原因和退款金额，并给出图表建议。",
    "分析近{date_range_days}天退单趋势、主要品类、原因和退款金额，并给出图表建议",
    "近{date_range_days}天退货退款分析",
    "退单原因/品类/趋势分析",
    "returns/refunds analysis",
    "请对近{date_range_days}天售后退单进行复杂综合分析",
    "分析近{date_range_days}天退单趋势、品类分布、主要原因和退款金额",
    "售后退单多维综合分析并给出图表建议",
    "请对近30天售后退单进行复杂综合分析：分别分析退单趋势、品类分布、主要原因和退款金额，最后综合汇总并给出图表建议。",
    "近{date_range_days}天售后退单分析",
    "售后退单综合分析",
    "退货退款多维度分析",
    "退单趋势/品类/原因/金额分析",
    "多 Agent 分析售后数据",
    "请对近30天售后退单进行复杂综合分析：分别分析退单趋势、品类分布、主要原因和退款金额，最后综合汇总并给出图表建议。请使用多 Agent 的方式。",
    "退单原因",
    "为什么退单",
    "退货原因分析",
    "退单原因呢",
    "分析一下退单原因",
    "{品类}退单原因是什么",
    "为什么{品类}退单多",
    "{品类}退单量按原因分布",
    "原因呢（在退单分析上下文中）",
    "原因呢"
  ],
  "updated_at": "2026-08-28T07:33:56.068213+00:00",
  "usage_count": 6,
  "version": 6,
  "workflow": [
    {
      "output_key": "reason_distribution_result",
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
