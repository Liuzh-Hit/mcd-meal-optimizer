#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从麦当劳 MCP 的混合文本返回里提取结构化载荷。

MCP 返回不是干净 JSON，实测有三种形态：
  1. markdown 说明 + JSON 信封（如 query-meals / query-nearby-stores）
  2. JSON 信封，但 data 是内嵌 CSV 的字符串（如 list-nutrition-foods）
  3. 纯 markdown 散文（如 query-my-coupons / available-coupons）

本模块统一成「JSON 优先、散文兜底」，仅标准库。
已被 mcd_core.py 复用，也可单独运行做自检：
    python mcp_payload.py <文件路径>
"""
import io
import json
import os
import re
import sys


def extract_json(text):
    """从混合文本里抠出第一个完整的 JSON 对象/数组。

    说明段落里也含 '{' 的几率很低，但为了稳，用逐字符括号配对 + 反转义尝试。
    返回 (obj, 剩余散文) 或 (None, 原文)。
    """
    if not isinstance(text, str):
        return text, ""
    # 优先找 API 信封的典型起点
    starts = [m.start() for m in re.finditer(r'\{\s*"(?:success|code|data|result)"', text)]
    starts += [m.start() for m in re.finditer(r"^\s*[\[{]", text, re.M)]
    for pos in sorted(set(starts)):
        chunk = text[pos:]
        hit = _balanced_json(chunk)
        if hit is not None:
            obj, consumed = hit
            prose = (text[:pos] + chunk[consumed:]).strip()
            return obj, prose
    return None, text


def _balanced_json(chunk):
    """括号配对找出第一个合法 JSON 值。

    返回 (obj, 消耗字符数) 或 None。逐字符扫描只对「收尾字符」做解析尝试，
    避免 O(n^2) 的同时保证拿到的是最短完整前缀。
    """
    for end, ch in enumerate(chunk, start=1):
        if ch not in "}]":
            continue
        candidate = chunk[:end]
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        return obj, end
    return None


def load_tool_text(path):
    """读取 dump_mcp 落盘的文件（可能是 JSON 也可能是纯散文）。"""
    with io.open(path, encoding="utf-8") as fh:
        raw = fh.read()
    stripped = raw.strip()
    if stripped[:1] in "{[":
        try:
            return json.loads(stripped), raw
        except ValueError:
            pass
    obj, prose = extract_json(raw)
    return obj, prose


def parse_nutrition_table(blob):
    """解析 list-nutrition-foods 的 data 字符串。

    形态: '[160]{productName,...,calcium}:\\n  猪柳麦满分,null,1288,308,16,16,24,781,213\\n  ...'
    返回 list[dict]，键用真实字段名。
    """
    if not isinstance(blob, str):
        return []
    m = re.search(r"\[(\d+)\]\s*\{([^}]*)\}\s*:(.*)", blob, re.S)
    if not m:
        return []
    header = [h.strip() for h in m.group(2).split(",") if h.strip()]
    rows = []
    for line in m.group(3).splitlines():
        line = line.strip()
        if not line:
            continue
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        rec = dict(zip(header, cells[: len(header)]))
        rows.append(rec)
    return rows


def parse_coupon_markdown(text):
    """解析 query-my-coupons 的 markdown 散文，抽成结构化券列表。

    实测形态:
        ## 中薯条买一送一
        - **优惠**: ¥13.5 (用券价格)
        - **有效期**: 2026-10-12 10:30-2026-10-16 23:59 周一、二、三、四、五 10:30-23:59
        - **领取时间**: 4日前收到
        - **标签**: 到店专用、外送专用
    """
    if not isinstance(text, str):
        return []
    coupons = []
    blocks = re.split(r"\n(?=##\s)", text)
    for block in blocks:
        title = re.search(r"^##\s*(.+)$", block, re.M)
        if not title:
            continue
        name = title.group(1).strip()
        if name.startswith(("您的优惠券", "麦麦省")):
            continue
        rec = {"name": name}

        pay = re.search(r"\*\*优惠\*\*[:：]\s*¥?\s*([\d.]+)", block)
        if pay:
            rec["pay_price_yuan"] = pay.group(1)

        valid = re.search(r"\*\*有效期\*\*[:：]\s*([^\n]+)", block)
        if valid:
            window = valid.group(1).strip()
            rec["validity_text"] = window
            rng = re.search(
                r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s*[-~至]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})",
                window,
            )
            if rng:
                rec["valid_from"] = rng.group(1)
                rec["valid_until"] = rng.group(2)

        tags = re.search(r"\*\*标签\*\*[:：]\s*([^\n]+)", block)
        if tags:
            rec["tags"] = [t.strip() for t in re.split(r"[、,，]", tags.group(1)) if t.strip()]

        got = re.search(r"\*\*领取时间\*\*[:：]\s*([^\n]+)", block)
        if got:
            rec["acquired_text"] = got.group(1).strip()

        coupons.append(rec)
    return coupons


def parse_available_markdown(text):
    """解析 available-coupons（麦麦省）的 markdown 散文。

    形态: '- 优惠券标题：中薯条买一送一 \\\\n  状态：已领取 \\\\n ...'
    """
    if not isinstance(text, str):
        return []
    items = []
    for chunk in re.split(r"-\s*优惠券标题[:：]", text)[1:]:
        name_m = re.match(r"\s*([^\\\n]+)", chunk)
        if not name_m:
            continue
        rec = {"name": name_m.group(1).strip()}
        st = re.search(r"状态[:：]\s*([^\s\\]+)", chunk)
        if st:
            rec["status"] = st.group(1).strip()
        items.append(rec)
    return items


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    path = sys.argv[1]
    if not os.path.exists(path):
        print("文件不存在:", path)
        return 1
    obj, prose = load_tool_text(path)
    print("提取到 JSON:", "是" if obj is not None else "否")
    print("散文长度:", len(prose))
    name = os.path.basename(path)
    if "nutrition" in name and obj:
        rows = parse_nutrition_table(obj.get("data"))
        print("营养行数:", len(rows), "| 首行:", rows[0] if rows else None)
    if "coupons_mine" in name:
        got = parse_coupon_markdown(prose)
        print("券数:", len(got))
        for c in got[:3]:
            print("  ", json.dumps(c, ensure_ascii=False))
    if "coupons_avail" in name:
        got = parse_available_markdown(prose)
        print("麦麦省券数:", len(got), "| 首项:", got[0] if got else None)
    if obj is not None:
        print("顶层键:", list(obj.keys())[:10] if isinstance(obj, dict) else type(obj).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
