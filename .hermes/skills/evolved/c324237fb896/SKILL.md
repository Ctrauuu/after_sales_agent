---
name: query_return_order_count_by_date_range
description: 查询最近指定天数内的退单量，按天汇总退单数、计算合计与峰值日期，用于售后退单趋势的快速分析。
---

# query_return_order_count_by_date_range

查询最近指定天数内的退单量，按天汇总退单数、计算合计与峰值日期，用于售后退单趋势的快速分析。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-25T12:25:58.861270+00:00",
  "description": "查询最近指定天数内的退单量，按天汇总退单数、计算合计与峰值日期，用于售后退单趋势的快速分析。",
  "enabled": true,
  "name": "query_return_order_count_by_date_range",
  "output_template": "近 {date_range_days} 天退单量（按{group_by}聚合）： {return_stats} 合计：{total} 单 峰值日期：{peak_date}（{peak_value} 单，占比 {peak_share}） ⚠️ 数据范围说明：若结果受当前账号 RBAC 权限收窄，需明确提示实际可访问范围，不得将可见数据描述为全国/全局口径。 如需按退单原因下钻，或更换时间窗口/聚合维度，可继续查询。",
  "required_mcp_tools": [
    "query_return_stats_nl2sql"
  ],
  "skill_id": "c324237fb896",
  "trigger_patterns": [
    "最近{days}天内退单量",
    "近{days}天退单量",
    "查询近{days}天退货量",
    "{days}天内的退单量",
    "最近7天内退单量",
    "查询近{days}天退单量",
    "查询近{days}天退单统计",
    "{days}天内退单量趋势",
    "查询近7天退单量",
    "退单原因是什么",
    "退货原因分布",
    "退款原因分析",
    "为什么退单",
    "原因呢",
    "近{time_window_days}天退单量",
    "近{time_window_days}天退单趋势",
    "按日统计退单量",
    "查一下最近{time_window_days}天的退货/退款量",
    "近7天退单量",
    "退单原因",
    "退单原因分析",
    "退单量",
    "退单统计",
    "全国退单量",
    "按原因查退单",
    "退货数据",
    "最近{天数}天退单量",
    "退单分析",
    "哪个品类最多",
    "近{number}天退单量",
    "退单{维度}分布",
    "退单量排名",
    "退单最多的{dimension}",
    "退货量",
    "退单趋势",
    "退货趋势",
    "近30天退单量"
  ],
  "updated_at": "2026-08-28T07:30:28.718591+00:00",
  "usage_count": 9,
  "version": 9,
  "workflow": [
    {
      "output_key": "return_stats",
      "params": {
        "date_range_days": "{date_range_days}",
        "group_by": "{group_by}"
      },
      "step": 1,
      "tool": "query_return_stats_nl2sql"
    }
  ]
}
```
