---
name: aftersale_return_reason_trend_analysis
description: 可复用的售后退单/退货分析 Skill：针对指定品类和时间范围，通过编排 MCP 并行完成品类维度、原因维度、趋势维度的实时数据查询，并汇总输出带数据边界的结构化结论。
---

# aftersale_return_reason_trend_analysis

可复用的售后退单/退货分析 Skill：针对指定品类和时间范围，通过编排 MCP 并行完成品类维度、原因维度、趋势维度的实时数据查询，并汇总输出带数据边界的结构化结论。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-15T13:50:36.911903+00:00",
  "description": "可复用的售后退单/退货分析 Skill：针对指定品类和时间范围，通过编排 MCP 并行完成品类维度、原因维度、趋势维度的实时数据查询，并汇总输出带数据边界的结构化结论。",
  "name": "aftersale_return_reason_trend_analysis",
  "output_template": "已基于实时查询完成最近{date_range_days}天{product_category}退单分析（多 Agent DAG 子任务执行完成，无缺失维度）。结论如下： {analysis_result}",
  "required_mcp_tools": [
    "orchestrate_aftersale_analysis"
  ],
  "skill_id": "ed99e939d0c3",
  "trigger_patterns": [
    "分析最近{date_range_days}天{product_category}退单原因",
    "按品类、原因和趋势分析{product_category}退单/退货情况",
    "分析{product_category}退货原因及趋势"
  ],
  "updated_at": "2026-08-15T13:50:36.911903+00:00",
  "usage_count": 1,
  "version": 1,
  "workflow": [
    {
      "output_key": "analysis_result",
      "params": {
        "date_range_days": "{date_range_days}",
        "query": "分析最近{date_range_days}天{product_category}品类的退单情况：并行完成三个维度——(1) 品类维度：{product_category}品类及子品类的退单数量与金额；(2) 原因维度：退单原因分布及占比；(3) 趋势维度：按日退单数量趋势与峰值。全部完成后汇总，用实时查询数据作答，不得编造或补零。"
      },
      "step": 1,
      "tool": "orchestrate_aftersale_analysis"
    }
  ]
}
```
