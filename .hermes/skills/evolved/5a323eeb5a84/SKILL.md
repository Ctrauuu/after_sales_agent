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
  "output_template": "## 近 {date_range_days} 天{category}退单量（实时查询 · 合计 {total} 单） | 日期 | 退单量 | |------|-------:| {row_lines} | **合计** | **{total}** | **要点**： - 近 {date_range_days} 天窗口内{category}退单 **{total} 单**，占全部品类退单的 **{proportion}**，是退单最集中的品类； - 单日峰值在 **{peak_date}（{peak_value} 单）**； - {category}退单主要原因集中在 **{main_reasons}** 等问题； - **数据范围**：受当前账号 RBAC 权限收窄（可访问区域 {region}，城市 {cities}），**非全量口径**；实际返回 {actual_days} 天记录，其余天数无返回数据。金额字段为脱敏（***）。 可继续按**原因**下钻（如“{category}退单的原因分布”）或拉长窗口（{suggested_longer_window} 天）查看趋势。",
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
    "最近{n}天内哪个品类退单量最大",
    "{category}退单量",
    "{category}退货量",
    "近{date_range_days}天{category}退单量",
    "退单量按天统计",
    "冰箱退单量",
    "{category}退单趋势",
    "退单量统计",
    "退单量按天/按周/按月看",
    "空调退单量"
  ],
  "updated_at": "2026-08-28T07:31:36.009580+00:00",
  "usage_count": 4,
  "version": 4,
  "workflow": [
    {
      "output_key": "result_1",
      "params": {
        "category": "{category}",
        "date_range_days": "{date_range_days}",
        "group_by": "{group_by}"
      },
      "step": 1,
      "tool": "query_return_stats_nl2sql"
    }
  ]
}
```
