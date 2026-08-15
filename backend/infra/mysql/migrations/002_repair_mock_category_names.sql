-- 按需求文档修复本地模拟品类数据中缺失的中文名称。
UPDATE t_product_category
SET category_name = CASE category_code
    WHEN 'C1' THEN '大家电'
    WHEN 'C1-AC' THEN '空调'
    WHEN 'C1-AC-WG' THEN '壁挂式空调'
    WHEN 'C1-AC-CB' THEN '柜式空调'
    WHEN 'C1-AC-CN' THEN '中央空调'
    WHEN 'C1-RF' THEN '冰箱'
    WHEN 'C1-RF-DOOR' THEN '多门冰箱'
    WHEN 'C1-WM' THEN '洗衣机'
    WHEN 'C1-WM-FRONT' THEN '滚筒洗衣机'
    WHEN 'C1-TV' THEN '电视'
    WHEN 'C2' THEN '3C数码'
    WHEN 'C2-MB' THEN '手机'
    ELSE category_name
END
WHERE category_name IS NULL OR category_name = '';
