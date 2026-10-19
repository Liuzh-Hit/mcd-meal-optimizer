#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成「麦麦智配」可视化面板（单文件 HTML，无需服务器即可打开）。

设计取舍（重要）：
    前端**不做任何算术**。所有组合、金额、营养、券价都由本地确定性求解器
    预先算好（实测单次求解 0.011 秒），整张参数网格烘焙进 HTML。
    前端只负责展示与切换。这样既拿到「零延迟交互」，又不会让模型/JS
    去心算金额——这是本项目的一条铁律。

多门店与体积：
    面板要能「选餐厅」，而每家门店的菜单不同、组合结果也不同，所以每家都要
    各算一遍 347 组。若照直烘焙，5 家就是 ~7 MB。为此引入了**跨门店共享对象池**
    （`Interner`）：items / checks / 营养覆盖度 / 告警这些对象在同一门店的不同
    参数之间、甚至不同门店之间大量重复，全部归一成一个全局数组，网格里只存
    下标。这是无损压缩，前端解引用即可还原，5 家的规模因此回到 ~2 MB 量级。

用法:
    python build_ui.py                        # 默认输出 assets/dashboard.html
    python build_ui.py --out assets/panel.html
    python build_ui.py --store <门店编码>    # 只烘焙指定门店（可逗号分隔多家）
    python build_ui.py --grid-only            # 只算网格，不写 HTML（自检用）

仅标准库。
"""
import argparse
import glob
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import optimize  # noqa: E402
from mcp_payload import load_tool_text  # noqa: E402

PEOPLE_OPTIONS = [1, 2, 3, 4, 5, 6]
BUDGET_MIN, BUDGET_MAX, BUDGET_STEP = 20, 200, 10
MODES = ["value", "slim", "muscle"]

# 场景预设：主网格之外的「精选场景」，直接给出该场景下合理的预算
SCENARIO_PRESETS = [
    {"key": "solo", "label": "单人工作日", "people": 1, "budget": 45, "scenario": "solo"},
    {"key": "date", "label": "约会", "people": 2, "budget": 120, "scenario": "date"},
    {"key": "family", "label": "家庭", "people": 4, "budget": 160, "scenario": "family"},
    {"key": "birthday", "label": "生日派对", "people": 6, "budget": 220, "scenario": "birthday"},
    {"key": "team", "label": "团建", "people": 8, "budget": 300, "scenario": "team"},
]

MODE_LABELS = {"value": "常规性价比", "slim": "减脂", "muscle": "健身增肌"}

LIVE_ROOT = os.path.join(ROOT, "data", "live_stores")
DEFAULT_UNSELLABLE = os.path.join(ROOT, "data", "unsellable.json")


# --------------------------------------------------------------------------
# 跨门店共享对象池（无损去重，决定面板体积）
# --------------------------------------------------------------------------


class Interner:
    """把重复出现的对象归一成一个全局数组，网格里只存 {"r": 下标}。

    网格里的 items / checks / 营养覆盖度 / warnings 高度重复：
    同一家门店不同参数之间重复，不同门店之间也重复。
    不做这层去重，5 家门店的 HTML 会膨胀到 7 MB 以上。
    """

    def __init__(self):
        self.pool = []
        self._index = {}

    def add(self, obj):
        key = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        idx = self._index.get(key)
        if idx is None:
            idx = len(self.pool)
            self.pool.append(obj)
            self._index[key] = idx
        return {"r": idx}

    def add_list(self, items):
        return [self.add(x) for x in items]

    def stats(self):
        return len(self.pool)


# --------------------------------------------------------------------------
# 门店
# --------------------------------------------------------------------------


def store_codes():
    """已取数的门店编码列表（按 index.json 顺序）。"""
    path = os.path.join(LIVE_ROOT, "index.json")
    if os.path.isfile(path):
        with io.open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
        return [str(r.get("code")) for r in rows if isinstance(r, dict) and r.get("code")]
    return []


def load_stores():
    """门店清单：[{code,name,address,distance,status,hours,...}]。"""
    path = os.path.join(LIVE_ROOT, "index.json")
    if os.path.isfile(path):
        with io.open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
        out = []
        for r in rows:
            if not isinstance(r, dict) or not r.get("code"):
                continue
            out.append(
                {
                    "code": str(r.get("code")),
                    "name": r.get("name") or str(r.get("code")),
                    "address": r.get("address") or "",
                    "distance": r.get("distance"),
                    "status": r.get("status") or "",
                    "hours": r.get("hours") or "",
                }
            )
        if out:
            return out

    # 回退：单店模式，用 _probe/stores.json 的第一家
    probe = os.path.join(ROOT, "_probe", "stores.json")
    if not os.path.isfile(probe):
        return []
    obj, _ = load_tool_text(probe)
    rows = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(rows, list) or not rows:
        return []
    row = rows[0]
    return [
        {
            "code": str(row.get("storeCode") or ""),
            "name": row.get("storeName") or "",
            "address": row.get("storeAddress") or row.get("address") or "",
            "distance": row.get("distance"),
            "status": row.get("businessStatus") or "",
            "hours": row.get("businessHours") or "",
        }
    ]


_CN_NUM = "一二三四五六七八九十"


def anonymize_stores(stores):
    """门店脱敏：真实门店名/地址/编码/精确距离一律不写进 HTML。

    面板是要公开到仓库的，而「门店名 + 距你 141 米」这两条信息合起来，
    足以反推使用者日常所处的具体位置——这属于个人信息，不能公开。
    做法是按距离排序后改成「一号店…N号店」，编码换成 S1…Sn，
    地址只留城市，距离压成档位；菜单/价格/券等业务数据完全不动。

    返回 (脱敏后的 stores, 旧编码 -> 新编码 的映射)。
    """
    ordered = sorted(
        stores,
        key=lambda s: (s.get("distance") is None, s.get("distance") or 0),
    )
    code_map = {}
    out = []
    for i, s in enumerate(ordered, 1):
        new_code = "S{}".format(i)
        code_map[str(s["code"])] = new_code
        label = _CN_NUM[i - 1] if i <= len(_CN_NUM) else str(i)
        out.append(
            {
                "code": new_code,
                "name": "麦当劳 · {}号店".format(label),
                "address": "地址已脱敏",
                "distance": s.get("distance"),
                "status": s.get("status"),
                "hours": s.get("hours"),
            }
        )
    return out, code_map


def mask_credential(value, head=4, tail=3):
    """把券码/券 ID 打码成 MCD6…L9T 这类可辨认但不可用的形式。

    券码本身不是账号凭证，但它是能直接下单的兑换凭据，
    按参赛声明的口径一律不公开原值。
    """
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= head + tail:
        return "•••"
    return "{}…{}".format(text[:head], text[-tail:])


def sanitize_deals(deals):
    """券商品清单脱敏：只打码凭据字段，价格/营养/有效期等业务信息保留。"""
    out = []
    for d in deals or []:
        item = dict(d)
        if item.get("coupon_id"):
            item["coupon_id"] = mask_credential(item["coupon_id"])
        if item.get("coupon_code"):
            item["coupon_code"] = mask_credential(item["coupon_code"])
        out.append(item)
    return out


def store_dir(code):
    """优先用多店目录，回退到旧的扁平 data/ 目录（单店模式）。"""
    d = os.path.join(LIVE_ROOT, code)
    if os.path.isfile(os.path.join(d, "live_menu.json")):
        return d
    return os.path.join(ROOT, "data")


def unsellable_for(code):
    """不可售清单是**按门店**的，不能跨店串用。"""
    per_store = os.path.join(LIVE_ROOT, code, "unsellable.json")
    if os.path.isfile(per_store):
        return per_store
    if os.path.isfile(DEFAULT_UNSELLABLE):
        try:
            with io.open(DEFAULT_UNSELLABLE, encoding="utf-8") as fh:
                obj = json.load(fh)
            if str(obj.get("store") or "") == str(code):
                return DEFAULT_UNSELLABLE
        except (OSError, ValueError):
            pass
    return os.path.join(ROOT, "data", "__none__.json")  # 不存在 -> 不排除任何餐品


def load_coverage(code):
    path = os.path.join(store_dir(code), "live_coverage.json")
    if not os.path.isfile(path):
        return {}
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_deals(code):
    """该门店的券商品清单（面板用）。与 optimize 输出的 coupon_deals 同构。"""
    path = os.path.join(store_dir(code), "live_coupons.json")
    if not os.path.isfile(path):
        return []
    from mcd_core import load_coupon_deals

    out = []
    for d in load_coupon_deals(path):
        out.append(
            {
                "name": d["name"],
                "price_yuan": round(d["price_fen"] / 100.0, 2),
                "list_price_yuan": round(d["list_price_fen"] / 100.0, 2),
                "category": d["category"],
                "portions": d["portions"],
                "coupon_id": d["coupon_id"],
                "coupon_code": d["coupon_code"],
                "virtual_product_code": d["virtual_product_code"],
                "valid_until": d["valid_until"],
            }
        )
    return out


# --------------------------------------------------------------------------
# 网格
# --------------------------------------------------------------------------


def make_args(people, budget, mode, scenario="", strategy="value", delivery_fen=0, store=""):
    d = store_dir(store) if store else os.path.join(ROOT, "data")
    return argparse.Namespace(
        menu=os.path.join(d, "live_menu.json"),
        coupons=os.path.join(d, "live_coupons.json"),
        people=people,
        budget=str(budget),
        mode=mode,
        scenario=scenario,
        strategy=strategy,
        delivery_fee="0",
        delivery_fee_fen=delivery_fen,
        coupon_headroom="15",
        coupon_headroom_fen=1500,
        max_coupons=2,
        max_order_coupons=1,
        max_coupon_deals=1,
        rules=optimize.DEFAULT_RULES,
        scenarios=optimize.DEFAULT_SCENARIOS,
        unsellable=unsellable_for(store) if store else optimize.DEFAULT_UNSELLABLE,
        ignore_unsellable=False,
        json_out="",
        format="json",
    )


def compact_plan(plan, pool):
    if not plan:
        return None
    chosen = plan.get("coupon", {}).get("chosen", {})
    return {
        "label": plan["label"],
        "total": plan["total_yuan"],
        "per": plan["per_person_yuan"],
        "subtotal": plan["subtotal_yuan"],
        "discount": round(plan["discount_fen"] / 100.0, 2),
        "delivery": round(plan["delivery_fen"] / 100.0, 2),
        "remaining": plan["remaining_yuan"],
        "within": plan["within_budget"],
        "structure": plan["structure"],
        "rationale": plan.get("rationale", ""),
        "items": pool.add_list(
            [
                {
                    "n": it["name"],
                    "cat": it["category"],
                    "price": it["price_yuan"],
                    "kcal": it["nutrition"].get("energy_kcal", 0),
                    "p": it["nutrition"].get("protein_g", 0),
                    "f": it["nutrition"].get("fat_g", 0),
                    "c": it["nutrition"].get("carb_g", 0),
                    "known": it["nutrition_known"],
                    "hf": it["is_high_fat"],
                    "deal": it["is_coupon_deal"],
                    "list": round(it.get("list_price_fen", 0) / 100.0, 2),
                    "cid": it.get("coupon_id", ""),
                    "ccode": it.get("coupon_code", ""),
                    "vcode": it.get("virtual_product_code", ""),
                    "until": it.get("valid_until", ""),
                }
                for it in plan["items"]
            ]
        ),
        "parts": chosen.get("parts") or [],
        "conflict": plan["coupon"].get("conflict_note"),
        "unverifiable": plan["coupon"].get("unverifiable") or [],
        "nut_total": plan["nutrition_total"],
        "nut_pp": plan["nutrition_per_person"],
        "cov": pool.add(plan["nutrition_coverage"]),
        "checks": pool.add(plan["checks"]),
        "reps": plan["replacements"],
        "deal_count": plan.get("deal_count", 0),
        "deal_savings": round(plan.get("deal_savings_fen", 0) / 100.0, 2),
    }


def compact(result, pool):
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "未知错误"), "hint": result.get("hint", "")}
    return {
        "ok": True,
        "input": result["input"],
        "primary": compact_plan(result["primary"], pool),
        "alternative": compact_plan(result["alternative"], pool),
        "cheapest": result["cheapest"],
        "budget_mode": result["budget_mode"],
        "warnings": pool.add_list(result["warnings"]),
        "candidates": result["candidates_considered"],
    }


def build_grid(stores, pool, verbose=True):
    """逐门店预计算整张参数网格。对象池在门店之间共享，跨店重复项也能去重。"""
    grids = {}
    total = 0
    t0 = time.time()
    for store in stores:
        code = store["code"]
        grid = {}
        for people in PEOPLE_OPTIONS:
            for budget in range(BUDGET_MIN, BUDGET_MAX + 1, BUDGET_STEP):
                for mode in MODES:
                    grid["{}|{}|{}".format(people, budget, mode)] = compact(
                        optimize.run(make_args(people, budget, mode, store=code)), pool
                    )
                    total += 1
        for preset in SCENARIO_PRESETS:
            grid["s:{}".format(preset["key"])] = compact(
                optimize.run(
                    make_args(preset["people"], preset["budget"], "value", preset["scenario"], store=code)
                ),
                pool,
            )
            total += 1
        grids[code] = grid
        if verbose:
            print("  {} 已烘焙 {} 组".format(code, len(grid)))
    if verbose:
        print("预计算 {} 组参数（{} 家门店），耗时 {:.2f} 秒".format(total, len(stores), time.time() - t0))
    return grids


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>麦麦智配 · 配餐决策面板</title>
<style>
:root{--red:#DA291C;--yellow:#FFC72C;--ink:#1f2328;--muted:#6b7280;--line:#e8eaed;
  --bg:#f5f6f8;--card:#fff;--ok:#1a7f37;--warn:#b45309}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);
  font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:22px 20px 60px}
header.top{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;
  justify-content:space-between;padding-bottom:16px;border-bottom:2px solid var(--line)}
.brand{display:flex;align-items:center;gap:12px}
.mark{width:40px;height:40px;border-radius:11px;background:var(--red);color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:700}
h1{font-size:19px;font-weight:700;letter-spacing:.2px}
.sub{color:var(--muted);font-size:12.5px;margin-top:2px}
.store{text-align:right;font-size:12.5px;color:var(--muted);line-height:1.7}
.store b{color:var(--ink);font-size:13.5px;font-weight:600}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;
  background:#fff3d4;color:#8a6100;border:1px solid #ffe1a3;margin-left:6px}
.storesel{display:flex;align-items:center;gap:8px;justify-content:flex-end}
.storesel span{font-size:12px;color:var(--muted)}
select{font-family:inherit;font-size:13px;padding:6px 10px;border:1px solid var(--line);
  border-radius:9px;background:#fff;color:var(--ink);cursor:pointer;max-width:320px}
select:hover{border-color:#cbd0d6}

.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:16px 18px;margin-top:16px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.panel h2{font-size:13px;font-weight:600;color:var(--muted);letter-spacing:.6px;
  text-transform:uppercase;margin-bottom:12px}
.ctrls{display:flex;flex-wrap:wrap;gap:22px;align-items:flex-start}
.ctrl{display:flex;flex-direction:column;gap:7px}
.ctrl>span{font-size:12px;color:var(--muted)}
.btns{display:flex;flex-wrap:wrap;gap:6px}
.btns button{border:1px solid var(--line);background:#fff;color:var(--ink);
  padding:6px 13px;border-radius:9px;cursor:pointer;font-size:13px;font-family:inherit;
  transition:all .12s}
.btns button:hover{border-color:#cbd0d6;background:#fafbfc}
.btns button.on{background:var(--red);border-color:var(--red);color:#fff;font-weight:600}
input[type=range]{width:250px;accent-color:var(--red);cursor:pointer}
.budgetval{font-weight:700;font-size:17px;color:var(--red);min-width:74px;display:inline-block}
.tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);padding:0 18px;margin-bottom:0}
.tabs button{border:0;background:none;padding:12px 4px;margin-right:20px;cursor:pointer;
  font-family:inherit;font-size:14px;color:var(--muted);border-bottom:2px solid transparent}
.tabs button.on{color:var(--red);border-bottom-color:var(--red);font-weight:600}

.hero{display:flex;flex-wrap:wrap;gap:18px;align-items:baseline;
  padding:4px 0 14px;border-bottom:1px dashed var(--line);margin-bottom:14px}
.price{font-size:34px;font-weight:750;color:var(--red);line-height:1.1}
.price small{font-size:15px;font-weight:600;margin-left:2px}
.meta{color:var(--muted);font-size:12.5px}
.meta b{color:var(--ink)}
.badge{display:inline-block;padding:2px 9px;border-radius:6px;font-size:12px;font-weight:600}
.badge.ok{background:#e7f6ec;color:var(--ok)}
.badge.over{background:#fdeaea;color:var(--red)}
.badge.deal{background:#fff3d4;color:#8a6100}

table{width:100%;border-collapse:collapse;font-size:13px;margin-top:4px}
th{text-align:left;color:var(--muted);font-weight:500;font-size:12px;
  padding:7px 8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid #f2f4f6;vertical-align:middle}
td.num{text-align:right;font-variant-numeric:tabular-nums}
tr.deal td{background:#fffdf5}
.tag{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11.5px;
  background:#f2f4f6;color:var(--muted);margin-left:6px}
.tag.f{background:#fdeaea;color:var(--red)}
.tag.d{background:#fff3d4;color:#8a6100}

.bars{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-top:8px}
.bar{display:flex;flex-direction:column;gap:5px}
.bar .lab{display:flex;justify-content:space-between;font-size:12.5px}
.bar .lab b{font-variant-numeric:tabular-nums}
.track{height:7px;background:#eef0f3;border-radius:6px;overflow:hidden}
.fill{height:100%;background:var(--red);border-radius:6px}
.fill.y{background:var(--yellow)}
.fill.ok{background:var(--ok)}
.chk{display:flex;flex-direction:column;gap:5px;margin-top:10px;font-size:13px}
.chk div{display:flex;gap:8px}
.chk .yes{color:var(--ok)}.chk .no{color:var(--red);font-weight:600}

.note{margin-top:12px;padding:10px 12px;border-radius:10px;font-size:12.5px;
  background:#fbfbfc;border:1px solid var(--line);color:var(--muted)}
.note.warn{background:#fffbf2;border-color:#f7e2b8;color:#7a5a12}
.note b{color:var(--ink)}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kv div{font-size:12.5px;color:var(--muted)}
.kv span{display:block;font-size:17px;font-weight:650;color:var(--ink);
  font-variant-numeric:tabular-nums;margin-top:2px}
.empty{padding:30px;text-align:center;color:var(--muted)}
code{background:#f2f4f6;padding:1px 6px;border-radius:5px;font-size:12px;
  font-family:ui-monospace,Consolas,monospace}
.foot{margin-top:22px;text-align:center;color:var(--muted);font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="brand">
      <div class="mark">M</div>
      <div>
        <h1>麦麦智配 · 配餐决策面板</h1>
        <div class="sub">预算 × 营养 × 优惠券 三组约束同时成立的组合｜数据来自麦当劳中国 MCP</div>
      </div>
    </div>
    <div class="store">
      <div class="storesel"><span>餐厅</span><select id="storeSel"></select></div>
      <div id="storeInfo" style="margin-top:6px"></div>
    </div>
  </header>

  <div class="panel">
    <h2>参数</h2>
    <div class="ctrls">
      <div class="ctrl"><span>用餐人数</span><div class="btns" id="peopleBtns"></div></div>
      <div class="ctrl"><span>预算上限</span>
        <div class="btns" style="align-items:center">
          <input type="range" id="budgetRange" min="__BMIN__" max="__BMAX__" step="__BSTEP__">
          <span class="budgetval" id="budgetVal"></span>
        </div>
      </div>
      <div class="ctrl"><span>配餐模式</span><div class="btns" id="modeBtns"></div></div>
    </div>
    <div class="ctrl" style="margin-top:16px"><span>场景预设</span>
      <div class="btns" id="scenarioBtns"></div>
    </div>
  </div>

  <div class="panel" style="padding:0;overflow:hidden">
    <div class="tabs">
      <button id="tabPrimary" class="on">主方案</button>
      <button id="tabAlt">备选方案</button>
    </div>
    <div id="result" style="padding:16px 18px"></div>
  </div>

  <div class="panel" id="dealPanel"></div>
  <div class="panel" id="coverPanel"></div>

  <div class="foot">
    由 mcd-meal-optimizer 生成 · 所有金额与营养均由本地确定性求解器预计算，前端不参与算术<br>
    门店名称、地址、编码与距离档位均已脱敏；券码与券 ID 已打码，原始值只存在于本地<br>
    __STAMP__
  </div>
</div>

<script id="grid" type="application/json">__GRID__</script>
<script>
var GRIDS = JSON.parse(document.getElementById('grid').textContent);
var POOL = __POOL__;
var STORES = __STORES__;
var COVERS = __COVERS__;
var SLIST = __SLIST__;
var state = {store: SLIST.length?SLIST[0].code:'', people: 2, budget: 80, mode: 'value',
  tab: 'primary', scenario: null};

var CAT_LABEL = {MAIN:'主餐',SIDE:'小食',DRINK:'饮品',DESSERT:'甜品',COMBO:'套餐',
  OTHER:'其他',BREAKFAST:'早餐',MERCH:'周边'};
var MODE_LABEL = {value:'常规性价比',slim:'减脂',muscle:'健身增肌'};
// 营养参考值（仅用于画条，不是达标阈值；阈值以求解器给出的 checks 为准）
var REF = {value:{kcal:900,p:30,f:30,c:110},slim:{kcal:750,p:30,f:25,c:90},
  muscle:{kcal:1100,p:45,f:40,c:110}};

function money(v){return (Math.round(v*100)/100).toFixed(2);}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
// 距离只给档位：精确到米会暴露使用者所在位置
function fmtDist(d){
  if(typeof d!=='number') return '';
  if(d<300) return '步行可达';
  if(d<1000) return '1 km 内';
  if(d<2000) return '2 km 内';
  return '较远';
}

// 对象池解引用：{"r":n} -> POOL[n]。只认「单键且键名是 r 的整数」，不会误伤业务对象。
function deref(v){
  if(Array.isArray(v)){for(var i=0;i<v.length;i++)v[i]=deref(v[i]);return v;}
  if(v&&typeof v==='object'){
    if(typeof v.r==='number'){
      var ks=Object.keys(v);
      if(ks.length===1) return POOL[v.r];
    }
    for(var k in v)v[k]=deref(v[k]);
    return v;
  }
  return v;
}
var _cache={};
function currentKey(){
  if(state.scenario) return 's:'+state.scenario;
  return state.people+'|'+state.budget+'|'+state.mode;
}
function currentResult(){
  var g=GRIDS[state.store];
  if(!g) return null;
  var k=currentKey(), ck=state.store+'|'+k;
  if(!(ck in _cache)){var raw=g[k];_cache[ck]=raw?deref(raw):null;}
  return _cache[ck];
}
function currentCover(){return COVERS[state.store]||{};}

function renderStoreSel(){
  var sel=document.getElementById('storeSel');
  sel.innerHTML=SLIST.map(function(s){
    var d=fmtDist(s.distance);
    return '<option value="'+esc(s.code)+'">'+esc(s.name)+(d?'（'+d+'）':'')+'</option>';
  }).join('');
  sel.value=state.store;
  sel.onchange=function(e){
    state.store=e.target.value;
    renderAll();
  };
}

function renderControls(){
  var pb=document.getElementById('peopleBtns');
  pb.innerHTML='';
  __PEOPLE__.forEach(function(n){
    var b=document.createElement('button');
    b.textContent=n+' 人';
    if(!state.scenario&&state.people===n) b.className='on';
    b.onclick=function(){state.people=n;state.scenario=null;renderAll();};
    pb.appendChild(b);
  });
  var mb=document.getElementById('modeBtns');
  mb.innerHTML='';
  ['value','slim','muscle'].forEach(function(m){
    var b=document.createElement('button');
    b.textContent=MODE_LABEL[m];
    if(!state.scenario&&state.mode===m) b.className='on';
    b.onclick=function(){state.mode=m;state.scenario=null;renderAll();};
    mb.appendChild(b);
  });
  var sb=document.getElementById('scenarioBtns');
  sb.innerHTML='';
  var clr=document.createElement('button');
  clr.textContent='清除场景';
  if(!state.scenario) clr.className='on';
  clr.onclick=function(){state.scenario=null;renderAll();};
  sb.appendChild(clr);
  __SCENARIOS__.forEach(function(s){
    var b=document.createElement('button');
    b.textContent=s.label;
    if(state.scenario===s.key) b.className='on';
    b.onclick=function(){state.scenario=s.key;renderAll();};
    sb.appendChild(b);
  });
  document.getElementById('budgetRange').value=state.budget;
  document.getElementById('budgetVal').textContent='¥'+state.budget;
  document.getElementById('budgetRange').oninput=function(e){
    state.budget=parseInt(e.target.value,10);state.scenario=null;
    document.getElementById('budgetVal').textContent='¥'+state.budget;
    renderAll();
  };
  document.getElementById('tabPrimary').className=state.tab==='primary'?'on':'';
  document.getElementById('tabAlt').className=state.tab==='alternative'?'on':'';
}

function itemsTable(plan){
  var rows=plan.items.map(function(it){
    var nut=it.known?(Math.round(it.kcal)+' / '+Math.round(it.p)+' / '+Math.round(it.f)+' / '+Math.round(it.c)):'—（无数据）';
    var tag='';
    if(it.deal) tag='<span class="tag d">🎟️券商品</span>';
    else if(it.hf) tag='<span class="tag f">高油</span>';
    return '<tr class="'+(it.deal?'deal':'')+'"><td>'+esc(it.n)+tag+'</td>'+
      '<td>'+esc(CAT_LABEL[it.cat]||it.cat)+'</td>'+
      '<td class="num">'+money(it.price)+(it.deal&&it.list?'<div style="color:#8a6100;font-size:11.5px">原价 '+money(it.list)+'</div>':'')+'</td>'+
      '<td class="num">'+nut+'</td></tr>';
  }).join('');
  return '<table><thead><tr><th>餐品</th><th>品类</th><th style="text-align:right">单价</th>'+
    '<th style="text-align:right">热量/蛋白/脂肪/碳水</th></tr></thead><tbody>'+rows+'</tbody></table>';
}

function bars(pp,mode){
  var r=REF[mode]||REF.value;
  var defs=[['热量 (kcal)',pp.energy_kcal||0,r.kcal,''],
            ['蛋白质 (g)',pp.protein_g||0,r.p,'y'],
            ['脂肪 (g)',pp.fat_g||0,r.f,''],
            ['碳水 (g)',pp.carb_g||0,r.c,'y']];
  return '<div class="bars">'+defs.map(function(d){
    var pct=Math.max(3,Math.min(100,Math.round(d[1]/d[2]*100)));
    return '<div class="bar"><div class="lab"><span>'+d[0]+'</span><b>'+Math.round(d[1])+
      ' <span style="color:#9aa1a9">/ 参考 '+d[2]+'</span></b></div>'+
      '<div class="track"><div class="fill '+d[3]+'" style="width:'+pct+'%"></div></div></div>';
  }).join('')+'</div>';
}

function dealPanel(plan){
  var el=document.getElementById('dealPanel');
  var cov=currentCover();
  var deals=cov.deals||[];
  var storeName=(STORES[state.store]||{}).name||'本门店';
  if(!deals.length){el.innerHTML='<h2>优惠券</h2><div class="empty">'+esc(storeName)+'当前没有可用的「用券价格」券</div>';return;}
  var used={};
  if(plan&&plan.items) plan.items.forEach(function(it){if(it.deal) used[it.n]=1;});
  var rows=deals.map(function(d){
    var save=Math.max(0,d.list_price_yuan-d.price_yuan);
    return '<tr><td>'+esc(d.name)+(used[d.name]?'<span class="tag d">已采用</span>':'')+'</td>'+
      '<td>'+esc(CAT_LABEL[d.category]||d.category)+'</td>'+
      '<td class="num">'+money(d.price_yuan)+'</td>'+
      '<td class="num" style="text-decoration:line-through;color:#9aa1a9">'+money(d.list_price_yuan)+'</td>'+
      '<td class="num" style="color:#DA291C;font-weight:700">'+money(save)+'</td>'+
      '<td style="color:#6b7280">'+esc(d.valid_until||'—')+'</td>'+
      '<td><code>'+esc(d.coupon_code||'')+'</code></td></tr>';
  }).join('');
  var best=deals.reduce(function(a,d){return Math.max(a,Math.max(0,(d.list_price_yuan||0)-(d.price_yuan||0)));},0);
  el.innerHTML='<h2>优惠券 · 已由服务端核价</h2>'+
    '<table><thead><tr><th>券商品</th><th>品类</th><th style="text-align:right">券价</th>'+
    '<th style="text-align:right">原价</th><th style="text-align:right">省</th><th>有效期</th><th>couponCode</th>'+
    '</tr></thead><tbody>'+rows+'</tbody></table>'+
    '<div class="note"><b>比价结论：</b>券价由 <code>calculate-price</code> 服务端折算，非本地估算；'+
    '单张券最高可省 <b>'+money(best)+'</b> 元。券商品已与同品类餐品一起参与组合优化（本单限用 '+
    (cov.deal_limit!=null?cov.deal_limit:1)+' 个）。'+
    '<b>未被选中不代表不可用</b>——只是当前预算下它没能让整单更优；'+
    '若你本来就想要这份商品，直接带 couponCode 下单即可。</div>';
}

function coverPanel(){
  var cov=currentCover();
  var c=cov.coverage||{};
  var warns=cov.warnings||[];
  var s=STORES[state.store]||{};
  var el=document.getElementById('coverPanel');
  var kv=[['在售餐品',c.menu_items],['有营养数据',c.nutrition_items+'（'+Math.round((c.nutrition_ratio||0)*100)+'%）'],
    ['套餐类',c.combo_items],['已展开求和',c.combo_solved],
    ['营养表条目',c.nutrition_table_size],['可用券',c.coupons],
    ['服务端核价',c.coupons_priced_by_server],['券商品',c.coupon_deals]];
  el.innerHTML='<h2>数据覆盖度 · '+esc(s.name||'')+'</h2><div class="kv">'+kv.map(function(x){
    return '<div>'+x[0]+'<span>'+esc(x[1])+'</span></div>';}).join('')+'</div>'+
    (warns.length?'<div class="note warn">'+warns.map(function(w){return '⚠️ '+esc(w);}).join('<br>')+'</div>':'');
}

function renderResult(){
  var r=currentResult();
  var el=document.getElementById('result');
  if(!r){el.innerHTML='<div class="empty">该参数组合未预计算</div>';return;}
  if(!r.ok){
    el.innerHTML='<div class="empty"><b style="color:#DA291C">这个预算装不下</b><br>'+
      esc(r.error)+'<br><span style="font-size:12.5px">'+esc(r.hint||'')+'</span></div>';
    document.getElementById('dealPanel').innerHTML='';
    return;
  }
  var plan=state.tab==='primary'?r.primary:r.alternative;
  if(!plan){
    el.innerHTML='<div class="empty">该参数下没有结构不同的备选方案</div>';
    dealPanel(r.primary);return;
  }
  var inp=r.input;
  var badge=plan.within?'<span class="badge ok">预算内</span>':'<span class="badge over">超预算</span>';
  var html='<div class="hero"><div><div class="price">¥'+money(plan.total)+'<small></small></div>'+
    '<div class="meta">人均 <b>¥'+money(plan.per)+'</b>　'+badge+
    (plan.discount>0?'　<span class="badge deal">已省 ¥'+money(plan.discount)+'</span>':'')+
    (plan.deal_savings>0?'　<span class="badge deal">券商品省 ¥'+money(plan.deal_savings)+'</span>':'')+
    '</div></div>'+
    '<div class="meta" style="margin-left:auto;text-align:right">'+
    esc(inp.scenario_label)+'｜'+inp.people+' 人｜预算 ¥'+money(inp.budget_yuan)+'<br>'+
    esc(inp.structure)+'<br>候选 '+r.candidates+' 套'+
    (plan.within?'　剩余 ¥'+money(plan.remaining):'　超出 ¥'+money(-plan.remaining))+'</div></div>';
  if(plan.rationale) html+='<div class="note">'+esc(plan.rationale)+'</div>';

  html+=itemsTable(plan);

  html+='<div style="margin-top:14px" class="meta">优惠前 <b>¥'+money(plan.subtotal)+'</b>'+
    (plan.discount>0?'　优惠 <b style="color:#DA291C">-¥'+money(plan.discount)+'</b>':'')+
    (plan.delivery>0?'　配送费 <b>+¥'+money(plan.delivery)+'</b>':'')+
    '　优惠后 <b style="color:#DA291C">¥'+money(plan.total)+'</b></div>';

  var parts=plan.parts||[];
  if(parts.length){
    html+='<div class="note">'+parts.map(function(p){
      return '🎟️ '+esc(p.name)+'　-¥'+money(p.amount_fen/100)+(p.expires?'（有效期至 '+esc(p.expires)+'）':'');}).join('<br>')+'</div>';
  }
  var unv=plan.unverifiable||[];
  if(unv.length){
    html+='<div class="note warn">另有 '+unv.length+' 张券未计入优惠（本地无法确定优惠额）：<br>'+
      unv.map(function(u){return '· '+esc(u.name)+'：'+esc(u.reason);}).join('<br>')+'</div>';
  }
  if(plan.conflict) html+='<div class="note warn">互斥提醒：'+esc(plan.conflict)+'</div>';

  var pp=plan.nut_pp||{}, cov=plan.cov||{};
  html+='<div style="margin-top:18px" class="panel" ><h2>每人份营养'+
    (cov.complete?'':'（仅统计 '+cov.known+' / '+cov.total+' 项）')+'</h2>'+
    bars(pp,state.mode)+'</div>';
  var checks=plan.checks||[];
  if(checks.length){
    html+='<div class="chk">'+checks.map(function(c){
      return '<div><span class="'+(c.ok?'yes':'no')+'">'+(c.ok?'✅':'❌')+'</span>'+
        '<span><b>'+esc(c.label)+'</b>　'+esc(c.detail)+'</span></div>';}).join('')+'</div>';
  }
  if(cov.complete===false&&cov.known>0){
    html+='<div class="note warn">缺营养数据的餐品：'+esc((cov.unknown_names||[]).join('、'))+
      '。以上汇总<b>未计入</b>这些餐品，实际值更高。</div>';
  }
  if(cov.known===0){
    html+='<div class="note warn">本方案餐品<b>全部</b>缺营养数据，营养汇总与达标判定不成立，仅供参考价格。</div>';
  }
  var reps=plan.reps||[];
  if(reps.length){
    html+='<div class="note">高油替换建议：<br>'+reps.slice(0,4).map(function(rp){
      return '· '+esc(rp.replace)+' → <b>'+esc(rp.with)+'</b>：脂肪 '+Math.round(rp.fat_delta_g)+
        'g，蛋白 '+Math.round(rp.protein_delta_g)+'g，热量 '+Math.round(rp.kcal_delta)+
        ' kcal，价格 '+(rp.price_delta_yuan>0?'+':'')+money(rp.price_delta_yuan)+' 元';
    }).join('<br>')+'</div>';
  }
  el.innerHTML=html;
  dealPanel(plan);
}

function renderStore(){
  var s=STORES[state.store]||{};
  var el=document.getElementById('storeInfo');
  if(!s.name){el.innerHTML='';return;}
  var dist=fmtDist(s.distance);
  el.innerHTML=
    '<b>'+esc(s.name)+'</b><span class="pill">'+esc(s.status||'营业中')+'</span><br>'+
    (dist?'距你约 '+dist+'　':'')+
    esc(s.address||'');
}

function renderAll(){renderStoreSel();renderControls();renderResult();renderStore();coverPanel();}

document.getElementById('tabPrimary').onclick=function(){state.tab='primary';renderAll();};
document.getElementById('tabAlt').onclick=function(){state.tab='alternative';renderAll();};

renderAll();
</script>
</body>
</html>
"""


def render_html(grids, stores, covers, pool, stamp=None):
    people_js = json.dumps(PEOPLE_OPTIONS)
    scen_js = json.dumps([{"key": s["key"], "label": s["label"]} for s in SCENARIO_PRESETS], ensure_ascii=False)
    grid_js = json.dumps(grids, ensure_ascii=False, separators=(",", ":"))
    pool_js = json.dumps(pool, ensure_ascii=False, separators=(",", ":"))
    stores_js = json.dumps({s["code"]: s for s in stores}, ensure_ascii=False)
    covers_js = json.dumps(covers, ensure_ascii=False)
    slist_js = json.dumps(
        [
            {"code": s["code"], "name": s["name"], "distance": s["distance"], "status": s["status"]}
            for s in stores
        ],
        ensure_ascii=False,
    )
    html = HTML_TEMPLATE
    # 防止 </script> 提前闭合脚本块
    for token, value in (
        ("__GRID__", grid_js),
        ("__POOL__", pool_js),
        ("__STORES__", stores_js),
        ("__COVERS__", covers_js),
        ("__SLIST__", slist_js),
        ("__SCENARIOS__", scen_js),
        ("__PEOPLE__", people_js),
    ):
        html = html.replace(token, value.replace("</", "<\\/"))
    html = html.replace("__BMIN__", str(BUDGET_MIN))
    html = html.replace("__BMAX__", str(BUDGET_MAX))
    html = html.replace("__BSTEP__", str(BUDGET_STEP))
    html = html.replace("__STAMP__", stamp or time.strftime("生成于 %Y-%m-%d %H:%M"))
    return html


def main() -> int:
    parser = argparse.ArgumentParser(description="生成麦麦智配可视化面板")
    parser.add_argument("--out", default=os.path.join(ROOT, "assets", "dashboard.html"))
    parser.add_argument("--store", default="", help="只烘焙指定门店（逗号分隔可多家）；默认 index.json 里的全部门店")
    parser.add_argument("--grid-only", action="store_true", help="只预计算网格，不写 HTML")
    parser.add_argument(
        "--no-anonymize",
        action="store_true",
        help="保留真实门店名/地址/编码与券码（仅限本地自用，切勿用于公开仓库）",
    )
    args = parser.parse_args()

    stores = load_stores()
    if args.store:
        want = [c.strip() for c in args.store.split(",") if c.strip()]
        by_code = {s["code"]: s for s in stores}
        stores = [by_code.get(c) or {"code": c, "name": c, "address": "", "distance": None} for c in want]
    if not stores:
        print("没有可用门店：请先运行 refresh_live.py 取数（或多店模式）", file=sys.stderr)
        return 2

    pool = Interner()
    grids = build_grid(stores, pool)

    covers = {}
    for s in stores:
        cov = dict(load_coverage(s["code"]))
        cov["deal_limit"] = int(make_args(1, 30, "value", store=s["code"]).max_coupon_deals)
        cov["deals"] = load_deals(s["code"])
        covers[s["code"]] = cov

    if args.grid_only:
        print("对象池：{} 个唯一对象".format(pool.stats()))
        return 0

    if not args.no_anonymize:
        masked, code_map = anonymize_stores(stores)
        grids = {code_map[k]: v for k, v in grids.items() if k in code_map}
        covers = {code_map[k]: v for k, v in covers.items() if k in code_map}
        for c in covers:
            covers[c]["deals"] = sanitize_deals(covers[c].get("deals") or [])
        # 券商品若被选进方案，其凭据会随 items 进对象池，这里一并打码
        for obj in pool.pool:
            if isinstance(obj, dict):
                for key in ("cid", "ccode"):
                    if obj.get(key):
                        obj[key] = mask_credential(obj[key])
        stores = masked
        print("已脱敏：门店名/地址/编码/距离档位、券码与券 ID（--no-anonymize 可关闭）")

    html = render_html(grids, stores, covers, pool.pool)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    size_kb = os.path.getsize(args.out) / 1024.0
    print(
        "已写出 {}（{:.1f} KB，{} 家门店 × {} 组 = {} 组预计算，对象池 {} 个）".format(
            os.path.relpath(args.out, ROOT),
            size_kb,
            len(stores),
            len(grids[stores[0]["code"]]),
            len(stores) * len(grids[stores[0]["code"]]),
            pool.stats(),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
