"""make_examples.py - 用脱敏样例数据生成 assets/example-output.md。

    python scripts/make_examples.py

仓库里的示例文档由本脚本生成，便于在样例数据变动后一键刷新。
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "assets", "example-output.md")

MENU = os.path.join(DATA, "sample_menu.json")
COUPONS = os.path.join(DATA, "sample_coupons.json")
STORES = os.path.join(DATA, "sample_stores.json")
ADDRESSES = os.path.join(DATA, "sample_addresses.json")

CASES: List[Tuple[str, List[str]]] = [
    (
        "1. 预算最优配餐 + 优惠券组合比价｜2 人 / 预算 80 元",
        ["optimize.py", "--menu", MENU, "--coupons", COUPONS, "--people", "2", "--budget", "80", "--mode", "value"],
    ),
    (
        "2. 减脂模式 + 高油替换建议｜1 人 / 预算 45 元",
        ["optimize.py", "--menu", MENU, "--coupons", COUPONS, "--people", "1", "--budget", "45", "--mode", "slim"],
    ),
    (
        "3. 健身增肌模式｜1 人 / 预算 60 元",
        ["optimize.py", "--menu", MENU, "--coupons", COUPONS, "--people", "1", "--budget", "60", "--mode", "muscle"],
    ),
    (
        "4. 场景套餐（生日，预算不足时自动放宽结构）｜6 人 / 预算 200 元",
        ["optimize.py", "--menu", MENU, "--coupons", COUPONS, "--people", "6", "--budget", "200", "--scenario", "birthday"],
    ),
    (
        "5. 最省策略（同等满足度下最省）｜3 人 / 预算 120 元",
        ["optimize.py", "--menu", MENU, "--coupons", COUPONS, "--people", "3", "--budget", "120", "--strategy", "frugal"],
    ),
    (
        "6. 外送场景（计入 9 元配送费，命中免配送费券）｜1 人 / 预算 45 元",
        [
            "optimize.py", "--menu", MENU, "--coupons", COUPONS,
            "--people", "1", "--budget", "45", "--mode", "slim", "--delivery-fee", "9",
        ],
    ),
    ("7. 门店与配送可行性判定", ["analyze.py", "--action", "delivery", "--stores", STORES, "--addresses", ADDRESSES, "--radius-km", "3"]),
    ("8. 菜品状态识别（限时 / 即将下架 / 已下架）", ["analyze.py", "--action", "status", "--menu", MENU]),
    (
        "9. 套餐营养汇总｜巨无霸 + 中薯条 + 可口可乐",
        ["analyze.py", "--action", "nutrition", "--menu", MENU, "--codes", "M01,M10,D01", "--people", "1"],
    ),
]


def run(script: str, args: List[str]) -> str:
    cmd = [sys.executable, os.path.join(HERE, script)] + args + ["--format", "text"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    text = (proc.stdout or "").strip()
    if proc.returncode != 0 and not text:
        text = (proc.stderr or "").strip()
    return text


def main() -> int:
    blocks: List[str] = [
        "# 示例输出",
        "",
        "以下输出全部由 `data/` 下的脱敏样例数据生成，不包含任何真实门店、真实 Token 或个人地址；",
        "可执行 `python scripts/make_examples.py` 一键复现。",
        "",
        "> 真实使用时，菜单、营养、优惠券与门店数据来自麦当劳 MCP 的实时返回；",
        "> 样例数据仅用于验证算法与排版。",
        "",
    ]
    for title, args in CASES:
        blocks.append("## " + title)
        blocks.append("")
        blocks.append("```text")
        blocks.append(run(args[0], args[1:]))
        blocks.append("```")
        blocks.append("")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(blocks))
    print("written:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
