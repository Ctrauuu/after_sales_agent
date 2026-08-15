-- 修复本地模拟库中缺失的品牌名和退单原因描述，供自然语言槽位白名单使用。
UPDATE t_product_brand
SET brand_name = CASE brand_id
    WHEN 'B001' THEN '格力'
    WHEN 'B002' THEN '美的'
    WHEN 'B003' THEN '海尔'
    WHEN 'B004' THEN '海信'
    WHEN 'B005' THEN '华为'
    ELSE brand_name
END
WHERE brand_id IN ('B001', 'B002', 'B003', 'B004', 'B005');

UPDATE t_aftersale_return
SET return_reason_desc = CASE return_id
    WHEN 1 THEN '安装后异响严重'
    WHEN 2 THEN '制冷效果差'
    WHEN 3 THEN '屏幕有坏点'
    WHEN 4 THEN '噪音过大'
    WHEN 5 THEN '安装后异响严重（二次报修）'
    WHEN 6 THEN '不制冷'
    WHEN 7 THEN '制冷效果达不到预期'
    WHEN 8 THEN '安装不当导致漏水'
    WHEN 9 THEN '七天无理由退货'
    WHEN 10 THEN '外观有划痕'
    WHEN 11 THEN '室内机漏水'
    WHEN 12 THEN '七天无理由退货'
    WHEN 13 THEN '遥控器失灵'
    WHEN 14 THEN '信号不稳定'
    WHEN 15 THEN '屏幕漏光'
    ELSE return_reason_desc
END
WHERE return_id BETWEEN 1 AND 15;
