#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键把真实门店数据取回并翻译成本 Skill 的内部结构。

流程:
    1. 拉取只读数据（菜单 / 营养表 / 券 / 门店 / 地址 / 积分）
    2. 对疑似套餐逐个取 query-meal-detail，用于展开子项算营养
    3. 对每张「用券价格」券调 calculate-price，拿服务端权威的券后价
    4. 调 mcd_adapter 翻译成内部结构并落盘

用法:
    # 单店（就近搜索后取最近的一家）
    python refresh_live.py --city <城市> --keyword <地标关键词>
    python refresh_live.py --store <门店编码>            # 已知门店编码时跳过搜索

    # 多店（可视化面板要「选餐厅」，所以把附近几家一起取回来）
    python refresh_live.py --city <城市> --keyword <地标关键词> --all-nearby
    python refresh_live.py --stores <编码1>,<编码2>,<编码3>
    python refresh_live.py --all-nearby --store-limit 5

产物:
    单店模式 -> _probe/*.json                        + data/live_*.json      （旧行为不变）
    多店模式 -> _probe/stores/<code>/*               + data/live_stores/<code>/*
              另有 data/live_stores/index.json 记录门店清单；
              并把「最近的一家」镜像到 _probe/*.json 与 data/live_*.json，
              保证旧脚本与自测仍按原路径跑通。

营养表、个人券、账号、地址、活动日历是**全店通用**的，多店模式下只取一次并共享。
仅标准库。需要 ~/.workbuddy/mcp.json 里已配置 mcd-mcp 的 Bearer Token。
"""
import argparse
import io
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mcd_adapter import build_live, looks_like_combo, parse_menu, render_report, write_live  # noqa: E402
from mcd_core import fmt_yuan  # noqa: E402
from mcp_payload import load_tool_text  # noqa: E402
from probe_mcp import load_token, rpc, unwrap  # noqa: E402

ROOT = os.path.dirname(HERE)
PROBE = os.path.join(ROOT, "_probe")
STORES_PROBE = os.path.join(PROBE, "stores")
LIVE_ROOT = os.path.join(ROOT, "data", "live_stores")

# 与门店无关的只读数据：多店模式下取一次即可
SHARED_FILES = ("nutrition.json", "coupons_mine.json", "coupons_avail.json", "account.json",
                "addresses.json", "campaign.json")
# 与门店强绑定的数据：每家门店各存一份
PER_STORE_FILES = ("meals_store.json", "coupons_store.json", "coupon_pricing.json")


def call(token, tool, args, rid):
    """调用一次 MCP 工具并取回结构化载荷。返回 (obj, text)。"""
    payload = rpc(token, "tools/call", {"name": tool, "arguments": args}, rid=rid)
    data = unwrap(payload)
    if isinstance(data, (dict, list)):
        return data, json.dumps(data, ensure_ascii=False)
    from mcp_payload import extract_json

    obj, prose = extract_json(str(data))
    return (obj if obj is not None else {}), str(data)


def save(path, obj_or_text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as fh:
        if isinstance(obj_or_text, (dict, list)):
            json.dump(obj_or_text, fh, ensure_ascii=False, indent=1)
        else:
            fh.write(str(obj_or_text))


def read_store_list():
    """读 _probe/stores.json，返回门店行列表。"""
    path = os.path.join(PROBE, "stores.json")
    if not os.path.isfile(path):
        return []
    obj, _ = load_tool_text(path)
    rows = obj.get("data") if isinstance(obj, dict) else None
    return [r for r in (rows or []) if isinstance(r, dict) and r.get("storeCode")]


def search_stores(token, city, keyword, be_type, next_id):
    """按城市/关键词搜门店，落盘 _probe/stores.json。"""
    sq = {"beType": be_type, "searchType": 2}
    if city:
        sq["city"] = city
    if keyword:
        sq["keyword"] = keyword
    obj, text = call(token, "query-nearby-stores", sq, next_id())
    save(os.path.join(PROBE, "stores.json"), text)
    rows = obj.get("data") if isinstance(obj, dict) else None
    return [r for r in (rows or []) if isinstance(r, dict) and r.get("storeCode")]


def fetch_shared(token, next_id):
    """取与门店无关的只读数据（营养表 / 个人券 / 账号 / 地址 / 活动）。"""
    print("[共享] 与门店无关的只读数据")
    plan = [
        ("nutrition", "list-nutrition-foods", {}),
        ("coupons_mine", "query-my-coupons", {"page": "1", "pageSize": "200"}),
        ("coupons_avail", "available-coupons", {}),
        ("account", "query-my-account", {}),
        ("addresses", "delivery-query-addresses", {}),
        ("campaign", "campaign-calendar", {}),
    ]
    for label, tool, targs in plan:
        try:
            _, text = call(token, tool, targs, next_id())
            save(os.path.join(PROBE, label + ".json"), text)
            print("  ok  {:<16s} {}".format(label, tool))
        except Exception as exc:  # noqa: BLE001
            print("  失败 {:<15s} {} -> {}".format(label, tool, repr(exc)[:70]))


def fetch_store_readonly(token, store_code, order_type, be_type, probe_dir, next_id):
    """取该门店的菜单与门店券。"""
    plan = [
        ("meals_store", "query-meals", {"storeCode": store_code, "orderType": order_type, "beType": be_type}),
        ("coupons_store", "query-store-coupons", {"storeCode": store_code, "orderType": order_type, "beType": be_type}),
    ]
    ok = True
    for label, tool, targs in plan:
        try:
            _, text = call(token, tool, targs, next_id())
            save(os.path.join(probe_dir, label + ".json"), text)
            print("  ok  {:<16s} {}".format(label, tool))
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  失败 {:<15s} {} -> {}".format(label, tool, repr(exc)[:70]))
    return ok


def price_coupons(token, store_code, order_type, be_type, probe_dir, next_id):
    """券核价：把券商品交给 calculate-price，拿服务端权威的原价与券后价。

    这一步是「券后价闭环」的关键——券的商品编码与菜单编码不在同一命名空间，
    只有服务端知道一张券到底折掉多少钱。
    """
    pricing = {}
    sc_path = os.path.join(probe_dir, "coupons_store.json")
    if not os.path.isfile(sc_path):
        return pricing
    sc_obj, _ = load_tool_text(sc_path)
    rows = sc_obj.get("data") if isinstance(sc_obj, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        prods = row.get("products") or []
        if not prods:
            continue
        vcode = str(prods[0].get("productCode") or "")
        cid = str(row.get("couponId") or "")
        ccode = str(row.get("couponCode") or "")
        if not (vcode and cid):
            continue
        try:
            po, _t = call(
                token,
                "calculate-price",
                {
                    "storeCode": store_code,
                    "orderType": order_type,
                    "beType": be_type,
                    "items": [{"productCode": vcode, "quantity": 1, "couponId": cid, "couponCode": ccode}],
                },
                next_id(),
            )
        except Exception as exc:  # noqa: BLE001
            print("  核价失败 {:<12s} {}".format(str(row.get("title"))[:12], repr(exc)[:60]))
            continue
        data = po.get("data") if isinstance(po, dict) else None
        if not isinstance(data, dict):
            continue
        pricing[cid] = {
            "title": row.get("title"),
            "list_price_fen": int(data.get("originalPrice") or 0),
            "pay_price_fen": int(data.get("price") or 0),
            "discount_fen": int(data.get("discount") or 0),
        }
        print(
            "  核价 {:<14s} 原价 {:>7s} / 券价 {:>7s}（省 {}）".format(
                str(row.get("title"))[:14],
                fmt_yuan(pricing[cid]["list_price_fen"]),
                fmt_yuan(pricing[cid]["pay_price_fen"]),
                fmt_yuan(pricing[cid]["discount_fen"]),
            )
        )
    save(os.path.join(probe_dir, "coupon_pricing.json"), pricing)
    return pricing


def fetch_details(token, store_code, order_type, be_type, probe_dir, next_id, limit):
    """抓套餐明细，用于展开默认子项算营养（已抓过的跳过）。"""
    obj, _ = load_tool_text(os.path.join(probe_dir, "meals_store.json"))
    items = parse_menu(obj) if obj else []
    combos = [it for it in items if looks_like_combo(it) and it["price_fen"] > 0]
    combos = sorted(combos, key=lambda x: -x["price_fen"])[:limit]
    detail_dir = os.path.join(probe_dir, "details")
    os.makedirs(detail_dir, exist_ok=True)
    done = 0
    for it in combos:
        path = os.path.join(detail_dir, "{}_detail.json".format(it["code"]))
        if os.path.isfile(path):
            done += 1
            continue
        try:
            _, text = call(
                token,
                "query-meal-detail",
                {"storeCode": store_code, "orderType": order_type, "beType": be_type, "code": it["code"]},
                next_id(),
            )
            save(path, text)
            done += 1
        except Exception as exc:  # noqa: BLE001
            print("  明细失败 {} -> {}".format(it["code"], repr(exc)[:60]))
        time.sleep(0.05)
    print("  套餐明细：{} / {}".format(done, len(combos)))
    return combos


def mirror_primary(store_code):
    """把最近一家的数据镜像到旧的扁平路径，保证旧脚本/自测仍能跑。"""
    src_probe = os.path.join(STORES_PROBE, store_code)
    for name in PER_STORE_FILES:
        s = os.path.join(src_probe, name)
        if os.path.isfile(s):
            shutil.copyfile(s, os.path.join(PROBE, name))
    src_details = os.path.join(src_probe, "details")
    dst_details = os.path.join(PROBE, "details")
    if os.path.isdir(src_details):
        os.makedirs(dst_details, exist_ok=True)
        for f in os.listdir(src_details):
            s = os.path.join(src_details, f)
            if os.path.isfile(s):
                shutil.copyfile(s, os.path.join(dst_details, f))
    src_live = os.path.join(LIVE_ROOT, store_code)
    for name in ("live_menu.json", "live_coupons.json", "live_coverage.json"):
        s = os.path.join(src_live, name)
        if os.path.isfile(s):
            shutil.copyfile(s, os.path.join(ROOT, "data", name))


def main():
    ap = argparse.ArgumentParser(description="刷新真实门店数据")
    ap.add_argument("--city", default="")
    ap.add_argument("--keyword", default="")
    ap.add_argument("--store", default="", help="已知门店编码；给了就直接跳过门店搜索")
    ap.add_argument("--stores", default="", help="多店模式：逗号分隔的门店编码，如 <编码1>,<编码2>")
    ap.add_argument("--all-nearby", action="store_true", help="多店模式：取 stores.json 里的全部门店")
    ap.add_argument("--store-limit", type=int, default=0, help="多店模式最多取几家（0=不限）")
    ap.add_argument("--be-type", type=int, default=1, help="1-到店 2-外送 5-得来速 6-团餐")
    ap.add_argument("--order-type", type=int, default=1, help="1-到店 2-外送")
    ap.add_argument("--details-limit", type=int, default=80, help="每家最多抓多少个套餐明细")
    ap.add_argument("--skip-details", action="store_true")
    args = ap.parse_args()

    token = load_token()
    rid = [100]

    def next_id():
        rid[0] += 1
        return rid[0]

    os.makedirs(PROBE, exist_ok=True)
    order_type, be_type = args.order_type, args.be_type

    # ---- 门店清单 ----
    explicit = [c.strip() for c in args.stores.split(",") if c.strip()]
    multi = bool(explicit) or args.all_nearby

    if multi:
        if explicit:
            rows = [{"storeCode": c} for c in explicit]
            if not os.path.isfile(os.path.join(PROBE, "stores.json")):
                save(os.path.join(PROBE, "stores.json"), {"data": rows})
        else:
            rows = read_store_list()
            if not rows:
                if not (args.city or args.keyword):
                    print("多店模式需要 _probe/stores.json，或提供 --city / --keyword 先搜门店", file=sys.stderr)
                    return 2
                rows = search_stores(token, args.city, args.keyword, be_type, next_id)
        if args.store_limit > 0:
            rows = rows[: args.store_limit]
        if not rows:
            print("未找到任何门店", file=sys.stderr)
            return 1
        codes = [str(r.get("storeCode")) for r in rows]
        print("多店模式：共 {} 家 -> {}".format(len(codes), "、".join(codes)))
        print("")
        fetch_shared(token, next_id)
        print("")

        os.makedirs(LIVE_ROOT, exist_ok=True)
        index = []
        results = {}
        for i, row in enumerate(rows):
            code = str(row.get("storeCode"))
            name = row.get("storeName") or code
            print("[{}/{}] {}（{}）".format(i + 1, len(codes), name, code))
            probe_dir = os.path.join(STORES_PROBE, code)
            os.makedirs(probe_dir, exist_ok=True)
            if not fetch_store_readonly(token, code, order_type, be_type, probe_dir, next_id):
                print("  跳过（菜单或券拉取失败）")
                print("")
                continue
            price_coupons(token, code, order_type, be_type, probe_dir, next_id)
            combos = []
            if not args.skip_details:
                combos = fetch_details(
                    token, code, order_type, be_type, probe_dir, next_id, args.details_limit
                )
            result = build_live(
                probe_dir,
                nutrition=os.path.join(PROBE, "nutrition.json"),
                coupons_mine=os.path.join(PROBE, "coupons_mine.json"),
            )
            paths = write_live(result, os.path.join(LIVE_ROOT, code))
            results[code] = result
            cov = result["coverage"]
            index.append(
                {
                    "code": code,
                    "name": name,
                    "address": row.get("storeAddress") or row.get("address") or "",
                    "distance": row.get("distance"),
                    "status": row.get("businessStatus") or "",
                    "hours": row.get("businessHours") or "",
                    "menu_items": cov["menu_items"],
                    "nutrition_ratio": cov["nutrition_ratio"],
                    "combo_solved": cov["combo_solved"],
                    "coupons": cov["coupons"],
                    "coupon_deals": cov["coupon_deals"],
                    "order_type": order_type,
                    "be_type": be_type,
                }
            )
            print(
                "  覆盖度：菜单 {} 项 / 营养 {:.0%} / 套餐展开 {}/{} / 券 {} 张（券商品 {}）".format(
                    cov["menu_items"], cov["nutrition_ratio"], cov["combo_solved"],
                    cov["combo_items"], cov["coupons"], cov["coupon_deals"],
                )
            )
            print("")
            del paths, combos

        if not index:
            print("没有成功取到任何门店数据", file=sys.stderr)
            return 1

        save(os.path.join(LIVE_ROOT, "index.json"), index)
        primary = index[0]["code"]
        mirror_primary(primary)
        print("门店索引：{} 家 -> data/live_stores/index.json".format(len(index)))
        print("已将最近一家（{}）镜像到 data/live_*.json 与 _probe/，旧脚本与自测可继续使用".format(primary))
        print("")
        print(render_report(results[primary]))
        print("")
        for label, path in write_live(results[primary], os.path.join(ROOT, "data")).items():
            print("已写出 {:<9s} {}".format(label, os.path.relpath(path, ROOT)))
        return 0

    # ---- 单店模式（旧行为） ----
    store_code = args.store
    if not store_code:
        if not (args.city or args.keyword):
            print("需要至少提供 --store，或 --city / --keyword 之一；或用 --all-nearby 取多店", file=sys.stderr)
            return 2
        rows = search_stores(token, args.city, args.keyword, be_type, next_id)
        if not rows:
            print("未找到门店", file=sys.stderr)
            return 1
        store_code = str(rows[0].get("storeCode"))
        print("选中门店：{}（{}）".format(rows[0].get("storeName"), store_code))
    else:
        save(os.path.join(PROBE, "stores.json"), {"data": [{"storeCode": store_code}]})

    fetch_shared(token, next_id)
    print("")
    fetch_store_readonly(token, store_code, order_type, be_type, PROBE, next_id)
    price_coupons(token, store_code, order_type, be_type, PROBE, next_id)
    if not args.skip_details:
        fetch_details(token, store_code, order_type, be_type, PROBE, next_id, args.details_limit)

    result = build_live(PROBE)
    paths = write_live(result, os.path.join(ROOT, "data"))
    print("")
    print(render_report(result))
    print("")
    for label, path in paths.items():
        print("已写出 {:<9s} {}".format(label, os.path.relpath(path, ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
