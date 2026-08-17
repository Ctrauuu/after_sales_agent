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
  "name": "aftersales_return_cost_estimation_by_reason",
  "output_template": "## 成本/金额估算节点摘要（{category} · {time_window}） **数据依据**：调用 query_aftersale_nl2sql 实时查询，按退单原因分组聚合退款金额，返回 {row_count} 行（金额脱敏状态：{is_masked}）。 **结论**： - 退款总金额 {total_amount} 元；其中 {zero_amount_count} 单退款金额为 0 元，未计入总额。 - 各原因金额：{reason_amount_summary}。 - 按原因簇汇总：{cluster_summary}。 **局限**： - {limitations} - 未返回数据时明确写“未返回数据”。",
  "required_mcp_tools": [
    "tool_describe",
    "query_aftersale_nl2sql"
  ],
  "skill_id": "5a323eeb5a84",
  "trigger_patterns": [
    "估算最近{time_window}{category}的退单金额/成本",
    "按退单原因统计退款金额",
    "cost_estimation节点需要计算退款成本",
    "分析退单退款总额和平均每单金额",
    "你是售后分析子 Agent，专长是 cost_estimation。 用户原始问题：分析最近30天【空调】品类的退单情况，必须给出三个维度：(1) 按退单原因分组的原因分布，(2) 按子品类分组的品类分布，(3) 按日期的每日退单趋势。先完成原因和品类统计，再汇总趋势，最后综合结论。只使用实时查询返回的数据，不要编造或补零。 时间窗口：最近 30 天。 本节点目标：估算最近 30 天退单相关金额或成本；没有数据时明确说明。 前置任务压缩摘要： - 无前置依赖，请自行查询。 只使用已授权的苏宁业务工具查询事实；没有返回数据时明确写‘未返回数据’，绝不编造。除 chart_gen 只能使用前置摘要"
  ],
  "updated_at": "2026-08-15T15:35:16.535364+00:00",
  "usage_count": 1,
  "version": 1,
  "workflow": [
    {
      "output_key": "tool_schema_1",
      "params": {
        "name": "query_aftersale_nl2sql"
      },
      "step": 1,
      "tool": "tool_describe"
    },
    {
      "output_key": "tool_schema_2",
      "params": {
        "name": "query_return_stats_nl2sql"
      },
      "step": 2,
      "tool": "tool_describe"
    },
    {
      "output_key": "query_result",
      "params": {
        "question": "统计最近{time_window}{category}品类的退单退款总金额和平均每单退款金额，并按退单原因分组给出各原因的退款金额；如果金额字段脱敏或无法统计，请明确说明"
      },
      "step": 3,
      "tool": "query_aftersale_nl2sql"
    }
  ]
}
```
