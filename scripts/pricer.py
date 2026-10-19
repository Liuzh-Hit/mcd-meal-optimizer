#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用麦当劳 MCP 的 calculate-price 对配餐方案做服务端权威核价。

为什么要单独这一步（2026-10-16 实测结论）：
    本地只负责「组合优化」——预算、营养、券商品的取舍都在本地确定性求解。
    但**金额的最终解释权在服务端**：券的抵扣规则、会员折扣、随单购、
    门店差异都可能让本地估算与实付不一致。
    实测确认「用券价格」型券的正确用法是：把**券的虚拟商品码**当作一个商品
    加进 items，并带上 couponId / couponCode，由服务端折算出券价。
    所以定稿前应调用本脚本复核一次，而不是直接告诉用户「一共多少钱」。

用法:
    # 按配餐参数跑：内部先求解，再对主/备选方案核价
    python pricer.py --menu data/live_menu.json --coupons data/live_coupons.json \
        --store <门店编码> --people 2 --budget 80

    # 对已导出的方案核价（optimize.py --json 的产物）
    python pricer.py --store <门店编码> --from-result result.json

仅标准库。Token 从 ~/.workbuddy/mcp.json 读取，不落盘、不回显。
"""
import argparse
import io
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mcd_core import fmt_yuan, to_fen  # noqa: E402
from mcp_payload import extract_json  # noqa: E402
from probe_mcp import load_token, rpc, unwrap  # noqa: E402


def build_api_items(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把方案里的餐品聚合成 calculate-price 的 items。

    券商品必须带上 couponId / couponCode——服务端据此把「用券价格」折出来；
    普通餐品按编码聚合数量，避免重复传同一 productCode。
    """
    counts: Dict[str, int] = {}
    order: List[str] = []
    deals: List[Dict[str, Any]] = []
    for item in plan.get("items", []):
        if item.get("is_coupon_deal"):
            code = str(item.get("virtual_product_code") or "").strip()
            if not code:
                continue
            entry: Dict[str, Any] = {"productCode": code, "quantity": 1}
            if item.get("coupon_id"):
                entry["couponId"] = str(item["coupon_id"])
            if item.get("coupon_code"):
                entry["couponCode"] = str(item["coupon_code"])
            deals.append(entry)
            continue
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        if code not in counts:
            counts[code] = 0
            order.append(code)
        counts[code] += 1

    items = [{"productCode": code, "quantity": counts[code]} for code in order]
    items.extend(deals)
    return items


def price_once(
    token: str,
    store_code: str,
    order_type: int,
    be_type: int,
    items: List[Dict[str, Any]],
    rid: int,
    be_code: str = "",
) -> Optional[Dict[str, Any]]:
    """调用 calculate-price，返回 data 段（失败返回 None）。"""
    arguments: Dict[str, Any] = {
        "storeCode": store_code,
        "orderType": order_type,
        "beType": be_type,
        "items": items,
    }
    if be_code:
        arguments["beCode"] = be_code
    payload = rpc(token, "tools/call", {"name": "calculate-price", "arguments": arguments}, rid=rid)
    data = unwrap(payload)
    if isinstance(data, str):
        obj, _prose = extract_json(data)
        data = obj
    if not isinstance(data, dict):
        return None
    if data.get("success") is False:
        return {"__error__": data.get("message") or "服务端返回失败"}
    inner = data.get("data")
    return inner if isinstance(inner, dict) else None


def diagnose(
    token: str,
    store_code: str,
    order_type: int,
    be_type: int,
    items: List[Dict[str, Any]],
    rid_start: int,
    be_code: str = "",
) -> tuple:
    """整单被拒时逐项核价，找出真正不可售的商品。

    实测（2026-10-16）：本地菜单快照里标记在售的商品，在 calculate-price 里
    仍可能返回「商品已售罄或不可售」——菜单的 available 字段并不等于
    该门店/该渠道当下真的能下单。逐项排查能把问题定位到具体商品，
    而不是只丢一句「下单失败」。
    返回 (不可售编码列表, 已检查条数)。
    """
    bad: List[str] = []
    rid = rid_start
    for item in items:
        rid += 1
        one = price_once(token, store_code, order_type, be_type, [item], rid, be_code)
        if one is None or "__error__" in one:
            bad.append(str(item.get("productCode") or ""))
    return bad, len(items)


def extract_take_ways(server: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """从核价返回里取出可取餐方式。

    这是下单的**必需前置**：`create-order` 在到店场景（orderType=1）必传
    `takeWayCode`，而该值唯一的合法来源就是 `calculate-price` 返回的
    `takeWayList[].code`。实测（2026-10-19）该门店返回两项：
    `eat-in`（堂食）/ `take-in-store`（外带）。
    核价时顺手把它带出来，下单环节才不用二次猜测。
    """
    ways: List[Dict[str, str]] = []
    for row in (server or {}).get("takeWayList") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        ways.append(
            {
                "code": code,
                "title": str(row.get("title") or code),
                "subtitle": str(row.get("subtitle") or ""),
            }
        )
    return ways


def render_take_ways(ways: List[Dict[str, str]]) -> List[str]:
    if not ways:
        return []
    desc = "｜".join(
        "{}{}".format(w["title"], "（{}）".format(w["code"]))
        for w in ways
    )
    return ["    - 可选取餐方式：{}　← 下单时必选其一".format(desc)]


def render_verification(label: str, local_fen: int, server: Optional[Dict[str, Any]]) -> List[str]:
    lines = []
    if server is None:
        lines.append("- **{}**：服务端核价失败（网络或参数问题），本地估算 {:.2f} 元仅供参考".format(label, local_fen / 100.0))
        return lines
    if "__error__" in server:
        lines.append("- **{}**：服务端拒绝核价（{}），本地估算 {:.2f} 元仅供参考".format(label, server["__error__"], local_fen / 100.0))
        return lines

    server_fen = int(server.get("price") or 0)
    original_fen = int(server.get("originalPrice") or 0)
    discount_fen = int(server.get("discount") or 0)
    delta = server_fen - local_fen
    mark = "✅ 一致" if delta == 0 else ("⚠️ 有差异" if abs(delta) >= 100 else "≈ 基本一致")
    lines.append(
        "- **{}**：本地估算 {} 元 ｜ 服务端 {} 元（原价 {} 元，优惠 {} 元）→ {}".format(
            label, fmt_yuan(local_fen), fmt_yuan(server_fen), fmt_yuan(original_fen), fmt_yuan(discount_fen), mark
        )
    )
    if delta:
        lines.append(
            "    - 差额 {} 元；以服务端为准（本地未建模的会员价/随单购/门店差异都可能造成差额）".format(
                fmt_yuan(abs(delta))
            )
        )
    for row in server.get("productList") or []:
        lines.append(
            "    - {} ×{}：{} 元（原价 {} 元）".format(
                row.get("productName"),
                row.get("quantity"),
                fmt_yuan(int(row.get("subtotal") or 0)),
                fmt_yuan(int(row.get("originalSubtotal") or 0)),
            )
        )
    lines.extend(render_take_ways(extract_take_ways(server)))
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="用 calculate-price 对配餐方案做服务端权威核价")
    parser.add_argument("--store", required=True, help="门店编码（storeCode）")
    parser.add_argument("--order-type", type=int, default=1, help="1-到店 2-外送")
    parser.add_argument("--be-type", type=int, default=1, help="1-到店取餐 2-麦乐送 5-得来速 6-团餐")
    parser.add_argument("--be-code", default="", help="业务编码，外送/得来速/团餐必传")
    parser.add_argument("--from-result", default="", help="已导出的方案 JSON（optimize.py --json 产物）")

    # 未提供 --from-result 时，用这些参数在本地先求解
    parser.add_argument("--menu", default="", help="菜单 JSON")
    parser.add_argument("--coupons", default="", help="优惠券 JSON")
    parser.add_argument("--people", type=int, default=2)
    parser.add_argument("--budget", default="80")
    parser.add_argument("--mode", default="value", choices=["value", "slim", "muscle"])
    parser.add_argument("--scenario", default="")
    parser.add_argument("--strategy", default="value", choices=["value", "frugal"])
    parser.add_argument("--delivery-fee", default="0")
    parser.add_argument("--max-coupon-deals", type=int, default=1)
    parser.add_argument("--plan", default="both", choices=["primary", "alternative", "both"])
    parser.add_argument("--unsellable", default="", help="不可售商品清单的输出路径（默认 data/unsellable.json）")
    args = parser.parse_args()

    # 多店模式下每家的菜单/券/不可售清单都在 data/live_stores/<code>/ 各存一份，
    # 只要门店目录存在就自动指向它——避免把 A 店的不可售清单误用到 B 店。
    live_dir = os.path.join(os.path.dirname(HERE), "data", "live_stores", args.store)
    if os.path.isdir(live_dir):
        if not args.menu:
            args.menu = os.path.join(live_dir, "live_menu.json")
        if not args.coupons:
            args.coupons = os.path.join(live_dir, "live_coupons.json")
        if not args.unsellable:
            args.unsellable = os.path.join(live_dir, "unsellable.json")

    if args.from_result:
        with io.open(args.from_result, encoding="utf-8") as fh:
            result = json.load(fh)
    else:
        if not args.menu:
            print("需要 --from-result，或至少提供 --menu 让本地先求解", file=sys.stderr)
            return 2
        import optimize

        ns = argparse.Namespace(
            menu=args.menu,
            coupons=args.coupons,
            people=args.people,
            budget=args.budget,
            mode=args.mode,
            scenario=args.scenario,
            strategy=args.strategy,
            delivery_fee=args.delivery_fee,
            delivery_fee_fen=to_fen(args.delivery_fee),
            coupon_headroom="15",
            coupon_headroom_fen=to_fen("15"),
            max_coupons=2,
            max_order_coupons=1,
            max_coupon_deals=args.max_coupon_deals,
            rules=optimize.DEFAULT_RULES,
            scenarios=optimize.DEFAULT_SCENARIOS,
            unsellable=args.unsellable or optimize.DEFAULT_UNSELLABLE,
            ignore_unsellable=False,
            json_out="",
            format="text",
        )
        result = optimize.run(ns)

    if not result.get("ok"):
        print("方案不可行，无需核价：{}".format(result.get("error")))
        return 1

    plans = []
    if args.plan in ("primary", "both") and result.get("primary"):
        plans.append(result["primary"])
    if args.plan in ("alternative", "both") and result.get("alternative"):
        plans.append(result["alternative"])
    if not plans:
        print("方案里没有可核价的组合")
        return 1

    token = load_token()
    rid = 500
    lines: List[str] = ["# 服务端核价（calculate-price）", "", "门店 {}｜订单类型 {}".format(args.store, args.order_type), ""]
    blocked: List[str] = []
    priced: Dict[str, Any] = {}
    for plan in plans:
        items = build_api_items(plan)
        if not items:
            lines.append("- **{}**：无可核价餐品".format(plan.get("label")))
            continue
        rid += 1
        server = price_once(token, args.store, args.order_type, args.be_type, items, rid, args.be_code)
        lines.extend(render_verification(plan.get("label", "方案"), int(plan.get("total_fen") or 0), server))
        # 落盘一份核价快照：take_way_list 是下单时 takeWayCode 的唯一来源，
        # 订单号/金额/券的当前状态也留痕，便于事后核对。
        if isinstance(server, dict) and "__error__" not in server:
            priced[plan.get("label", "方案")] = {
                "items": items,
                "price_fen": int(server.get("price") or 0),
                "original_price_fen": int(server.get("originalPrice") or 0),
                "discount_fen": int(server.get("discount") or 0),
                "take_way_list": extract_take_ways(server),
                "product_list": server.get("productList") or [],
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        if server is None or "__error__" in server:
            name_of: Dict[str, str] = {}
            for item in plan.get("items", []):
                code = str(item.get("virtual_product_code") if item.get("is_coupon_deal") else item.get("code") or "")
                if code:
                    name_of[code] = str(item.get("name") or code)
            bad, checked = diagnose(token, args.store, args.order_type, args.be_type, items, rid, args.be_code)
            rid += checked
            if bad:
                blocked.extend(bad)
                lines.append("    - 🔎 逐项排查（共 {} 项）：以下商品在该门店/渠道**不可售**，需剔除后重算方案".format(checked))
                for code in bad:
                    lines.append("        - {}（{}）".format(name_of.get(code, code), code))
                lines.append("    - 其余 {} 项核价正常".format(checked - len(bad)))
        lines.append("")

    if blocked:
        out_path = args.unsellable or os.path.join(os.path.dirname(HERE), "data", "unsellable.json")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        payload = {
            "store": args.store,
            "order_type": args.order_type,
            "be_type": args.be_type,
            "codes": sorted(set(blocked)),
        }
        with io.open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        lines.append("")
        lines.append("已把不可售商品写入 {}，供下次刷新时参考。".format(os.path.relpath(out_path, os.path.dirname(HERE))))

    if priced:
        probe_dir = os.path.join(os.path.dirname(HERE), "_probe")
        os.makedirs(probe_dir, exist_ok=True)
        snap_path = os.path.join(probe_dir, "last_pricing.json")
        snapshot = {
            "store": args.store,
            "order_type": args.order_type,
            "be_type": args.be_type,
            "be_code": args.be_code,
            "plans": priced,
        }
        with io.open(snap_path, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=1)
        lines.append("")
        lines.append("核价快照已写入 `_probe/last_pricing.json`（含 takeWayList 与逐项金额）。")
    lines.append("请求明细：")
    for plan in plans:
        lines.append("- {}：{}".format(plan.get("label"), json.dumps(build_api_items(plan), ensure_ascii=False)))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
