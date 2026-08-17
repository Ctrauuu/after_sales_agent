"""集中保存权限校验和 NL2SQL Prompt 共用的业务编码映射。"""


REGION_ALIASES = {"华东": "HD", "华北": "HB", "华南": "HN", "西南": "XN"}

CATEGORY_ALIASES = {
    "大家电": "C1",
    "空调": "C1-AC",
    "壁挂式空调": "C1-AC-WG",
    "柜式空调": "C1-AC-CB",
    "中央空调": "C1-AC-CN",
    "冰箱": "C1-RF",
    "多门冰箱": "C1-RF-DOOR",
    "洗衣机": "C1-WM",
    "滚筒洗衣机": "C1-WM-FRONT",
    "电视": "C1-TV",
    "3C数码": "C2",
    "手机": "C2-MB",
}


__all__ = ["CATEGORY_ALIASES", "REGION_ALIASES"]
