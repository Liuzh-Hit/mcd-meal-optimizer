---
name: mcd-meal-optimizer
description: 基于麦当劳中国 MCP 的智能配餐助手。在预算上限内自动组合餐品并给出主/备选两套方案，组合优惠券做优惠前后比价与互斥提示，按减脂或健身增肌模式统计热量/蛋白/碳水/脂肪并给高油替换建议，支持生日/约会/团建等场景套餐，判断门店配送可行性，识别限时与下架菜品。当用户提到点麦当劳、麦乐送、麦麦省、配餐、控预算、减肥餐、增肌餐、优惠券怎么用最省时使用。
---

# 麦麦智配（mcd-meal-optimizer）

面向麦当劳中国场景的配餐决策助手。核心不是「把菜单念一遍」，而是在
**预算、营养、优惠券三组约束同时成立**的前提下，给出可以直接去下单的那一套组合。

## 一、五条铁律

1. **数字一律交给脚本算，不要自己心算。** 预算求和、满减门槛、折扣封顶、脂肪供能比
   这类运算交给 `scripts/` 下的 Python 脚本。模型的算术在这类问题上不可靠，
   最容易产出「看起来对但总价超预算」的方案。
2. **先取真实数据，再算，最后才谈下单。** 菜单、营养、优惠券、门店必须来自 MCP 实时返回，
   不允许凭记忆编造餐品名、价格或券面规则。
3. **「查不到」和「零」必须分开说。** MCP 的营养表覆盖不全（实测门店菜单只有约
   2/3 能查到营养）。查不到就写「未知」，**绝不能按 0 参与汇总**——
   否则会输出「这份套餐 0 千卡」这种明显错误的结论。脚本已按此实现，转述时也要保持。
4. **绝不自动下单。** `create-order` 与 `party-order-create` 只有在用户明确确认门店、
   餐品清单与金额后才可调用。任何时候都不要替用户点掉一单。
   `scripts/order.py` 因此**默认只演练**：不加 `--confirm` 它不会创建订单；
   即使加了，也必须由用户先点头——不要因为脚本支持就自作主张。
5. **金额的最终解释权在服务端。** 本地只做组合优化；定稿前用
   `scripts/pricer.py` 调 `calculate-price` 复核一次。实测本地估算与服务端一致，
   但服务端还会额外暴露「商品已售罄或不可售」这类本地看不到的信息。

## 二、MCP 依赖检查

- 连接器名 `mcd-mcp`，域 `https://mcp.mcd.cn`，协议 Streamable HTTP。
- **鉴权头必须是 `Authorization: Bearer <Token>`，`Bearer ` 前缀不能省。**
  只填裸 Token 会返回 `403 / 校验鉴权authToken必填!`，在连接器页面上表现为「需要认证」。
- 自检连通：`python <SKILL_DIR>/scripts/probe_mcp.py now-time-info`

若工具列表里没有 `query-meals` / `list-nutrition-foods`，先让用户按
`references/mcp-tools.md` 的步骤完成配置，不要用虚构数据继续。

## 三、数据准备：一个命令拿到可用数据

MCP 的返回**不是干净 JSON**，有 markdown 说明段、内嵌 CSV 字符串、纯散文三种形态，
字段名也与直觉不同（`energyKcal` 而非 `kcal`、`distance` 单位是米、餐品实体没有
`category` 字段……）。这些差异已由适配层处理，**不要自己手工拼 JSON**。

```bash
# 一步到位：找店 → 拉菜单/营养/券/门店 → 展开套餐营养 → 翻译成内部结构
python <SKILL_DIR>/scripts/refresh_live.py --city <城市> --keyword <地标关键词>
# 已知门店编码时
python <SKILL_DIR>/scripts/refresh_live.py --store <门店编码>
# 多店（可视化面板要在界面上「选餐厅」，就得把附近几家一起取回来）
python <SKILL_DIR>/scripts/refresh_live.py --city <城市> --keyword <地标关键词> --all-nearby
python <SKILL_DIR>/scripts/refresh_live.py --stores <编码1>,<编码2>,<编码3> --skip-details
```

单店模式写出三个文件，后续所有能力都读这三个：

| 文件 | 内容 |
|---|---|
| `data/live_menu.json` | 内部结构的菜单（含营养、分类、套餐子项、换配选项） |
| `data/live_coupons.json` | 内部结构的券（含 `couponId`、`couponCode`、有效期、可用性判定） |
| `data/live_coverage.json` | 覆盖度报告与警告 |

多店模式在此之外为**每家门店各存一份**到 `data/live_stores/<门店编码>/`，
并写一份门店清单 `data/live_stores/index.json`；同时把最近的一家镜像回上面三个文件，
所以单店脚本与自测不受影响。营养表、个人券、账号、地址、活动日历是**全店通用**的，
多店模式下只取一次并共享。

⚠️ **不可售清单是按门店的**。`data/unsellable.json` 带 `store` 字段，
只有门店编码匹配时才生效；多店模式下请写到 `data/live_stores/<编码>/unsellable.json`，
免得把 A 店卖光的商品误判成 B 店也不可售。

**必须把覆盖度报告读给用户听**。营养覆盖率低意味着营养结论只覆盖了部分餐品，
这一点不能藏。

各步骤也可单独执行（调试用）：`dump_mcp.py`（落盘原始返回）、
`fetch_details.py`（只抓套餐明细）、`mcd_adapter.py`（只做翻译）。

## 四、能力与执行方式

### 能力 1｜预算最优配餐

触发：用户给出「预算上限 + 用餐人数」。

```bash
python <SKILL_DIR>/scripts/optimize.py \
  --menu data/live_menu.json --coupons data/live_coupons.json \
  --people 2 --budget 80 --mode value
```

输出：主方案 + 备选方案，每套含餐品明细、优惠前/后价格、人均价、剩余预算、营养汇总。

可选参数：
- `--strategy value`（默认，预算内吃到最好）/ `--strategy frugal`（同等满足度下最省）
- `--delivery-fee 9`（外送时把配送费计入总价）
- `--coupon-headroom 15`（允许优惠前价格超出预算多少元，以便用券压回预算内）

预算装不下时脚本会自动逐级放宽结构（先删甜品、再删套餐、再把小食/饮品压到每人一份），
并在报告顶部说明放宽了什么；连最低配置都装不下时会直接给出最低所需金额。

### 能力 2｜优惠券智能组合与省钱比价

本项目的做法是**把券变成商品**，而不是事后抵扣：

`refresh_live.py` 会把每张「用券价格」券交给 `calculate-price` 核价，得到
**服务端权威的原价与券价**（例：中薯条买一送一 原价 ¥28 → 券价 ¥13.5，省 ¥14.5），
再把券翻译成一个**可直接下单的候选餐品**写进 `data/live_coupons.json` 的 `deal_item`。

这样做的原因（实测结论）：券的商品编码是 `99` 开头的虚拟商品码，
与门店菜单编码（`1100/1106/…`）**不在同一命名空间**，本地按编码或按名字都 join 不上。
但把券商品当成一个候选餐品就完全成立了——它的价格就是券价，还能被服务端复核。

于是券会与同品类餐品**一起参与组合优化**，而不是等方案定了再往上套。
报告里会：

- 逐项列出券商品的券价 / 原价 / 省 / 有效期 / `couponCode`；
- 标注主/备选方案是否真的用到了券；
- **明确说明「未使用」不等于「不可用」**——只是当前预算下它没能让整单更优。

⚠️ 其余类型的券（满减 / 折扣 / 免配送费）仍走本地券组合枚举，
并受 `--max-coupons` / `--max-order-coupons` 限制，互斥时写出被挡原因与差额。

⚠️ **必须如实说明「未能核价的券」。** 若某张券缺少 `couponId` 或券价无法获取，
报告会单独列出并写明原因，**不要假装没有券，也不要编一个优惠额**。

## 五、定稿前必做：服务端核价

```bash
python <SKILL_DIR>/scripts/pricer.py \
  --menu data/live_menu.json --coupons data/live_coupons.json \
  --store <storeCode> --people 2 --budget 80
```

它会调 `calculate-price`，把本地估算与服务端金额并列对照（一致 / 有差异），
并打印服务端的逐项明细。**整单被拒时会自动逐项排查**，
点名到底是哪几个商品「已售罄或不可售」——这是本地菜单快照看不出来的。

排查结果会写入 `data/unsellable.json`，优化器下次运行会**自动排除**这些商品，
并在报告顶部说明排除了什么（加 `--ignore-unsellable` 可保留）。
这形成了一条闭环：**核价 → 发现不可售 → 反哺配餐**。

## 六、下单（默认只演练）

核价通过、用户也确实要下单时：

```bash
# 演练（默认）：重新核价 → 打印完整订单预览 + 即将提交的请求体，**不创建订单**
python <SKILL_DIR>/scripts/order.py --store <storeCode> \
  --menu data/live_menu.json --coupons data/live_coupons.json \
  --people 2 --budget 80 --take-way eat-in

# 真下单（会扣钱，仅在用户明确确认门店/餐品/金额之后）
python <SKILL_DIR>/scripts/order.py ... --confirm
```

三个必须讲清的点：

1. **`takeWayCode` 只能从核价返回里取。** 到店场景（`orderType=1`）的 `create-order`
   必传它，而它唯一的合法来源是 `calculate-price` 返回的 `takeWayList[].code`
   （实测该门店是 `eat-in` 堂食 / `take-in-store` 外带）。
   `order.py` 会先重新核价拿到这个列表，然后**绝不猜**：存在多个选项而用户没指定时，
   直接报错列出选项并退出（退出码 3）——选错取餐方式等于下错单，这种决定不该由脚本代做。
2. **下单前必重新核价，不复用旧金额。** 券会过期、商品会当天售罄，只有下单那一刻
   服务端给的价格才算数；核价被拒就中止，绝不凭本地估算下单。
3. **不加 `--confirm` 就没有任何代码路径能调 `create-order`。**
   这是结构保证（演练分支的提前返回在调用点之前），自测里有一条断言专门锁死它。

⚠️ `create-order` 的成功路径**未经真实下单验证**（本 Skill 默认演练、不产生真实订单），
所以返回体字段只能「找到什么报什么」：找到 `orderId` / 支付链接就报，
找不到就原样打印服务端返回，并提示去麦当劳 APP / 小程序的「我的订单」继续支付——**绝不臆造链接**。

## 七、可视化面板

```bash
python <SKILL_DIR>/scripts/build_ui.py            # → assets/dashboard.html
python <SKILL_DIR>/scripts/build_ui.py --store <门店编码>   # 只烘焙指定门店（可逗号分隔多家）
```

生成**单文件、免服务器**的交互式面板：顶部**可切换餐厅**（多店取数后每家都在下拉里），
以及人数 / 预算 / 模式 / 场景四组参数，展示主备选方案、餐品营养、达标检查、
券商品核价与数据覆盖度。

设计上仍然遵守铁律 1：**前端不做任何算术**。每家的整张参数网格都由本地求解器
预先算好（实测单次 0.011 秒）烘焙进 HTML，前端只负责展示与切换，
因此既零延迟、也不会出现 JS 算错金额的情况。

体积上有个关键处理：面板要能选餐厅，而每家菜单不同、组合结果也不同，
照直烘焙 5 家会到 ~7 MB。为此用了**跨门店共享对象池**——items / 达标检查 /
营养覆盖度 / 告警这些对象在同一门店不同参数之间、甚至不同门店之间大量重复，
全部归一成一个全局数组，网格里只存下标（前端解引用即可还原，无损）。
实测 5 家门店 1735 组预计算压到 **2.6 MB**（对象池仅 421 个唯一对象）。

### 能力 3｜营养配餐推荐（减脂 / 健身增肌）

```bash
# 减脂
python <SKILL_DIR>/scripts/optimize.py --menu data/live_menu.json --people 1 --budget 45 --mode slim
# 健身增肌
python <SKILL_DIR>/scripts/optimize.py --menu data/live_menu.json --people 1 --budget 60 --mode muscle
```

除总价外，报告会给出整单与每人份的热量/蛋白质/脂肪/碳水/钠/钙、脂肪供能比、蛋白质密度，
并按模式给出达标检查（✅/❌ 逐条列出）。若有 ❌，**必须原样告诉用户哪一条没达标**，
并给出提高预算或替换菜品的建议。

报告里的营养汇总会标注**覆盖了几项**；若本方案有餐品缺营养数据，会明确列出并说明
「汇总未计入这些餐品，实际值更高」。转述时保留这个限定。

对高油菜品会自动给出同品类的替换建议。套餐还带 `combo_upgrades`（换配差价），
可以据此给出「薯条换苹果片省 X 元且更低脂」这类建议。
判定口径与阈值见 `references/nutrition-rules.md`。

### 能力 4｜场景套餐推荐

先用 `campaign-calendar` 看当期活动，再按场景取结构模板：

| 场景 | `--scenario` | 结构差异 | 权重倾向 |
|---|---|---|---|
| 生日 | `birthday` | 标准结构 + 甜品 | 分享装、蛋糕、仪式感单品 |
| 约会 | `date` | 小食减半共享 + 甜品 | 甜品、咖啡、沙拉，压低油炸权重 |
| 团建 | `team` | 小食 1.5 份/人 | 分享桶、多人装、大份 |
| 家庭 | `family` | 标准 + 甜品，小食共享 | 性价比 + 分享 |
| 单人 | `solo` | 每人一份，不凑单 | 常规 |

```bash
python <SKILL_DIR>/scripts/optimize.py \
  --menu data/live_menu.json --coupons data/live_coupons.json \
  --people 6 --budget 220 --scenario birthday
```

场景模板在 `data/scenarios.json`，是可编辑的数据而非代码——真实菜单品类对不上时改这里。

### 能力 5｜门店与配送可行性

先备齐门店与地址数据（`refresh_live.py` 已含），再判定：

```bash
python <SKILL_DIR>/scripts/analyze.py --action delivery \
  --stores _probe/stores.json --addresses _probe/addresses.json \
  --radius-km 3
```

结论分三档：**可外送 / 只得到店取餐 / 信息不足**。

⚠️ 三个实测硬约束：
- 到店查询（`query-nearby-stores`）**不返回外送能力**，外送必须走
  `delivery-query-stores`，而后者**必须带非空 `addressId`**；
- 用户没有收货地址 → 外送完全无法评估，需先 `delivery-create-address`；
- 门店未返回外送能力字段 → 归入「信息不足」，**不要替它下结论**。

配送费拿到后用能力 1 带上 `--delivery-fee` 复算一次——免配送费券会改变最优方案。

### 能力 6｜菜品分析与营养汇总

```bash
# 限时 / 即将下架 / 已下架 识别
python <SKILL_DIR>/scripts/analyze.py --action status --menu data/live_menu.json

# 指定套餐的营养汇总（编码或名称都行，逗号分隔）
python <SKILL_DIR>/scripts/analyze.py --action nutrition \
  --menu data/live_menu.json --codes M01,M10,D01 --people 2
```

注意：`analyze.py --action status` 识别的是**菜单返回的标签与售罄状态**。要判断
「某菜品现在到底还有没有」，以 `query-meals` 的实时返回为准，不要用历史缓存下结论。

## 八、回复格式

给用户的最终答复按这个骨架组织，不要直接把脚本原始输出贴过去：

1. **一句结论**：这套方案多少钱、人均多少、省了多少。
2. **餐品清单表格**：餐品｜品类｜单价｜单份营养（缺数据写「—」）。
3. **价格明细**：优惠前 → 用了哪几张券（含有效期）→ 优惠后 → 剩余预算。
4. **未能核价的券**（如果有）：逐张列出券名、原因，并说明建议用 `calculate-price` 复核。
5. **互斥提醒**（如果有）：本可多省多少、被哪条规则挡住。
6. **营养小结**：每人份热量/蛋白/脂肪/碳水 + 达标检查 + **覆盖了几项**；
   若有不完整必须点明「未计入的餐品」。
7. **替换建议**（减脂/增肌模式）：高油菜品的替代选项或套餐换配。
8. **需要用户确认的信息**：门店、是否外送、是否现在下单。

## 九、边界

- 不用仿真数据替代真实 MCP 数据；MCP 不可用就如实说明并给出配置步骤。
- 不编造券面规则、价格或营养数值。缺失就给缺失提示。
- 不把「未知」当成「零」——两者在饮食决策里含义完全不同。
- 不自动下单、不自动领券（`auto-bind-coupons` 需用户明确同意）。
- **不承诺「一定能拿到支付链接」**：`order.py` 的 `create-order` 成功路径未经真实下单验证，
  返回体字段只能找到什么报什么。找不到就如实说，让用户去 APP 的「我的订单」里继续。
- 不把用户地址、手机号等个人信息写进任何输出文件或日志。
- 营养建议只作参考，不构成医疗建议；涉及减脂/增肌时要点明。
