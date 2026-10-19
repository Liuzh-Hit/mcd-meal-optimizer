#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP 冒烟探针：真实调用麦当劳 MCP，打印返回结构与字段名。

用法:
    python probe_mcp.py <tool-name> [json-args]

Token 从 ~/.workbuddy/mcp.json 读取，不落盘、不回显。
仅标准库，不依赖第三方。
"""
import io
import json
import os
import sys
import urllib.request

MCP_URL = "https://mcp.mcd.cn"
CFG = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")


def load_token() -> str:
    with io.open(CFG, encoding="utf-8") as fh:
        cfg = json.load(fh)
    raw = cfg["mcpServers"]["mcd-mcp"]["headers"]["Authorization"]
    return raw.split("Bearer", 1)[-1].strip()


def rpc(token: str, method: str, params: dict, rid: int = 1) -> dict:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        MCP_URL,
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        raw = resp.read().decode("utf-8", "replace")
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise RuntimeError("无法解析响应: " + raw[:400])


def unwrap(payload: dict):
    """MCP tool 的返回被包在 content[0].text 里，通常是 JSON 字符串。"""
    if "error" in payload:
        return {"__error__": payload["error"]}
    result = payload.get("result", {})
    content = result.get("content") or []
    if not content:
        return result
    text = content[0].get("text", "")
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return text


def shape(obj, depth=0, max_depth=4):
    """打印结构骨架：字段名 + 类型，列表只展开首个元素。"""
    pad = "  " * depth
    if depth > max_depth:
        return [pad + "..."]
    out = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            if isinstance(val, dict):
                out.append("{}{} : object{{}}".format(pad, key))
                out.extend(shape(val, depth + 1, max_depth))
            elif isinstance(val, list):
                out.append("{}{} : array[{}]".format(pad, key, len(val)))
                if val:
                    out.extend(shape(val[0], depth + 1, max_depth))
            else:
                preview = repr(val)
                if len(preview) > 60:
                    preview = preview[:57] + "..."
                out.append("{}{} : {}".format(pad, key, preview))
    elif isinstance(obj, list):
        if obj:
            out.extend(shape(obj[0], depth, max_depth))
    else:
        out.append(pad + repr(obj))
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    tool = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    token = load_token()
    payload = rpc(token, "tools/call", {"name": tool, "arguments": args}, rid=7)
    data = unwrap(payload)

    print("=" * 68)
    print("TOOL:", tool, "| ARGS:", json.dumps(args, ensure_ascii=False))
    print("=" * 68)
    if isinstance(data, (dict, list)):
        print("\n".join(shape(data)))
        text = json.dumps(data, ensure_ascii=False)
        print("\n--- 原始（截断 1200 字符）---")
        print(text[:1200])
    else:
        print(str(data)[:1200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
