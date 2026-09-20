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
  "enabled": true,
  "name": "aftersale_return_reason_trend_analysis",
  "output_template": "## 近 {date_range_days} 天各品类退单量（实时查询） {result_1} **要点**： - 按退单量从高到低识别重点品类； - 计算各品类占比； - 如存在父类聚合（例如空调类），可合并说明； - 注明数据范围（RBAC 权限、实际时间窗口、金额字段是否脱敏）。 需要按品类继续下钻原因吗？",
  "required_mcp_tools": [
    "query_return_stats_nl2sql"
  ],
  "skill_id": "ed99e939d0c3",
  "trigger_patterns": [
    "分析最近{date_range_days}天{product_category}退单原因",
    "按品类、原因和趋势分析{product_category}退单/退货情况",
    "分析{product_category}退货原因及趋势",
    "哪个品类退单多",
    "退单集中在哪个品类",
    "按品类分析退单量",
    "各品类退单量/占比",
    "近7天退单品类分布",
    "哪个品类"
  ],
  "updated_at": "2026-08-28T07:32:44.431570+00:00",
  "usage_count": 2,
  "version": 2,
  "workflow": [
    {
      "output_key": "result_1",
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
