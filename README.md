# 麦麦智配 · mcd-meal-optimizer

> 基于麦当劳中国 MCP 的智能配餐 Skill。在**预算、营养、优惠券**三组约束同时成立的前提下，
> 给出可以直接拿去下单的那一套组合——而不是把菜单念一遍。

[![selftest](https://img.shields.io/badge/selftest-78%2F78%20passed-brightgreen)](#自测)
[![deps](https://img.shields.io/badge/dependencies-stdlib%20only-blue)](#环境要求)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](#环境要求)
[![real-data](https://img.shields.io/badge/verified%20against-live%20MCP%20API-orange)](#与真实接口对齐实测记录)

## 它解决什么问题

在麦当劳点餐时，真正难的不是「吃什么」，而是这三件事同时成立：

1. **总价不能超预算** —— 满减门槛、折扣封顶、配送费，手算极易踩线超支；
2. **手里的券要用对** —— 哪些能叠、哪些互斥，券面上往往写不清楚；
3. **这一餐得符合我的目标** —— 减脂还是增肌，决定了油炸物和高蛋白主食怎么取舍。

这三件事互相耦合：为了凑满 50 减 12 可能要多加一份小食，但加了之后热量就超标了。
本 Skill 把这组耦合约束交给确定性算法求解，而不是让语言模型心算。

## 六项能力

| # | 能力 | 说明 |
|---|---|---|
| 1 | **预算最优配餐** | 输入预算上限与用餐人数，自动组合餐品，输出 **1 套主方案 + 1 套备选方案**，券后总价不超预算 |
| 2 | **优惠券智能组合 & 省钱比价** | 把「用券价格」券**翻译成可直接下单的券商品**（价格由 `calculate-price` 服务端核价）并参与组合优化；列出券价/原价/省/有效期/`couponCode`；互斥时明确告知被挡住的规则与差额 |
| 3 | **营养配餐推荐** | 减脂 / 健身增肌两种模式，统计热量、蛋白质、碳水、脂肪，逐条给出达标检查；减脂模式附**高油菜品替换建议** |
| 4 | **场景套餐推荐** | 识别生日 / 约会 / 团建 / 家庭 / 单人场景，按场景调整套餐骨架与权重，兼顾预算与营养 |
| 5 | **门店工具** | 查询附近门店、判断是否支持配送、按配送半径给出「可外送 / 仅到店 / 信息不足」三档结论 |
| 6 | **菜品分析** | 识别限时、即将下架、已下架菜品；对指定套餐做完整营养汇总 |

另有三项支撑能力：**服务端权威核价**（`pricer.py`，并顺带排查本地看不到的「不可售」商品）、
**可视化面板**（`build_ui.py`，单文件 HTML，免服务器）
与**下单演练**（`order.py`，默认不创建订单，加 `--confirm` 才真下单）。

## 与真实接口对齐（实测记录）

开发过程中对着真实 MCP 接口逐项验证，发现并修正了若干「照着文档写就会错」的问题。
这些结论都写进了代码与回归测试，下面只列最有代表性的几条：

| # | 实测发现 | 影响 |
|---|---|---|
| 1 | 鉴权头必须写成 `Bearer <Token>`，只填裸 Token 会返回 `403 / 校验鉴权authToken必填!` | 连接器页面表现为「需要认证」，接入直接失败 |
| 2 | 返回**不是干净 JSON**：有「markdown 说明段 + JSON」「`data` 内嵌 CSV 字符串」「纯 markdown 散文」三种形态 | 直接 `json.loads` 全部报错 |
| 3 | `data.meals` 是**以餐品编码为键的映射表**，且餐品实体**没有 `category` 字段** | 按数组解析会拿到空菜单 |
| 4 | 营养字段是 `energyKcal` / `carbohydrate`，不是 `kcal` / `carbs` | 精确键匹配下会静默取到 0 |
| 5 | **只有套餐才有 `rounds`**，单品为空数组；套餐营养必须展开默认子项求和 | 这是判断套餐的精确依据，实测比菜名可靠 |
| 6 | 券的主力形态是「**用券价格**」（付 ¥13.5 拿走某商品），不是满减；且券的商品编码与菜单编码**不在同一命名空间** | 本地算不出券后价，必须交 `calculate-price` 结算 |
| 7 | 菜单分区是**销售分区**，「小食甜品/其他」这类混合分区会把「薯条」判成甜品 | 分类改为「结构优先于分区」 |
| 8 | 营养表覆盖不全（实测 103 项菜单仅 68 项可查，约 66%） | 「未知」与「零」必须分开，否则会输出「本套餐 0 千卡」 |

第 5、6、8 条是设计层面的修正，不是补丁：**套餐营养靠子项展开**、
**券后价交服务端核价**、**营养覆盖如实标注**。这三条决定了 Skill 给出的数字是否可信。

## 环境要求

- Python **3.9+**，**仅使用标准库**，无需安装任何第三方包
- 麦当劳中国 MCP Token（[申请入口](https://open.mcd.cn/mcp)：手机号登录 → 右上角「控制台」→「激活」）
- 支持 Streamable HTTP 的 MCP 客户端：WorkBuddy / Cursor / Trae / Cherry Studio / Kiro / VSCode

## 安装

### 1. 接入麦当劳 MCP

复制 `mcp-config.example.json`，把 `YOUR_MCP_TOKEN` 换成实际 Token：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_TOKEN"
      }
    }
  }
}
```

WorkBuddy 用户：左侧边栏【专家·技能·连接器】→【连接器】→ 右上角【自定义连接器】→【配置 MCP】，
粘贴上面的 JSON，保存后在【自定义连接器】里启用 `mcd-mcp`。

### 2. 安装本 Skill

把整个目录放到客户端的 Skill 目录下即可。WorkBuddy 用户：

```bash
cp -r mcd-meal-optimizer ~/.workbuddy/skills/
```

### 3. 拉取真实门店数据

MCP 的返回形态与字段名都比较特殊，**不要手工拼 JSON**，用适配层一步到位：

```bash
# 找店 → 拉菜单/营养/券/门店 → 展开套餐营养 → 翻译成内部结构
python scripts/refresh_live.py --city <城市> --keyword <地标关键词>
# 已知门店编码时
python scripts/refresh_live.py --store <门店编码>
# 多店：可视化面板要在界面上「选餐厅」，就把附近几家一起取回来
python scripts/refresh_live.py --city <城市> --keyword <地标关键词> --all-nearby
python scripts/refresh_live.py --stores <编码1>,<编码2>,<编码3>
```

产出 `data/live_menu.json`、`data/live_coupons.json`、`data/live_coverage.json`，
后续所有能力都读这三个文件。命令结束会打印营养覆盖度报告——**这个覆盖率要如实转达给用户**。

多店模式在此外为每家门店各存一份到 `data/live_stores/<门店编码>/`，并写一份门店清单
`data/live_stores/index.json`；同时把最近的一家镜像回上面三个文件，单店脚本与自测不受影响。
营养表、个人券、账号、地址、活动日历是全店通用的，多店模式下只取一次并共享。

⚠️ **不可售清单按门店隔离**：`data/unsellable.json` 带 `store` 字段，只有门店编码匹配时才生效；
多店模式下更推荐写到 `data/live_stores/<编码>/unsellable.json`，免得把 A 店卖光的商品误判成 B 店也不可售。

调试时也可分步执行：`dump_mcp.py`（落盘原始返回）、`fetch_details.py`（只抓套餐明细）、
`mcd_adapter.py`（只做翻译）。

### 4. 自测（不需要 MCP）

```bash
python scripts/selftest.py        # 78 项断言
python scripts/make_examples.py   # 重新生成 assets/example-output.md
```

若 `_probe/` 目录存在（即跑过 `refresh_live.py`），自测会额外执行
**37 项真实数据回归**，覆盖上面那张表里的每一个坑，以及券商品、服务端核价与多门店隔离。

## 使用示例

### 直接对话（推荐）

装好 Skill 后，用自然语言即可：

> 两个人吃麦当劳，预算 80 块，想省钱一点

> 我在减脂，帮我配一份 45 块以内的，别有油炸的

> 六个人过生日，预算 220，要能分享的那种

### 命令行直接调用

```bash
# 预算最优配餐（2 人 / 80 元）
python scripts/optimize.py --menu data/live_menu.json --coupons data/live_coupons.json \
  --people 2 --budget 80 --mode value

# 减脂模式（1 人 / 45 元，自动给高油替换建议）
python scripts/optimize.py --menu data/live_menu.json --coupons data/live_coupons.json \
  --people 1 --budget 45 --mode slim

# 生日场景（6 人 / 220 元）
python scripts/optimize.py --menu data/live_menu.json --coupons data/live_coupons.json \
  --people 6 --budget 220 --scenario birthday

# 最省策略（同等满足度下最省）
python scripts/optimize.py --menu data/live_menu.json --people 3 --budget 120 --strategy frugal

# 外送场景（把配送费计入总价）
python scripts/optimize.py --menu data/live_menu.json --coupons data/live_coupons.json \
  --people 1 --budget 45 --delivery-fee 9

# 门店与配送可行性
python scripts/analyze.py --action delivery --stores _probe/stores.json \
  --addresses _probe/addresses.json --radius-km 3

# 菜品状态（限时 / 即将下架 / 已下架）
python scripts/analyze.py --action status --menu data/live_menu.json

# 指定套餐的营养汇总
python scripts/analyze.py --action nutrition --menu data/live_menu.json --codes M01,M10,D01 --people 2

# 服务端权威核价（定稿前跑一次；被拒时自动逐项排查不可售商品）
python scripts/pricer.py --menu data/live_menu.json --coupons data/live_coupons.json \
  --store <门店编码> --people 2 --budget 80

# 下单演练（默认不创建订单，只打印预览与即将提交的请求体）
python scripts/order.py --menu data/live_menu.json --coupons data/live_coupons.json \
  --store <门店编码> --people 2 --budget 80 --take-way eat-in

# 生成可视化面板（单文件 HTML，直接双击打开）
python scripts/build_ui.py            # → assets/dashboard.html
python scripts/build_ui.py --store <门店编码>  # 只烘焙指定门店（可逗号分隔多家）
```

面板顶部**可切换餐厅**（多店取数后每家都在下拉里），另有 人数 / 预算 / 模式 / 场景 四组参数。
每家的参数网格都由本地求解器预先算好烘焙进 HTML，**前端不做任何算术**。
为避免 5 家门店把文件撑到 ~7 MB，构建时用了**跨门店共享对象池**做无损去重——
实测 1735 组预计算压到 2.6 MB（对象池仅 421 个唯一对象）。

完整输出见 [`assets/example-output.md`](assets/example-output.md)，
可视化面板见 [`assets/dashboard.html`](assets/dashboard.html)。

## 输出示例（节选）

```text
## 配餐方案：常规一餐｜2 人｜预算 80.00 元｜常规性价比模式

### 主方案｜券后 80.00 元（人均 40.00 元）

| 餐品 | 品类 | 单价 | 营养（热量/蛋白/脂肪/碳水） |
|---|---|---:|---|
| 板烧鸡腿堡 ×2 | 主餐 | 25.00 | 470 / 30 / 20 / 41 |
| 玉米杯 ×2 | 小食 | 9.00 | 90 / 3 / 2 / 17 |
| 热牛奶（中） ×2 | 饮品 | 12.00 | 150 / 8 / 8 / 12 |

- 优惠前：92.00 元
- 优惠券：满 50 减 12｜减免 12.00 元（满 50.00 减 12.00），有效期至 2026-10-31 23:59
- 优惠后：80.00 元，共省 12.00 元
- 互斥提醒：「满 50 减 12」与「全场 8 折（封顶 10 元）」互斥，不能同单使用，否则可多省 10.00 元

### 备选方案｜券后 54.00 元（人均 27.00 元）
说明：换一组主餐的做法，券后比主方案再省 26.00 元
```

## 技术架构

```text
┌─────────────────────────────────────────────────────────────┐
│  Agent 层（WorkBuddy / 任意 MCP 客户端）                     │
│  · 理解意图、决定调用顺序、把结论转述给用户                  │
└───────────────────────────┬─────────────────────────────────┘
                            │ MCP 调用（返回是混合文本，不是 JSON）
┌───────────────────────────▼─────────────────────────────────┐
│  适配层（scripts/，纯标准库）                                │
│  probe_mcp.py     MCP 调用探针                               │
│  mcp_payload.py   三种混合返回形态 → 结构化对象              │
│  mcd_adapter.py   真实字段 → 内部 schema；套餐子项展开        │
│  refresh_live.py  一键编排：取数 → 展开 → 翻译               │
└───────────────────────────┬─────────────────────────────────┘
                            │ data/live_menu.json / live_coupons.json
┌───────────────────────────▼─────────────────────────────────┐
│  确定性计算层（scripts/，纯标准库）                          │
│  mcd_core.py   数据模型、品类判定、高油判定、券可核验性      │
│  optimize.py   背包 DP 组合优化 + 券商品进池 + 报告渲染       │
│  nutrition.py  营养汇总、达标判定、高油替换建议              │
│  analyze.py    菜品状态识别、门店配送判定、套餐营养汇总       │
│  pricer.py     服务端权威核价 + 不可售商品逐项排查            │
│  order.py      下单演练（默认不创建订单，需 --confirm）       │
│  build_ui.py   预计算整张参数网格 → 单文件可视化面板          │
└───────────────────────────┬─────────────────────────────────┘
                            │ 核价回灌（unsellable.json）
┌───────────────────────────▼─────────────────────────────────┐
│  麦当劳中国 MCP（https://mcp.mcd.cn）                        │
│  菜单 / 营养 / 优惠券 / 门店 / calculate-price / 下单（需确认）│
└─────────────────────────────────────────────────────────────┘
```

**核心设计：把「算」和「说」分开。** 模型负责取数、理解意图与表达；预算求和、满减门槛、
折扣封顶、背包优化这类容易出错的整数运算全部交给 Python。算法细节见
[`references/optimization.md`](references/optimization.md)。

**第二条设计原则：宁可说「不知道」，也不给假确定。**
营养查不到就标「未知」而不是 0；券算不出来就标「未核验」而不是「减免 0 元」。
在饮食决策里，「这份 0 千卡」和「这份营养未知」是两件完全不同的事。

## 目录结构

```text
mcd-meal-optimizer/
├── SKILL.md                       # Skill 主入口：六项能力的执行流程
├── README.md
├── MCP_INTEGRATION.md             # 用到的 MCP Server / Tool / 调用流程 / 业务价值
├── CONTEST_DECLARATION.md         # 参赛声明（官方原文，未修改）
├── mcp-config.example.json        # 脱敏配置示例（仅环境变量占位符）
├── workbuddy.md                   # 使用 WorkBuddy 开发的对话上下文
├── scripts/
│   ├── probe_mcp.py               # MCP 调用探针（含连通性自检）
│   ├── dump_mcp.py                # 落盘原始返回，用于摸字段
│   ├── mcp_payload.py             # 三种混合返回形态 → 结构化载荷
│   ├── mcd_adapter.py             # 真实字段 → 内部 schema；套餐子项展开
│   ├── refresh_live.py            # 一键：取数 → 展开套餐 → 翻译
│   ├── fetch_details.py           # 只抓套餐明细
│   ├── mcd_core.py                # 数据模型、品类判定、券可核验性、券商品读取
│   ├── optimize.py                # 预算最优配餐 + 券商品组合（主入口）
│   ├── nutrition.py               # 营养汇总与达标判定
│   ├── analyze.py                 # 菜品状态 / 门店配送 / 营养汇总
│   ├── pricer.py                  # 调 calculate-price 做服务端权威核价 + 不可售商品排查
│   ├── order.py                   # 下单演练（默认不创建订单，--confirm 才真下单）
│   ├── build_ui.py                # 生成单文件可视化面板 assets/dashboard.html
│   ├── selftest.py                # 自测（77 项断言）
│   └── make_examples.py           # 生成示例文档
├── references/
│   ├── mcp-tools.md               # MCP 工具速查、真实字段名、调用顺序（实测版）
│   ├── optimization.md            # 算法说明
│   ├── nutrition-rules.md         # 营养口径与阈值
│   ├── category-map.json          # 品类关键词规则（可改，不用改代码）
│   └── mcd-sections.json          # 真实菜单分区 → 品类映射（可改）
├── data/
│   ├── scenarios.json             # 场景模板（可改，不用改代码）
│   ├── sample_menu.json           # 脱敏样例数据（自测用）
│   ├── sample_coupons.json
│   ├── sample_stores.json
│   ├── sample_addresses.json
│   ├── live_*.json                # refresh_live.py 产出的真实数据快照（不入库）
│   ├── live_stores/               # 多门店取数：<门店编码>/{菜单,券,覆盖度} + index.json（不入库）
│   └── unsellable.json            # pricer.py 排查出的不可售商品（回灌给优化器，按门店隔离）
└── assets/
    ├── example-output.md
    └── dashboard.html             # build_ui.py 生成的可视化面板（单文件，免服务器）
```

> `_probe/`、`data/live_*.json`、`data/live_stores/` 与 `data/unsellable.json`
> 是本地运行产物，含个人地址/门店信息，已在 `.gitignore` 中排除。

## 设计取舍

| 取舍 | 理由 |
|---|---|
| 数字交给脚本，不给模型 | 整数约束下模型算术不可靠，最容易产出「看起来对但超预算」的方案 |
| **「未知」不当作 0** | 营养覆盖只有 ~66%，按 0 汇总会输出「本套餐 0 千卡」。未知单独列出并标注 |
| **算不出的券不硬算** | 券的商品编码与菜单不同命名空间，硬算只会得到「减免 0.00 元」这种虚假确定性 |
| 套餐营养靠子项展开 | 营养表只有单品；套餐必须展开默认子项求和，实测与手算一致 |
| 分类「结构优先于分区」 | MCP 分区是销售分区且存在混合分区，直接采信会把「薯条」判成甜品 |
| 同款餐品允许重复 | 两个人点同一款汉堡是常态；报告里按 `×N` 合并展示 |
| 评分之外单独检查营养硬约束 | 加权评分是软目标，无法保证阈值全达标，达标性必须独立参与排序 |
| 评分对「营养未知」轻微扣分 | 否则优化器会偏好便宜但查不到营养的餐品，输出没法做判断的清单 |
| 品类映射与场景模板做成数据 | 真实菜单的品类名不可预知，改 JSON 比改代码安全 |
| 预算装不下时逐级「减项」放宽 | 只减项不换项，保证放宽后的结构仍是用户看得懂的套餐形态 |
| 写操作永不自动执行 | `create-order` 必须经过用户明确确认 |

## 自测

```bash
python scripts/selftest.py
```

两个层次，共 **78 项**：

- **基础层（40 项）**：基于 `data/` 下的脱敏样例数据，覆盖六项能力的正常路径与边界——
  券后价必须等于「原价 − 优惠 + 配送费」、营养汇总与原料手算一致、互斥券必须给出差额提示、
  不支持外送的门店不能被判成可外送、缺少外送字段时不能硬下结论、
  低预算必须如实报错并给出最低所需金额；以及下单环节的安全性质——
  **多个取餐方式时必须显式指定（不许猜）、非法取餐方式被拒、到店不带地址 / 外送不带取餐方式、
  空返回不编造订单号、非 http 值不算支付链接，
  并断言「演练分支的提前返回在 create-order 调用之前」**。
  **在只装了 Skill、未跑过真实取数的机器上，这一层必定全部通过。**
- **真实数据回归层（38 项）**：基于 `_probe/` 下的真实 MCP 返回，逐条锁死本文开头那张
  「实测发现」表里的坑——三种返回形态都能解析、`energyKcal` 字段名、
  套餐营养等于子项求和（921 kcal）、周边不被当食品、薯条不被判成甜品、
  算不出的券必须标记为不可核验；以及券商品机制——「买一送一」被识别、价格为券价（13.50 元）、
  营养按份数折算（578 kcal）、带 `couponId`/`couponCode`，
  和核价侧的「同码餐品被聚合」「券商品带券字段」。**跑过 `refresh_live.py` 后会自动启用。**

## 合规与免责

- 本项目为麦当劳程序员节创意开发大赛参赛作品，由参赛者独立开发，**非麦当劳官方产品**。
- 项目输出仅供参考，不构成医疗、营养或其他专业建议；餐品信息、价格及供应状态以麦当劳官方渠道实时结果为准。
- 仓库内所有 Token、密钥、地址均已脱敏；`mcp-config.example.json` 仅保留占位符。
- 样例数据为手工整理的脱敏数据，仅用于算法验证，不代表真实在售信息。

## 致谢

- 麦当劳中国 MCP 服务：https://open.mcd.cn/mcp
- MCP Server 使用指南：https://github.com/M-China/mcd-mcp-server
- 开发工具：[WorkBuddy](https://www.workbuddy.cn/)
