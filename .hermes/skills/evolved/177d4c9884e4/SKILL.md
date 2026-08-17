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
  "name": "return_refund_multi_dimension_analysis",
  "output_template": "## 近{date_range_days}天退单分析 ### ① 主要品类构成（退单量 + 退款金额） | 品类 | 退单量 | 退款金额（元） | |---|---:|---:| {category_amount_table} ### ② 按退单原因 {reason_stats_table} ### ③ 按日趋势 {daily_trend_table} ### 结论 1. **原因**：首要原因为 {top_reason}（{top_reason_count} 单，占比 {top_reason_rate}）；相关原因簇合计 {related_reason_count} 单（占比 {related_reason_rate}）。样本量偏小时仅作参考，不建议据此单独启动赔付决策。 2. **趋势**：{peak_date} 为单日峰值（{peak_count} 单），其余日期均值约 {avg_count} 单，若次日明显回落则为一次性脉冲，建议排查该日事件背景（如促销/集中发货/安装高峰）。 3. **品类与金额**：{top_category} 为退单量与金额双第一（{top_category_count} 单 / {top_category_amount} 元）；{top_category_group} 合计 {top_category_group_amount} 元，占退款总额 {top_category_group_rate}，归因指向相关环节。 4. **数据覆盖提示**：实际可按日窗口为 {data_start}~{data_end} 共 {actual_days} 天，其余日期未返回记录时结论仅限定性参考，不宜外推为完整 {date_range_days} 天周期结论。 ### 图表建议 | # | 图表 | 类型 | 数据 | |---|---|---|---| | 1 | 按日退单趋势 | 折线图/柱状图 | {daily_trend_chart_data}，凸显峰值 | | 2 | 品类退单量构成 | 横向条形图/饼图 | {category_chart_data} | | 3 | 退单原因分布 | 饼图/条形图 | {reason_chart_data} | | 4 | 品类退单量 vs 退款金额 | 双轴图/表格 | {category_amount_chart_data} |",
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
    "returns/refunds analysis"
  ],
  "updated_at": "2026-08-16T08:42:37.070053+00:00",
  "usage_count": 2,
  "version": 2,
  "workflow": [
    {
      "output_key": "reason_stats",
      "params": {
        "date_range_days": "{date_range_days}",
        "group_by": "reason"
      },
      "step": 1,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "category_stats",
      "params": {
        "date_range_days": "{date_range_days}",
        "group_by": "category"
      },
      "step": 2,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "daily_trend",
      "params": {
        "date_range_days": "{date_range_days}",
        "group_by": "day"
      },
      "step": 3,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "category_amount_stats",
      "params": {
        "question": "近{date_range_days}天退单按品类统计退单数量和退款总金额"
      },
      "step": 4,
      "tool": "query_aftersale_nl2sql"
    }
  ]
}
```
