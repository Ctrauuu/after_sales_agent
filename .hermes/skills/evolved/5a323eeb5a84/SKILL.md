---
name: aftersales_return_cost_estimation_by_reason
description: 用于在售后分析流程中估算指定品类/时间窗口的退单退款金额或成本。先检查授权NL2SQL查询工具能力，再调用最匹配的查询工具，按退单原因分组聚合退款金额，并给出总额、原因簇占比和局限说明。
---

# aftersales_return_cost_estimation_by_reason

用于在售后分析流程中估算指定品类/时间窗口的退单退款金额或成本。先检查授权NL2SQL查询工具能力，再调用最匹配的查询工具，按退单原因分组聚合退款金额，并给出总额、原因簇占比和局限说明。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-15T15:35:16.535364+00:00",
  "description": "用于在售后分析流程中估算指定品类/时间窗口的退单退款金额或成本。先检查授权NL2SQL查询工具能力，再调用最匹配的查询工具，按退单原因分组聚合退款金额，并给出总额、原因簇占比和局限说明。",
  "enabled": true,
  "name": "aftersales_return_cost_estimation_by_reason",
  "output_template": "## 近 {date_range_days} 天退单品类分布（实时查询） **退单最多的品类：{top_category}（{top_count} 单，占 {top_percentage}%）** | 品类 | 退单量 | 占比 | |---|---:|---:| {category_rows} - **{top_category}居首**（{top_count} 单），如需归因细节可继续下钻。",
  "required_mcp_tools": [
    "query_return_stats_nl2sql"
  ],
  "skill_id": "5a323eeb5a84",
  "trigger_patterns": [
    "估算最近{time_window}{category}的退单金额/成本",
    "按退单原因统计退款金额",
    "cost_estimation节点需要计算退款成本",
    "分析退单退款总额和平均每单金额",
    "你是售后分析子 Agent，专长是 cost_estimation。 用户原始问题：分析最近30天【空调】品类的退单情况，必须给出三个维度：(1) 按退单原因分组的原因分布，(2) 按子品类分组的品类分布，(3) 按日期的每日退单趋势。先完成原因和品类统计，再汇总趋势，最后综合结论。只使用实时查询返回的数据，不要编造或补零。 时间窗口：最近 30 天。 本节点目标：估算最近 30 天退单相关金额或成本；没有数据时明确说明。 前置任务压缩摘要： - 无前置依赖，请自行查询。 只使用已授权的苏宁业务工具查询事实；没有返回数据时明确写‘未返回数据’，绝不编造。除 chart_gen 只能使用前置摘要",
    "哪个品类最多",
    "最近{n}天退单最多的品类",
    "按品类统计退单量",
    "退单品类分布",
    "最近{n}天内哪个品类退单量最大"
  ],
  "updated_at": "2026-08-25T13:06:52.939704+00:00",
  "usage_count": 2,
  "version": 2,
  "workflow": [
    {
      "output_key": "stats_result",
      "params": {
        "date_range_days": "{date_range_days}",
        "group_by": "category"
      },
      "step": 1,
      "tool": "query_return_stats_nl2sql"
    }
  ]
}
```
