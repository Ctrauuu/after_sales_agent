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
  "output_template": "## 近{date_range_days}天退单综合分析（实时查询） ### ① 按日趋势 {trend_result} ### ② 按品类构成 {category_result} ### ③ 按退单原因 {reason_result} ### ④ 退款金额 {refund_amount_result} ### 结论 1. 趋势：根据按日趋势识别峰值日与走势形态。 2. 品类：根据品类构成识别主导品类及其占比。 3. 原因：根据原因分布识别首要原因及原因簇，结合品类进行归因。 4. 数据覆盖：注明可见数据窗口，避免外推。 ### 图表建议 根据实时返回数据生成趋势图、饼图、条形图。",
  "required_mcp_tools": [
    "query_return_stats_nl2sql",
    "query_aftersale_nl2sql"
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
    "分析一下退单原因"
  ],
  "updated_at": "2026-08-25T21:54:24.387992+08:00",
  "usage_count": 5,
  "version": 5,
  "workflow": [
    {
      "output_key": "trend_result",
      "params": {
        "date_range_days": "{date_range_days}",
        "group_by": "day"
      },
      "step": 1,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "category_result",
      "params": {
        "date_range_days": "{date_range_days}",
        "group_by": "category"
      },
      "step": 2,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "reason_result",
      "params": {
        "date_range_days": "{date_range_days}",
        "group_by": "reason"
      },
      "step": 3,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "refund_amount_result",
      "params": {
        "question": "近{date_range_days}天退单的退款总金额是多少"
      },
      "step": 4,
      "tool": "query_aftersale_nl2sql"
    }
  ]
}
```
