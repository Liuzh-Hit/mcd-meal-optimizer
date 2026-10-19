# 项目状态总结（更新于 2026-10-16）

麦麦智配 `mcd-meal-optimizer` —— 基于麦当劳中国 MCP 的配餐决策助手。
本文件汇总**已实现功能、使用方法、已知问题**，用于交接与参赛说明。

---

## 一、目前的功能

### 定位

不是在「把菜单念一遍」，而是在**预算、营养、优惠券三组约束同时成立**的前提下，
给出可以直接去下单的那一套组合。核心设计是**把「算」和「说」分开**：
模型负责取数、理解意图、转述结论；预算求和、券价折算、背包组合这类
整数运算全部交给 Python 脚本，**前端与模型都不做算术**。

### 六项能力

| # | 能力 | 输入 | 产出 | 入口 |
|---|---|---|---|---|
| 1 | 预算最优配餐 | 预算上限、人数 | **主方案 + 备选方案**两套，含明细/单价/人均/剩余预算 | `optimize.py` |
| 2 | 优惠券组合 & 省钱比价 | 券池 | 券商品参与组合优化、券价/原价/省/有效期/`couponCode`、互斥差额 | `optimize.py` |
| 3 | 营养配餐推荐 | `--mode slim\|muscle` | 热量/蛋白/脂肪/碳水/钠/钙、脂肪供能比、蛋白质密度、逐条达标检查、**高油替换建议** | `optimize.py` |
| 4 | 场景套餐推荐 | `--scenario birthday\|date\|team\|family\|solo` | 按场景换结构（小食份数/甜品/分享装）与权重倾向 | `optimize.py` |
| 5 | 门店与配送 | 门店 + 地址 | 三档结论：**可外送 / 只得到店 / 信息不足** | `analyze.py --action delivery` |
| 6 | 菜品分析 | 菜单 | 限时/即将下架/已下架识别；指定套餐营养汇总 | `analyze.py --action status\|nutrition` |

### 支撑能力（2026-10-16 / 10-19 新增）

| 能力 | 脚本 | 说明 |
|---|---|---|
| **服务端权威核价** | `pricer.py` | 调 `calculate-price` 复核金额；整单被拒时**逐项排查**出本地看不到的「不可售」商品，写回 `data/unsellable.json` |
| **下单演练** | `order.py` | 默认**不创建订单**：先重新核价，再打印完整订单预览与待提交请求体。加 `--confirm` 才真正下单。取餐方式取自核价的 `takeWayList`，多选时**拒绝猜测**（退出码 3） |
| **多门店取数与切换** | `refresh_live.py --all-nearby` | 一次把附近多家门店各取一份数据到 `data/live_stores/<编码>/`；面板顶部出现餐厅下拉，切换即换整张网格与门店信息 |
| **可视化面板** | `build_ui.py` | 生成**单文件、免服务器**的交互式 HTML：**顶部切餐厅**，再切人数/预算/模式/场景，看方案、营养、券、覆盖度 |

### 多门店：为什么「选餐厅」需要重算，以及怎么把体积压下来

面板原先只烘焙一家门店（`_probe/stores.json` 的第一家）。要能选餐厅，问题在于
**每家门店菜单不同、组合结果也不同**，所以每家都得各算一遍 347 组——前端加个下拉是不够的。

为此 `refresh_live.py` 增加多店模式（`--all-nearby` / `--stores`），
`build_ui.py` 按门店分桶产出 `GRIDS[门店编码][参数]`，前端下拉切换。
实测取到 5 家门店（下表已脱敏，真实名称与地址只存在于本地快照）：

| 门店 | 距离档位 | 在售餐品 | 营养覆盖 | 券商品 |
|---|---|---|---|---|
| 一号店 | 步行可达 | 101 | 67% | 2 |
| 二号店 | 1 km 内 | 127 | 70% | 2 |
| 三号店 | 2 km 内 | 107 | 76% | 2 |
| 四号店 | 2 km 内 | 111 | 75% | 0 |
| 五号店 | 2 km 内 | 94 | 69% | 0 |

**体积是这里最实际的约束**：照直烘焙 5 家 × 1.5 MB ≈ 7.5 MB。
实测网格里 `items` 占 47%、`warnings` 占 7%、`checks` 占 9%，
而这些对象在同一门店不同参数之间、甚至不同门店之间**大量重复**
（同一份菜单、同样的达标检查、同样的告警）。
于是引入**跨门店共享对象池**：所有重复对象归一成一个全局数组，网格里只存下标
（`{"r": n}`，前端解引用还原，**无损**）。

结果：5 家门店 1735 组预计算的 HTML 从 ~7.5 MB 压到 **2.6 MB**，对象池仅 **421 个唯一对象**。

### 关键机制：券从「事后抵扣」变成「参与优化」

「用券价格」是麦当劳券的主力形态（付 ¥13.5 拿走「中薯条买一送一」）。
它的商品编码是 `99` 开头的虚拟商品码，与门店菜单编码（`1100/1106/…`）
**不在同一命名空间**，所以本地算不出优惠额。

本项目改用另一条路：**把券翻译成一个可直接下单的候选餐品**。

```
refresh_live.py 调 calculate-price 核价
  → 中薯条买一送一：原价 ¥28.00 / 券价 ¥13.50 / 省 ¥14.50
  → 生成 deal_item（价格=券价、营养=单份中薯条×2、带 couponId/couponCode）
  → 与菜单餐品一起进候选池，参与组合优化
```

于是报告可以给出券价、原价、省了多少、有效期与 `couponCode`，
还能明确说明「未使用 ≠ 不可用」——只是当前预算下它没能让整单更优。

### 闭环：核价 → 发现不可售 → 反哺配餐

实测发现本地菜单快照里 `available=true` 的商品（如「双层深海鳕鱼堡」）
在 `calculate-price` 上会返回「商品已售罄或不可售」。`pricer.py` 逐项排查后
写入 `data/unsellable.json`，优化器下次运行自动排除并说明排除了什么。

### 券商品为什么不总是被选中（诚实结论）

把券变成候选餐品后，曾期望「买一送一」能稳定进主方案。实测**只在少数配置下会**——
5 家门店 1735 组参数展开成 2198 份主/备方案，其中只有 **3 份**（0.14%）把券商品选进主方案，
且集中在人数 ≥3、预算 ≥130 元的「常规性价比」模式
（例：3 人 / 130 元 / `value`，4 人 / 170 元 / `value`）。

原因是**逐槽位比价的数学结果**。券商品按 1 个候选条目占用 1 个结构槽位，
它自带的份数（如「买一送一」= 2 份）只如实计入营养与餐品清单，不额外替代槽位。同一个小食槽位上比：

| 候选 | 价格 | 单槽位质量分 |
|---|---|---|
| 麦当劳素龙限定饼干 | ¥2.00 | 2.00 |
| 中薯条买一送一（券） | ¥13.50 | 42.67 |
| 麦辣鸡翅 | ¥14.50 | 51.36 |

「预算内质量最大化」（value）通常选**麦辣鸡翅**（分更高）；「同等满足度下最省」（frugal）会选**饼干**（更便宜）。
只有当人数多、预算宽、小食槽位供不应求时，券商品的**双份**才真正占优，此时它才入选。
这不是 bug，而是**券的真实定位**：它的价值在于「你本来就想要这份商品」时的省钱，
而不是无条件让配餐评分配方更优。

面板因此把券单独成块展示比价结论（券价 / 原价 / 省 / 有效期 / couponCode），
并明确「未被选中不代表不可用」。

### 本轮的诚实性缺陷修复（2026-10-16）

排查上述问题时发现两个真实缺陷，都已修复并补了回归断言：

| 缺陷 | 表现 | 修复 |
|---|---|---|
| 券商品营养被补 0 | 查不到营养的券商品（如「王牌小食拼」）被填成全 0；`has_known_nutrition` 因「营养字典非空」把它**误判成已知**，等于把「未知」当成「零」 | `load_coupon_deals` 只收真实存在的营养键，查不到就留空字典 |
| `coupon_deal` 匹配标记不识别 | `coupon_deal` / `coupon_deal_miss` 不在命中/未命中表里，判定靠「字典是否非空」侥幸通过 | `has_known_nutrition` 增加前后缀兜底：`*_miss` → 未知，`coupon_deal*` → 已知 |

### 支撑数据层

| 脚本 | 职责 |
|---|---|
| `mcp_payload.py` | 把 MCP 的三种混合返回（markdown 说明段+JSON / 内嵌 CSV 字符串 / 纯散文）解析成结构化 |
| `mcd_adapter.py` | 真实字段 → 内部 schema；**套餐子项展开求和**；**券商品翻译**；营养匹配可信度标注 |
| `mcd_core.py` | 数据模型、字段别名容错、品类归一、高油判定、券可核验性、券商品读取 |
| `nutrition.py` | 营养汇总、达标判定、高油替换建议 |
| `refresh_live.py` | 一键编排：找店 → 拉菜单/营养/券/门店 → **券核价** → 抓套餐明细 → 翻译 |

### 工程状态

- **脚本 14 个，全部纯标准库**（零第三方依赖）
- **自测 78/78 通过**（`python scripts/selftest.py`，不依赖 MCP）
  - 基础层 40 项（脱敏样例数据，任何机器上必过）
  - 真实回归层 38 项（`_probe/` 真实返回，跑过 `refresh_live.py` 后自动启用）
- 当前真实快照：菜单 106 项、营养覆盖 73 项（69%）、套餐 43/44 展开成功
- **面板运行时冒烟：5 家门店 × 347 组 × 主/备选 = 3470 次渲染零错误**
- **绝不自动下单**：`create-order` / `party-order-create` 仅在用户明确确认后调用

---

## 二、使用方法

### 0. 前置：连接 MCP

配置在 `~/.workbuddy/mcp.json`，鉴权头**必须是** `Authorization: Bearer <Token>`：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": { "Authorization": "Bearer 你的MCP_TOKEN" }
    }
  }
}
```

写入后需在【连接器】页对 `mcd-mcp` 点「信任」。
连通性自检：`python scripts/probe_mcp.py now-time-info`。

### 1. 取数据（一步到位，含券核价）

```bash
python scripts/refresh_live.py --city <城市> --keyword <地标关键词>
python scripts/refresh_live.py --store <门店编码>     # 已知门店编码更快
python scripts/refresh_live.py --all-nearby           # 多店：面板「选餐厅」需要
```

产出：`data/live_menu.json`（菜单）、`data/live_coupons.json`（券 + 券商品）、
`data/live_coverage.json`（覆盖度报告）、`_probe/coupon_pricing.json`（核价结果）。

### 2. 跑配餐（能力 1–4，同一入口）

```bash
python scripts/optimize.py --menu data/live_menu.json --coupons data/live_coupons.json \
  --people 2 --budget 80 --mode value
```

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--strategy` | `value` | `value`=预算内吃到最好；`frugal`=同等满足度下最省 |
| `--mode` | `value` | `value` / `slim`（减脂）/ `muscle`（增肌） |
| `--scenario` | 空 | `birthday` / `date` / `team` / `family` / `solo` |
| `--delivery-fee` | `0` | 外送时把配送费计入总价 |
| `--max-coupon-deals` | `1` | 同单最多用几个券商品，`-1` 表示不限 |
| `--ignore-unsellable` | 关 | 忽略不可售清单，保留全部餐品 |

### 3. 定稿前核价（**必做**）

```bash
python scripts/pricer.py --menu data/live_menu.json --coupons data/live_coupons.json \
  --store <门店编码> --people 2 --budget 80
```

输出本地估算 vs 服务端金额的对照，以及逐项明细；被拒时自动点名不可售商品。

### 4. 下单（默认只演练）

```bash
# 演练：重新核价 → 打印订单预览 + 待提交请求体，**不创建订单**
python scripts/order.py --menu data/live_menu.json --coupons data/live_coupons.json \
  --store <门店编码> --people 2 --budget 80 --take-way eat-in

# 真下单（会扣钱，仅在用户明确确认门店/餐品/金额之后）
python scripts/order.py ... --take-way eat-in --confirm
```

到店场景（`orderType=1`）的 `create-order` **必传 `takeWayCode`**，而它唯一的合法来源是
`calculate-price` 返回的 `takeWayList[].code`（实测该门店为 `eat-in` 堂食 / `take-in-store` 外带）。
脚本会先重新核价拿到这个列表，**存在多个选项而没指定时直接报错退出（退出码 3）**——
选错取餐方式等于下错单，这种决定不该由脚本代做。

不加 `--confirm` 时，代码里**没有任何路径**会调到 `create-order`（自测有一条断言锁死这个结构）。

### 5. 生成可视化面板

```bash
python scripts/build_ui.py        # → assets/dashboard.html（单文件，双击即可打开）
```

面板顶部有**餐厅下拉**——但下拉里的门店来自 `data/live_stores/index.json`，
所以先要多店取数：

```bash
python scripts/refresh_live.py --all-nearby        # 把 stores.json 里的门店全取回来
# 或指定门店
python scripts/refresh_live.py --stores <编码1>,<编码2>,<编码3>
python scripts/build_ui.py                          # 重建面板（会读上面取到的所有门店）
python scripts/build_ui.py --store <门店编码>          # 只想烘一家也行
```

### 6. 门店与菜品分析（能力 5–6）

```bash
python scripts/analyze.py --action delivery \
  --stores _probe/stores.json --addresses _probe/addresses.json --radius-km 3
python scripts/analyze.py --action status --menu data/live_menu.json
python scripts/analyze.py --action nutrition --menu data/live_menu.json --codes M01,M10 --people 2
```

### 7. 作为 Skill 使用

已装到 `~/.workbuddy/skills/mcd-meal-optimizer/`。对话里说
「帮我点麦当劳 / 80 块两个人怎么配 / 减脂餐 / 优惠券怎么用最省」即可触发。

---

## 三、存在的问题

### A. 数据源限制（MCP 侧，非本项目可解）

| 问题 | 影响 | 当前处理 |
|---|---|---|
| **营养表覆盖只有 69%** | 106 项菜单里 73 项有营养数据 | 按「未知」处理，营养汇总**如实标注缺口**，不按 0 计 |
| **营养按名字 join，口径不一致** | 菜单叫「薯条」、营养表叫「中薯条」 | 规格变体 + 模糊匹配，仍有 miss |
| **本地菜单快照 ≠ 当下可售** | 快照里在售的商品，服务端可能判不可售 | `pricer.py` 排查 → `unsellable.json` → 优化器自动排除 |
| **券商品营养未必能查到** | 如「王牌小食拼」营养表里没有 | 标为 `coupon_deal_miss`，如实显示「—」 |
| **到店门店查询不返回外送能力** | `query-nearby-stores` 无配送字段 | 外送必须走 `delivery-query-stores`；缺失归入「信息不足」不下结论 |
| **外送查询必须有非空 addressId** | 用户无收货地址 → 完全无法评估 | 提示先 `delivery-create-address` |

### B. 功能缺口（可继续做）

1. **券商品原价依赖核价** —— `list_price_fen` 来自 `calculate-price`；
   核价失败时退化为「菜单同名单品×份数」估算，可能偏低或缺失。
2. **一单最多 1 个券商品是保守默认** —— 麦当劳实际能否叠加多张
   「用券价格」券未经验证，`--max-coupon-deals` 可放宽，但需真实下单确认。
3. **换配不重算营养** —— 套餐营养是「默认子项求和」，用户换子项
   （如薯条→玉米杯）后营养会变，目前 `combo_upgrades` 只给出**差价**。
4. **未覆盖的 MCP 能力** —— 积分商城兑换下单、主题活动（派对/品鉴会）预约、企业团餐流程。
5. **面板是预计算网格** —— 滑块吸附到预设档位（人数 1–6、预算 20–200 步长 10、
   三档模式）；组合外的参数需重新运行 `build_ui.py`。
6. **面板里的门店是取数那一刻的快照** —— 下拉里的门店来自 `data/live_stores/index.json`，
   不是实时搜索。想加门店要重跑 `refresh_live.py --all-nearby`（或 `--stores`）再重建面板；
   面板**不提供**「搜任意门店」的输入框。
7. **场景模板是本地静态数据** —— `data/scenarios.json` 不在 MCP 侧，需手工校准。
8. **`create-order` 的成功路径未经真实下单验证** —— 本项目默认演练、不产生真实订单，
   因此返回体的字段名只能「找到什么报什么」。若服务端的成功结构与预期不符，脚本会
   原样打印响应，**不会伪造订单号或支付链接**；真要下单时请以麦当劳 APP 的订单页为准。
9. **面板不提供下单入口** —— `assets/dashboard.html` 是纯静态单文件（零 `fetch`/`XHR`），
   只做方案展示与参数切换。下单必须走 `order.py`，而且默认只演练、需 `--confirm` 才真下单。

### C. 工程与合规注意

1. **`Bearer ` 前缀**（已修）—— 只填裸 Token 会 `403 / 校验鉴权authToken必填!`。
2. **连接器 UI 编辑会整体替换 `mcp.json`** —— 曾把原有的 `gaodun-job` 冲掉。改前先备份。
3. **数据快照含门店/地址信息** —— `_probe/`、`data/live_*`、`data/live_stores/`、`data/unsellable.json`
   已在 `.gitignore` 中，**不得进仓库**。真实 Token 绝不入库。
4. **营养建议不构成医疗建议**。
5. **面板文件较大**（5 家门店 1735 组预计算约 2.6 MB）——本地打开无压力，但不适合嵌入网页。
   构建时用**跨门店共享对象池**做了无损去重（items/达标检查/覆盖度/告警大量重复），
   否则 5 家会是 ~7 MB。
6. **券商品按 1 个候选条目占用 1 个结构槽位**，但它自带的份数（如「买一送一」= 2 份）会如实计入营养与餐品清单。
   即：选中它会让你**多得到一份**，而不是替代两个槽位。因此「买一送一」在逐槽位比价中多数情况下不占便宜，
   只在人数多、预算宽（如 3 人 / 130 元）时才入选——2198 份方案里有 3 份用上了它，这是真实结果，不是 bug（详见上节）。
7. **参赛已报名** —— 仓库 <https://github.com/Liuzh-Hit/mcd-meal-optimizer>（Public），
   报名 Issue #886 已提交至 `M-China/mcd-developer-innovation-challenge`，等待系统审核回复。

---

## 四、下一步建议（按优先级）

| 优先级 | 事项 | 说明 |
|---|---|---|
| 已完成 | **报名参赛** | 仓库 <https://github.com/Liuzh-Hit/mcd-meal-optimizer>，Issue #886 已提交，等审核 |
| 高 | **拉 Star 提升排名** | 排行榜按公开 Star 数排，Star>0 才进榜；截止 10/25 23:59，10/26 00:00 定榜 |
| 中 | 换配选项营养重算 | 让「薯条换玉米杯」同时给出省多少钱 + 降多少卡 |
| 中 | 补主题活动 / 积分兑换 | 拓展能力边界，也避开营养素这条拥挤赛道 |
| 中 | 面板加「自选参数即算」 | 可选做一个本地小服务，参数不吸附档位 |
| 低 | 券商品叠加规则验证 | 需要真实下单确认能否一单多券 |
