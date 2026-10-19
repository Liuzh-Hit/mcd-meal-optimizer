"""analyze.py - 菜品状态识别 + 门店/配送可行性判定 + 套餐营养汇总。

对应技能能力 5（门店工具）与能力 6（菜品分析）。

门店判定规则
------------
麦当劳 MCP 的 query-nearby-stores 给出「附近门店」，delivery-query-stores 给出
「可配送门店」，两者口径不同。这里按以下顺序判定：
1. 用户是否有可用配送地址（delivery-query-addresses）——没有则外送直接不成立。
2. 门店是否标记支持外送（support_delivery / isDelivery / 配送标签）。
3. 若返回了距离，则与配送半径比较（默认 3 km，可调）。
4. 营业状态与预计送达时间作为补充提示。
结论分三档：可外送 / 只得到店取餐 / 信息不足需人工确认。

仅依赖 Python 标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nutrition  # noqa: E402
from mcd_core import (  # noqa: E402
    as_list,
    dump_json,
    fmt_yuan,
    load_category_rules,
    load_menu,
    pick,
    to_fen,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(os.path.dirname(HERE), "references", "category-map.json")

LIMITED_KEYWORDS = ("限时", "限时上新", "季节限定", "限定", "限时供应", "新品", "区域限定")
DISCONTINUED_KEYWORDS = ("下架", "停售", "敬请期待", "已售罄", "售罄", "即将下架", "即将停售")


def classify_menu(menu: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把菜单分成：在售 / 限时在售 / 即将下架 / 已下架。"""
    on_sale: List[Dict[str, Any]] = []
    limited: List[Dict[str, Any]] = []
    ending: List[Dict[str, Any]] = []
    off_sale: List[Dict[str, Any]] = []

    for item in menu:
        blob = f"{item.get('name', '')} {' '.join(item.get('tags', []))} {item.get('limited_note', '')} {item.get('raw_category', '')}"
        if not item.get("available", True):
            off_sale.append(item)
            continue
        if any(k in blob for k in DISCONTINUED_KEYWORDS):
            ending.append(item)
            continue
        if any(k in blob for k in LIMITED_KEYWORDS):
            limited.append(item)
            continue
        on_sale.append(item)

    return {
        "on_sale": on_sale,
        "limited": limited,
        "ending": ending,
        "off_sale": off_sale,
        "summary": {
            "on_sale": len(on_sale),
            "limited": len(limited),
            "ending": len(ending),
            "off_sale": len(off_sale),
            "total": len(menu),
        },
    }


def combo_nutrition(menu: Sequence[Dict[str, Any]], codes: Sequence[str], people: int = 1) -> Dict[str, Any]:
    by_code: Dict[str, Dict[str, Any]] = {}
    by_name: Dict[str, Dict[str, Any]] = {}
    for item in menu:
        by_code[item["code"]] = item
        by_name[item["name"]] = item

    picked: List[Dict[str, Any]] = []
    missing: List[str] = []
    for token in codes:
        item = by_code.get(token) or by_name.get(token)
        if item is None:
            missing.append(token)
        else:
            picked.append(item)

    totals = nutrition.sum_totals(picked)
    return {
        "matched": [it["name"] for it in picked],
        "missing": missing,
        "count": len(picked),
        "subtotal_yuan": round(sum(it["price_fen"] for it in picked) / 100.0, 2),
        "nutrition_total": {k: round(v, 1) for k, v in totals.items()},
        "nutrition_per_person": nutrition.totals_row(totals, people),
        "high_fat_items": [it["name"] for it in picked if it["is_high_fat"]],
        "items": [
            {
                "name": it["name"],
                "category": it["category"],
                "price_yuan": round(it["price_fen"] / 100.0, 2),
                "is_high_fat": it["is_high_fat"],
                "nutrition": {k: round(v, 1) for k, v in it["nutrition"].items()},
            }
            for it in picked
        ],
    }


# --------------------------------------------------------------------------
# 门店与配送
# --------------------------------------------------------------------------


def _boolish(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "支持", "可外送", "可配送"):
        return True
    if text in ("false", "0", "no", "n", "不支持", "不可外送", "不可配送"):
        return False
    return None


def normalize_store(raw: Dict[str, Any]) -> Dict[str, Any]:
    name = str(pick(raw, ["name", "storeName", "store_name", "门店名称", "餐厅名称"], "") or "").strip()
    code = str(pick(raw, ["code", "storeCode", "storeId", "store_id", "门店编码"], "") or "").strip()
    address = str(pick(raw, ["address", "storeAddress", "addr", "门店地址", "地址"], "") or "").strip()

    distance_m: Optional[float] = None
    raw_distance = pick(raw, ["distance", "distanceM", "distance_m", "距离"], None)
    if raw_distance is not None:
        text = str(raw_distance)
        m = re.search(r"(\d+(?:\.\d+)?)", text)
        if m:
            value = float(m.group(1))
            distance_m = value * 1000.0 if ("km" in text.lower() or "公里" in text) else value

    delivery_flag = None
    for key in ("supportDelivery", "isDelivery", "canDelivery", "deliveryEnabled", "supportsDelivery", "支持外送"):
        if key in raw:
            delivery_flag = _boolish(raw.get(key))
            if delivery_flag is not None:
                break
    if delivery_flag is None:
        blob = json.dumps(raw, ensure_ascii=False)
        if any(k in blob for k in ("麦乐送", "外送", "配送")):
            delivery_flag = True

    fee_raw = pick(raw, ["deliveryFee", "delivery_fee", "配送费"], None)
    status = str(pick(raw, ["status", "businessStatus", "营业状态"], "") or "").strip()
    eta = pick(raw, ["eta", "deliveryTime", "预计送达", "estimatedTime"], None)
    hours = str(pick(raw, ["businessHours", "hours", "营业时间"], "") or "").strip()

    return {
        "name": name or code or "未命名门店",
        "code": code,
        "address": address,
        "distance_m": distance_m,
        "support_delivery": delivery_flag,
        "delivery_fee_fen": to_fen(fee_raw) if fee_raw is not None else None,
        "delivery_fee_yuan": round(to_fen(fee_raw) / 100.0, 2) if fee_raw is not None else None,
        "status": status,
        "eta": str(eta) if eta is not None else "",
        "hours": hours,
    }


def load_stores(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = []
        for key in ("stores", "data", "list", "items", "result", "nearbyStores", "deliveryStores"):
            value = raw.get(key)
            if isinstance(value, list):
                rows = value
                break
    else:
        rows = []
    return [normalize_store(row) for row in rows if isinstance(row, dict)]


def load_user_addresses(path: str) -> List[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    rows = raw if isinstance(raw, list) else []
    if isinstance(raw, dict):
        for key in ("addresses", "data", "list", "items", "result"):
            value = raw.get(key)
            if isinstance(value, list):
                rows = value
                break
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "name": str(pick(row, ["name", "contact", "收货人", "标签"], "") or "").strip(),
                "address": str(pick(row, ["address", "detail", "fullAddress", "地址"], "") or "").strip(),
                "code": str(pick(row, ["code", "addressId", "id", "地址编码"], "") or "").strip(),
            }
        )
    return out


def delivery_feasibility(
    stores: Sequence[Dict[str, Any]],
    addresses: Sequence[Dict[str, Any]],
    radius_km: float = 3.0,
    address_hint: str = "",
) -> Dict[str, Any]:
    notes: List[str] = []

    if not addresses:
        notes.append("未查询到可用配送地址，外送不成立；需先通过 delivery-create-address 新增地址，或改为到店取餐。")

    matched_address = None
    if addresses:
        if address_hint:
            for addr in addresses:
                if address_hint and (address_hint in addr.get("address", "") or addr.get("name") == address_hint):
                    matched_address = addr
                    break
        matched_address = matched_address or addresses[0]

    deliverable: List[Dict[str, Any]] = []
    pickup_only: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []

    for store in stores:
        distance = store.get("distance_m")
        reason: List[str] = []
        if store.get("support_delivery") is False:
            pickup_only.append({**store, "reason": "门店不支持外送"})
            continue
        if store.get("support_delivery") is None:
            unknown.append({**store, "reason": "门店未返回外送能力字段"})
            continue
        if distance is not None and distance > radius_km * 1000:
            pickup_only.append({**store, "reason": f"距离 {distance / 1000:.1f} km 超出 {radius_km:.0f} km 配送半径"})
            continue
        if store.get("delivery_fee_fen") is None:
            reason.append("配送费未返回，下单前需以 calculate-price 复核")
        if distance is not None:
            reason.append(f"距离 {distance / 1000:.1f} km")
        deliverable.append({**store, "reason": "；".join(reason) if reason else "可外送"})

    deliverable.sort(key=lambda s: (s.get("distance_m") if s.get("distance_m") is not None else 9e9))

    if deliverable:
        verdict = "可外送"
        notes.append(f"共 {len(deliverable)} 家门店可配送到「{(matched_address or {}).get('address') or address_hint or '默认地址'}」。")
    elif stores:
        verdict = "建议到店取餐"
        notes.append("没有门店满足外送条件，建议改用「到店取餐」场景（query-meals + calculate-price + create-order 的堂食/外带分支）。")
    else:
        verdict = "信息不足"
        notes.append("未传入门店数据，请先调用 query-nearby-stores（到店）或 delivery-query-stores（外送）。")

    return {
        "verdict": verdict,
        "radius_km": radius_km,
        "address_used": matched_address,
        "address_count": len(addresses),
        "deliverable": deliverable,
        "pickup_only": pickup_only,
        "unknown": unknown,
        "notes": notes,
        "recommended": deliverable[0] if deliverable else (pickup_only[0] if pickup_only else None),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render_status(result: Dict[str, Any]) -> str:
    s = result["summary"]
    lines = [
        f"## 菜单状态：在售 {s['on_sale']}｜限时在售 {s['limited']}｜即将下架 {s['ending']}｜已下架 {s['off_sale']}（共 {s['total']}）",
        "",
    ]
    for key, title in (("limited", "限时 / 限定在售"), ("ending", "即将下架（建议避开）"), ("off_sale", "已下架")):
        rows = result.get(key, [])
        if not rows:
            continue
        lines.append(f"**{title}**")
        for item in rows:
            note = f"｜{item['limited_note']}" if item.get("limited_note") else ""
            lines.append(f"- {item['name']}（{item['price_fen'] / 100:.2f} 元）{'｜标签：' + '/'.join(item['tags']) if item['tags'] else ''}{note}")
        lines.append("")
    return "\n".join(lines)


def render_delivery(result: Dict[str, Any]) -> str:
    lines = [f"## 门店与配送判定：{result['verdict']}", ""]
    for note in result["notes"]:
        lines.append(f"- {note}")
    lines.append("")
    if result["deliverable"]:
        lines.append("**可外送门店**")
        lines.append("| 门店 | 距离 | 配送费 | 预计送达 | 备注 |")
        lines.append("|---|---:|---:|---|---|")
        for store in result["deliverable"][:8]:
            distance = f"{store['distance_m'] / 1000:.1f} km" if store.get("distance_m") is not None else "未知"
            fee = f"{store['delivery_fee_yuan']:.2f} 元" if store.get("delivery_fee_yuan") is not None else "待确认"
            lines.append(f"| {store['name']} | {distance} | {fee} | {store.get('eta') or '-'} | {store.get('reason', '')} |")
        lines.append("")
    if result["pickup_only"]:
        lines.append("**仅可到店**")
        for store in result["pickup_only"][:8]:
            lines.append(f"- {store['name']}：{store.get('reason', '')}")
        lines.append("")
    if result["unknown"]:
        lines.append("**信息不足**")
        for store in result["unknown"][:8]:
            lines.append(f"- {store['name']}：{store.get('reason', '')}")
    return "\n".join(lines)


def render_nutrition(result: Dict[str, Any]) -> str:
    lines = [f"## 套餐营养汇总（{result['count']} 件，共 {result['subtotal_yuan']:.2f} 元）", ""]
    if result["missing"]:
        lines.append(f"⚠️ 未能匹配：{', '.join(result['missing'])}")
        lines.append("")
    lines.append("| 餐品 | 品类 | 单价 | 热量 | 蛋白 | 脂肪 | 碳水 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for item in result["items"]:
        nut = item["nutrition"]
        flag = " ⚠️" if item["is_high_fat"] else ""
        lines.append(
            f"| {item['name']}{flag} | {item['category']} | {item['price_yuan']:.2f} | "
            f"{nut.get('energy_kcal', 0):.0f} | {nut.get('protein_g', 0):.0f} | {nut.get('fat_g', 0):.0f} | {nut.get('carb_g', 0):.0f} |"
        )
    lines.append("")
    total = result["nutrition_total"]
    pp = result["nutrition_per_person"]
    lines.append(
        f"- 整单合计：{total.get('energy_kcal', 0):.0f} kcal｜蛋白 {total.get('protein_g', 0):.0f}g｜"
        f"脂肪 {total.get('fat_g', 0):.0f}g｜碳水 {total.get('carb_g', 0):.0f}g｜钠 {total.get('sodium_mg', 0):.0f}mg"
    )
    lines.append(
        f"- 每人份：{pp.get('energy_kcal', 0):.0f} kcal｜蛋白 {pp.get('protein_g', 0):.0f}g｜"
        f"脂肪 {pp.get('fat_g', 0):.0f}g｜脂肪供能比 {pp.get('fat_energy_ratio', 0) * 100:.0f}%｜"
        f"蛋白质密度 {pp.get('protein_density', 0):.0f} g/1000kcal"
    )
    if result["high_fat_items"]:
        lines.append(f"- 高油菜品警告：{'、'.join(result['high_fat_items'])}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="麦当劳菜品状态 / 门店配送 / 套餐营养分析")
    parser.add_argument("--action", required=True, choices=["status", "delivery", "nutrition"])
    parser.add_argument("--menu", default="", help="菜单 JSON")
    parser.add_argument("--stores", default="", help="门店 JSON")
    parser.add_argument("--addresses", default="", help="用户配送地址 JSON")
    parser.add_argument("--codes", default="", help="菜品编码或名称，逗号分隔（nutrition 用）")
    parser.add_argument("--people", type=int, default=1)
    parser.add_argument("--radius-km", type=float, default=3.0)
    parser.add_argument("--address-hint", default="")
    parser.add_argument("--rules", default=DEFAULT_RULES)
    parser.add_argument("--json", dest="json_out", default="")
    parser.add_argument("--format", default="both", choices=["text", "json", "both"])
    args = parser.parse_args()

    if args.action == "status":
        if not args.menu:
            print("需要 --menu")
            return 2
        menu = load_menu(args.menu, load_category_rules(args.rules))
        result = classify_menu(menu)
        result["ok"] = True
        text = render_status(result)
    elif args.action == "delivery":
        stores = load_stores(args.stores) if args.stores else []
        addresses = load_user_addresses(args.addresses)
        result = delivery_feasibility(stores, addresses, args.radius_km, args.address_hint)
        result["ok"] = True
        text = render_delivery(result)
    else:
        if not args.menu:
            print("需要 --menu")
            return 2
        menu = load_menu(args.menu, load_category_rules(args.rules))
        codes = [c.strip() for c in re.split(r"[,，]", args.codes) if c.strip()]
        if not codes:
            print("需要 --codes")
            return 2
        result = combo_nutrition(menu, codes, args.people)
        result["ok"] = True
        text = render_nutrition(result)

    if args.json_out:
        dump_json(result, args.json_out)
    if args.format in ("text", "both"):
        print(text)
    if args.format == "json":
        print(dump_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
