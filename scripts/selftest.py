"""selftest.py - 用脱敏样例数据跑通全部六项能力，并校验关键不变量。

不需要任何第三方依赖，也不需要 MCP 连通：
    python scripts/selftest.py

退出码 0 表示全部用例通过。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")

MENU = os.path.join(DATA, "sample_menu.json")
COUPONS = os.path.join(DATA, "sample_coupons.json")
STORES = os.path.join(DATA, "sample_stores.json")
ADDRESSES = os.path.join(DATA, "sample_addresses.json")

RESULTS: List[Tuple[str, bool, str]] = []


def run_json(script: str, args: List[str]) -> Dict[str, Any]:
    out_file = tempfile.mktemp(suffix=".json")
    cmd = [sys.executable, os.path.join(HERE, script)] + args + ["--format", "json", "--json", out_file]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if not os.path.isfile(out_file):
        raise AssertionError("脚本未产出 JSON。stderr:\n" + (proc.stderr or "")[-2000:])
    with open(out_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    os.remove(out_file)
    return data


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))


def optimize(args: List[str]) -> Dict[str, Any]:
    return run_json("optimize.py", args + ["--menu", MENU, "--coupons", COUPONS])


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------


def case_budget() -> None:
    res = optimize(["--people", "2", "--budget", "80", "--mode", "value"])
    check("能力1 生成方案", res.get("ok") is True)
    primary = res.get("primary", {})
    check("能力1 券后不超预算", primary.get("total_fen", 10 ** 9) <= primary.get("budget_fen", 0),
          "券后 {} / 预算 {}".format(primary.get("total_yuan"), primary.get("budget_yuan")))
    check("能力1 给出备选方案", res.get("alternative") is not None)
    if res.get("alternative"):
        check("能力1 备选与主方案不同",
              [i["code"] for i in res["alternative"]["items"]] != [i["code"] for i in primary["items"]])
    check("能力1 至少两套候选", res.get("candidates_considered", 0) >= 2,
          "候选 {} 套".format(res.get("candidates_considered")))


def case_coupon() -> None:
    res = optimize(["--people", "2", "--budget", "80", "--mode", "value"])
    primary = res.get("primary", {})
    coupon = primary.get("coupon", {}).get("chosen", {})
    check("能力2 使用了优惠券", coupon.get("discount_fen", 0) > 0,
          "优惠 {} 元".format(round(coupon.get("discount_fen", 0) / 100, 2)))
    check("能力2 券后价 = 原价 - 优惠",
          primary.get("total_fen") == primary.get("subtotal_fen", 0) - coupon.get("discount_fen", 0)
          + primary.get("delivery_fen", 0))
    check("能力2 标注了有效期", any(p.get("expires") for p in coupon.get("parts", [])))
    check("能力2 给出互斥提示", bool(res.get("primary", {}).get("coupon", {}).get("conflict_note")),
          res.get("primary", {}).get("coupon", {}).get("conflict_note") or "无")


def case_slim() -> None:
    res = optimize(["--people", "1", "--budget", "45", "--mode", "slim"])
    primary = res.get("primary", {})
    check("能力3 减脂模式可达标", primary.get("checks_passed") is True,
          "未达标：" + "、".join(c["label"] for c in primary.get("checks", []) if not c["ok"]))
    pp = primary.get("nutrition_per_person", {})
    check("能力3 每人份营养齐全",
          all(k in pp for k in ("energy_kcal", "protein_g", "fat_g", "carb_g", "fat_energy_ratio", "protein_density")))
    check("能力3 给出达标检查清单", len(primary.get("checks", [])) >= 3)

    res_m = optimize(["--people", "1", "--budget", "60", "--mode", "muscle"])
    pp_m = res_m.get("primary", {}).get("nutrition_per_person", {})
    check("能力3 增肌模式蛋白下限达成", pp_m.get("protein_g", 0) >= 40,
          "蛋白 {:.0f}g".format(pp_m.get("protein_g", 0)))


def case_scenario() -> None:
    res = optimize(["--people", "6", "--budget", "200", "--scenario", "birthday"])
    check("能力4 生日场景可运行", res.get("ok") is True)
    check("能力4 预算不足时自动放宽结构并说明",
          (not res.get("structure_relaxed")) or any("放宽" in w for w in res.get("warnings", [])),
          "；".join(res.get("warnings", [])) or "无需放宽")

    res_team = optimize(["--people", "8", "--budget", "300", "--scenario", "team"])
    check("能力4 团建场景可运行", res_team.get("ok") is True)

    res_date = optimize(["--people", "2", "--budget", "90", "--scenario", "date"])
    check("能力4 约会场景含甜品",
          any(i["category"] == "DESSERT" for i in res_date.get("primary", {}).get("items", [])),
          "餐品：" + "、".join(i["name"] for i in res_date.get("primary", {}).get("items", [])))


def case_store() -> None:
    res = run_json("analyze.py", ["--action", "delivery", "--stores", STORES, "--addresses", ADDRESSES, "--radius-km", "3"])
    check("能力5 输出配送结论", res.get("verdict") in ("可外送", "建议到店取餐", "信息不足"),
          str(res.get("verdict")))
    check("能力5 正确识别不支持外送的门店",
          any("五道口" in s["name"] for s in res.get("pickup_only", [])))
    check("能力5 正确识别超出半径的门店",
          any("超出" in s.get("reason", "") for s in res.get("pickup_only", [])) or res.get("radius_km", 0) >= 5.6)
    check("能力5 缺少外送字段时不硬下结论",
          any("西直门" in s["name"] for s in res.get("unknown", [])))


def case_analyze() -> None:
    status = run_json("analyze.py", ["--action", "status", "--menu", MENU])
    summary = status.get("summary", {})
    check("能力6 识别限时菜品", summary.get("limited", 0) >= 1, str(summary))
    check("能力6 识别已下架菜品", summary.get("off_sale", 0) >= 1, str(summary))
    check("能力6 识别即将下架菜品", summary.get("ending", 0) >= 1, str(summary))

    nut = run_json("analyze.py", ["--action", "nutrition", "--menu", MENU, "--codes", "M01,M10,D01", "--people", "1"])
    totals = nut.get("nutrition_total", {})
    expect_kcal = 550 + 340 + 150
    check("能力6 营养汇总求和正确", abs(totals.get("energy_kcal", 0) - expect_kcal) < 1e-6,
          "得到 {}，期望 {}".format(totals.get("energy_kcal"), expect_kcal))
    check("能力6 高油菜品被标出", len(nut.get("high_fat_items", [])) >= 1,
          "、".join(nut.get("high_fat_items", [])))


def case_frugal() -> None:
    value = optimize(["--people", "3", "--budget", "120", "--strategy", "value"])
    frugal = optimize(["--people", "3", "--budget", "120", "--strategy", "frugal"])
    check("附加 最省策略可用", frugal.get("ok") is True)
    check("附加 最省策略比常规更便宜",
          frugal.get("primary", {}).get("total_fen", 10 ** 9) <= value.get("primary", {}).get("total_fen", 0),
          "最省 {} 元 vs 常规 {} 元".format(
              frugal.get("primary", {}).get("total_yuan"), value.get("primary", {}).get("total_yuan")))


def case_budget_reject() -> None:
    res = optimize(["--people", "6", "--budget", "20", "--scenario", "birthday"])
    check("附加 预算过低时如实报错", res.get("ok") is False and bool(res.get("hint")),
          str(res.get("hint"))[:80])


# --------------------------------------------------------------------------
# 真实 MCP 数据回归
#
# 这一组用 _probe/ 下由真实 MCP 拉取的原始返回做回归，专防下面这些
# 「只有真实数据才会暴露」的错误再次出现。_probe/ 不存在时整组跳过。
# --------------------------------------------------------------------------

PROBE = os.path.join(ROOT, "_probe")


def _probe_ready() -> bool:
    need = ("meals_store.json", "nutrition.json", "coupons_store.json", "coupons_mine.json")
    return all(os.path.isfile(os.path.join(PROBE, n)) for n in need)


def case_real_payload_parsing() -> None:
    """三种真实返回形态都要能解析出结构。"""
    sys.path.insert(0, HERE)
    from mcp_payload import extract_json, parse_coupon_markdown, parse_nutrition_table

    # 形态 1：markdown 说明 + JSON 信封
    with open(os.path.join(PROBE, "meals_store.json"), "r", encoding="utf-8") as fh:
        text = fh.read()
    obj, prose = extract_json(text)
    check("真实 说明段落+JSON 可提取", isinstance(obj, dict) and obj.get("success") is True,
          "散文长度 {}".format(len(prose)))
    check("真实 JSON 提取后不含说明段", "# API Response Information" not in json.dumps(obj)[:200])

    # 形态 2：data 内嵌 CSV 字符串
    with open(os.path.join(PROBE, "nutrition.json"), "r", encoding="utf-8") as fh:
        nut_obj, _ = extract_json(fh.read())
    rows = parse_nutrition_table(nut_obj.get("data"))
    check("真实 营养 CSV 表可解析", len(rows) > 100, "{} 行".format(len(rows)))
    check("真实 营养字段名正确", rows and "energyKcal" in rows[0] and "carbohydrate" in rows[0],
          str(sorted(rows[0].keys()))[:90] if rows else "")

    # 形态 3：纯 markdown 散文
    with open(os.path.join(PROBE, "coupons_mine.json"), "r", encoding="utf-8") as fh:
        raw = fh.read()
    coupons = parse_coupon_markdown(raw)
    check("真实 券 markdown 可解析", len(coupons) >= 5, "{} 张".format(len(coupons)))
    with_pay = [c for c in coupons if c.get("pay_price_yuan")]
    check("真实 券能解析出用券价格", bool(with_pay),
          (with_pay[0]["name"] + " ¥" + with_pay[0]["pay_price_yuan"]) if with_pay else "")


def case_real_adapter() -> None:
    """适配层：字段映射、套餐展开、营养覆盖与分类正确性。"""
    sys.path.insert(0, HERE)
    from mcd_adapter import build_live, default_composition, parse_meal_detail

    result = build_live(PROBE)
    menu = result["menu"]
    by_name = {m["name"]: m for m in menu}

    check("真实 菜单有餐品", len(menu) > 50, "{} 项".format(len(menu)))

    # 分类修正：「薯条」曾被「小食甜品/其他」分区误判成甜品
    check("真实 薯条归为小食而非甜品", by_name.get("薯条", {}).get("category") == "SIDE",
          str(by_name.get("薯条", {}).get("category")))
    # 非食品周边必须剔出食品品类
    merch = [m for m in menu if "毛绒周边" in m["name"] or "痒痒挠" in m["name"]]
    check("真实 周边被识别为 MERCH", merch and all(m["category"] == "MERCH" for m in merch),
          "、".join(m["category"] for m in merch))
    # 套餐判定不能只看 MCP 分区
    check("真实 三件套归为套餐", by_name.get("麦辣鸡腿汉堡三件套", {}).get("category") == "COMBO",
          str(by_name.get("麦辣鸡腿汉堡三件套", {}).get("category")))

    # 套餐营养 = 默认子项求和，且必须与手算一致
    combo = by_name.get("麦辣鸡腿汉堡三件套", {})
    score = combo.get("nutrition", {}).get("energy_kcal")
    check("真实 套餐营养为子项求和", score == 921.0, "得到 {}（期望 921）".format(score))
    check("真实 套餐默认子项可追溯",
          combo.get("combo_default_items") == ["麦辣鸡腿汉堡", "中薯条", "可乐中杯"],
          str(combo.get("combo_default_items")))

    # 未知营养必须显式标记为 miss，不能留待被当成 0
    missing = [m for m in menu if m.get("nutrition_match") == "miss"]
    check("真实 无营养餐品被标为 miss", len(missing) > 0, "{} 项".format(len(missing)))
    check("真实 营养覆盖率合理",
          result["coverage"]["nutrition_ratio"] >= 0.6,
          "{:.0%}".format(result["coverage"]["nutrition_ratio"]))

    # 单品不能被误展开（单品 rounds 为空）
    ok = True
    for path in [os.path.join(PROBE, "details", f) for f in os.listdir(os.path.join(PROBE, "details"))][:5]:
        with open(path, "r", encoding="utf-8") as fh:
            from mcp_payload import extract_json as _ex

            obj, _ = _ex(fh.read())
        detail = parse_meal_detail(obj)
        if detail.get("rounds") and not default_composition(detail):
            ok = False
    check("真实 明细解析一致（有 rounds 必有默认子项）", ok)


def case_real_coupon_honesty() -> None:
    """券的诚实性：算不出来的券必须标记为不可核验，而不是报「减免 0 元」。"""
    sys.path.insert(0, HERE)
    from mcd_core import is_coupon_verifiable, load_coupons

    if not os.path.isfile(os.path.join(DATA, "live_coupons.json")):
        check("真实 券诚实性（需先跑 mcd_adapter）", False, "缺少 data/live_coupons.json")
        return
    coupons = load_coupons(os.path.join(DATA, "live_coupons.json"))
    check("真实 券加载", len(coupons) > 0, "{} 张".format(len(coupons)))
    unver = [c for c in coupons if not is_coupon_verifiable(c)]
    check("真实 用券价格型券被标记不可核验", len(unver) == len(coupons),
          "不可核验 {}/{}".format(len(unver), len(coupons)))
    check("真实 不可核验券带原因", all(c.get("unverifiable_reason") for c in unver))


def case_coupon_deals_and_pricer() -> None:
    """券商品与权威核价链路（2026-10-16 新增能力）。

    覆盖三件事：
      1. 「用券价格」券被正确翻译成可下单的券商品（价格=券价、营养=单品×份数）
      2. 核价请求把券商品的 couponId/couponCode 带上（否则服务端折不出券价）
      3. calculate-price 判定不可售的商品会被优化器排除
    """
    sys.path.insert(0, HERE)
    import optimize
    import pricer
    from mcd_core import load_coupon_deals, has_known_nutrition

    coupon_path = os.path.join(DATA, "live_coupons.json")
    if not os.path.isfile(coupon_path):
        check("真实 券商品（需先跑 mcd_adapter）", False, "缺少 data/live_coupons.json")
        return

    deals = load_coupon_deals(coupon_path)
    check("真实 券商品可加载", len(deals) > 0, "{} 个".format(len(deals)))
    if not deals:
        return

    # 券虚拟码与菜单码不在同一命名空间，但**前缀会重叠**：实测该门店 101 项菜单里
    # 有 54 项是 99 开头（三件套、分享餐、小食组合都在内）。所以「是不是券」只能看
    # is_coupon_deal 标记或「菜单里查不到」，绝不能用编码前缀判断。
    from mcd_core import load_menu

    menu_path = os.path.join(DATA, "live_menu.json")
    if os.path.isfile(menu_path):
        menu_rows = load_menu(menu_path)
        n99 = [row for row in menu_rows if str(row.get("code", "")).startswith("99")]
        check(
            "真实 菜单里也有 99 开头商品（券不能用前缀判定）",
            len(n99) > 0,
            "{} / {} 项菜单以 99 开头".format(len(n99), len(menu_rows)),
        )

    bogo = next((d for d in deals if "买一送一" in d["name"]), None)
    check("真实 买一送一券商品被识别", bogo is not None)
    if bogo:
        check("真实 券商品价格为券价", bogo["price_fen"] == 1350, "{} 分".format(bogo["price_fen"]))
        check("真实 券商品带 couponId/couponCode",
              bool(bogo["coupon_id"]) and bool(bogo["coupon_code"]))
        check("真实 券商品份数解析", bogo["portions"] == 2, "份数 {}".format(bogo["portions"]))
        # 「中薯条买一送一」的营养应为单份中薯条的 2 倍
        check("真实 券商品营养按份数折算",
              abs(float(bogo["nutrition"].get("energy_kcal", 0)) - 578.0) < 1.0,
              "{:.0f} kcal".format(bogo["nutrition"].get("energy_kcal", 0)))
        check("真实 有营养的券商品被判为已知",
              has_known_nutrition(bogo) is True)

    # 「未知 ≠ 零」在券商品上同样必须成立：
    # 查不到营养的券商品要留空字典、判为未知，绝不能被补成 0 而冒充「已知」。
    miss_deals = [d for d in deals if not has_known_nutrition(d)]
    check("真实 查不到营养的券商品判为未知", len(miss_deals) >= 1,
          "{} 个".format(len(miss_deals)))
    for d in miss_deals:
        check("真实 未知券商品营养字典为空（不补 0：{}）".format(d["name"]),
              not d["nutrition"],
              str(d["nutrition"]))

    # 核价请求必须带上券字段
    plan = {
        "items": [
            {"code": "1100", "name": "巨无霸", "is_coupon_deal": False},
            {"code": "1100", "name": "巨无霸", "is_coupon_deal": False},
            {
                "name": "中薯条买一送一",
                "is_coupon_deal": True,
                "virtual_product_code": "9900012387",
                "coupon_id": "CID",
                "coupon_code": "CCODE",
            },
        ]
    }
    api_items = pricer.build_api_items(plan)
    check("核价 同码餐品被聚合", api_items[0] == {"productCode": "1100", "quantity": 2},
          str(api_items[0]))
    check("核价 券商品带 couponId/couponCode",
          api_items[1].get("couponId") == "CID" and api_items[1].get("couponCode") == "CCODE")

    # 不可售清单回灌
    unsellable_path = os.path.join(DATA, "unsellable.json")
    blocked = optimize.load_unsellable(unsellable_path)
    if os.path.isfile(unsellable_path):
        check("真实 不可售清单可读", isinstance(blocked.get("codes"), set),
              "{} 项".format(len(blocked.get("codes") or [])))
    else:
        check("真实 不可售清单（可选，未生成时跳过）", True, "未生成")


def case_multi_store() -> None:
    """多门店（可视化面板「选餐厅」的数据基础）。

    要点：每家的菜单/券必须各存一份且互不串用，不可售清单只能作用于它自己那家门店。
    """
    sys.path.insert(0, HERE)
    import build_ui
    import optimize  # 局部绑定成模块；模块级的 optimize 是本文件里的辅助函数

    index_path = os.path.join(ROOT, "data", "live_stores", "index.json")
    if not os.path.isfile(index_path):
        check("多店 门店索引（可选，未跑多店取数时跳过）", True, "未生成")
        return

    with open(index_path, encoding="utf-8") as fh:
        rows = json.load(fh)
    check("多店 门店索引可读", len(rows) >= 2, "{} 家".format(len(rows)))

    stores = build_ui.load_stores()
    check("多店 门店清单可加载", len(stores) == len(rows), "{} 家".format(len(stores)))

    codes = [str(r["code"]) for r in rows]

    # 门店目录必须各自独立——都落到同一个目录就说明取数串了店
    dirs = {c: build_ui.store_dir(c) for c in codes}
    check("多店 各店目录相互独立", len(set(dirs.values())) == len(codes),
          "{} 个独立目录".format(len(set(dirs.values()))))

    missing = [c for c in codes
               if not os.path.isfile(os.path.join(build_ui.store_dir(c), "live_menu.json"))]
    check("多店 每店都有独立菜单", not missing, "缺失 {}".format(missing) if missing else "{} 家全部就绪".format(len(codes)))

    # 不同门店的菜单内容确实不同（用编码集合比，避免只是数量巧合）
    sigs = {}
    for c in codes:
        p = os.path.join(build_ui.store_dir(c), "live_menu.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                sigs[c] = frozenset(str(it.get("code")) for it in json.load(fh))
    check("多店 各店菜单内容不一致（确实按店取了数）", len(set(sigs.values())) > 1,
          "{} 种不同菜单".format(len(set(sigs.values()))))

    # 不可售清单按门店隔离：没记录 store 的门店不能拿到别人的清单
    leaked = []
    for c in codes:
        path = build_ui.unsellable_for(c)
        if os.path.basename(path) == "__none__.json":
            continue
        with open(path, encoding="utf-8") as fh:
            owner = str((json.load(fh) or {}).get("store") or "")
        if owner != c:
            leaked.append(c)
    check("多店 不可售清单不跨店串用", not leaked, "越界 {}".format(leaked) if leaked else "无越界")

    # 面板生成器能按店求解出结果（证明网格确实按门店分桶）
    res = optimize.run(build_ui.make_args(2, 80, "value", store=codes[0]))
    check("多店 按门店求解可运行", bool(res.get("ok")),
          "{} 主方案 ¥{}".format(codes[0], (res.get("primary") or {}).get("total_yuan")))


# --------------------------------------------------------------------------


def case_order_dry_run() -> None:
    """下单演练：结构上保证不加 --confirm 就不会创建订单（全程离线，不打桩网络）。"""
    sys.path.insert(0, HERE)
    import order as order_mod

    ways = [
        {"code": "eat-in", "title": "堂食", "subtitle": "店内用餐"},
        {"code": "take-in-store", "title": "外带", "subtitle": "店内自提"},
    ]

    # 1) 取餐方式绝不猜——选错等于下错单，这种决定不该由脚本代做
    code, errs = order_mod.pick_take_way(ways, "")
    check("下单 多取餐方式必须显式指定", code == "" and bool(errs), "未指定时拒绝并列出选项")

    code, errs = order_mod.pick_take_way(ways, "eat-in")
    check("下单 合法取餐方式被采纳", code == "eat-in" and not errs, code or "无")

    code, errs = order_mod.pick_take_way(ways, "dine-in")
    check("下单 不在列表里的取餐方式被拒", code == "" and bool(errs), "dine-in 应被拒")

    code, errs = order_mod.pick_take_way([{"code": "eat-in", "title": "堂食"}], "")
    check("下单 仅一种取餐方式时自动采用", code == "eat-in" and not errs, code or "无")

    # 2) 请求体按渠道取舍（到店传 takeWayCode，外送传 addressId，两者不混用）
    items = [{"productCode": "1406", "quantity": 2}]
    pickup = order_mod.build_order_request("TEST_STORE", 1, 1, items, take_way_code="eat-in")
    check(
        "下单 到店请求带 takeWayCode 不带 addressId",
        pickup.get("takeWayCode") == "eat-in" and "addressId" not in pickup,
        json.dumps(pickup, ensure_ascii=False),
    )

    delivery = order_mod.build_order_request(
        "TEST_STORE", 2, 2, items, take_way_code="eat-in", address_id="ADDR1", be_code="BE1"
    )
    check(
        "下单 外送请求带 addressId 不带 takeWayCode",
        delivery.get("addressId") == "ADDR1" and "takeWayCode" not in delivery,
        json.dumps(delivery, ensure_ascii=False),
    )

    long_remark = "备" * 80
    trimmed = order_mod.build_order_request("TEST_STORE", 2, 2, items, remark=long_remark)
    check("下单 备注被截断到 50 字", len(trimmed.get("remark") or "") == 50, "{} 字".format(len(trimmed.get("remark") or "")))

    # 3) 返回体解析不臆造：找不到就报空，非 http 值不算支付链接
    check(
        "下单 空返回不编造订单号",
        order_mod.find_first_value({}, order_mod._ORDER_ID_KEYS) == "",
        "空",
    )
    nested = {"data": {"order": {"orderId": "123", "payUrl": "https://pay.example/x"}}}
    check(
        "下单 能从嵌套返回里找到订单号与支付链接",
        order_mod.find_first_value(nested, order_mod._ORDER_ID_KEYS) == "123"
        and order_mod.find_first_value(nested, order_mod._PAY_URL_KEYS, require_prefix="http") == "https://pay.example/x",
        "orderId=123",
    )
    check(
        "下单 非 http 值不会被当成支付链接",
        order_mod.find_first_value({"data": {"payUrl": "not-a-url"}}, order_mod._PAY_URL_KEYS, require_prefix="http") == "",
        "已过滤",
    )

    # 4) 结构保证：演练分支的提前返回必须出现在 create-order 调用之前
    with open(os.path.join(HERE, "order.py"), encoding="utf-8") as fh:
        src = fh.read()
    i_guard = src.find("if not args.confirm:")
    i_submit = src.find("data = submit_order(token, request")
    check(
        "下单 演练分支早于 create-order 调用（默认必然不下单）",
        0 <= i_guard < i_submit,
        "guard@{} submit@{}".format(i_guard, i_submit),
    )


def main() -> int:
    cases = [
        ("预算最优配餐", case_budget),
        ("优惠券组合比价", case_coupon),
        ("营养配餐推荐", case_slim),
        ("场景套餐推荐", case_scenario),
        ("门店与配送", case_store),
        ("菜品分析", case_analyze),
        ("最省策略", case_frugal),
        ("预算拒绝路径", case_budget_reject),
        ("下单演练（不下单）", case_order_dry_run),
    ]
    if _probe_ready():
        cases += [
            ("真实返回形态解析", case_real_payload_parsing),
            ("真实数据适配", case_real_adapter),
            ("真实券诚实性", case_real_coupon_honesty),
            ("券商品与权威核价", case_coupon_deals_and_pricer),
            ("多门店与门店隔离", case_multi_store),
        ]
    else:
        print("（跳过真实数据回归：_probe/ 不存在，先跑 scripts/dump_mcp.py）\n")

    for name, fn in cases:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check("{} 执行异常".format(name), False, "{}: {}".format(type(exc).__name__, exc))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    for name, ok, detail in RESULTS:
        line = "  {} {}".format("PASS" if ok else "FAIL", name)
        if detail:
            line += "  <- " + detail
        print(line)
    print("\n{}/{} 通过".format(passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
