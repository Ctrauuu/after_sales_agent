-- 仅用于既有模拟库：修复历史初始化中缺失的工单、物流和退款展示字段。
-- 生产库不得执行本迁移，因为以下 ID 与描述是演示数据的固定值。
UPDATE t_aftersale_workflow
SET
    step_name = CASE workflow_id
        WHEN 1 THEN '用户提交退货申请'
        WHEN 2 THEN '客服审核'
        WHEN 3 THEN '物流上门取件'
        WHEN 4 THEN '仓库签收入库'
        WHEN 5 THEN '仓库质检'
        WHEN 6 THEN '用户提交退货申请'
        WHEN 7 THEN '客服审核'
        WHEN 8 THEN '物流上门取件'
        WHEN 9 THEN '仓库签收入库'
        WHEN 10 THEN '仓库质检'
        WHEN 11 THEN '退款处理'
        WHEN 12 THEN '用户提交退货申请'
        WHEN 13 THEN '客服审核'
        ELSE step_name
    END,
    remark = CASE workflow_id
        WHEN 1 THEN '安装后异响'
        WHEN 2 THEN '审核通过'
        WHEN 3 THEN '取件完成'
        WHEN 4 THEN '南京江宁仓'
        WHEN 5 THEN '待检测'
        WHEN 6 THEN '制冷差'
        WHEN 7 THEN '审核通过'
        WHEN 8 THEN '取件完成'
        WHEN 9 THEN '杭州仓'
        WHEN 10 THEN '质检合格'
        WHEN 11 THEN '退款成功'
        WHEN 12 THEN '屏幕有坏点'
        WHEN 13 THEN '审核通过'
        ELSE remark
    END
WHERE workflow_id BETWEEN 1 AND 13;

UPDATE t_logistics_trace
SET
    node_desc = CASE trace_id
        WHEN 1 THEN '上门取件'
        WHEN 2 THEN '运输中-到达南京分拣中心'
        WHEN 3 THEN '到达南京江宁仓'
        WHEN 4 THEN '上门取件'
        WHEN 5 THEN '运输中-到达杭州分拣中心'
        WHEN 6 THEN '到达杭州仓'
        ELSE node_desc
    END,
    operator_name = CASE trace_id
        WHEN 1 THEN '刘建国'
        WHEN 2 THEN '系统'
        WHEN 3 THEN '系统'
        WHEN 4 THEN '李大力'
        WHEN 5 THEN '系统'
        WHEN 6 THEN '系统'
        ELSE operator_name
    END
WHERE trace_id BETWEEN 1 AND 6;

UPDATE t_payment_refund
SET refund_method = '原路退回'
WHERE refund_id IN (1, 2);
