"""mcd_core.py - 共享数据模型、字段容错归一化与价格工具。

设计要点
--------
1. 所有金额在内部一律以「分」的整数保存，杜绝浮点误差。对外输出时才转成元。
2. MCP 返回的字段名没有稳定契约，因此所有归一化函数都做「多别名 + 关键词推断」的
   容错处理，字段缺失时降级而不是抛异常。
3. 菜品品类用一套 canonical 桶（MAIN/SIDE/DRINK/DESSERT/BREAKFAST/COMBO/OTHER）
   收敛，映射规则放在 references/category-map.json，可随时按真实菜单调整。

仅依赖 Python 标准库。
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# 金额工具
# --------------------------------------------------------------------------


def to_fen(value: Any, default: int = 0) -> int:
    """把元（int/float/str）转成分。容错：无法解析时返回 default。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        return int(round(value * 100))
    text = str(value).strip()
    if not text:
        return default
    text = text.replace(",", "").replace("，", "")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return default
    return int(round(float(m.group(0)) * 100))


def fen_to_yuan(fen: int) -> float:
    return round(fen / 100.0, 2)


def fmt_yuan(fen: int) -> str:
    return f"{fen / 100.0:.2f}"


def pick(raw: Dict[str, Any], aliases: Sequence[str], default: Any = None) -> Any:
    """按别名顺序取第一个「存在且非空」的字段。"""
    for key in aliases:
        if key in raw:
            value = raw.get(key)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, (list, dict)) and len(value) == 0:
                continue
            return value
    return default


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        parts = re.split(r"[,，;；/|、\s]+", value.strip())
        return [p for p in (x.strip() for x in parts) if p]
    return [value]


# --------------------------------------------------------------------------
# 品类归一化
# --------------------------------------------------------------------------

CANONICAL_CATEGORIES = (
    "BREAKFAST",
    "COMBO",
    "DESSERT",
    "DRINK",
    "SIDE",
    "MAIN",
    "MERCH",
    "OTHER",
)

# 关键词按「名字优先」的判定顺序排列，顺序本身就是优先级：
# MERCH 必须在最前（「薯条脆卜卜毛绒周边」含「薯条」但它是周边）；
# COMBO 必须在 MAIN 前（「麦辣鸡腿汉堡三件套」含「汉堡」但它是套餐）。
_DEFAULT_CATEGORY_RULES: List[Tuple[str, List[str]]] = [
    (
        "MERCH",
        [
            "周边",
            "毛绒",
            "玩具",
            "玩偶",
            "挂件",
            "徽章",
            "贴纸",
            "痒痒挠",
            "按摩捶",
            "抱枕",
            "盲盒",
            "手办",
            "钥匙扣",
            "帆布袋",
            "渔夫帽",
            "卫衣",
        ],
    ),
    (
        "COMBO",
        [
            "套餐",
            "件套",
            "乐园餐",
            "双人",
            "三人",
            "四人",
            "五人",
            "拼盘",
            "分享",
            "随心选",
            "任选",
            "组合",
            "桶",
            "拼",
        ],
    ),
    ("BREAKFAST", ["早餐", "麦满分", "满福堡", "粥", "油条", "蛋堡", "松饼", "猪柳蛋"]),
    (
        "DESSERT",
        [
            "冰淇淋",
            "冰激凌",
            "新地",
            "麦旋风",
            "派",
            "蛋糕",
            "圣代",
            "甜筒",
            "圆筒",
            "雪糕",
            "阿芙佳朵",
            "布丁",
        ],
    ),
    (
        "DRINK",
        [
            "可乐",
            "雪碧",
            "咖啡",
            "茶",
            "果汁",
            "奶茶",
            "冰沙",
            "豆浆",
            "牛奶",
            "鲜橙",
            "矿泉水",
            "纯悦",
            "美汁源",
            "拿铁",
            "摩卡",
            "卡布",
            "麦咖啡",
        ],
    ),
    (
        "SIDE",
        [
            "薯条",
            "薯饼",
            "鸡块",
            "鸡翅",
            "沙拉",
            "玉米",
            "洋葱圈",
            "香骨",
            "小食",
            "脆薯",
            "麦乐鸡",
            "脆汁鸡",
        ],
    ),
    (
        "MAIN",
        [
            "汉堡",
            "堡",
            "卷",
            "巨无霸",
            "板烧",
            "麦辣",
            "双拼",
            "鸡排",
            "麦香鱼",
            "麦香鸡",
            "牛堡",
            "鱼堡",
            "厚牛",
            "安格斯",
        ],
    ),
]

# 命中这些词时，前面的关键词不算数（「派对」不该被「派」判成甜品）
_NEGATIVE_CONTEXT = {"DESSERT": ("派对",)}


def load_sections_map(path: Optional[str] = None) -> Dict[str, str]:
    """加载真实菜单分区名 → 品类的映射；MIXED 表示混合分区，需靠菜名二次判定。"""
    default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "references", "mcd-sections.json")
    target = path or default
    if os.path.isfile(target):
        try:
            with open(target, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            sections = raw.get("sections") if isinstance(raw, dict) else None
            if isinstance(sections, dict):
                return {re.sub(r"\s+", "", str(k)): str(v).upper() for k, v in sections.items()}
        except Exception:
            pass
    return {}


def load_category_rules(path: Optional[str] = None) -> List[Tuple[str, List[str]]]:
    """加载品类映射规则；文件缺失或损坏时回落到内置规则。"""
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            rules = raw.get("rules", raw) if isinstance(raw, dict) else raw
            parsed: List[Tuple[str, List[str]]] = []
            for entry in rules:
                cat = str(entry.get("category", "")).upper()
                keys = [str(k) for k in entry.get("keywords", [])]
                if cat in CANONICAL_CATEGORIES and keys:
                    parsed.append((cat, keys))
            if parsed:
                return parsed
        except Exception:
            pass
    return list(_DEFAULT_CATEGORY_RULES)


def normalize_category(raw_category: Any, name: str, rules=None, sections=None) -> str:
    """判定优先级（依次）：

    1. 菜名命中 MERCH —— 周边可能被摆进任意食品分区
    2. 菜名命中 COMBO —— MCP 的分区是「销售分区」不是「结构分类」，
       实测「麦辣鸡腿汉堡三件套」挂在「鸡肉汉堡/卷」分区下，但它仍然是套餐
    3. 分区映射表（非 MIXED）—— 权威且无歧义，如「500大卡套餐」→ COMBO
    4. 菜名其余关键词 —— 关键词顺序即优先级
    5. 分区名关键词 —— 最后兜底

    为什么这么排：MCP 分区里有多个混合分区（「小食甜品/其他」「人气热卖」
    「开心乐园」「蘸酱炸鸡」），且分区会与结构分类冲突。先信任「结构」，
    再信任「分区」。
    """
    rules = rules if rules is not None else _DEFAULT_CATEGORY_RULES
    if sections is None:
        sections = load_sections_map()

    rule_map = {cat: kws for cat, kws in rules}
    name_text = str(name or "")
    probe = name_text
    for cat, negatives in _NEGATIVE_CONTEXT.items():
        for neg in negatives:
            if neg in probe:
                probe = probe.replace(neg, "")

    def hits(cat: str, text: str) -> bool:
        return any(kw and kw in text for kw in rule_map.get(cat, []))

    # 1. 结构性强信号：周边 / 套餐
    if probe and hits("MERCH", probe):
        return "MERCH"
    if probe and hits("COMBO", probe):
        return "COMBO"

    # 2. 分区映射表
    key = re.sub(r"\s+", "", str(raw_category or ""))
    mapped = sections.get(key)
    if mapped and mapped != "MIXED" and mapped in CANONICAL_CATEGORIES:
        return mapped

    # 3. 菜名其余关键词
    if probe:
        for cat, keywords in rules:
            if cat in ("MERCH", "COMBO"):
                continue
            for kw in keywords:
                if kw and kw in probe:
                    return cat

    # 4. 分区名关键词兜底
    if key:
        for cat, keywords in rules:
            for kw in keywords:
                if kw and kw in key:
                    return cat
    return "OTHER"


# --------------------------------------------------------------------------
# 高油判定
# --------------------------------------------------------------------------

HIGH_FAT_NAME_HINTS = [
    "炸",
    "麦辣",
    "辣翅",
    "鸡块",
    "薯条",
    "薯饼",
    "鸡翅",
    "脆",
    "培根",
    "油条",
    "派",
]


def fat_energy_ratio(nutrition: Dict[str, float]) -> float:
    """脂肪供能比 = 脂肪(g)*9 / 总热量(kcal)。"""
    kcal = float(nutrition.get("energy_kcal") or 0.0)
    fat = float(nutrition.get("fat_g") or 0.0)
    if kcal <= 0:
        return 0.0
    return (fat * 9.0) / kcal


def is_high_fat(item: Dict[str, Any], ratio_threshold: float = 0.42) -> bool:
    """高油判定：标签命中 or 名称命中 or 脂肪供能比超标。"""
    tags = " ".join(str(t) for t in item.get("tags", []))
    name = str(item.get("name", ""))
    if any(k in tags for k in ("油炸", "高油", "炸物")):
        return True
    if any(k in name for k in HIGH_FAT_NAME_HINTS):
        return True
    nutrition = item.get("nutrition", {})
    if float(nutrition.get("fat_g") or 0.0) < 10.0:
        # 绝对脂肪量很低的餐品（牛奶、咖啡、清饮）不按供能比判高油，
        # 否则会出现「热牛奶被标高油」这类反直觉结论。
        return False
    return fat_energy_ratio(nutrition) >= ratio_threshold


# --------------------------------------------------------------------------
# 菜品归一化
# --------------------------------------------------------------------------


# 这些匹配方式意味着「确实没查到营养」，而不是「营养为零」
NUTRITION_MISS_MATCHES = ("miss", "no_table", "pending")
# 这些匹配方式意味着「查到了」，即使数值恰好为 0 也算已知（如纯悦矿泉水 0 千卡）
NUTRITION_HIT_MATCHES = ("exact", "size_variant", "size_assumed", "fuzzy", "combo_default", "combo_partial")


def has_known_nutrition(item: Dict[str, Any]) -> bool:
    """判断该餐品是否**真的**有营养数据。

    这一点很关键：MCP 的营养表覆盖不全（实测 112 项菜单只有 86 项能查到）。
    把「查不到」按 0 处理会得出「这份套餐 0 千卡」这种荒谬结论，
    所以必须区分「未知」与「零」，并在输出里如实标注。

    券商品（coupon deal）的营养匹配标记形如 ``coupon_deal`` / ``coupon_deal_miss``，
    不在上面两张表里，所以按 **前缀 + 后缀** 兜底判定：
    以 ``_miss`` 结尾一律视为「未知」，``coupon_deal`` 前缀其余情况视为「已知」。
    否则一个查不到营养的券商品会因为「营养字典非空（全 0）」而被误判成已知。
    """
    match = item.get("nutrition_match")
    if isinstance(match, str):
        if match in NUTRITION_MISS_MATCHES:
            return False
        if match in NUTRITION_HIT_MATCHES:
            return True
        if match.endswith("_miss"):
            return False
        if match.startswith("coupon_deal"):
            return True
    # 没有 match 字段（本地样例数据）→ 有营养字典就算已知
    nutrition = item.get("nutrition")
    return isinstance(nutrition, dict) and bool(nutrition)


def normalize_nutrition(raw: Any) -> Dict[str, float]:
    """把各种可能的营养字段名收敛成统一结构。

    真实字段名（list-nutrition-foods，2026-10-15 实测）：
    energyKj / energyKcal / protein / fat / carbohydrate / sodium / calcium
    注意是 energyKcal 而非 kcal——pick() 做的是精确键匹配，漏掉会直接归零。
    """
    if not isinstance(raw, dict):
        raw = {}
    return {
        "energy_kcal": float(
            pick(raw, ["energy_kcal", "energyKcal", "energy", "calorie", "calories", "kcal", "热量", "能量"], 0) or 0
        ),
        "protein_g": float(pick(raw, ["protein_g", "protein", "蛋白质"], 0) or 0),
        "fat_g": float(pick(raw, ["fat_g", "fat", "脂肪"], 0) or 0),
        "carb_g": float(pick(raw, ["carb_g", "carbohydrate", "carbs", "碳水化合物", "碳水"], 0) or 0),
        "sodium_mg": float(pick(raw, ["sodium_mg", "sodium", "钠"], 0) or 0),
        "calcium_mg": float(pick(raw, ["calcium_mg", "calcium", "钙"], 0) or 0),
    }


def normalize_item(raw: Dict[str, Any], category_rules=None) -> Dict[str, Any]:
    """归一化单条餐品。

    幂等：mcd_adapter 产出的 live_menu.json 已经是内部结构（带 price_fen /
    category / nutrition），再次过一遍本函数必须原样保留，否则价格会被冲成 0。
    """
    code = str(pick(raw, ["code", "productCode", "sku", "id", "mealCode", "餐品编码"], "") or "").strip()
    name = str(pick(raw, ["name", "productName", "title", "mealName", "餐品名称"], "") or "").strip()

    # 优先吃内部字段，其次才做别名推断
    if "price_fen" in raw and raw.get("price_fen") is not None:
        price_fen = int(raw["price_fen"])
    else:
        price_fen = to_fen(pick(raw, ["price", "salePrice", "currentPrice", "amount", "价格", "售价"], 0))

    raw_cat = pick(raw, ["category", "categoryName", "分类", "type"], "")
    if raw_cat in CANONICAL_CATEGORIES:
        category = raw_cat
    else:
        category = normalize_category(raw_cat, name, category_rules)

    nutrition = normalize_nutrition(pick(raw, ["nutrition", "nutrients", "nutritionInfo", "营养成分"], {}))

    available_raw = pick(raw, ["available", "isAvailable", "onSale", "saleable", "status"], None)
    if available_raw is None:
        available = True
    elif isinstance(available_raw, bool):
        available = available_raw
    elif isinstance(available_raw, (int, float)):
        available = bool(available_raw)
    else:
        text = str(available_raw).strip().lower()
        available = text not in ("false", "0", "no", "下架", "sold_out", "soldout", "unavailable", "off")

    tags = [str(t) for t in as_list(pick(raw, ["tags", "tag", "labels", "标签"], []))]

    item = {
        "code": code or name,
        "name": name or code,
        "category": category,
        "raw_category": str(pick(raw, ["raw_category"], raw_cat) or ""),
        "price_fen": price_fen,
        "tags": tags,
        "available": available,
        "limited_note": str(pick(raw, ["limitedNote", "note", "remark", "限时说明"], "") or ""),
        "nutrition": nutrition,
        # 以下字段只在真实数据路径出现，缺省不写死，保证向后兼容
        "nutrition_match": raw.get("nutrition_match", "pending"),
        "original_price_fen": raw.get("original_price_fen"),
        "discount_type": raw.get("discount_type"),
        "combo_default_items": raw.get("combo_default_items"),
        "combo_missing_items": raw.get("combo_missing_items"),
        "combo_upgrades": raw.get("combo_upgrades"),
        "source": raw.get("source", "local"),
    }
    item["is_high_fat"] = is_high_fat(item)
    return item


def load_menu(path: str, category_rules=None) -> List[Dict[str, Any]]:
    """读取菜单 JSON。

    兼容四种形状：
        1. 裸数组                          [ {..}, {..} ]
        2. {"meals": [...]}                内部结构
        3. {"data": {"meals": [...]}}      兼容包装
        4. MCP 原始返回                    data.meals 是「以 code 为键的映射表」
    第 4 种直接交给 mcd_adapter 解析（延迟导入，避免循环依赖）。
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    # 形态 4：MCP 原始返回
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, dict) and isinstance(data.get("meals"), dict) and data.get("categories"):
            from mcd_adapter import parse_menu  # noqa: WPS433

            return [normalize_item(row, category_rules) for row in parse_menu(raw)]

    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = []
        for key in ("meals", "items", "products", "list", "menu", "data", "result"):
            value = raw.get(key)
            if isinstance(value, list):
                rows = value
                break
            if isinstance(value, dict):
                for sub in ("meals", "items", "products", "list"):
                    if isinstance(value.get(sub), list):
                        rows = value[sub]
                        break
                if rows:
                    break
    else:
        rows = []

    return [normalize_item(row, category_rules) for row in rows if isinstance(row, dict)]


# --------------------------------------------------------------------------
# 优惠券归一化
# --------------------------------------------------------------------------

_EXCLUSIVE_HINTS = ("不可叠加", "不与其他", "互斥", "不可同享", "不与")


def infer_coupon_kind(name: str, desc: str, explicit: Any, has_applicable: bool = False) -> str:
    """推断券的结算方式。先认显式 type，再做关键词兜底。"""
    mapping = {
        "threshold": "threshold",
        "满减": "threshold",
        "满减券": "threshold",
        "percent": "percent",
        "discount": "percent",
        "折扣": "percent",
        "折扣券": "percent",
        "item_free": "item_free",
        "free": "item_free",
        "免费": "item_free",
        "免费兑换": "item_free",
        "兑换券": "item_free",
        "item_discount": "item_discount",
        "单品立减": "item_discount",
        "单品券": "item_discount",
        "单品折扣": "item_discount",
        "delivery": "delivery_free",
        "delivery_free": "delivery_free",
        "免配送费": "delivery_free",
        "免配送": "delivery_free",
        # 以下两类是 2026-10-15 实测麦当劳真实券的主要形态
        "item_price": "item_price",
        "用券价格": "item_price",
        "固定价": "item_price",
        "item_bogo": "item_bogo",
        "买一送一": "item_bogo",
        "第二份半价": "item_bogo",
    }
    if explicit:
        text = str(explicit).strip().lower()
        if text in mapping:
            return mapping[text]
        for key, value in mapping.items():
            if key.isascii() and key in text:
                return value

    blob = f"{name} {desc}"
    if "免配送" in blob or "配送费" in blob:
        return "delivery_free"
    if "买一送一" in blob:
        return "item_bogo"
    if "免费" in blob or "0元" in blob or "兑换" in blob:
        return "item_free"
    if re.search(r"\d+(\.\d+)?\s*折", blob):
        return "percent"
    if "满" in blob and "减" in blob:
        return "threshold"
    if "立减" in blob or "减免" in blob:
        return "item_discount" if has_applicable else "threshold"
    return "threshold"


def infer_threshold_fen(name: str, desc: str, explicit: Any) -> int:
    if explicit is not None:
        return to_fen(explicit)
    m = re.search(r"满\s*(\d+(?:\.\d+)?)", f"{name} {desc}")
    return to_fen(m.group(1)) if m else 0


def infer_percent(name: str, desc: str, explicit: Any) -> float:
    """返回「支付比例」，8折 -> 0.8。"""
    if explicit is not None:
        value = float(explicit)
        return value / 10.0 if value > 1 else value
    m = re.search(r"(\d+(?:\.\d+)?)\s*折", f"{name} {desc}")
    if m:
        return float(m.group(1)) / 10.0
    m = re.search(r"减\s*(\d+(?:\.\d+)?)\s*元", f"{name} {desc}")
    if m:
        return max(0.0, 1.0 - float(m.group(1)) / 100.0)
    return 1.0


def infer_value_fen(name: str, desc: str, explicit: Any, kind: str) -> int:
    if explicit is not None and kind != "percent":
        return to_fen(explicit)
    if kind == "percent":
        return 0
    m = re.search(r"减\s*(\d+(?:\.\d+)?)", f"{name} {desc}")
    if m:
        return to_fen(m.group(1))
    return 0


def normalize_coupon(raw: Dict[str, Any], category_rules=None) -> Dict[str, Any]:
    coupon_id = str(
        pick(raw, ["couponId", "coupon_id", "couponCode", "id", "code", "券码"], "") or ""
    ).strip()
    name = str(pick(raw, ["name", "couponName", "title", "shortName", "券名称"], "") or "").strip()
    desc = str(pick(raw, ["desc", "description", "ruleDesc", "useRule", "说明", "使用规则"], "") or "")

    applicable = [
        str(x)
        for x in as_list(
            pick(
                raw,
                ["applicable_codes", "applicableCodes", "applicableProducts", "productCodes", "适用商品", "适用餐品"],
                [],
            )
        )
    ]

    # 真实券的「用券价格」——这是麦当劳主力券型，必须显式携带
    pay_price_raw = pick(raw, ["pay_price_fen", "payPrice", "payPriceYuan"], None)
    pay_price_fen = int(pay_price_raw) if isinstance(pay_price_raw, int) else (
        to_fen(pay_price_raw) if pay_price_raw is not None else None
    )

    explicit_kind = pick(raw, ["kind", "type", "couponType", "券类型"], None)
    if pay_price_fen is not None and not explicit_kind:
        explicit_kind = "item_price"

    kind = infer_coupon_kind(name, desc, explicit_kind, has_applicable=bool(applicable))
    if kind == "delivery_free":
        scope = "delivery"
    elif kind in ("item_free", "item_discount", "item_price", "item_bogo"):
        scope = "item"
    else:
        scope = "order"

    excluded = [
        str(x) for x in as_list(pick(raw, ["excludes", "exclusive", "mutuallyExclusiveWith", "cannotUseWith", "互斥券"], []))
    ]
    if not excluded and any(h in f"{name} {desc}" for h in _EXCLUSIVE_HINTS):
        excluded.append("*")  # 标记：与其他优惠券互斥，但未指明具体券

    expires = str(
        pick(
            raw,
            ["expires", "expireTime", "endTime", "validUntil", "validEndTime", "valid_until", "有效期", "截止时间"],
            "",
        )
        or ""
    ).strip()

    coupon = {
        "coupon_id": coupon_id or name,
        "name": name or coupon_id,
        "kind": kind,
        "scope": scope,
        "threshold_fen": infer_threshold_fen(name, desc, pick(raw, ["threshold", "minAmount", "minSpend", "门槛"], None)),
        "value_fen": infer_value_fen(name, desc, pick(raw, ["value", "amount", "discount", "面额"], None), kind),
        "pay_price_fen": pay_price_fen,
        "pay_ratio": (
            infer_percent(name, desc, pick(raw, ["payRatio", "rate", "折扣"], None)) if kind == "percent" else 1.0
        ),
        "max_discount_fen": to_fen(pick(raw, ["maxDiscount", "cap", "封顶"], None))
        if pick(raw, ["maxDiscount", "cap", "封顶"], None) is not None
        else None,
        "applicable_codes": applicable,
        "expires": expires,
        "excludes": excluded,
        "channel_tags": [str(t) for t in as_list(pick(raw, ["channel_tags", "channelTags", "标签"], []))],
        "desc": desc,
        "source": str(pick(raw, ["source", "来源"], "") or ""),
    }
    coupon["verifiable"] = is_coupon_verifiable(coupon)
    if not coupon["verifiable"]:
        coupon["unverifiable_reason"] = explain_unverifiable(coupon)
    return coupon


def is_coupon_verifiable(coupon: Dict[str, Any]) -> bool:
    """判断这张券能不能在本地算出确定的优惠金额。

    实测痛点：麦当劳券的 productCode 与门店菜单编码**不在同一命名空间**
    （券是 99 开头的虚拟商品码，菜单是 1100/1106/...），
    因此「买一送一」「用券价格」这类券在本地**无法核价**。
    如果硬算，就会输出「减免 0.00 元」这种看起来很确定、实则毫无依据的结果。
    诚实的做法是标记为不可核验，交给 calculate-price 由服务端结算。
    """
    kind = coupon.get("kind")
    if kind == "percent" and coupon.get("pay_ratio", 1.0) < 1.0:
        return True
    if kind == "threshold" and coupon.get("value_fen", 0) > 0:
        return True
    if kind in ("item_free", "item_discount") and coupon.get("value_fen", 0) > 0:
        return True
    if kind == "delivery_free":
        return True
    if kind in ("item_price", "item_bogo"):
        # 需要把券的适用商品与菜单对上才能算；productCode 不通用时即为不可核验
        return False
    return False


def explain_unverifiable(coupon: Dict[str, Any]) -> str:
    kind = coupon.get("kind")
    if kind == "item_price":
        return "「用券价格」型券：需把券的适用商品与门店菜单对齐，二者编码体系不同，本地无法确定优惠额"
    if kind == "item_bogo":
        return "「买一送一」型券：优惠额取决于实际购买件数，需服务端核价"
    if kind == "threshold":
        return "满减门槛或面额无法从券面文本解析"
    return "券型无法识别或缺少面额信息"


def load_coupons(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = []
        for key in ("coupons", "items", "list", "data", "result", "availableCoupons"):
            value = raw.get(key)
            if isinstance(value, list):
                rows = value
                break
    else:
        rows = []
    return [normalize_coupon(row) for row in rows if isinstance(row, dict)]


# 营养字段白名单——券商品只保留数值字段，避免字符串键污染求和
_DEAL_NUTRIENT_KEYS = ("energy_kcal", "protein_g", "fat_g", "carb_g", "sodium_mg", "calcium_mg")


def load_coupon_deals(path: str) -> List[Dict[str, Any]]:
    """读取券商品——「用券价格」型券的可下单形态（由 mcd_adapter 生成）。

    券商品是**直接参与配餐组合的候选餐品**：它的价格就是券价，
    所以券后价不必再本地推算，求和即可，并且能交给 calculate-price 权威复核。
    这是把「券」从「事后抵扣」变成「参与优化」的关键。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return []

    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = raw.get("coupons") or raw.get("data") or []
    else:
        rows = []

    deals: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        src = row.get("deal_item") if isinstance(row.get("deal_item"), dict) else row
        if not (src.get("is_coupon_deal") or "deal_item" in row):
            continue
        code = str(src.get("code") or "").strip()
        price_fen = int(src.get("price_fen") or 0)
        if not code or price_fen <= 0:
            continue
        # 只收「真的存在」的营养键。查不到时保持空字典，
        # 绝不补 0——补 0 会让下游把「未知」误判成「已知」（见 has_known_nutrition）。
        raw_nut = src.get("nutrition") if isinstance(src.get("nutrition"), dict) else {}
        nutrition = {
            key: float(raw_nut[key])
            for key in _DEAL_NUTRIENT_KEYS
            if raw_nut.get(key) is not None
        }
        deals.append(
            {
                "code": code,
                "name": str(src.get("name") or code),
                "category": str(src.get("category") or "OTHER"),
                "raw_category": "券商品",
                "price_fen": price_fen,
                "list_price_fen": int(src.get("list_price_fen") or 0),
                "portions": int(src.get("portions") or 1),
                "is_coupon_deal": True,
                "coupon_id": str(src.get("coupon_id") or row.get("coupon_id") or ""),
                "coupon_code": str(src.get("coupon_code") or row.get("coupon_code") or ""),
                "virtual_product_code": str(src.get("virtual_product_code") or ""),
                "base_item_name": str(src.get("base_item_name") or ""),
                "valid_until": str(src.get("valid_until") or src.get("expires") or ""),
                "channel_tags": [str(t) for t in (src.get("channel_tags") or [])],
                "tags": ["券商品"],
                "available": True,
                "limited_note": "券商品",
                "nutrition": nutrition,
                "nutrition_match": str(src.get("nutrition_match") or "coupon_deal_miss"),
                "is_high_fat": bool(src.get("is_high_fat")),
                "source": str(src.get("source") or "mcp:query-store-coupons"),
            }
        )
    return deals


# --------------------------------------------------------------------------
# IO
# --------------------------------------------------------------------------


def dump_json(obj: Any, path: Optional[str] = None) -> str:
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if path:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return text


def gcd_of(values: Iterable[int]) -> int:
    result = 0
    for value in values:
        v = abs(int(value))
        while v:
            result, v = v, result % v
    return result or 1


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)
