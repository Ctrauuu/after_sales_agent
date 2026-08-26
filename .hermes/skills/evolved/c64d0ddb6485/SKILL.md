---
name: query_top_sku_by_return_rate
description: 当用户需要查询退单率/退货率最高的指定数量 SKU 时，使用专用工具 query_sku_return_rate 按时间窗口计算并返回排名，避免走 NL2SQL 或品类维度。
---

# query_top_sku_by_return_rate

当用户需要查询退单率/退货率最高的指定数量 SKU 时，使用专用工具 query_sku_return_rate 按时间窗口计算并返回排名，避免走 NL2SQL 或品类维度。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-25T13:31:59.891845+00:00",
  "description": "当用户需要查询退单率/退货率最高的指定数量 SKU 时，使用专用工具 query_sku_return_rate 按时间窗口计算并返回排名，避免走 NL2SQL 或品类维度。",
  "enabled": true,
  "name": "query_top_sku_by_return_rate",
  "output_template": "## 近 {date_range_days} 天退单率最高的 {limit} 个 SKU（实时查询） {result_1} **注意**：若样本量过小，退单率可能无统计意义，建议扩大时间窗口或设置订单量阈值后复核。",
  "required_mcp_tools": [
    "query_sku_return_rate"
  ],
  "skill_id": "c64d0ddb6485",
  "trigger_patterns": [
    "退单率最高的{limit}个SKU",
    "退货率最高的{limit}个SKU",
    "top {limit} SKUs by return rate",
    "查询近{date_range_days}天退单率最高的SKU",
    "退单率最高的5个SKU"
  ],
  "updated_at": "2026-08-25T13:31:59.891845+00:00",
  "usage_count": 1,
  "version": 1,
  "workflow": [
    {
      "output_key": "result_1",
      "params": {
        "date_range_days": "{date_range_days}",
        "limit": "{limit}"
      },
      "step": 1,
      "tool": "query_sku_return_rate"
    }
  ]
}
```
