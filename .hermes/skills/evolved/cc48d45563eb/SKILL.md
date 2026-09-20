---
name: category_returns_top_ranking
description: 当用户询问某时间窗口内退单量/订单量最多的品类时，按品类分组统计并返回排名，提炼TOP品类、占比及关键结论。可替换参数：时间窗口、分组维度。
---

# category_returns_top_ranking

当用户询问某时间窗口内退单量/订单量最多的品类时，按品类分组统计并返回排名，提炼TOP品类、占比及关键结论。可替换参数：时间窗口、分组维度。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-27T15:05:42.171110+00:00",
  "description": "当用户询问某时间窗口内退单量/订单量最多的品类时，按品类分组统计并返回排名，提炼TOP品类、占比及关键结论。可替换参数：时间窗口、分组维度。",
  "enabled": true,
  "name": "category_returns_top_ranking",
  "output_template": "近 {date_range_days} 天退单量最多的品类（实时查询，合计 {total_orders} 单）： | 品类 | 退单量 | 占比 | |---|---:|---:| {rows} 要点： - 🥇 单品类最多：{top_category}（{top_count} 单，{top_percentage}） - 可结合品类群（如空调类、大家电、3C）补充结构占比结论。 - 数据覆盖说明：{coverage_note}",
  "required_mcp_tools": [
    "query_return_stats_nl2sql"
  ],
  "skill_id": "cc48d45563eb",
  "trigger_patterns": [
    "哪个品类最多",
    "退单量最多的品类",
    "哪类商品退单最多",
    "按品类统计退单量",
    "品类退单排行",
    "top category by returns"
  ],
  "updated_at": "2026-08-27T15:05:42.171110+00:00",
  "usage_count": 1,
  "version": 1,
  "workflow": [
    {
      "output_key": "category_stats_result",
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
