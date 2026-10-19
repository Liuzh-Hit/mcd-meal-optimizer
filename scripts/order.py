#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把配餐方案推进到「下单」，但默认只演练。

三条原则（缺一不可）：

1. **默认不下单。** 不加 `--confirm` 时，本脚本没有任何代码路径会调用
   `create-order`——不是靠「记得别调」，而是靠函数只在那个分支里被引用。
2. **下单前必重新核价。** 不复用缓存金额：券会过期、商品会当天售罄，
   只有下单那一刻服务端给的价格才算数。核价同时带回 `takeWayList`，
   因为到店场景（orderType=1）的 `create-order` **必传 `takeWayCode`**，
   而该值唯一的合法来源就是 `calculate-price` 的返回。
3. **金额被拒就停。** 核价失败或被拒时直接退出，绝不「猜个金额就下单」。

⚠️ 加上 `--confirm` 会创建**真实订单**并生成支付链接，钱是要真付的。
   本项目其余脚本（optimize / pricer / build_ui）都不会触达下单接口。

用法：

    # 演练（默认）：核价 → 打印完整订单预览与即将提交的请求体，不创建订单
    python order.py --store <门店编码> --menu data/live_menu.json \
        --coupons data/live_coupons.json --people 2 --budget 80 --take-way eat-in

    # 对已导出的方案演练
    python order.py --store <门店编码> --from-result result.json --take-way eat-in

    # 真下单（会扣钱，仅在用户明确确认后使用）
    python order.py --store <门店编码> --from-result result.json --take-way eat-in --confirm

仅标准库。Token 从 ~/.workbuddy/mcp.json 读取，不落盘、不回显。

⚠️ 已知边界：`create-order` 的成功路径**未经真实下单验证**（默认演练，
   不产生真实订单）。因此返回体的字段名只能「找到什么报什么」，
   不做臆测——若字段对不上，脚本会原样打印服务端返回，而不是伪造一个链接。
"""
import argparse
import io
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from mcd_core import fmt_yuan, to_fen  # noqa: E402
from mcp_payload import extract_json  # noqa: E402
from probe_mcp import load_token, rpc, unwrap  # noqa: E402
import pricer  # noqa: E402

# create-order 返回体里可能承载支付入口的键（按优先级）
_PAY_URL_KEYS = ("payUrl", "payLink", "paymentUrl", "payH5Url", "h5Url", "codeUrl", "qrCodeUrl", "url")
_ORDER_ID_KEYS = ("orderId", "orderNo", "orderCode")


def find_first_value(obj: Any, keys: Tuple[str, ...], require_prefix: str = "") -> str:
    """在嵌套结构里找第一个命中的键值。

    返回体形状没有权威文档（默认演练模式下不会产生真实订单），
    所以这里做广度优先搜索：找到就报，找不到就返回空串，
    **绝不拼一个看起来像链接的东西糊弄过去**。
    """
    stack: List[Any] = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key in keys:
                value = cur.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    text = str(value).strip()
                    if require_prefix and not text.lower().startswith(require_prefix):
                        continue
                    return text
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return ""


def store_label(code: str) -> str:
    """门店编码 → 店名（读不到就退回编码本身）。"""
    for rel in (os.path.join("data", "live_stores", "index.json"),):
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        try:
            with io.open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
        except (ValueError, OSError):
            continue
        for row in rows if isinstance(rows, list) else []:
            if str(row.get("code")) == str(code):
                return str(row.get("name") or code)
    return code


def pick_take_way(ways: List[Dict[str, str]], wanted: str) -> Tuple[str, List[str]]:
    """决定 takeWayCode。返回 (code, 错误说明)。

    **不猜。** 服务端给两项（堂食 / 外带）时必须由人指定——
    选错等于下错单，这种错不该由脚本替用户做决定。
    """
    codes = [w["code"] for w in ways]
    if wanted:
        if wanted in codes:
            return wanted, []
        return "", [
            "指定的取餐方式 `{}` 不在服务端返回的列表里。可选：{}".format(
                wanted,
                "｜".join("{}（{}）".format(w.get("title") or w["code"], w["code"]) for w in ways) or "（空）",
            )
        ]
    if len(codes) == 1:
        return codes[0], []
    if not codes:
        return "", ["服务端没有返回可选取餐方式（takeWayList 为空），无法确定 takeWayCode。"]
    return "", ["存在多个取餐方式，请用 --take-way 明确指定："] + [
        "    - {}（{}）{}".format(
            w.get("title") or w["code"], w["code"], ("— " + w["subtitle"]) if w.get("subtitle") else ""
        )
        for w in ways
    ]


def build_order_request(
    store_code: str,
    order_type: int,
    be_type: int,
    items: List[Dict[str, Any]],
    take_way_code: str = "",
    be_code: str = "",
    address_id: str = "",
    remark: str = "",
    need_tableware: Optional[bool] = None,
) -> Dict[str, Any]:
    """拼 create-order 的请求体。

    只放服务端契约里有的字段（见 references/mcp-tools.md 的必传规则）：
    到店/得来速传 takeWayCode，外送/团餐传地址不传 takeWayCode。
    """
    request: Dict[str, Any] = {
        "storeCode": store_code,
        "orderType": order_type,
        "beType": be_type,
        "items": items,
    }
    if order_type == 1 and take_way_code:
        request["takeWayCode"] = take_way_code
    if be_code:
        request["beCode"] = be_code
    if order_type == 2 and address_id:
        request["addressId"] = address_id
    if remark:
        request["remark"] = remark[:50]
    if need_tableware is not None:
        request["needTableware"] = bool(need_tableware)
    return request


def submit_order(token: str, request: Dict[str, Any], rid: int = 900) -> Any:
    """真正调用 create-order。

    **只有 main() 里 --confirm 那个分支会引用本函数**——这是「默认不下单」的
    结构性保证，不依赖调用方自觉。
    """
    payload = rpc(token, "tools/call", {"name": "create-order", "arguments": request}, rid=rid)
    data = unwrap(payload)
    if isinstance(data, str):
        obj, _prose = extract_json(data)
        data = obj if obj is not None else data
    return data


def render_items(plan: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    for item in plan.get("items", []):
        qty = int(item.get("portions") or 1)
        if item.get("is_coupon_deal"):
            saving = max(0, int(item.get("list_price_fen") or 0) - int(item.get("price_fen") or 0))
            line = "    - 🎟️ {} ×{}　券价 {} 元".format(item.get("name"), qty, item.get("price_yuan"))
            if saving:
                line += "（原价 {} 元，省 {} 元）".format(
                    fmt_yuan(int(item.get("list_price_fen") or 0)), fmt_yuan(saving)
                )
            lines.append(line)
            lines.append(
                "        couponId={}｜couponCode={}".format(item.get("coupon_id") or "-", item.get("coupon_code") or "-")
            )
        else:
            lines.append(
                "    - {} ×{}　{} 元".format(item.get("name"), qty, item.get("price_yuan"))
            )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把配餐方案推进到下单（默认只演练，不加 --confirm 绝不创建订单）"
    )
    parser.add_argument("--store", required=True, help="门店编码（storeCode）")
    parser.add_argument("--order-type", type=int, default=1, help="1-到店 2-外送（默认 1）")
    parser.add_argument("--be-type", type=int, default=1, help="1-到店取餐 2-麦乐送 5-得来速 6-团餐")
    parser.add_argument("--be-code", default="", help="业务编码，外送/得来速/团餐必传")
    parser.add_argument("--address-id", default="", help="外送地址 ID（orderType=2 必传）")
    parser.add_argument("--remark", default="", help="订单备注（最多 50 字，主要用在外送）")
    parser.add_argument("--take-way", default="", help="取餐方式 code，取值见核价返回的 takeWayList")

    table = parser.add_mutually_exclusive_group()
    table.add_argument("--tableware", dest="need_tableware", action="store_true", help="需要餐具")
    table.add_argument("--no-tableware", dest="need_tableware", action="store_false", help="不需要餐具")
    parser.set_defaults(need_tableware=None)

    parser.add_argument("--from-result", default="", help="已导出的方案 JSON（optimize.py --json 产物）")
    parser.add_argument("--plan", default="primary", choices=["primary", "alternative"], help="用哪套方案下单")

    parser.add_argument("--menu", default="", help="菜单 JSON（不给 --from-result 时本地先求解）")
    parser.add_argument("--coupons", default="", help="优惠券 JSON")
    parser.add_argument("--people", type=int, default=2)
    parser.add_argument("--budget", default="80")
    parser.add_argument("--mode", default="value", choices=["value", "slim", "muscle"])
    parser.add_argument("--scenario", default="")
    parser.add_argument("--strategy", default="value", choices=["value", "frugal"])
    parser.add_argument("--delivery-fee", default="0")
    parser.add_argument("--max-coupon-deals", type=int, default=1)
    parser.add_argument("--unsellable", default="")

    parser.add_argument(
        "--confirm",
        action="store_true",
        help="⚠️ 真的创建订单（会产生支付链接，钱要真付）。不加则只演练。",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出预览/结果")
    args = parser.parse_args()

    # 多店模式下每家的数据各存一份，只要门店目录存在就自动指向它
    live_dir = os.path.join(ROOT, "data", "live_stores", args.store)
    if os.path.isdir(live_dir):
        if not args.menu:
            args.menu = os.path.join(live_dir, "live_menu.json")
        if not args.coupons:
            args.coupons = os.path.join(live_dir, "live_coupons.json")
        if not args.unsellable:
            args.unsellable = os.path.join(live_dir, "unsellable.json")

    # ---- 1. 拿到待下单的方案 ----
    if args.from_result:
        with io.open(args.from_result, encoding="utf-8") as fh:
            result = json.load(fh)
    else:
        if not args.menu:
            print("需要 --from-result，或至少提供 --menu 让本地先求解", file=sys.stderr)
            return 1
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
        print("方案不可行，无法下单：{}".format(result.get("error")))
        return 1
    plan = result.get(args.plan)
    if not plan:
        print("结果里没有 `{}` 方案".format(args.plan))
        return 1

    items = pricer.build_api_items(plan)
    if not items:
        print("该方案没有可下单的餐品")
        return 1

    # ---- 2. 下单前必重新核价（含取餐方式的唯一来源）----
    token = load_token()
    server = pricer.price_once(
        token, args.store, args.order_type, args.be_type, items, rid=701, be_code=args.be_code
    )
    if server is None or "__error__" in server:
        reason = server["__error__"] if isinstance(server, dict) and "__error__" in server else "无返回"
        print("❌ 服务端核价失败：{}".format(reason))
        print("   下单已中止——绝不在金额未确认的情况下创建订单。")
        bad, checked = pricer.diagnose(
            token, args.store, args.order_type, args.be_type, items, rid_start=701, be_code=args.be_code
        )
        if bad:
            print("   逐项排查（{} 项）发现不可售：{}".format(checked, "、".join(bad)))
            print("   建议回到 optimize.py 重算方案，或运行 pricer.py 更新不可售清单。")
        return 2

    ways = pricer.extract_take_ways(server)
    take_way_code = ""
    if args.order_type == 1:
        take_way_code, way_errors = pick_take_way(ways, args.take_way)
        if way_errors:
            print("❌ 无法确定取餐方式，下单已中止：")
            for line in way_errors:
                print("   " + line)
            return 3

    request = build_order_request(
        args.store,
        args.order_type,
        args.be_type,
        items,
        take_way_code=take_way_code,
        be_code=args.be_code,
        address_id=args.address_id,
        remark=args.remark,
        need_tableware=args.need_tableware,
    )

    server_fen = int(server.get("price") or 0)
    original_fen = int(server.get("originalPrice") or 0)
    discount_fen = int(server.get("discount") or 0)
    local_fen = int(plan.get("total_fen") or 0)

    preview: Dict[str, Any] = {
        "store": {"code": args.store, "name": store_label(args.store)},
        "order_type": args.order_type,
        "be_type": args.be_type,
        "take_way_code": take_way_code,
        "items": items,
        "plan": {
            "label": plan.get("label"),
            "local_fen": local_fen,
            "server_fen": server_fen,
            "original_fen": original_fen,
            "discount_fen": discount_fen,
            "consistent": server_fen == local_fen,
        },
        "request": request,
        "submitted": False,
    }

    if not args.json:
        print("# 下单预览（未提交）" if not args.confirm else "# 即将创建真实订单")
        print()
        print("门店：{}（{}）".format(preview["store"]["name"], args.store))
        print("渠道：orderType={} / beType={}".format(args.order_type, args.be_type))
        if take_way_code:
            title = next((w.get("title") for w in ways if w["code"] == take_way_code), take_way_code)
            print("取餐：{}（{}）".format(title, take_way_code))
        print()
        print("餐品：")
        for line in render_items(plan):
            print(line)
        print()
        print(
            "服务端金额：{} 元（原价 {} 元，优惠 {} 元）｜本地估算 {} 元 → {}".format(
                fmt_yuan(server_fen),
                fmt_yuan(original_fen),
                fmt_yuan(discount_fen),
                fmt_yuan(local_fen),
                "✅ 一致" if server_fen == local_fen else "⚠️ 有差异，以服务端为准",
            )
        )
        if discount_fen:
            print("   优惠明细请在 pricer.py 的逐项输出里核对")
        print()
        print("即将提交的请求体：")
        print("```json")
        print(json.dumps(request, ensure_ascii=False, indent=1))
        print("```")

    # ---- 3. 只有 --confirm 才真的下单 ----
    if not args.confirm:
        if not args.json:
            print()
            print("⏸️ 未创建订单（演练模式）。确认门店 / 餐品 / 金额无误后，")
            print("   再加 `--confirm` 才会真正下单并生成支付链接。")
        else:
            print(json.dumps(preview, ensure_ascii=False, indent=1))
        return 0

    data = submit_order(token, request, rid=902)
    order_id = find_first_value(data, _ORDER_ID_KEYS)
    pay_url = find_first_value(data, _PAY_URL_KEYS, require_prefix="http")
    preview["submitted"] = True
    preview["order_id"] = order_id
    preview["pay_url"] = pay_url
    preview["raw"] = data if isinstance(data, (dict, str)) else str(data)

    if args.json:
        print(json.dumps(preview, ensure_ascii=False, indent=1))
    else:
        print()
        if order_id:
            print("✅ 订单已创建：orderId={}".format(order_id))
        else:
            print("⚠️ 服务端已返回，但未识别出订单号——下面是原始返回，请以麦当劳 APP 内的订单状态为准：")
            print(json.dumps(data, ensure_ascii=False, indent=1)[:2000])
        if pay_url:
            print("支付链接：{}".format(pay_url))
        else:
            print("没有从返回里找到支付链接。可到麦当劳 APP / 小程序「我的订单」里继续支付。")
        print()
        print("支付完成后可用 `python scripts/probe_mcp.py query-order <orderId>` 查询状态。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
