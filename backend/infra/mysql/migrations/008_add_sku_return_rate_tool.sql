-- SKU 退单率的订单分母：每行代表一个订单包含的一个 SKU。
-- 数据应由订单系统同步；不得从退单表反推，以免把所有 SKU 退单率误算为 100%。
CREATE TABLE IF NOT EXISTS t_order_item (
    order_id BIGINT NOT NULL,
    sku_code VARCHAR(64) NOT NULL,
    PRIMARY KEY (order_id, sku_code),
    KEY idx_order_item_sku_order (sku_code, order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO mcp_tool_registry (
    tool_id, server_id, tool_name, description, param_schema, is_enabled
) VALUES (
    'tool-010',
    'mcp-aftersale',
    'query_sku_return_rate',
    '按订单创建时间统计 SKU 退单订单率，返回退单订单数、订单数和退单率最高的前若干 SKU',
    JSON_OBJECT('date_range_days', 'int', 'limit', 'int', 'category', 'string'),
    1
)
ON DUPLICATE KEY UPDATE
    description = VALUES(description),
    param_schema = VALUES(param_schema),
    is_enabled = VALUES(is_enabled);
