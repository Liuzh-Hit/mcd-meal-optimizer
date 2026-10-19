#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""麦当劳 MCP 真实返回 → 本 Skill 内部结构的适配层。

背景（2026-10-15 实测）：
MCP 的返回不是稳定的 JSON 契约，而是三种形态混用：
  1. markdown 说明 + JSON 信封      —— query-meals / query-nearby-stores / query-store-coupons
  2. JSON 信封，data 内嵌 CSV 字符串 —— list-nutrition-foods
  3. 纯 markdown 散文               —— query-my-coupons / available-coupons / campaign-calendar

而且真实字段名与本 Skill 最初的设计假设差距较大，实测结论见 references/mcp-tools.md。
本模块只做「翻译」，不做「决策」：把 MCP 原始载荷翻译成
    data/live_menu.json / data/live_coupons.json
再用 optimize.py / analyze.py 去优化。仅标准库。

命令行:
    python mcd_adapter.py --probe-dir _probe --out-dir data
"""
import argparse
import io
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mcd_core import (  # noqa: E402
    fmt_yuan,
    is_high_fat,
    load_category_rules,
    normalize_category,
    to_fen,
)
from mcp_payload import (  # noqa: E402
    load_tool_text,
    parse_available_markdown,
    parse_coupon_markdown,
    parse_nutrition_table,
)

ROOT = os.path.dirname(HERE)

# --------------------------------------------------------------------------
# 文本归一化（营养表按名字 join，必须先拉平口径）
# --------------------------------------------------------------------------

_PUNCT = "（）()【】「」“”\"'’‘·・,，、。．. 　"


def norm_name(text: Any) -> str:
    """餐品名归一化：去空白、去标点、统一全半角，用于跨表 join。"""
    if text is None:
        return ""
    s = str(text)
    s = s.replace("\n", "").replace("\r", "")
    for ch in _PUNCT:
        s = s.replace(ch, "")
    # 全角字母数字转半角
    out = []
    for ch in s:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out).strip().lower()


# 规格词：营养表把「薯条」拆成「大/中/小/迷你薯条」，菜单侧可能只有「薯条」
_SIZE_TOKENS = ("迷你", "小", "中", "大", "超大")
# 量词：营养表写「小杯玉米杯」，菜单写「玉米杯」——规格词与量词都可能出现在中缀
_MEASURE_TOKENS = ("杯", "份", "块", "个", "条", "只")


def size_variants(name: str) -> List[str]:
    """生成规格维度的候选名。

    覆盖三种真实差异：
        菜单「薯条」    ← 营养表「中薯条」   （前缀规格）
        菜单「玉米杯」  ← 营养表「小杯玉米杯」（前缀规格 + 量词）
        菜单「大薯条」  → 营养表「大薯条」   （已一致）
    """
    base = norm_name(name)
    cands = [base]
    for tok in _SIZE_TOKENS:
        if base.startswith(tok):
            cands.append(base[len(tok):])
        cands.append(tok + base)
        for measure in _MEASURE_TOKENS:
            cands.append(tok + measure + base)
    return list(dict.fromkeys(c for c in cands if c))


_COMBO_NAME_HINTS = (
    "套餐",
    "件套",
    "双人",
    "三人",
    "四人",
    "拼盘",
    "分享",
    "随心选",
    "组合",
    "任选",
    "桶",
    "拼",
)


def looks_like_combo(item: Dict[str, Any]) -> bool:
    """判断是否是套餐。

    不能只看 category==COMBO：实测「麦辣鸡腿汉堡三件套」会被品类规则分到
    「精选单人餐」下，但仍需要展开子项才能算营养。所以名字特征也要认。
    """
    if item.get("category") == "COMBO":
        return True
    name = item.get("name") or ""
    cat = item.get("raw_category") or ""
    return any(h in name or h in cat for h in _COMBO_NAME_HINTS)


# --------------------------------------------------------------------------
# 营养表
# --------------------------------------------------------------------------


_NUT_KEYS = ("energyKcal", "protein", "fat", "carbohydrate", "sodium", "calcium", "energyKj")

# MCP 原始字段名 → 内部 schema 字段名。
# 适配层产物必须是内部契约的形状，否则下游按 energy_kcal 取值会全部拿到 0。
_NUT_OUT = {
    "energyKcal": "energy_kcal",
    "protein": "protein_g",
    "fat": "fat_g",
    "carbohydrate": "carb_g",
    "sodium": "sodium_mg",
    "calcium": "calcium_mg",
    "energyKj": "energy_kj",
}


def parse_nutrition(payload: Any) -> Dict[str, Dict[str, float]]:
    """list-nutrition-foods → {归一化名: 营养}。

    真实字段名：productName / nutritionDescription / energyKj / energyKcal /
                protein / fat / carbohydrate / sodium / calcium
    注意 energyKcal 与 energyKj 都要留，前者是常用口径。
    """
    rows = parse_nutrition_table((payload or {}).get("data") if isinstance(payload, dict) else payload)
    table: Dict[str, Dict[str, float]] = {}
    for row in rows:
        name = row.get("productName")
        if not name or str(name).strip().lower() in ("null", "none", ""):
            continue
        rec = {}
        for key in _NUT_KEYS:
            raw = row.get(key, "")
            rec[key] = _num(raw)
        table[norm_name(name)] = {"source_name": str(name).strip(), **rec}
    return table


def _num(raw: Any) -> float:
    if raw is None:
        return 0.0
    text = str(raw).strip()
    if text.lower() in ("", "null", "none", "-"):
        return 0.0
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else 0.0


def numeric_nutrition(rec: Dict[str, Any]) -> Dict[str, float]:
    """只取数值字段，并转成内部 schema 键名。

    source_name 是字符串，混进去会污染求和；
    energyKcal 等 MCP 原始名必须映射为 energy_kcal，否则下游按内部键取值全为 0。
    """
    out = {}
    for src, dst in _NUT_OUT.items():
        out[dst] = float(rec.get(src, 0.0) or 0.0)
    return out


def nutrition_lookup(name: str, table: Dict[str, Dict[str, float]]) -> Tuple[Optional[Dict[str, float]], str]:
    """按名字查营养，返回 (数值营养, 匹配方式)。匹配方式用于如实标注可信度。

    匹配方式取值：
        exact / size_variant / size_assumed / fuzzy —— 前两者可靠，后两者需标注
        no_table / miss —— 拿不到

    返回值**只含数值字段**，可直接参与求和。
    """
    if not table:
        return None, "no_table"
    key = norm_name(name)
    if key in table:
        return numeric_nutrition(table[key]), "exact"
    for cand in size_variants(name):
        if cand in table:
            return numeric_nutrition(table[cand]), "size_variant"

    # 模糊兜底：菜单名缺规格（如「可乐」对「可乐中杯/大杯/小杯」）。
    # 命中多个时不放弃，而是按「中杯 = 常规默认规格」取一个，并标注为推断。
    hits = [(k, v) for k, v in table.items() if k and (k in key or key in k) and abs(len(k) - len(key)) <= 3]
    if len(hits) == 1:
        return numeric_nutrition(hits[0][1]), "fuzzy"
    if len(hits) > 1:
        for k, v in hits:
            if "中" in k:
                return numeric_nutrition(v), "size_assumed"
        return numeric_nutrition(sorted(hits, key=lambda kv: len(kv[0]))[0][1]), "size_assumed"
    return None, "miss"


# --------------------------------------------------------------------------
# 菜单
# --------------------------------------------------------------------------


def parse_menu(payload: Any) -> List[Dict[str, Any]]:
    """query-meals → 内部菜单条目。

    真实结构：
        data.categories[].name           分类名（含换行，需拉平）
        data.categories[].meals[].code   餐品编码
        data.categories[].meals[].tags   标签
        data.meals{code} = {name, image, currentPrice, originalPrice,
                            canWithOrder, discountType?, withOrder?}
    关键：餐品实体里没有 category 字段，分类只能从 categories 的嵌套关系推。
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    meals_map = data.get("meals") or {}
    if not isinstance(meals_map, dict):
        return []

    # 先建 code -> 原始分类名
    code_cat: Dict[str, str] = {}
    code_tags: Dict[str, List[str]] = {}
    for cat in data.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        cat_name = re.sub(r"\s+", "", str(cat.get("name") or ""))
        for sub in cat.get("meals") or []:
            if not isinstance(sub, dict):
                continue
            c = str(sub.get("code") or "").strip()
            if not c:
                continue
            code_cat.setdefault(c, cat_name)
            tags = sub.get("tags")
            if isinstance(tags, list):
                code_tags.setdefault(c, [str(t) for t in tags])

    items = []
    for code, ent in meals_map.items():
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name") or "").strip()
        if not name:
            continue
        cat_raw = code_cat.get(str(code), "")
        price_fen = to_fen(ent.get("currentPrice"))
        orig_fen = to_fen(ent.get("originalPrice"))
        items.append(
            {
                "code": str(code),
                "name": name,
                "category": normalize_category(cat_raw, name),
                "raw_category": cat_raw,
                "price_fen": price_fen,
                "original_price_fen": orig_fen,
                # currentPrice 已是优惠后价，discountType 说明是哪种卡带来的
                "discount_type": ent.get("discountType"),
                "can_with_order": bool(ent.get("canWithOrder")),
                "image": ent.get("image") or "",
                "tags": code_tags.get(str(code), []),
                "available": True,
                "nutrition": {},
                "nutrition_match": "pending",
                "source": "mcp:query-meals",
            }
        )
    return items


# --------------------------------------------------------------------------
# 套餐明细
# --------------------------------------------------------------------------


def parse_meal_detail(payload: Any) -> Dict[str, Any]:
    """query-meal-detail → 套餐组成。

    真实结构：
        data.rounds[].name            槽位名（如「选择套餐内小食」）
        data.rounds[].choices[]       {code, name, quantity, isDefault, diffPrice, supportModify}
    diffPrice 形如 '+ ¥1.5' / '- ¥0.5'，是换配差价——对营养替换建议很有用。
    """
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data") or {}
    rounds = []
    for rnd in data.get("rounds") or []:
        if not isinstance(rnd, dict):
            continue
        choices = []
        for ch in rnd.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            choices.append(
                {
                    "code": str(ch.get("code") or ""),
                    "name": str(ch.get("name") or "").strip(),
                    "quantity": int(ch.get("quantity") or 0),
                    "is_default": int(ch.get("isDefault") or 0) == 1,
                    "diff_price_fen": _parse_diff_price(ch.get("diffPrice")),
                    "support_modify": bool(ch.get("supportModify")),
                }
            )
        rounds.append({"name": re.sub(r"\s+", "", str(rnd.get("name") or "")), "choices": choices})
    return {"code": str(data.get("code") or ""), "name": str(data.get("name") or ""), "rounds": rounds}


def _parse_diff_price(raw: Any) -> int:
    """'+ ¥1.5' / '- ¥0.5' / '+ ¥0' → 分（带符号）。"""
    if raw is None:
        return 0
    text = str(raw)
    sign = -1 if "-" in text else 1
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m:
        return 0
    return sign * int(round(float(m.group(1)) * 100))


def default_composition(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    """取出套餐默认选中的子项组成（isDefault=1 且数量 > 0）。"""
    picks = []
    for rnd in detail.get("rounds", []):
        for ch in rnd.get("choices", []):
            if ch.get("is_default") and ch.get("quantity", 0) > 0:
                picks.append({**ch, "round": rnd.get("name", "")})
    return picks


def upgrade_options(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    """取出「非默认、可换」的子项，带换配差价——用于给营养/省钱替换建议。"""
    out = []
    for rnd in detail.get("rounds", []):
        for ch in rnd.get("choices", []):
            if not ch.get("is_default"):
                out.append({**ch, "round": rnd.get("name", "")})
    return out


def compose_nutrition(
    detail: Dict[str, Any], table: Dict[str, Dict[str, float]]
) -> Tuple[Dict[str, float], List[str], List[str]]:
    """套餐营养 = 默认子项营养求和。

    返回 (营养合计, 已命中的子项名, 未命中的子项名)。
    这是本适配层最重要的一条：菜单里的套餐在营养表里没有对应项，
    只能展开成子项再求和。
    """
    total = {dst: 0.0 for dst in _NUT_OUT.values()}
    hit, miss = [], []
    for pick in default_composition(detail):
        nut, how = nutrition_lookup(pick["name"], table)
        if nut is None:
            miss.append(pick["name"])
            continue
        hit.append("{}{}".format(pick["name"], "" if how == "exact" else "（%s）" % how))
        for key in total:
            total[key] += nut.get(key, 0.0) * max(1, pick.get("quantity", 1))
    return total, hit, miss


# --------------------------------------------------------------------------
# 门店与地址
# --------------------------------------------------------------------------


def parse_stores(payload: Any) -> List[Dict[str, Any]]:
    """query-nearby-stores → 门店列表。

    真实字段：storeCode / storeName / address / distance（单位：米）/ businessStatus /
              businessStartTime / businessEndTime / reservation / reservationTimeOptions
    注意：**没有** supportDelivery 字段；到店查询拿不到外送能力，外送得走 delivery-query-stores。
    """
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    stores = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        dist_m = _num(row.get("distance"))
        stores.append(
            {
                "name": str(row.get("storeName") or "").strip(),
                "code": str(row.get("storeCode") or "").strip(),
                "be_code": str(row.get("beCode") or "").strip(),
                "address": str(row.get("address") or "").strip(),
                "distance_m": dist_m,
                "distance_km": round(dist_m / 1000.0, 2) if dist_m else None,
                "open": bool(row.get("businessStatus")),
                "business_hours": "{}-{}".format(row.get("businessStartTime") or "?", row.get("businessEndTime") or "?"),
                "reservation": bool(row.get("reservation")),
                "reservation_options": row.get("reservationTimeOptions") or [],
                "support_delivery": None,  # 到店口径不提供，未知而非 False
                "delivery_fee_fen": None,
                "source": "mcp:query-nearby-stores",
            }
        )
    return stores


def parse_delivery_stores(payload: Any) -> List[Dict[str, Any]]:
    """delivery-query-stores → 可配送门店。字段口径与到店查询不同。"""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data")
    if isinstance(rows, dict):
        rows = rows.get("stores") or rows.get("list") or []
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        dist_m = _num(row.get("distance"))
        out.append(
            {
                "name": str(row.get("storeName") or row.get("name") or "").strip(),
                "code": str(row.get("storeCode") or "").strip(),
                "be_code": str(row.get("beCode") or "").strip(),
                "address": str(row.get("address") or "").strip(),
                "distance_m": dist_m,
                "distance_km": round(dist_m / 1000.0, 2) if dist_m else None,
                "open": bool(row.get("businessStatus", True)),
                "support_delivery": True,
                "delivery_fee_fen": to_fen(
                    row.get("deliveryFee") if row.get("deliveryFee") is not None else row.get("deliveryPrice")
                ),
                "source": "mcp:delivery-query-stores",
            }
        )
    return out


def parse_addresses(payload: Any) -> List[Dict[str, Any]]:
    """delivery-query-addresses → 收货地址。真实字段：addressId/contactName/phone/fullAddress。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    rows = data.get("addresses") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        {
            "address_id": str(r.get("addressId") or ""),
            "contact": str(r.get("contactName") or ""),
            "phone": str(r.get("phone") or ""),
            "full_address": str(r.get("fullAddress") or ""),
        }
        for r in rows
        if isinstance(r, dict)
    ]


def parse_account(payload: Any) -> Dict[str, Any]:
    """query-my-account → 积分账户。数值都是字符串。"""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data") or {}
    mapping = {
        "available": "availablePoint",
        "accumulative": "accumulativePoint",
        "frozen": "frozenPoint",
        "used": "usedPoint",
        "expiring_this_month": "currentMouthExpirePoint",
        "expired": "expiredPoint",
    }
    out = {"currency": data.get("currency"), "account_id": data.get("accountId")}
    for key, src in mapping.items():
        out[key] = _num(data.get(src))
    return out


# --------------------------------------------------------------------------
# 优惠券
# --------------------------------------------------------------------------


def parse_store_coupons(payload: Any) -> List[Dict[str, Any]]:
    """query-store-coupons → 结构化券（**首选数据源**）。

    真实字段：title / couponId / couponCode / tradeDateTime /
              products[].productCode / products[].productName / promotionId

    重要实测结论：券的 productCode 与门店菜单编码**不在同一命名空间**
    （券是 99 开头的虚拟商品码，菜单是 1100/1106/...），
    按 code 或 name 都 join 不上。所以券后价**不要本地硬算**，
    应当把 couponId/couponCode 交给 calculate-price 由服务端权威计算。
    """
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        window = str(row.get("tradeDateTime") or "").strip()
        valid_from, valid_until = _split_window(window)
        products = []
        for p in row.get("products") or []:
            if isinstance(p, dict):
                products.append(
                    {
                        "product_code": str(p.get("productCode") or ""),
                        "product_name": str(p.get("productName") or "").strip(),
                    }
                )
        out.append(
            {
                "coupon_id": str(row.get("couponId") or ""),
                "coupon_code": str(row.get("couponCode") or ""),
                "name": title,
                "products": products,
                "applicable_names": [p["product_name"] for p in products if p["product_name"]],
                "applicable_codes": [p["product_code"] for p in products if p["product_code"]],
                "valid_from": valid_from,
                "valid_until": valid_until,
                "validity_text": window,
                "promotion_id": str(row.get("promotionId") or ""),
                "source": "mcp:query-store-coupons",
            }
        )
    return out


def _split_window(text: str) -> Tuple[str, str]:
    m = re.search(
        r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s*[-~至]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)",
        text,
    )
    return (m.group(1), m.group(2)) if m else ("", "")


def merge_coupon_pricing(
    coupons: List[Dict[str, Any]], my_coupons_md: str
) -> List[Dict[str, Any]]:
    """把 query-my-coupons（markdown，含「用券价格」）按标题合并进结构化券。

    query-store-coupons 有 couponId/applicable，query-my-coupons 有「用券价格」和
    「到店专用/外送专用」标签——两者互补，靠 title 对齐。
    """
    by_title = {c["name"]: c for c in parse_coupon_markdown(my_coupons_md)}
    for coupon in coupons:
        info = by_title.get(coupon["name"])
        if not info:
            coupon.setdefault("pay_price_fen", None)
            coupon.setdefault("channel_tags", [])
            coupon.setdefault("estimated", True)
            continue
        pay = info.get("pay_price_yuan")
        coupon["pay_price_fen"] = to_fen(pay) if pay else None
        coupon["channel_tags"] = info.get("tags") or []
        coupon["valid_from"] = coupon.get("valid_from") or info.get("valid_from", "")
        coupon["valid_until"] = coupon.get("valid_until") or info.get("valid_until", "")
        coupon["acquired_text"] = info.get("acquired_text", "")
        coupon["estimated"] = pay is None
    return coupons


def parse_available(payload_text: str) -> List[Dict[str, Any]]:
    """available-coupons（麦麦省）→ 券名 + 领取状态。"""
    return parse_available_markdown(payload_text)


# --------------------------------------------------------------------------
# 券商品（「用券价格」型券 = 一个可以直接下单的虚拟商品）
# --------------------------------------------------------------------------

# 券名里表示「实际拿到几份」的后缀
_DEAL_PORTION_SUFFIXES = (
    ("买一送一", 2),
    ("第二份半价", 2),
    ("第二件半价", 2),
    ("1+1", 2),
)

# 判定券商品品类时，弱 COMBO 关键词要摘掉，否则「王牌小食拼」会被当成套餐
_STRONG_COMBO_KEYWORDS = (
    "套餐", "件套", "乐园餐", "双人", "三人", "四人", "五人",
    "拼盘", "分享", "随心选", "任选", "组合", "桶",
)


def coupon_deal_parts(title: str) -> Tuple[str, int]:
    """从券名拆出「基准餐品名」与「实际份数」。

    实测（2026-10-16）：麦当劳主力券是「用券价格」型——付 ¥13.5 拿走
    「中薯条买一送一」。它本质是一个**可下单的虚拟商品**，
    券名里的后缀决定了实际拿到几份（营养与份数挂钩）。
    """
    raw = str(title or "").strip()
    base, portions = raw, 1
    for kw, n in _DEAL_PORTION_SUFFIXES:
        if kw in raw:
            base = raw.replace(kw, "").strip()
            portions = n
            break
    return base or raw, portions


def deal_category(name: str, rules=None) -> str:
    """券商品的品类判定：收紧 COMBO 关键词，避免「小食拼」被误判成套餐。"""
    base_rules = rules if rules is not None else load_category_rules()
    patched = [
        (cat, list(_STRONG_COMBO_KEYWORDS) if cat == "COMBO" else list(kws))
        for cat, kws in base_rules
    ]
    return normalize_category("", name, patched)


def build_coupon_deal(
    coupon: Dict[str, Any],
    table: Dict[str, Dict[str, float]],
    menu_items: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """把一张「用券价格」券翻译成一个可下单的候选餐品。

    为什么要这么做（2026-10-16 实测结论）：
    券的虚拟商品码（99 开头）与门店菜单编码（1100/1106/…）**不在同一命名空间**，
    所以「本地算优惠额」这条路走不通（会输出「减免 0.00 元」这种假确定）。
    但把券商品当成**一个候选餐品**是完全成立的——它的价格就是券价，
    且能交给 calculate-price 做权威复核。券由此从「事后抵扣」变成
    「参与组合优化」，才有可能真正选到最省的方案。
    """
    pay = coupon.get("pay_price_fen")
    products = coupon.get("products") or []
    if not pay or not products:
        return None
    virtual_code = str(products[0].get("product_code") or "").strip()
    if not virtual_code:
        return None

    title = str(coupon.get("name") or "").strip()
    base_name, portions = coupon_deal_parts(title)
    category = deal_category(base_name)

    nut, _how = nutrition_lookup(base_name, table)
    nutrition: Dict[str, float] = {}
    match = "coupon_deal_miss"
    if nut is not None:
        nutrition = {k: float(v) * portions for k, v in nut.items()}
        match = "coupon_deal"

    # 原价：优先用服务端核出的 list_price_fen；否则按菜单同名单品 × 份数兜底
    list_price = coupon.get("list_price_fen")
    if not list_price and menu_items:
        target = norm_name(base_name)
        for mi in menu_items:
            if norm_name(mi.get("name")) == target:
                list_price = int(mi.get("price_fen") or 0) * portions
                break

    deal = {
        "code": "CPN:{}".format(virtual_code),
        "name": title or base_name,
        "category": category,
        "price_fen": int(pay),
        "list_price_fen": int(list_price or 0),
        "is_coupon_deal": True,
        "coupon_id": str(coupon.get("coupon_id") or ""),
        "coupon_code": str(coupon.get("coupon_code") or ""),
        "virtual_product_code": virtual_code,
        "portions": portions,
        "base_item_name": base_name,
        "valid_until": str(coupon.get("valid_until") or ""),
        "expires": str(coupon.get("valid_until") or ""),
        "channel_tags": list(coupon.get("channel_tags") or []),
        "nutrition": nutrition,
        "nutrition_match": match,
        "tags": ["券商品"],
        "available": True,
        "limited_note": "券商品",
        "source": "mcp:query-store-coupons",
    }
    src = next((mi for mi in (menu_items or []) if norm_name(mi.get("name")) == norm_name(base_name)), None)
    deal["is_high_fat"] = is_high_fat({"nutrition": nutrition, "category": category}) if nutrition else bool(
        src and src.get("is_high_fat")
    )
    return deal


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


def build_live(
    probe_dir: str,
    menu: Optional[str] = None,
    nutrition: Optional[str] = None,
    coupons_store: Optional[str] = None,
    coupons_mine: Optional[str] = None,
    details: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """把 _probe/ 下的原始返回组装成内部可用的菜单与券。

    返回 dict: {menu:[...], coupons:[...], coverage:{...}, warnings:[...]}
    """
    warnings: List[str] = []

    def _load(name: str, override: Optional[str] = None):
        path = override or os.path.join(probe_dir, name)
        if not os.path.isfile(path):
            return None, ""
        obj, prose = load_tool_text(path)
        return obj, prose

    nut_obj, _ = _load("nutrition.json", nutrition)
    table = parse_nutrition(nut_obj) if nut_obj else {}
    if not table:
        warnings.append("未取到营养表（list-nutrition-foods），营养功能将不可用")

    menu_obj, _ = _load("meals_store.json", menu)
    items = parse_menu(menu_obj) if menu_obj else []
    if not items:
        warnings.append("未取到菜单（query-meals），配餐功能将不可用")

    # 单品类先按名字直接查营养
    for item in items:
        nut, how = nutrition_lookup(item["name"], table)
        # 无论成败都如实记录匹配方式——「缺营养」必须显式，不能留 pending 冒充未知
        item["nutrition_match"] = how
        if nut is not None:
            item["nutrition"] = nut

    # 套餐类：用 query-meal-detail 展开默认子项求和
    detail_dir = os.path.join(probe_dir, "details")
    detail_files = []
    if details:
        detail_files = details
    elif os.path.isdir(detail_dir):
        detail_files = [os.path.join(detail_dir, f) for f in sorted(os.listdir(detail_dir)) if f.endswith(".json")]

    combo_filled = 0
    single_confirmed = 0
    for path in detail_files:
        obj, _ = load_tool_text(path)
        detail = parse_meal_detail(obj)
        if not detail.get("code"):
            continue
        target = next((it for it in items if it["code"] == detail["code"]), None)
        if target is None:
            continue
        picks = default_composition(detail)
        if not picks:
            # 实测：单品（如巨无霸）的 rounds 为空。所以「有默认子项」才是套餐的
            # 精确判据——不能用菜名猜，否则会把单品误展开成特调选项求和。
            single_confirmed += 1
            continue
        total, hit, miss = compose_nutrition(detail, table)
        target["nutrition"] = total
        target["nutrition_match"] = "combo_default" if hit and not miss else "combo_partial"
        target["combo_default_items"] = [p["name"] for p in picks]
        target["combo_missing_items"] = miss
        target["combo_upgrades"] = upgrade_options(detail)
        if hit:
            combo_filled += 1

    # 券
    coupons: List[Dict[str, Any]] = []
    sc_obj, _ = _load("coupons_store.json", coupons_store)
    if sc_obj:
        coupons = parse_store_coupons(sc_obj)
    if not coupons:
        warnings.append("未取到门店券（query-store-coupons），优惠功能将降级")

    _, my_md = _load("coupons_mine.json", coupons_mine)
    if coupons and my_md:
        coupons = merge_coupon_pricing(coupons, my_md)
    elif coupons:
        warnings.append("未取到个人券（query-my-coupons），券后价无法补齐，将由 calculate-price 兜底")

    # 权威核价：refresh_live 已把券商品交给 calculate-price，
    # 这里用服务端返回的原价/券价覆盖本地推断值——券后价必须以服务端为准。
    pricing_obj, _ = _load("coupon_pricing.json")
    pricing = pricing_obj if isinstance(pricing_obj, dict) else {}
    priced = 0
    for coupon in coupons:
        info = pricing.get(coupon.get("coupon_id"))
        if not isinstance(info, dict):
            continue
        if info.get("list_price_fen"):
            coupon["list_price_fen"] = int(info["list_price_fen"])
        if info.get("pay_price_fen"):
            coupon["pay_price_fen"] = int(info["pay_price_fen"])
        coupon["priced"] = True
        priced += 1

    # 券商品：把「用券价格」券翻译成可下单的候选条目，让券参与组合优化而不再只是事后抵扣
    deal_ready = 0
    for coupon in coupons:
        deal = build_coupon_deal(coupon, table, items)
        if deal:
            coupon["deal_item"] = deal
            deal_ready += 1
    if coupons and not deal_ready:
        warnings.append("本店券中没有「用券价格」型券，无法转为可直接下单的券商品，券功能仅作提示")

    # 覆盖度统计
    total_items = len(items)
    with_nut = sum(1 for it in items if it.get("nutrition"))
    combo_items = sum(1 for it in items if it.get("category") == "COMBO")
    coverage = {
        "menu_items": total_items,
        "nutrition_items": with_nut,
        "nutrition_ratio": round(with_nut / total_items, 4) if total_items else 0.0,
        "combo_items": combo_items,
        "combo_solved": combo_filled,
        "detail_single_confirmed": single_confirmed,
        "nutrition_table_size": len(table),
        "coupons": len(coupons),
        "coupons_with_pay_price": sum(1 for c in coupons if c.get("pay_price_fen")),
        "coupons_priced_by_server": priced,
        "coupon_deals": deal_ready,
    }
    if total_items and coverage["nutrition_ratio"] < 0.9:
        warnings.append(
            "营养覆盖不完整：{}/{} 项有营养数据（{:.0%}），"
            "其余按「未知」处理，营养汇总会如实标注缺口".format(
                with_nut, total_items, coverage["nutrition_ratio"]
            )
        )

    return {"menu": items, "coupons": coupons, "coverage": coverage, "warnings": warnings}


def write_live(result: Dict[str, Any], out_dir: str) -> Dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    menu_path = os.path.join(out_dir, "live_menu.json")
    coupon_path = os.path.join(out_dir, "live_coupons.json")
    report_path = os.path.join(out_dir, "live_coverage.json")
    with io.open(menu_path, "w", encoding="utf-8") as fh:
        json.dump(result["menu"], fh, ensure_ascii=False, indent=1)
    with io.open(coupon_path, "w", encoding="utf-8") as fh:
        json.dump(result["coupons"], fh, ensure_ascii=False, indent=1)
    with io.open(report_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"coverage": result["coverage"], "warnings": result["warnings"]},
            fh,
            ensure_ascii=False,
            indent=1,
        )
    return {"menu": menu_path, "coupons": coupon_path, "coverage": report_path}


def render_report(result: Dict[str, Any]) -> str:
    cov = result["coverage"]
    lines = [
        "# 实测数据覆盖度",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        "| 门店在售餐品 | {} |".format(cov["menu_items"]),
        "| 其中有营养数据 | {} （{:.0%}）|".format(cov["nutrition_items"], cov["nutrition_ratio"]),
        "| 营养表条目 | {} |".format(cov["nutrition_table_size"]),
        "| 套餐类餐品 | {} |".format(cov["combo_items"]),
        "| 已展开求和的套餐 | {} |".format(cov["combo_solved"]),
        "| 可用优惠券 | {} |".format(cov["coupons"]),
        "| 券中已知「用券价格」 | {} |".format(cov["coupons_with_pay_price"]),
        "| 服务端已核价 | {} |".format(cov.get("coupons_priced_by_server", 0)),
        "| 已转为可下单券商品 | {} |".format(cov.get("coupon_deals", 0)),
    ]
    if result["warnings"]:
        lines += ["", "## 警告", ""]
        lines += ["- " + w for w in result["warnings"]]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="麦当劳 MCP 返回 → 内部结构适配")
    parser.add_argument("--probe-dir", default=os.path.join(ROOT, "_probe"))
    parser.add_argument("--out-dir", default=os.path.join(ROOT, "data"))
    parser.add_argument("--menu")
    parser.add_argument("--nutrition")
    parser.add_argument("--coupons-store")
    parser.add_argument("--coupons-mine")
    parser.add_argument("--details", nargs="*")
    args = parser.parse_args()

    result = build_live(
        args.probe_dir,
        menu=args.menu,
        nutrition=args.nutrition,
        coupons_store=args.coupons_store,
        coupons_mine=args.coupons_mine,
        details=args.details,
    )
    paths = write_live(result, args.out_dir)
    print(render_report(result))
    print("")
    for label, path in paths.items():
        print("已写出 {:<9s} {}".format(label, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
