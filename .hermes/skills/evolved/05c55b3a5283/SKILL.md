---
name: query_product_info_by_sku
description: 当用户要求查询某个 SKU 的商品信息（名称/品牌/品类/价格/保修/批次等）时，调用 get_product_info 获取商品详情，并在回答中说明该操作所属场景及所用工具。
---

# query_product_info_by_sku

当用户要求查询某个 SKU 的商品信息（名称/品牌/品类/价格/保修/批次等）时，调用 get_product_info 获取商品详情，并在回答中说明该操作所属场景及所用工具。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-22T11:09:53.192335+00:00",
  "description": "当用户要求查询某个 SKU 的商品信息（名称/品牌/品类/价格/保修/批次等）时，调用 get_product_info 获取商品详情，并在回答中说明该操作所属场景及所用工具。",
  "enabled": true,
  "name": "query_product_info_by_sku",
  "output_template": "## {sku_code} 商品信息 **{product_name} · {brand} {model}** | 字段 | 内容 | |---|---| | 商品名 | {product_name} | | 品牌 | {brand} | | 品类 | {category} | | 价格 | {price} | | 服务政策 | {service_policy} | | 整机保修 | {whole_machine_warranty} | | 部件保修 | {parts_warranty} | | 常见故障 | {common_faults} | | 生产批次 | {production_batch} | --- ### 场景与工具说明 - **场景**：**商品查询（product_query）**——用户请求查询 SKU 对应的商品、品牌、品类、保修与批次信息。 - **使用工具**：**`get_product_info`**（suning_business 工具集）——按 `sku_code` 查询商品详情，返回商品基础信息（名称/品牌/品类/价格）、整机与部件保修、常见故障统计和生产批次。",
  "required_mcp_tools": [
    "get_product_info"
  ],
  "skill_id": "05c55b3a5283",
  "trigger_patterns": [
    "查询 {sku_code} 的商品信息",
    "查一下 SKU {sku_code} 的商品信息",
    "查询一个随机 SKU 的商品信息",
    "商品信息查询",
    "查询一个随机 SKU 的商品信息，回答完需要告诉我这是什么场景 用了什么工具",
    "查询 SKU 的商品信息",
    "查一下这个 SKU 的商品详情",
    "查询某个SKU的信息并说明用了什么工具",
    "帮我看看这个SKU是什么商品",
    "查询一个随机 SKU 的商品信息@Hermes_agent 回答完需要告诉我这是什么场景 用了什么工具"
  ],
  "updated_at": "2026-08-25T21:54:22.663687+08:00",
  "usage_count": 2,
  "version": 2,
  "workflow": [
    {
      "output_key": "product_info",
      "params": {
        "sku_code": "{sku_code}"
      },
      "step": 1,
      "tool": "get_product_info"
    }
  ]
}
```
