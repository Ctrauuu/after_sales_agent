-- 补齐 MCP 服务名称和工具描述；可重复执行，不修改连接、心跳和启用状态。
SET NAMES utf8mb4;

UPDATE mcp_server_registry
SET server_name = CASE server_id
    WHEN 'mcp-order' THEN '订单数据服务'
    WHEN 'mcp-aftersale' THEN '售后工单服务'
    WHEN 'mcp-product' THEN '商品品类服务'
    WHEN 'mcp-logistics' THEN '物流轨迹服务'
    WHEN 'mcp-payment' THEN '支付退款服务'
    WHEN 'mcp-order-timeline' THEN '订单全链路聚合服务'
END
WHERE server_id IN (
    'mcp-order', 'mcp-aftersale', 'mcp-product', 'mcp-logistics',
    'mcp-payment', 'mcp-order-timeline'
);

UPDATE mcp_tool_registry
SET description = CASE tool_name
    WHEN 'search_orders' THEN '根据订单状态、商品品类、日期范围及区域筛选订单列表，返回按下单时间倒序排列并按权限脱敏的结果'
    WHEN 'get_order_detail' THEN '根据订单 ID 查询订单明细及其全部关联退单，返回状态标签并按访问权限脱敏'
    WHEN 'get_aftersale_workflow' THEN '根据退单 ID 或订单 ID 查询售后工单的完整流转节点，并按发生时间顺序返回'
    WHEN 'query_return_stats_nl2sql' THEN '按日期、品类、退单原因、区域或品牌聚合指定时间范围内的退单数量和金额'
    WHEN 'query_aftersale_nl2sql' THEN '将自然语言售后分析问题转换为受控只读 SQL，返回经过权限过滤和脱敏的聚合结果'
    WHEN 'get_product_info' THEN '根据 SKU 编码查询商品、品牌、品类、价格、保修期限、常见故障和生产批次信息'
    WHEN 'query_logistics' THEN '根据退单 ID 或订单 ID 查询完整逆向物流轨迹，按退单分组并按发生时间排序'
    WHEN 'get_refund_status' THEN '根据退单 ID 或订单 ID 查询退款金额、退款方式、退款时间及处理状态'
    WHEN 'trace_order_timeline' THEN '根据订单 ID 并行聚合订单、售后工单、逆向物流和退款数据，返回标准化时间线、当前卡点、SLA 状态及部分失败来源'
END
WHERE tool_name IN (
    'search_orders', 'get_order_detail', 'get_aftersale_workflow',
    'query_return_stats_nl2sql', 'query_aftersale_nl2sql', 'get_product_info',
    'query_logistics', 'get_refund_status', 'trace_order_timeline'
);
