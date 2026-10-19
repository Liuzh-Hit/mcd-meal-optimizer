#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抓取套餐明细（query-meal-detail）并存到 _probe/details/。

套餐营养只能靠展开默认子项求和得到，所以必须先把明细取回来。
用法:
    python fetch_details.py [storeCode] [orderType] [beType] [最多条数]
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mcd_adapter import looks_like_combo, parse_menu  # noqa: E402
from mcp_payload import load_tool_text  # noqa: E402
from probe_mcp import load_token, rpc, unwrap  # noqa: E402

ROOT = os.path.dirname(HERE)
PROBE = os.path.join(ROOT, "_probe")
OUT = os.path.join(PROBE, "details")


def main():
    store = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MCD_STORE", "")
    order_type = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    be_type = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    limit = int(sys.argv[4]) if len(sys.argv) > 4 else 25

    obj, _ = load_tool_text(os.path.join(PROBE, "meals_store.json"))
    items = parse_menu(obj)
    # 只抓「疑似套餐」——单品 rounds 为空，抓了也是浪费调用。
    # 判据用 looks_like_combo（名字/品类），真实是否套餐由返回的 rounds 说话。
    combos = [it for it in items if looks_like_combo(it) and it["price_fen"] > 0]
    combos = sorted(combos, key=lambda x: -x["price_fen"])[:limit]
    print("候选套餐:", len(combos))

    os.makedirs(OUT, exist_ok=True)
    token = load_token()
    ok = 0
    for it in combos:
        code = it["code"]
        try:
            payload = rpc(
                token,
                "tools/call",
                {
                    "name": "query-meal-detail",
                    "arguments": {
                        "storeCode": store,
                        "orderType": order_type,
                        "beType": be_type,
                        "code": code,
                    },
                },
                rid=21,
            )
            data = unwrap(payload)
        except Exception as exc:  # noqa: BLE001
            print("  失败", code, it["name"][:16], repr(exc)[:60])
            continue
        path = os.path.join(OUT, "{}_detail.json".format(code))
        with io.open(path, "w", encoding="utf-8") as fh:
            if isinstance(data, (dict, list)):
                json.dump(data, fh, ensure_ascii=False, indent=1)
            else:
                fh.write(str(data))
        ok += 1
        print("  ok", code, it["name"][:20])
    print("已保存明细:", ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
