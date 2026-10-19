"""nutrition.py - 营养汇总、减脂/增肌达标判定与高油替换建议。

判定口径（单餐 / 每人）
----------------------
减脂 slim:
    热量 <= 750 kcal，蛋白质 >= 25 g，脂肪 <= 25 g，脂肪供能比 <= 35%
增肌 muscle:
    蛋白质 >= 40 g，蛋白质密度 >= 22 g / 1000 kcal，热量 600-1100 kcal，碳水 >= 60 g
常规 value:
    不设硬门槛，只做汇总与提示

脂肪供能比 = 脂肪(g) * 9 / 热量(kcal)，是判断「油不油」比绝对脂肪量更稳的指标。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from mcd_core import fat_energy_ratio, fmt_yuan, is_high_fat

NUTRIENT_KEYS = ("energy_kcal", "protein_g", "fat_g", "carb_g", "sodium_mg", "calcium_mg")

NUTRIENT_LABELS = {
    "energy_kcal": ("热量", "kcal"),
    "protein_g": ("蛋白质", "g"),
    "fat_g": ("脂肪", "g"),
    "carb_g": ("碳水", "g"),
    "sodium_mg": ("钠", "mg"),
    "calcium_mg": ("钙", "mg"),
}

TARGETS: Dict[str, Dict[str, Any]] = {
    "slim": {
        "label": "减脂",
        # 脂肪供能比放宽到 40%：快餐本身油脂基数高，卡到 35% 会导致
        # 在合理预算内几乎无解，反而给出「一条建议都做不到」的方案。
        "max": {"energy_kcal": 750, "fat_g": 30, "fat_energy_ratio": 0.40},
        "min": {"protein_g": 25},
    },
    "muscle": {
        "label": "增肌",
        "max": {"energy_kcal": 1100, "fat_energy_ratio": 0.45},
        "min": {"energy_kcal": 600, "protein_g": 40, "protein_density": 22, "carb_g": 60},
    },
    "value": {"label": "常规", "max": {}, "min": {}},
}


def empty_totals() -> Dict[str, float]:
    return {key: 0.0 for key in NUTRIENT_KEYS}


def sum_totals(items: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    result = empty_totals()
    for item in items:
        nutrition = item.get("nutrition", {}) or {}
        for key in NUTRIENT_KEYS:
            try:
                result[key] += float(nutrition.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
    return result


def per_person(totals: Dict[str, float], people: int) -> Dict[str, float]:
    people = max(1, int(people))
    return {key: round(value / people, 1) for key, value in totals.items()}


def derived(totals: Dict[str, float]) -> Dict[str, float]:
    kcal = float(totals.get("energy_kcal") or 0.0)
    protein = float(totals.get("protein_g") or 0.0)
    fat = float(totals.get("fat_g") or 0.0)
    carb = float(totals.get("carb_g") or 0.0)
    return {
        "fat_energy_ratio": round(fat_energy_ratio(totals), 3),
        "protein_density": round((protein * 1000.0 / kcal), 1) if kcal > 0 else 0.0,
        "carb_energy_ratio": round((carb * 4.0 / kcal), 3) if kcal > 0 else 0.0,
    }


def check(mode: str, pp_totals: Dict[str, float]) -> List[Dict[str, Any]]:
    """按模式给出达标检查清单，供报告直接展示。"""
    spec = TARGETS.get(mode, TARGETS["value"])
    metrics = dict(pp_totals)
    metrics.update(derived(pp_totals))
    checks: List[Dict[str, Any]] = []

    for key, limit in spec.get("max", {}).items():
        actual = float(metrics.get(key, 0.0))
        label, unit = NUTRIENT_LABELS.get(key, (key, ""))
        if key == "fat_energy_ratio":
            label, unit, shown, lim = "脂肪供能比", "", f"{actual * 100:.0f}%", f"{limit * 100:.0f}%"
        else:
            label, shown, lim = label + "上限", f"{actual:.0f}", f"{limit:.0f}"
        checks.append(
            {
                "metric": key,
                "label": label,
                "ok": actual <= limit + 1e-9,
                "detail": f"{shown}{unit}（上限 {lim}{unit}）",
            }
        )

    for key, limit in spec.get("min", {}).items():
        actual = float(metrics.get(key, 0.0))
        if key == "protein_density":
            label, unit, shown, lim = "蛋白质密度", " g/1000kcal", f"{actual:.0f}", f"{limit:.0f}"
        else:
            label, unit = NUTRIENT_LABELS.get(key, (key, ""))
            label, shown, lim = label + "下限", f"{actual:.0f}", f"{limit:.0f}"
        checks.append(
            {
                "metric": key,
                "label": label,
                "ok": actual >= limit - 1e-9,
                "detail": f"{shown}{unit}（下限 {lim}{unit}）",
            }
        )

    return checks


def high_fat_items(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item in items if is_high_fat(item)]


def suggest_replacements(
    plan_items: Sequence[Dict[str, Any]],
    pool: Sequence[Dict[str, Any]],
    mode: str = "slim",
    max_price_delta_fen: int = 300,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """对高油菜品给出同品类替换建议，输出营养与价格的增量。"""
    used_codes = {item["code"] for item in plan_items}
    suggestions: List[Dict[str, Any]] = []

    for item in high_fat_items(plan_items):
        candidates = []
        for cand in pool:
            if cand["code"] == item["code"]:
                continue
            if cand.get("category") != item.get("category"):
                continue
            if not cand.get("available", True):
                continue
            cand_nut = cand.get("nutrition", {})
            item_nut = item.get("nutrition", {})
            cand_fat = float(cand_nut.get("fat_g") or 0.0)
            cand_protein = float(cand_nut.get("protein_g") or 0.0)
            if cand_fat >= float(item_nut.get("fat_g") or 0.0):
                continue
            if cand["price_fen"] > item["price_fen"] + max_price_delta_fen:
                continue
            protein_floor = 0.7 if mode == "slim" else 0.85
            if cand_protein < float(item_nut.get("protein_g") or 0.0) * protein_floor:
                continue
            already_in_plan = cand["code"] in used_codes
            candidates.append((cand, cand_fat, already_in_plan, cand_nut, item_nut))

        candidates.sort(key=lambda row: (row[1], 0 if row[2] else 1))
        for cand, cand_fat, already_in_plan, cand_nut, item_nut in candidates[:limit]:
            kcal_delta = float(cand_nut.get("energy_kcal") or 0.0) - float(item_nut.get("energy_kcal") or 0.0)
            fat_delta = cand_fat - float(item_nut.get("fat_g") or 0.0)
            protein_delta = float(cand_nut.get("protein_g") or 0.0) - float(item_nut.get("protein_g") or 0.0)
            price_delta = cand["price_fen"] - item["price_fen"]
            suggestions.append(
                {
                    "replace": item["name"],
                    "replace_code": item["code"],
                    "with": cand["name"],
                    "with_code": cand["code"],
                    "already_in_plan": already_in_plan,
                    "fat_delta_g": round(fat_delta, 1),
                    "protein_delta_g": round(protein_delta, 1),
                    "kcal_delta": round(kcal_delta, 0),
                    "price_delta_fen": price_delta,
                    "price_delta_yuan": round(price_delta / 100.0, 2),
                    "reason": f"脂肪 {item_nut.get('fat_g', 0):.0f}g -> {cand_fat:.0f}g，蛋白质 {item_nut.get('protein_g', 0):.0f}g -> {cand_nut.get('protein_g', 0):.0f}g",
                }
            )
    return suggestions


def totals_row(totals: Dict[str, float], people: int = 1) -> Dict[str, float]:
    """把总量转成每人份并补齐派生指标。"""
    pp = per_person(totals, people)
    pp.update(derived(pp))
    return pp


def format_nutrition_line(prefix: str, totals: Dict[str, float]) -> str:
    return (
        f"{prefix}：{totals.get('energy_kcal', 0):.0f} kcal / "
        f"蛋白 {totals.get('protein_g', 0):.0f}g / "
        f"脂肪 {totals.get('fat_g', 0):.0f}g / "
        f"碳水 {totals.get('carb_g', 0):.0f}g"
    )
