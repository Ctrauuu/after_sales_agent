"""NL2SQL 使用的数据库结构、关联关系和生成提示词。"""

from __future__ import annotations

import json

from mcp_suning.domain_registry import CATEGORY_ALIASES


SCHEMA_REGISTRY = {
    "t_aftersale_return": {
        "alias": "r",
        "description": "售后退单事实表，退单统计必须从此表开始",
        "columns": {
            "return_id": "退单ID，主键",
            "order_id": "订单ID，关联 t_order_main.order_id",
            "user_id": "用户ID，敏感字段",
            "sku_code": "SKU编码，关联 t_product_sku.sku_code",
            "return_type": "refund/replace/repair",
            "return_reason_code": "退单原因编码",
            "return_reason_desc": "退单原因说明",
            "return_status": "0待审核、1通过、2驳回、3完成",
            "return_amount": "退单金额，单位为分",
            "create_time": "退单申请时间，Unix秒",
            "update_time": "退单更新时间，Unix秒",
        },
    },
    "t_order_main": {
        "alias": "o",
        "description": "订单主表，提供渠道、区域和城市",
        "columns": {
            "order_id": "订单ID，主键",
            "channel": "online/offline/phone",
            "region_code": "区域编码，例如HD",
            "city_code": "城市编码，例如NJ",
            "order_amount": "订单金额，单位为分",
            "create_time": "下单时间，Unix秒",
            "order_status": "订单状态",
        },
    },
    "t_product_sku": {
        "alias": "s",
        "description": "SKU主数据，连接退单、品牌和品类",
        "columns": {
            "sku_code": "SKU编码，主键",
            "product_name": "商品名称",
            "brand_id": "品牌ID",
            "category_l3_code": "三级品类编码",
            "price": "商品价格，单位为分",
        },
    },
    "t_product_category": {
        "alias": "c",
        "description": "商品品类表",
        "columns": {
            "category_code": "品类编码，主键",
            "category_name": "品类中文名称",
            "parent_code": "父级品类编码",
            "level": "品类层级",
        },
    },
    "t_product_brand": {
        "alias": "b",
        "description": "商品品牌表",
        "columns": {
            "brand_id": "品牌ID，主键",
            "brand_name": "品牌名称",
            "service_policy": "品牌售后政策",
        },
    },
}


JOIN_RELATIONS = [
    {
        "left": "t_aftersale_return.order_id",
        "right": "t_order_main.order_id",
        "sql": "JOIN t_order_main AS o ON o.order_id = r.order_id",
    },
    {
        "left": "t_aftersale_return.sku_code",
        "right": "t_product_sku.sku_code",
        "sql": "JOIN t_product_sku AS s ON s.sku_code = r.sku_code",
    },
    {
        "left": "t_product_sku.category_l3_code",
        "right": "t_product_category.category_code",
        "sql": (
            "JOIN t_product_category AS c "
            "ON c.category_code = s.category_l3_code"
        ),
    },
    {
        "left": "t_product_sku.brand_id",
        "right": "t_product_brand.brand_id",
        "sql": "JOIN t_product_brand AS b ON b.brand_id = s.brand_id",
    },
]


def build_schema_prompt(prompt_template: str | None = None) -> str:
    """输入：可选的版本化 Prompt 模板 ``prompt_template``，以及模块内 Schema 和 JOIN 注册信息。

    输出：交给 SQL 生成模型的 Schema-first System Prompt。
    功能：向版本化完整模板注入当前 Schema、JOIN 和品类映射，用户问题由独立消息传入。
    """

    schema_json = json.dumps(
        SCHEMA_REGISTRY,
        ensure_ascii=False,
        indent=2,
    )
    joins_json = json.dumps(
        JOIN_RELATIONS,
        ensure_ascii=False,
        indent=2,
    )
    categories_json = json.dumps(
        CATEGORY_ALIASES,
        ensure_ascii=False,
        indent=2,
    )
    template = prompt_template or """
你是苏宁售后数据分析系统的 MySQL 查询生成器。

只能生成一条 SELECT 查询，不得生成解释、Markdown代码块或其他文字。

必须遵守以下规则：

1. 退单分析必须以 t_aftersale_return AS r 为事实表。
2. 必须关联：
   JOIN t_order_main AS o ON o.order_id = r.order_id
   JOIN t_product_sku AS s ON s.sku_code = r.sku_code
3. 查询品类时关联 t_product_category AS c。
4. 查询品牌时关联 t_product_brand AS b。
5. 禁止 SELECT *。
6. 退单数量使用 COUNT(DISTINCT r.return_id)。
7. 金额字段单位是分；展示元时除以100。
8. 必须包含 WHERE 子句，可以先写 WHERE 1 = 1。
9. 所有字段必须带表别名。
10. 不需要主动添加 RBAC 条件，服务端会统一注入。
11. 最多返回1000行。
12. 只能返回 COUNT、SUM、AVG 等聚合结果，禁止返回用户或订单明细。
13. “最近N天”以退单表中的最新数据时间为基准，避免模拟或离线数据被系统时钟过滤。例如最近7天写成：
    r.create_time >= (
        SELECT MAX(create_time) - (7 * 86400)
        FROM t_aftersale_return
    )
14. 按品类或品牌分组时，同时返回对应编码和名称，便于名称缺失时识别分组。
15. 用户使用中文品类名筛选时，必须使用下方品类编码映射过滤 s.category_l3_code；父品类同时匹配自身编码和“父编码-%”子品类。禁止用 c.category_name LIKE 代替编码过滤。
16. 只输出SQL。

可用Schema：
{{schema_json}}

合法关联：
{{joins_json}}

中文品类与编码映射：
{{categories_json}}
"""
    return (
        template.replace("{{schema_json}}", schema_json)
        .replace("{{joins_json}}", joins_json)
        .replace("{{categories_json}}", categories_json)
        .strip()
    )
