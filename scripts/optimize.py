"""optimize.py - 预算最优配餐 + 优惠券智能组合比价。

为什么用 DP 而不是让模型直接算
-----------------------------
「预算上限 + 人数 + 每人 N 个槽位」本质是一个受限组合优化问题，候选组合是
乘法级增长的（品类 15 款 x 6 个槽位 ≈ 1.1e7）。大模型的算术在价格求和、
满减门槛、折扣封顶这类整数约束上不可靠，容易给出「看起来对但总价超预算」的方案。
因此这里把决策交给确定性算法：DP 负责在预算内搜出最优组合，优惠券部分做
小规模子集枚举并显式检查互斥，最后把结构化的结论交回给模型做表达。

算法
----
1. 品类内先做 Pareto 支配剪枝：价格更贵、评分更低的菜品直接剔除。
2. 把每个槽位的选择建成一维背包：dp[成本] = 最大评分，逐槽位迭代并回传父指针。
3. 按成本单位做支配剪枝，压缩状态数。
4. 取评分最高的前 N 个终态还原成完整方案，去重。
5. 对每个方案枚举优惠券组合（张数受限 + 互斥校验），算出券后价。
6. 先按「券后价 <= 预算」筛可行，再按评分排序，输出主方案 + 结构不同的备选方案。

仅依赖 Python 标准库。
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nutrition  # noqa: E402
from mcd_core import (  # noqa: E402
    as_list,
    dump_json,
    fmt_yuan,
    gcd_of,
    has_known_nutrition,
    load_category_rules,
    load_coupon_deals,
    load_coupons,
    load_menu,
    to_fen,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SCENARIOS = os.path.join(os.path.dirname(HERE), "data", "scenarios.json")
DEFAULT_RULES = os.path.join(os.path.dirname(HERE), "references", "category-map.json")
DEFAULT_UNSELLABLE = os.path.join(os.path.dirname(HERE), "data", "unsellable.json")

QUALITY_PRESETS: Dict[str, Dict[str, float]] = {
    # unknown_nut_pen：对「营养表里查不到」的餐品轻微扣分。
    # 实测门店菜单营养覆盖率只有 ~77%，不加这项惩罚，优化器会倾向挑最便宜的
    # 无数据餐品，给出「整单 0 千卡」这种没法用来决策的方案。
    "value": {
        "protein_g": 6.0,
        "energy_kcal": 0.015,
        "fat_g": -1.0,
        "high_fat_pen": -25.0,
        "base": 10.0,
        "unknown_nut_pen": -8.0,
    },
    # 减脂的核心诉求是「高蛋白、低油」，所以脂肪权重和高油惩罚都要压过蛋白质的正收益，
    # 否则会出现「香骨鸡腿（油炸、脂肪 15g）压过蔬菜沙拉」这种反直觉选择。
    "slim": {
        "protein_g": 20.0,
        "energy_kcal": -0.08,
        "fat_g": -6.0,
        "high_fat_pen": -150.0,
        "base": 0.0,
        "unknown_nut_pen": -80.0,
    },
    "muscle": {
        "protein_g": 26.0,
        "energy_kcal": -0.03,
        "fat_g": -2.0,
        "high_fat_pen": -60.0,
        "base": 0.0,
        "unknown_nut_pen": -80.0,
    },
}

MODE_LABELS = {"value": "常规性价比", "slim": "减脂", "muscle": "健身增肌"}

CATEGORY_LABELS = {
    "MAIN": "主餐",
    "SIDE": "小食",
    "DRINK": "饮品",
    "DESSERT": "甜品",
    "COMBO": "套餐",
    "OTHER": "其他",
    "BREAKFAST": "早餐",
    "MERCH": "周边",
}

MAX_STATES = 6000
MAX_CANDIDATES = 260


# --------------------------------------------------------------------------
# 场景
# --------------------------------------------------------------------------


def load_scenarios(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def expand_slots(profile: Dict[str, Any], people: int) -> List[str]:
    slots: List[str] = []
    for spec in profile.get("slots", []):
        category = str(spec.get("category", "")).upper()
        if not category:
            continue
        if spec.get("count") is not None:
            count = int(spec["count"])
        else:
            per = float(spec.get("per_person", 1.0))
            count = max(1, int(math.ceil(per * people - 1e-9)))
        count = max(int(spec.get("min_count", 0) or 0), count)
        slots.extend([category] * count)
    return slots


def describe_slots(slots: Sequence[str]) -> str:
    labels = {
        "MAIN": "主餐",
        "SIDE": "小食",
        "DRINK": "饮品",
        "DESSERT": "甜品",
        "BREAKFAST": "早餐",
        "COMBO": "套餐",
        "OTHER": "其他",
    }
    counter = Counter(slots)
    return " + ".join(f"{count} 份{labels.get(cat, cat)}" for cat, count in counter.items())


def relax_levels(slots: Sequence[str], people: int) -> List[List[str]]:
    """预算装不下原结构时的逐级放宽阶梯。

    顺序：删甜品 -> 删套餐 -> 小食压到每人一份 -> 饮品压到每人一份。
    只做「减项」不做「换项」，保证放宽后的结构仍是用户看得懂的套餐形态。
    """
    levels: List[List[str]] = []
    current = list(slots)
    for drop in ("DESSERT", "COMBO"):
        if drop in current:
            current = [c for c in current if c != drop]
            levels.append(list(current))
    for category in ("SIDE", "DRINK"):
        count = current.count(category)
        if count > people:
            kept: List[str] = []
            removed = 0
            for c in current:
                if c == category and removed < count - people:
                    removed += 1
                    continue
                kept.append(c)
            current = kept
            levels.append(list(current))
    return levels


# --------------------------------------------------------------------------
# 评分
# --------------------------------------------------------------------------


def item_quality(item: Dict[str, Any], weights: Dict[str, float], tag_bonus: Optional[Dict[str, float]] = None) -> float:
    nut = item.get("nutrition", {}) or {}
    quality = float(weights.get("base", 0.0))
    quality += weights.get("protein_g", 0.0) * float(nut.get("protein_g") or 0.0)
    quality += weights.get("energy_kcal", 0.0) * float(nut.get("energy_kcal") or 0.0)
    quality += weights.get("fat_g", 0.0) * float(nut.get("fat_g") or 0.0)
    quality += weights.get("carb_g", 0.0) * float(nut.get("carb_g") or 0.0)
    if item.get("is_high_fat"):
        quality += float(weights.get("high_fat_pen", 0.0))
    if not has_known_nutrition(item):
        # 营养未知 → 扣分。不这么做的话，优化器会偏好「便宜但查不到营养」的餐品，
        # 最后输出一份无法做营养判断的清单。
        quality += float(weights.get("unknown_nut_pen", 0.0))
    if tag_bonus:
        blob = f"{item.get('name', '')} {' '.join(item.get('tags', []))} {item.get('raw_category', '')}"
        for keyword, bonus in tag_bonus.items():
            if keyword and keyword in blob:
                quality += float(bonus)
    return quality


def pareto_prune(pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """剔除「更贵且评分不更高」的菜品。保留同价同分的第一个。"""
    ordered = sorted(pool, key=lambda it: (it["price_fen"], -it["_quality"]))
    kept: List[Dict[str, Any]] = []
    best_quality = float("-inf")
    for item in ordered:
        if item["_quality"] > best_quality + 1e-9:
            kept.append(item)
            best_quality = item["_quality"]
    return kept


# --------------------------------------------------------------------------
# 背包求解
# --------------------------------------------------------------------------


def solve_plans(
    pools: Dict[str, List[Dict[str, Any]]],
    slots: Sequence[str],
    cap_units: int,
    exclude_codes: Sequence[str] = (),
    max_candidates: int = MAX_CANDIDATES,
) -> Optional[Tuple[List[Tuple[int, float]], List[Dict[int, Tuple[int, int]]]]]:
    """返回 (终态列表[(成本单位, 评分)], 每槽位父指针)，无可行解时返回 None。"""
    excluded = set(exclude_codes)
    dp: Dict[int, float] = {0: 0.0}
    history: List[Dict[int, Tuple[int, int]]] = []

    for slot_index, category in enumerate(slots):
        pool = pools.get(category, [])
        usable = [(idx, it) for idx, it in enumerate(pool) if it["code"] not in excluded] or list(enumerate(pool))
        if not usable:
            return None

        next_dp: Dict[int, float] = {}
        hist: Dict[int, Tuple[int, int]] = {}
        for cost, score in dp.items():
            for idx, item in usable:
                new_cost = cost + item["_units"]
                if new_cost > cap_units:
                    continue
                new_score = score + item["_quality"]
                if new_cost not in next_dp or new_score > next_dp[new_cost]:
                    next_dp[new_cost] = new_score
                    hist[new_cost] = (cost, idx)

        if not next_dp:
            return None

        pruned: Dict[int, float] = {}
        running_best = float("-inf")
        for cost in sorted(next_dp):
            if next_dp[cost] > running_best:
                running_best = next_dp[cost]
                pruned[cost] = next_dp[cost]
        dp = dict(sorted(pruned.items())[:MAX_STATES])
        history.append(hist)

    states = sorted(dp.items(), key=lambda kv: (-kv[1], kv[0]))[:max_candidates]
    return states, history


def reconstruct(
    history: Sequence[Dict[int, Tuple[int, int]]],
    slots: Sequence[str],
    pools: Dict[str, List[Dict[str, Any]]],
    cost: int,
) -> List[Dict[str, Any]]:
    picks: List[Dict[str, Any]] = []
    current = cost
    for slot_index in range(len(slots) - 1, -1, -1):
        previous, item_index = history[slot_index][current]
        picks.append(pools[slots[slot_index]][item_index])
        current = previous
    picks.reverse()
    return picks


# --------------------------------------------------------------------------
# 优惠券
# --------------------------------------------------------------------------


def coupon_prefilter(
    coupon: Dict[str, Any],
    plan_items: Sequence[Dict[str, Any]],
    subtotal_fen: int,
    delivery_fen: int,
) -> Tuple[bool, str]:
    if coupon["scope"] == "delivery":
        if delivery_fen <= 0:
            return False, "需要外送订单"
        return True, ""
    if coupon["scope"] == "item":
        if not coupon["applicable_codes"]:
            return False, "券面未标明适用餐品"
        if not any(item["code"] in coupon["applicable_codes"] for item in plan_items):
            return False, "方案内无适用餐品"
        return True, ""
    if subtotal_fen < coupon["threshold_fen"]:
        return False, f"未满 {fmt_yuan(coupon['threshold_fen'])} 元门槛"
    return True, ""


def coupons_exclusive(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    excl_a, excl_b = set(a.get("excludes", [])), set(b.get("excludes", []))
    if "*" in excl_a or "*" in excl_b:
        return True
    return b["coupon_id"] in excl_a or a["coupon_id"] in excl_b


def combo_discount(
    combo: Sequence[Dict[str, Any]],
    plan_items: Sequence[Dict[str, Any]],
    subtotal_fen: int,
    delivery_fen: int,
) -> Tuple[Optional[int], List[Dict[str, Any]]]:
    """返回 (总优惠金额, 每张券的明细)。不可用时返回 (None, [])。"""
    discount = 0
    parts: List[Dict[str, Any]] = []
    used_item_codes: set = set()

    for coupon in combo:
        scope = coupon["scope"]
        if scope == "delivery":
            discount += delivery_fen
            parts.append({"name": coupon["name"], "amount_fen": delivery_fen, "note": "免配送费", "expires": coupon["expires"]})
        elif scope == "item":
            matched = [
                it
                for it in plan_items
                if it["code"] in coupon["applicable_codes"] and it["code"] not in used_item_codes
            ]
            if not matched:
                return None, []
            target = max(matched, key=lambda it: it["price_fen"])
            used_item_codes.add(target["code"])
            if coupon["kind"] == "item_free":
                amount = target["price_fen"]
                note = "免费兑换「{}」".format(target["name"])
            else:
                amount = min(coupon["value_fen"], target["price_fen"])
                note = "「{}」立减".format(target["name"])
            discount += amount
            parts.append({"name": coupon["name"], "amount_fen": amount, "note": note, "expires": coupon["expires"]})
        else:
            if subtotal_fen < coupon["threshold_fen"]:
                return None, []
            if coupon["kind"] == "percent":
                amount = subtotal_fen - int(math.floor(subtotal_fen * coupon["pay_ratio"]))
                if coupon.get("max_discount_fen"):
                    amount = min(amount, coupon["max_discount_fen"])
                amount = max(0, amount)
                note = "{:.1f} 折".format(coupon["pay_ratio"] * 10)
            else:
                amount = coupon["value_fen"]
                note = "满 {} 减 {}".format(fmt_yuan(coupon["threshold_fen"]), fmt_yuan(amount))
            discount += amount
            parts.append({"name": coupon["name"], "amount_fen": amount, "note": note, "expires": coupon["expires"]})

    discount = min(discount, subtotal_fen + delivery_fen)
    return discount, parts


def best_coupon_combo(
    coupons: Sequence[Dict[str, Any]],
    plan_items: Sequence[Dict[str, Any]],
    subtotal_fen: int,
    delivery_fen: int,
    max_total: int = 2,
    max_order: int = 1,
) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    unverifiable: List[Dict[str, Any]] = []
    for coupon in coupons:
        # 先把本地算不出确定优惠额的券挑出来。
        # 硬算它们只会得到「减免 0.00 元」这种虚假确定性的结论。
        if coupon.get("verifiable") is False:
            unverifiable.append(
                {
                    "name": coupon["name"],
                    "coupon_id": coupon["coupon_id"],
                    "kind": coupon.get("kind"),
                    "reason": coupon.get("unverifiable_reason", "无法在本地核价"),
                    "expires": coupon.get("expires", ""),
                    "channel_tags": coupon.get("channel_tags", []),
                }
            )
            continue
        ok, reason = coupon_prefilter(coupon, plan_items, subtotal_fen, delivery_fen)
        if ok:
            candidates.append(coupon)
        else:
            rejected.append({"name": coupon["name"], "coupon_id": coupon["coupon_id"], "reason": reason, "expires": coupon["expires"]})

    best_feasible: Optional[Dict[str, Any]] = None
    best_blocked: Optional[Dict[str, Any]] = None

    for size in range(1, min(max_total, len(candidates)) + 1):
        for combo in itertools.combinations(candidates, size):
            order_count = sum(1 for c in combo if c["scope"] == "order")
            conflict = None
            for a, b in itertools.combinations(combo, 2):
                if coupons_exclusive(a, b):
                    conflict = f"「{a['name']}」与「{b['name']}」互斥，不能同单使用"
                    break
            if conflict is None and order_count > max_order:
                conflict = f"同一订单最多使用 {max_order} 张订单级优惠券"

            amount, parts = combo_discount(combo, plan_items, subtotal_fen, delivery_fen)
            if amount is None:
                continue

            record = {
                "coupons": [c["coupon_id"] for c in combo],
                "names": [c["name"] for c in combo],
                "discount_fen": amount,
                "parts": parts,
                "detail": "；".join(
                    "{} {} 元".format(p["note"], fmt_yuan(p["amount_fen"])) for p in parts
                ),
                "expires": [c["expires"] for c in combo],
                "blocked_reason": conflict,
            }
            if conflict:
                if best_blocked is None or amount > best_blocked["discount_fen"]:
                    best_blocked = record
            else:
                if best_feasible is None or amount > best_feasible["discount_fen"] or (
                    amount == best_feasible["discount_fen"] and len(combo) < len(best_feasible["coupons"])
                ):
                    best_feasible = record

    if best_feasible is None:
        best_feasible = {
            "coupons": [],
            "names": [],
            "discount_fen": 0,
            "parts": [],
            "detail": "无可用优惠券组合",
            "expires": [],
            "blocked_reason": None,
        }

    conflict_note = None
    if best_blocked and best_blocked["discount_fen"] > best_feasible["discount_fen"] + 1e-9:
        conflict_note = (
            f"{best_blocked['blocked_reason']}，否则可多省 {fmt_yuan(best_blocked['discount_fen'] - best_feasible['discount_fen'])} 元"
        )

    return {
        "chosen": best_feasible,
        "blocked": best_blocked if conflict_note else None,
        "conflict_note": conflict_note,
        "unverifiable": unverifiable,
        "candidates": [
            {
                "coupon_id": c["coupon_id"],
                "name": c["name"],
                "kind": c["kind"],
                "scope": c["scope"],
                "threshold_yuan": round(c["threshold_fen"] / 100.0, 2),
                "expires": c["expires"],
            }
            for c in candidates
        ],
        "rejected": rejected,
    }


# --------------------------------------------------------------------------
# 方案装配
# --------------------------------------------------------------------------


def build_plan(
    label: str,
    items: Sequence[Dict[str, Any]],
    slots: Sequence[str],
    people: int,
    budget_fen: int,
    coupons: Sequence[Dict[str, Any]],
    delivery_fen: int,
    mode: str,
    pool: Sequence[Dict[str, Any]],
    max_total: int,
    max_order: int,
) -> Dict[str, Any]:
    subtotal = sum(item["price_fen"] for item in items)
    coupon_result = best_coupon_combo(coupons, items, subtotal, delivery_fen, max_total, max_order)
    discount = coupon_result["chosen"]["discount_fen"]
    total = subtotal - discount + delivery_fen

    totals = nutrition.sum_totals(items)
    pp = nutrition.totals_row(totals, people)
    checks = nutrition.check(mode, nutrition.per_person(totals, people))
    replacements = nutrition.suggest_replacements(items, pool, mode) if mode in ("slim", "muscle") else []

    # 营养覆盖度：把「未知」和「零」分开记账。
    # 真实门店菜单里营养表覆盖不全（实测 77%），若不区分就会输出
    # 「这份套餐 0 千卡」这种明显错误的结论。
    known = [item["name"] for item in items if has_known_nutrition(item)]
    unknown = [item["name"] for item in items if not has_known_nutrition(item)]
    coverage = {
        "total": len(items),
        "known": len(known),
        "unknown": len(unknown),
        "unknown_names": unknown,
        "complete": not unknown,
    }

    return {
        "label": label,
        "items": [
            {
                "code": item["code"],
                "name": item["name"],
                "category": item["category"],
                "price_yuan": round(item["price_fen"] / 100.0, 2),
                "price_fen": item["price_fen"],
                "is_high_fat": item["is_high_fat"],
                "tags": item["tags"],
                "nutrition": {k: round(v, 1) for k, v in item["nutrition"].items()},
                "nutrition_known": has_known_nutrition(item),
                "nutrition_match": item.get("nutrition_match"),
                # 券商品（「用券价格」券的可下单形态）需要把券信息带到报告里，
                # 否则用户看到的是一个没有 couponId 的普通餐品，无法下单。
                "is_coupon_deal": bool(item.get("is_coupon_deal")),
                "list_price_fen": int(item.get("list_price_fen") or 0),
                "coupon_id": item.get("coupon_id", ""),
                "coupon_code": item.get("coupon_code", ""),
                "virtual_product_code": item.get("virtual_product_code", ""),
                "portions": int(item.get("portions") or 1),
                "valid_until": item.get("valid_until", ""),
            }
            for item in items
        ],
        "structure": describe_slots(slots),
        "subtotal_fen": subtotal,
        "subtotal_yuan": round(subtotal / 100.0, 2),
        "coupon": coupon_result,
        "discount_fen": discount,
        "delivery_fen": delivery_fen,
        "total_fen": total,
        "total_yuan": round(total / 100.0, 2),
        "per_person_yuan": round(total / 100.0 / max(1, people), 2),
        "budget_fen": budget_fen,
        "budget_yuan": round(budget_fen / 100.0, 2),
        "remaining_fen": budget_fen - total,
        "remaining_yuan": round((budget_fen - total) / 100.0, 2),
        "within_budget": total <= budget_fen,
        "nutrition_total": {k: round(v, 1) for k, v in totals.items()},
        "nutrition_per_person": pp,
        "nutrition_coverage": coverage,
        "checks": checks,
        "checks_passed": all(c["ok"] for c in checks) if checks else True,
        "high_fat_items": [item["name"] for item in items if item["is_high_fat"]],
        "replacements": replacements,
        # 券商品：价格已经是券价，相对原价的差额就是这张券真实省下的钱
        "deal_count": sum(1 for item in items if item.get("is_coupon_deal")),
        "deal_savings_fen": sum(
            max(0, int(item.get("list_price_fen") or 0) - item["price_fen"])
            for item in items
            if item.get("is_coupon_deal")
        ),
        "_score": 0.0,
    }


def plan_signature(items: Sequence[Dict[str, Any]]) -> Tuple[str, ...]:
    return tuple(sorted(item["code"] for item in items))


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def load_unsellable(path: str) -> Dict[str, Any]:
    """读取 calculate-price 判定为不可售的商品清单（pricer.py 的产物）。

    实测（2026-10-16）：本地菜单快照里 available=true 的餐品，在服务端
    calculate-price 上仍可能返回「商品已售罄或不可售」。把这份清单回灌给
    优化器，就能避免反复推荐一个根本下不了单的组合。
    """
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    codes = {str(c) for c in (data.get("codes") or []) if str(c).strip()}
    return {"codes": codes, "store": str(data.get("store") or "")}


def run(args: argparse.Namespace) -> Dict[str, Any]:
    rules = load_category_rules(args.rules)
    menu = load_menu(args.menu, rules)

    unsellable_note = None
    if not getattr(args, "ignore_unsellable", False):
        blocked = load_unsellable(getattr(args, "unsellable", "") or DEFAULT_UNSELLABLE)
        codes = blocked.get("codes") or set()
        if codes:
            removed = [it for it in menu if str(it.get("code")) in codes]
            if removed:
                menu = [it for it in menu if str(it.get("code")) not in codes]
                unsellable_note = (
                    "已排除 {} 项服务端核价判定为**不可售**的餐品（门店 {}）：{}。"
                    "如需保留请加 --ignore-unsellable。".format(
                        len(removed),
                        blocked.get("store") or "未知",
                        "、".join(it.get("name", it.get("code", "")) for it in removed),
                    )
                )

    has_coupon_file = bool(args.coupons and os.path.isfile(args.coupons))
    coupons = load_coupons(args.coupons) if has_coupon_file else []
    deals = load_coupon_deals(args.coupons) if has_coupon_file else []
    # 「用券价格」券已以券商品（可下单条目）的形式参与组合优化，
    # 必须从「事后抵扣」券池中剔除，否则同一张券会被计算两次。
    deal_coupon_ids = {d.get("coupon_id") for d in deals if d.get("coupon_id")}
    combo_coupons = [c for c in coupons if c.get("coupon_id") not in deal_coupon_ids]

    scenarios = load_scenarios(args.scenarios)
    profile = scenarios.get(args.scenario or "", {}) if args.scenario else {}

    weights = dict(QUALITY_PRESETS.get(args.mode, QUALITY_PRESETS["value"]))
    weights.update(profile.get("quality_overrides", {}) or {})
    tag_bonus = profile.get("tag_bonus", {}) or {}

    people = max(1, int(args.people))
    budget_fen = to_fen(args.budget)

    scenario_slots = profile.get("slots")
    if scenario_slots:
        slots = expand_slots(profile, people)
    else:
        slots = ["MAIN"] * people + ["SIDE"] * people + ["DRINK"] * people

    available = [item for item in menu if item.get("available", True)] + list(deals)
    warnings: List[str] = []
    if unsellable_note:
        warnings.append(unsellable_note)
    if deals:
        warnings.append(
            "已把 {} 张「用券价格」券转为可直接下单的券商品参与组合（本单最多 {} 个）：{}".format(
                len(deals),
                getattr(args, "max_coupon_deals", 1),
                "、".join(
                    "{}（券价 {} 元{}）".format(
                        d["name"],
                        fmt_yuan(d["price_fen"]),
                        "，原价 {} 元".format(fmt_yuan(d["list_price_fen"])) if d.get("list_price_fen") else "",
                    )
                    for d in deals
                ),
            )
        )

    pools: Dict[str, List[Dict[str, Any]]] = {}
    for item in available:
        item["_quality"] = item_quality(item, weights, tag_bonus)
        pools.setdefault(item["category"], []).append(item)

    if args.strategy == "frugal":
        # 「同等满足度下最省」：把评分函数直接换成「越便宜越好」。
        # 满足度不靠单品分数卡（那样会把便宜的实惠款整批筛掉），
        # 而是用方案级的营养下限统一把关，见下面的 select_pool。
        for pool in pools.values():
            for item in pool:
                item["_quality"] = -float(item["price_fen"])

    used_slots: List[str] = []
    for category in slots:
        pool = pools.get(category, [])
        pruned = pool if args.strategy == "frugal" else pareto_prune(pool)
        if not pruned:
            warnings.append(f"没有可售的「{category}」品类餐品，已从结构中移除该槽位")
            continue
        pools[category] = pruned
        used_slots.append(category)
    slots = used_slots

    if not slots:
        return {"ok": False, "error": "菜单中没有可用于组餐的餐品，请检查菜单数据或品类映射规则"}

    def min_structure_cost(slot_list: Sequence[str]) -> int:
        return sum(min(item["price_fen"] for item in pools[c]) for c in slot_list)

    relaxed_note = None
    base_cost = min_structure_cost(slots) if slots else 0
    if slots and base_cost > budget_fen:
        chosen_level = None
        for level in relax_levels(slots, people):
            if level and min_structure_cost(level) <= budget_fen:
                chosen_level = level
                break
        if chosen_level is None:
            floor_cost = sum(min(item["price_fen"] for item in pools[c]) for c in slots)
            return {
                "ok": False,
                "error": "当前结构下没有任何组合能落在预算内",
                "hint": "结构 {} 的最低成本约 {} 元，超出预算 {} 元；即使每类只取最便宜的一款也需约 {} 元。建议提高预算或减少人数。".format(
                    describe_slots(slots),
                    fmt_yuan(base_cost),
                    fmt_yuan(max(0, base_cost - budget_fen)),
                    fmt_yuan(floor_cost),
                ),
            }
        relaxed_note = "原结构（{}）的最低成本约 {} 元，超出预算，已自动放宽为：{}".format(
            describe_slots(slots), fmt_yuan(base_cost), describe_slots(chosen_level)
        )
        slots = chosen_level

    prices = [item["price_fen"] for category in slots for item in pools.get(category, [])]
    unit = gcd_of(prices) or 1
    hard_ceiling = budget_fen + max(0, args.coupon_headroom_fen)
    cap_units = max(1, min(hard_ceiling // unit, 20000))

    for category in slots:
        for item in pools.get(category, []):
            item["_units"] = item["price_fen"] // unit

    all_items = [item for pool in pools.values() for item in pool]
    seen_signatures: set = set()

    def collect(solved_pair) -> List[Dict[str, Any]]:
        states, history = solved_pair
        collected: List[Dict[str, Any]] = []
        for cost, score in states:
            items = reconstruct(history, slots, pools, cost)
            signature = plan_signature(items)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            plan = build_plan(
                "候选",
                items,
                slots,
                people,
                budget_fen,
                combo_coupons,
                args.delivery_fee_fen,
                args.mode,
                all_items,
                args.max_coupons,
                args.max_order_coupons,
            )
            plan["_score"] = score
            plan["_items"] = items
            collected.append(plan)
        return collected

    solved = solve_plans(pools, slots, cap_units)
    if solved is None:
        return {"ok": False, "error": "预算内没能还原出可行方案，建议提高预算或简化结构"}
    plans = collect(solved)

    def unknown_count(plan: Dict[str, Any]) -> int:
        return int((plan.get("nutrition_coverage") or {}).get("unknown", 0) or 0)

    def rank_key(plan: Dict[str, Any]):
        # 先看营养硬约束是否达标，再看评分。评分函数是软目标（加权求和），
        # 无法保证每一条阈值都满足，所以达标性必须独立参与排序，不能只靠分数。
        # 同分时优先「营养未知项更少」的方案——可解释性也是质量的一部分。
        if args.strategy == "frugal":
            return (0 if plan["checks_passed"] else 1, unknown_count(plan), plan["total_fen"])
        return (
            0 if plan["checks_passed"] else 1,
            -plan["_score"],
            unknown_count(plan),
            plan["total_fen"],
        )

    def budget_pools(candidate_plans: List[Dict[str, Any]]):
        feasible_local = [p for p in candidate_plans if p["within_budget"]]
        if feasible_local:
            return feasible_local, "after_coupon"
        by_subtotal = [p for p in candidate_plans if p["subtotal_fen"] <= budget_fen]
        if by_subtotal:
            return by_subtotal, "before_coupon"
        return sorted(candidate_plans, key=lambda p: p["total_fen"]), "over_budget"

    def frugal_ok(plan: Dict[str, Any]) -> bool:
        """最省策略的满足度下限：每人至少 20g 蛋白、350 kcal，避免省成「只买最便宜的水」。"""
        pp = plan["nutrition_per_person"]
        return float(pp.get("protein_g", 0.0)) >= 20.0 and float(pp.get("energy_kcal", 0.0)) >= 350.0

    def deal_ok(plan: Dict[str, Any]) -> bool:
        """同一订单能用的券商品数量上限（麦当劳实际规则下一次通常只用一张券）。"""
        limit = getattr(args, "max_coupon_deals", 1)
        return limit < 0 or int(plan.get("deal_count", 0)) <= limit

    def select_pool(candidate_plans: List[Dict[str, Any]]):
        allowed = [p for p in candidate_plans if deal_ok(p)]
        if allowed:
            candidate_plans = allowed
        chosen_pool, mode = budget_pools(candidate_plans)
        if args.strategy == "frugal":
            strict = [p for p in chosen_pool if frugal_ok(p)]
            if strict:
                return strict, mode
        return chosen_pool, mode

    pool_for_rank, budget_mode = select_pool(plans)
    if budget_mode == "over_budget":
        warnings.append(
            "没有任何方案能做到券后价不超预算，以下为最接近预算的组合（最低 {} 元）".format(
                fmt_yuan(pool_for_rank[0]["total_fen"])
            )
        )

    ranked = sorted(pool_for_rank, key=rank_key)
    primary = ranked[0]
    primary["label"] = "主方案"
    primary["rationale"] = (
        "预算内综合评分最高的一套" if primary["checks_passed"] else "预算内评分最高，但营养阈值未全部达标"
    )

    # 第二遍求解：排除主方案里重复出现（若全都只出现一次则排除全部）的菜品再解一次，
    # 逼出一套结构上真正不同的组合，作为备选方案的来源。
    primary_counts = Counter(item["code"] for item in primary["_items"])
    exclude = {code for code, count in primary_counts.items() if count > 1} or set(primary_counts)
    solved_alt = solve_plans(pools, slots, cap_units, exclude_codes=exclude)
    if solved_alt is not None:
        extra = collect(solved_alt)
        if extra:
            pool_for_rank, budget_mode = select_pool(plans + extra)
            ranked = sorted(pool_for_rank, key=rank_key)

    def main_codes(plan: Dict[str, Any]) -> List[str]:
        return sorted(item["code"] for item in plan["items"] if item["category"] == "MAIN")

    primary_mains = main_codes(primary)
    primary_signature = plan_signature(primary["items"])

    alt_candidates = [p for p in ranked if p is not primary and main_codes(p) != primary_mains]
    if primary["checks_passed"]:
        alt_candidates = [p for p in alt_candidates if p["checks_passed"]] or alt_candidates
    if not alt_candidates:
        alt_candidates = [
            p for p in ranked if p is not primary and plan_signature(p["items"]) != primary_signature
        ]

    alternative = None
    if alt_candidates:
        alternative = min(alt_candidates, key=lambda p: p["total_fen"])
    elif len(ranked) > 1 and ranked[1] is not primary:
        alternative = ranked[1]

    if alternative is not None:
        alternative["label"] = "备选方案"
        delta = primary["total_fen"] - alternative["total_fen"]
        if delta > 0:
            alternative["rationale"] = "换一组主餐的做法，券后比主方案再省 {} 元".format(fmt_yuan(delta))
        elif delta < 0:
            alternative["rationale"] = "换一组主餐的做法，券后比主方案贵 {} 元".format(fmt_yuan(-delta))
        else:
            alternative["rationale"] = "换一组主餐的做法，券后价与主方案持平"

    cheapest = min(pool_for_rank, key=lambda p: p["total_fen"])

    if relaxed_note:
        warnings.insert(0, relaxed_note)

    if not primary["checks_passed"]:
        failed = [c["label"] for c in primary["checks"] if not c["ok"]]
        warnings.append(
            "预算内没有找到营养阈值全部达标的组合，主方案未满足："
            + "、".join(failed)
            + "。可提高预算、换用其他场景结构，或按下方替换建议自行调整。"
        )

    result = {
        "ok": True,
        "input": {
            "people": people,
            "budget_yuan": round(budget_fen / 100.0, 2),
            "mode": args.mode,
            "mode_label": MODE_LABELS.get(args.mode, args.mode),
            "scenario": args.scenario,
            "scenario_label": profile.get("label", args.scenario or "默认结构"),
            "strategy": args.strategy,
            "delivery_fee_yuan": round(args.delivery_fee_fen / 100.0, 2),
            "structure": describe_slots(slots),
        },
        "primary": primary,
        "alternative": alternative,
        "cheapest": {
            "label": "最省组合",
            "total_yuan": cheapest["total_yuan"],
            "per_person_yuan": cheapest["per_person_yuan"],
            "items": [item["name"] for item in cheapest["items"]],
            "same_as_primary": plan_signature(cheapest["items"]) == primary_signature,
        },
        "budget_mode": budget_mode,
        "structure_relaxed": bool(relaxed_note),
        "candidates_considered": len(seen_signatures),
        "warnings": warnings,
        "coupon_overview": {
            "total": len(coupons),
            "effective": len(primary["coupon"]["candidates"]),
            "as_deals": len(deals),
            "combo_pool": len(combo_coupons),
        },
        "coupon_deals": [
            {
                "name": d["name"],
                "price_yuan": round(d["price_fen"] / 100.0, 2),
                "list_price_yuan": round(d["list_price_fen"] / 100.0, 2),
                "category": d["category"],
                "portions": d["portions"],
                "coupon_id": d["coupon_id"],
                "coupon_code": d["coupon_code"],
                "virtual_product_code": d["virtual_product_code"],
                "valid_until": d["valid_until"],
            }
            for d in deals
        ],
    }
    for plan in (primary, alternative):
        if plan is not None:
            plan.pop("_score", None)
            plan.pop("_items", None)
    if alternative is not None and plan_signature(alternative["items"]) == plan_signature(primary["items"]):
        alternative = None
        result["alternative"] = None
    return result


# --------------------------------------------------------------------------
# 文本渲染
# --------------------------------------------------------------------------


def render_plan(plan: Dict[str, Any], people: int) -> str:
    lines: List[str] = []
    lines.append(f"### {plan['label']}｜券后 {plan['total_yuan']:.2f} 元（人均 {plan['per_person_yuan']:.2f} 元）")
    lines.append("")
    lines.append(f"结构：{plan['structure']}")
    if plan.get("rationale"):
        lines.append(f"说明：{plan['rationale']}")
    lines.append("")
    lines.append("| 餐品 | 品类 | 单价 | 营养（热量/蛋白/脂肪/碳水） |")
    lines.append("|---|---|---:|---|")
    labels = {"MAIN": "主餐", "SIDE": "小食", "DRINK": "饮品", "DESSERT": "甜品", "COMBO": "套餐", "OTHER": "其他", "BREAKFAST": "早餐"}
    grouped: List[List[Any]] = []
    index: Dict[str, int] = {}
    for item in plan["items"]:
        if item["code"] in index:
            grouped[index[item["code"]]][1] += 1
        else:
            index[item["code"]] = len(grouped)
            grouped.append([item, 1])

    for item, count in grouped:
        nut = item["nutrition"]
        if item.get("is_coupon_deal"):
            flag = " 🎟️券商品"
        else:
            flag = " ⚠️高油" if item["is_high_fat"] else ""
        name = item["name"] + (" ×{}".format(count) if count > 1 else "")
        # 营养「未知」必须显示为 —，不能显示 0——否则会读成「0 千卡」
        if item.get("nutrition_known", True):
            nut_cell = "{:.0f} / {:.0f} / {:.0f} / {:.0f}".format(
                nut.get("energy_kcal", 0),
                nut.get("protein_g", 0),
                nut.get("fat_g", 0),
                nut.get("carb_g", 0),
            )
        else:
            nut_cell = "—（无数据）"
        lines.append(
            "| {}{} | {} | {:.2f} | {} |".format(
                name,
                flag,
                labels.get(item["category"], item["category"]),
                item["price_yuan"],
                nut_cell,
            )
        )
    lines.append("")

    chosen = plan["coupon"]["chosen"]
    deal_items = [it for it in plan["items"] if it.get("is_coupon_deal")]
    if deal_items:
        for it in deal_items:
            saving = max(0, int(it.get("list_price_fen") or 0) - int(it.get("price_fen") or 0))
            detail = "- 🎟️ 券商品：{}｜券价 {:.2f} 元".format(it["name"], it["price_yuan"])
            if it.get("list_price_fen"):
                detail += "（原价 {:.2f} 元，省 {} 元）".format(
                    it["list_price_fen"] / 100.0, fmt_yuan(saving)
                )
            if it.get("valid_until"):
                detail += "，有效期至 {}".format(it["valid_until"])
            lines.append(detail)
            lines.append(
                "    - 下单需带：couponId={}｜couponCode={}".format(
                    it.get("coupon_id", ""), it.get("coupon_code", "")
                )
            )
        lines.append("")
    lines.append("- 优惠前：{:.2f} 元".format(plan["subtotal_yuan"]))
    parts = chosen.get("parts") or []
    if parts:
        for part in parts:
            line = "- 优惠券：{}｜减免 {} 元（{}）".format(part["name"], fmt_yuan(part["amount_fen"]), part["note"])
            if part.get("expires"):
                line += "，有效期至 " + part["expires"]
            lines.append(line)
    else:
        lines.append("- 优惠券：本方案没有可用优惠券组合")
    lines.append("- 优惠后：{:.2f} 元，共省 {} 元".format(plan["total_yuan"], fmt_yuan(plan["discount_fen"])))
    unverifiable = plan["coupon"].get("unverifiable") or []
    if unverifiable:
        lines.append("")
        lines.append(
            "- ⚠️ 另有 {} 张券**未计入**上面的优惠（本地无法确定优惠额，需在结算时由服务端核价）：".format(
                len(unverifiable)
            )
        )
        for item in unverifiable:
            line = "    - {}：{}".format(item["name"], item["reason"])
            if item.get("expires"):
                line += "（有效期 {}".format(item["expires"])
                if item.get("channel_tags"):
                    line += "，{}".format("、".join(item["channel_tags"]))
                line += "）"
            lines.append(line)
        lines.append("    建议：用 calculate-price 传入 couponId/couponCode 拿到权威价格，再决定用不用。")
    if plan["coupon"].get("conflict_note"):
        lines.append("- 互斥提醒：" + plan["coupon"]["conflict_note"])
    if plan["within_budget"]:
        lines.append("- 剩余预算：{:.2f} 元".format(plan["remaining_yuan"]))
    else:
        lines.append("- 超出预算：{:.2f} 元".format(-plan["remaining_yuan"]))
    lines.append("")

    total_nut, pp = plan["nutrition_total"], plan["nutrition_per_person"]
    cov = plan.get("nutrition_coverage") or {}
    scope = ""
    if cov and not cov.get("complete", True):
        scope = "（仅统计有数据的 {} / {} 项）".format(cov.get("known", 0), cov.get("total", 0))
    lines.append(
        f"- 整单营养{scope}：{total_nut.get('energy_kcal', 0):.0f} kcal｜蛋白 {total_nut.get('protein_g', 0):.0f}g｜"
        f"脂肪 {total_nut.get('fat_g', 0):.0f}g｜碳水 {total_nut.get('carb_g', 0):.0f}g"
    )
    lines.append(
        f"- 每人份：{pp.get('energy_kcal', 0):.0f} kcal｜蛋白 {pp.get('protein_g', 0):.0f}g｜"
        f"脂肪 {pp.get('fat_g', 0):.0f}g｜碳水 {pp.get('carb_g', 0):.0f}g｜"
        f"脂肪供能比 {pp.get('fat_energy_ratio', 0) * 100:.0f}%｜蛋白质密度 {pp.get('protein_density', 0):.0f} g/1000kcal"
    )
    if cov and not cov.get("complete", True):
        lines.append(
            "⚠️ 缺营养数据的餐品：{}——以上汇总**未计入**这些餐品，"
            "实际值会更高。需要精确值时请以门店公示或 calculate-price 返回为准。".format(
                "、".join(cov.get("unknown_names", []))
            )
        )
    if cov and cov.get("known", 0) == 0:
        lines.append("⚠️ 本方案餐品**全部**缺营养数据，因此营养汇总与达标判定均不成立，仅供参考价格。")
    if plan["checks"]:
        lines.append("")
        lines.append("**达标检查**")
        for check in plan["checks"]:
            lines.append(f"- {'✅' if check['ok'] else '❌'} {check['label']}：{check['detail']}")
    if plan["replacements"]:
        lines.append("")
        lines.append("**高油替换建议**")
        for rep in plan["replacements"]:
            delta_price = rep["price_delta_yuan"]
            price_text = f"+{delta_price:.2f}" if delta_price > 0 else f"{delta_price:.2f}"
            lines.append(
                f"- {rep['replace']} → {rep['with']}：脂肪 {rep['fat_delta_g']:+.0f}g，"
                f"蛋白 {rep['protein_delta_g']:+.0f}g，热量 {rep['kcal_delta']:+.0f} kcal，价格 {price_text} 元"
            )
    lines.append("")
    return "\n".join(lines)


def render_report(result: Dict[str, Any]) -> str:
    if not result.get("ok"):
        out = [f"❌ {result.get('error')}"]
        if result.get("hint"):
            out.append(result["hint"])
        return "\n".join(out)

    info = result["input"]
    mode_text = {
        "after_coupon": "以券后价为准",
        "before_coupon": "以优惠前价格为准",
        "over_budget": "无完全可行方案，展示最接近预算的",
    }.get(result["budget_mode"], result["budget_mode"])
    lines = [
        "## 配餐方案：{}｜{} 人｜预算 {:.2f} 元｜{}模式".format(
            info["scenario_label"], info["people"], info["budget_yuan"], info["mode_label"]
        ),
        "",
        "结构：{}（候选 {} 套，{}）".format(info["structure"], result["candidates_considered"], mode_text),
        "",
    ]
    for warning in result.get("warnings", []):
        lines.append("> ⚠️ " + warning)
    if result.get("warnings"):
        lines.append("")
    lines.append(render_plan(result["primary"], info["people"]))
    if result.get("alternative"):
        lines.append("---")
        lines.append("")
        lines.append(render_plan(result["alternative"], info["people"]))

    deals = result.get("coupon_deals") or []
    if deals:
        lines.append("### 🎟️ 可用券商品（价格已由 calculate-price 服务端核价）")
        lines.append("")
        lines.append("| 券商品 | 品类 | 券价 | 原价 | 省 | 有效期 |")
        lines.append("|---|---|---:|---:|---:|---|")
        for d in deals:
            saving = max(0.0, d["list_price_yuan"] - d["price_yuan"])
            lines.append(
                "| {} | {} | {:.2f} | {:.2f} | **{:.2f}** | {} |".format(
                    d["name"],
                    CATEGORY_LABELS.get(d["category"], d["category"]),
                    d["price_yuan"],
                    d["list_price_yuan"],
                    saving,
                    d.get("valid_until") or "—",
                )
            )
        used = {
            it["name"]
            for plan in (result.get("primary"), result.get("alternative"))
            if plan
            for it in plan["items"]
            if it.get("is_coupon_deal")
        }
        unused = [d for d in deals if d["name"] not in used]
        lines.append("")
        if used:
            lines.append("- ✅ 主/备选方案已使用：" + "、".join(sorted(used)))
        if unused:
            lines.append(
                "- ⚪ 未使用的券商品："
                + "、".join("{}（券价 {:.2f} 元，可省 {:.2f} 元）".format(
                    d["name"], d["price_yuan"], max(0.0, d["list_price_yuan"] - d["price_yuan"])
                ) for d in unused)
            )
            lines.append(
                "    - 未使用并非不可用：券商品会与同品类餐品一起参与组合优化，"
                "只是当前预算下它没有让整单更优。若你本来就想要这份商品，直接用券下单即可。"
            )
        lines.append("")

    cheapest = result["cheapest"]
    if cheapest["same_as_primary"]:
        lines.append("参考：最省的一套就是主方案本身，说明当前预算已经用在了刀刃上。")
    else:
        lines.append(
            "参考：最省组合为 "
            + "、".join(cheapest["items"])
            + "，券后 {:.2f} 元（人均 {:.2f} 元）。".format(cheapest["total_yuan"], cheapest["per_person_yuan"])
        )
    return "\n".join(lines)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="麦当劳预算最优配餐与优惠券组合比价")
    parser.add_argument("--menu", required=True, help="菜单 JSON（来自 MCP query-meals）")
    parser.add_argument("--coupons", default="", help="优惠券 JSON（来自 MCP query-store-coupons / query-my-coupons）")
    parser.add_argument("--people", type=int, default=1)
    parser.add_argument("--budget", default="50", help="预算上限（元）")
    parser.add_argument("--mode", default="value", choices=["value", "slim", "muscle"])
    parser.add_argument("--scenario", default="", help="场景 id：birthday / date / team / solo / family")
    parser.add_argument("--strategy", default="value", choices=["value", "frugal"], help="value=预算内吃到最好；frugal=同等满足度下最省")
    parser.add_argument("--delivery-fee", default="0", help="配送费（元），外送场景填写")
    parser.add_argument("--max-coupons", type=int, default=2, help="同单最多使用优惠券张数")
    parser.add_argument("--max-order-coupons", type=int, default=1, help="同单最多使用订单级优惠券张数")
    parser.add_argument(
        "--max-coupon-deals",
        type=int,
        default=1,
        help="同单最多使用几个券商品（「用券价格」券），-1 表示不限",
    )
    parser.add_argument("--coupon-headroom", default="15", help="允许原价高于预算多少元，以便优惠券把券后价压回预算内")
    parser.add_argument("--rules", default=DEFAULT_RULES)
    parser.add_argument("--scenarios", default=DEFAULT_SCENARIOS)
    parser.add_argument(
        "--unsellable",
        default=DEFAULT_UNSELLABLE,
        help="不可售商品清单（pricer.py 产物）；存在时自动排除这些餐品",
    )
    parser.add_argument("--ignore-unsellable", action="store_true", help="忽略不可售清单，保留全部餐品")
    parser.add_argument("--json", dest="json_out", default="", help="结果 JSON 输出路径")
    parser.add_argument("--format", default="both", choices=["text", "json", "both"])
    return parser


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()

    args.delivery_fee_fen = to_fen(args.delivery_fee)
    args.coupon_headroom_fen = to_fen(args.coupon_headroom)

    result = run(args)

    if args.json_out:
        dump_json(result, args.json_out)
    if args.format in ("text", "both"):
        print(render_report(result))
    if args.format == "json":
        print(dump_json(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
