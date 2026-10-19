#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把麦当劳 MCP 的完整原始返回落盘，用于摸清字段契约。

用法:
    export MCD_STORE=<门店编码> MCD_CITY=<城市> MCD_KEYWORD=<地标关键词>
    python dump_mcp.py            # 拉取一整套只读数据存到 _probe/

门店与城市从环境变量读，不在源码里硬编码——公开仓库不保留任何真实门店标识。
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from probe_mcp import load_token, rpc, unwrap  # noqa: E402

OUT = os.path.join(HERE, "..", "_probe")
os.makedirs(OUT, exist_ok=True)

STORE = os.environ.get("MCD_STORE", "")
CITY = os.environ.get("MCD_CITY", "")
KEYWORD = os.environ.get("MCD_KEYWORD", "")

CALLS = [
    ("nutrition", "list-nutrition-foods", {}),
    ("meals_store", "query-meals", {"storeCode": STORE, "orderType": 1, "beType": 1}),
    ("coupons_mine", "query-my-coupons", {"page": "1", "pageSize": "200"}),
    ("coupons_avail", "available-coupons", {}),
    ("coupons_store", "query-store-coupons", {"storeCode": STORE, "orderType": 1, "beType": 1}),
    ("promotions", "query-promotions", {"storeCode": STORE, "orderType": 1, "beType": 1}),
    ("campaign", "campaign-calendar", {}),
    ("stores", "query-nearby-stores", {"beType": 1, "searchType": 2, "city": CITY, "keyword": KEYWORD}),
    ("deliv_stores", "delivery-query-stores", {"beType": 2, "addressId": ""}),
    ("account", "query-my-account", {}),
]


def main():
    token = load_token()
    for label, tool, args in CALLS:
        try:
            payload = rpc(token, "tools/call", {"name": tool, "arguments": args}, rid=11)
            data = unwrap(payload)
        except Exception as exc:  # noqa: BLE001
            data = {"__exception__": repr(exc)}
        path = os.path.join(OUT, label + ".json")
        with io.open(path, "w", encoding="utf-8") as fh:
            if isinstance(data, (dict, list)):
                fh.write(json.dumps(data, ensure_ascii=False, indent=1))
            else:
                fh.write(str(data))
        size = os.path.getsize(path)
        print("{:16s} {:28s} {:>8d} bytes".format(label, tool, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
