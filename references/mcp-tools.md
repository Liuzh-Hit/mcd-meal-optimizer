# 麦当劳 MCP 工具速查与调用顺序（实测版）

> 本文所有字段名、参数、返回形态均为 **2026-10-15 对 `https://mcp.mcd.cn` 实测所得**，
> 不是照着 README 抄的。实测与文档不一致处一律以本文为准。

- 服务地址：`https://mcp.mcd.cn`
- 协议：Streamable HTTP（POST + `Accept: application/json, text/event-stream`），JSON-RPC 2.0
- 鉴权：请求头 `Authorization: Bearer <MCP_TOKEN>`
  - ⚠️ **必须带 `Bearer ` 前缀**。实测只填裸 token 会返回
    `HTTP 403 / code 400003 / 校验鉴权authToken必填!`——连接器页面表现为「需要认证」。
- 限流：600 次/分钟，超限 429
- Token 申请：https://open.mcd.cn/mcp → 手机号登录 → 右上角「控制台」→「激活」
- 实测工具总数：**35 个**

---

## 一、最大的坑：返回不是干净 JSON

MCP 的返回被包装成了「给模型看的文本」，实测有三种形态，**必须分别处理**
（`scripts/mcp_payload.py` 已统一）：

| 形态 | 示例工具 | 特征 |
|---|---|---|
| ① markdown 说明段 + JSON 信封 | `query-meals`、`query-nearby-stores`、`query-store-coupons` | 前面有一大段 `# API Response Information` / `## Response Structure`，JSON 藏在后面 |
| ② JSON 信封，但 `data` 是**字符串** | `list-nutrition-foods` | `data` 是一段内嵌 CSV 的字符串：`[160]{productName,...,calcium}:\n  猪柳麦满分,null,1288,308,...` |
| ③ **纯 markdown 散文，没有 JSON** | `query-my-coupons`、`available-coupons`、`campaign-calendar` | 只能正则解析 |

**别做 `json.loads(整个返回)`**——形态 ①② 都会失败。

---

## 二、真实字段名（照抄，别猜）

### `query-meals` 菜单

```
data.categories[].name            菜单分区名（**含换行**，如 "鸡肉汉堡\n/卷"，需拉平）
data.categories[].meals[].code    餐品编码
data.categories[].meals[].tags    标签
data.meals{code}                  餐品详情 —— 注意是 **以 code 为键的映射表，不是数组**
data.meals[code].name
data.meals[code].image
data.meals[code].currentPrice     现价（**字符串**，如 "33.5"；已含卡/促销优惠）
data.meals[code].originalPrice    原价（划线价，字符串）
data.meals[code].discountType     null / "早餐卡优惠" / "促销优惠" / "麦金卡优惠" ...
data.meals[code].canWithOrder     是否可随单购
```

⚠️ **餐品实体里没有 `category` 字段**，分类只能靠 `categories[].meals[]` 的嵌套关系推。

### `query-meal-detail` 套餐明细

```
data.code / data.name
data.rounds[].name                 槽位名，如「选择套餐内小食」
data.rounds[].choices[].code
data.rounds[].choices[].name       子项名 —— 这个名字**能对上营养表**，是关键
data.rounds[].choices[].quantity
data.rounds[].choices[].isDefault  1 = 默认选中
data.rounds[].choices[].diffPrice  换配差价，形如 "+ ¥1.5" / "- ¥0.5"
data.rounds[].choices[].supportModify
```

**两条实测判据（很重要）**：

1. **只有套餐才有 `rounds`**。单品（如巨无霸）的 `rounds` 为空数组。
   所以「是不是套餐」有精确判据——看 `rounds` 有没有默认子项，**不要靠菜名猜**。
2. **套餐营养只能这样算**：展开默认子项 → 逐个查营养表 → 求和。
   菜单里的套餐在营养表里没有对应条目（营养表只有单品）。
   实测 `麦辣鸡腿汉堡三件套` = 麦辣鸡腿汉堡 485 + 中薯条 289 + 可乐中杯 147 = **921 kcal**。

### `list-nutrition-foods` 营养表

- **无入参**。全局表，与门店无关。
- `data` 是字符串，表头为：
  `productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium`
- 实测 160 行。字段名是 **`energyKcal`**（不是 `kcal`）、**`carbohydrate`**（不是 `carbs`）。
- ⚠️ **按名字 join，不是按编码**；且覆盖不全（实测 112 项菜单只有 86 项能查到，约 77%）。
  名字口径也不一致：菜单叫「薯条」，营养表叫「中薯条/大薯条/小薯条」。
  `mcd_adapter.nutrition_lookup` 做了规格变体与模糊兜底，并**标注匹配方式**。

### `query-nearby-stores` 到店门店

```
data[].storeCode      门店编码（后续 query-meals 必传）
data[].storeName
data[].address
data[].distance       ⚠️ 单位是**米**（如 269），不是 "3.1km" 字符串
data[].businessStatus 营业状态（**不是** status / open）
data[].businessStartTime / businessEndTime
data[].reservation / data[].reservationTimeOptions
data[].storeCode + beCode
```

⚠️ 两个坑：

1. `searchType=1` 是**搜索收藏餐厅**（默认值），`searchType=2` 才是按位置搜索。
   想按位置找店必须显式传 `searchType: 2`，且 **`city` 与 `keyword` 至少要有一个非空**，
   否则返回 `code 600058 城市名或者关键词不能为空`。
2. 到店查询**不返回外送能力**（没有 `supportDelivery` 字段）。
   「能不能外送」必须走 `delivery-query-stores`，且**必须先有地址**。

### `delivery-query-addresses` 收货地址

- 无入参。`data.addresses[]` = `{addressId, contactName, phone, fullAddress}`（手机号已脱敏）。
- ⚠️ 没有地址时，**外送能力完全无法评估**——这是硬前置。

### `delivery-query-stores` 可配送门店

- **必传 `addressId`**，为空直接报 `code 400 / addressId地址ID不能为空`。
  所以顺序是：`delivery-query-addresses` → 取 addressId → `delivery-query-stores`。

### `query-store-coupons` 门店券（**结构化，首选**）

```json
{
  "title": "中薯条买一送一",
  "couponId": "<32 位十六进制券 ID，已脱敏>",
  "couponCode": "<券码，形如 MCD691W…L9T，已脱敏>",
  "tradeDateTime": "2026-10-12 10:30:00-2026-10-16 23:59:59",
  "products": [{"productCode": "9900012387", "productName": "中薯条买一送一"}],
  "promotionId": "<促销活动 ID，已脱敏>"
}
```

> 券码与券 ID 是可直接下单的凭据，本仓库一律不保留原值。

### `query-my-coupons` 我的券（**纯 markdown**）

```
## 中薯条买一送一
- **优惠**: ¥13.5 (用券价格)
- **有效期**: 2026-10-12 10:30-2026-10-16 23:59 周一、二、三、四、五 10:30-23:59
- **领取时间**: 4日前收到
- **标签**: 到店专用、外送专用
<img src="...">
```

⭐ **关键结论**：麦当劳主力券是「**用券价格**」型（付 ¥13.5 拿走某商品），
**不是满减/折扣**。而且券的 `productCode` 与门店菜单编码**不在同一命名空间**
（券是 `9900012387` 这种虚拟商品码，菜单里是 `1100/1106/4810/...` 这类商品码），
按 code 或按名字**都 join 不上**。

⚠️ **不要用「99 开头」判断某个码是不是券。** 实测某门店 101 项菜单里有 **54 项**是 99 开头
（`9900005456 麦辣鸡腿汉堡三件套`、`9900011056 全明星双人分享餐八件套` 等套餐与组合都在内），
前缀完全重叠。可靠判据只有两条：**该编码在 `query-meals` 返回里查不到**（券虚拟码不会出现在菜单中），
或在内部结构里看 `is_coupon_deal` 标记。

因此：**不要本地硬算券后价**。`mcd_adapter` 会把这类券标为 `verifiable=false`，
报告里如实写「未计入，需 calculate-price 核价」——
硬算只会输出「减免 0.00 元」这种看似确定、实则无依据的结果。

### `query-my-account` 积分账户

`data.availablePoint / accumulativePoint / frozenPoint / usedPoint / currentMouthExpirePoint / expiredPoint`
—— 全部是**字符串**，注意 `currentMouth`（原文拼写如此，不是 Month）。

---

## 三、`beType` / `orderType` / `beCode` 组合矩阵

`query-meals` / `query-meal-detail` / `calculate-price` / `query-store-coupons` 都要这组参数：

| 场景 | orderType | beType | beCode |
|---|---|---|---|
| 到店自取 | 1 | 1 | 不传 |
| 得来速（DT） | 1 | 5 | **必传** |
| 麦乐送到家 | 2 | 2 | **必传** |
| 企业团餐 | 2 | 6 | **必传** |

`beCode` 从 `query-nearby-stores` 或 `delivery-query-stores` 的返回里取。
实测到店自取（beType=1）的返回里 `beCode` 往往为空——这是正常的，不用补。

---

## 四、工具清单（35 个）

### 取数（只读，本 Skill 主要依赖）

| Tool | 必传参数 | 用途 |
|---|---|---|
| `now-time-info` | — | 服务器时间，判断券是否过期 |
| `query-meals` | storeCode, orderType, beType | 门店在售餐品 + 价格 |
| `query-meal-detail` | storeCode, orderType, beType, code | 套餐组成与换配差价 |
| `list-nutrition-foods` | — | 营养表（160 条单品） |
| `query-nearby-stores` | beType, searchType | 到店门店（位置搜索要 searchType=2 + city/keyword） |
| `delivery-query-addresses` | — | 我的收货地址 |
| `delivery-query-stores` | beType, addressId | 可配送门店 |
| `query-my-coupons` | —（page/pageSize 可选） | 我的券（markdown，含用券价格） |
| `query-store-coupons` | orderType, beType, storeCode | 门店券（结构化，含 couponId） |
| `available-coupons` | — | 麦麦省可领券（markdown） |
| `query-my-account` | — | 积分账户 |
| `campaign-calendar` | — | 营销活动日历（markdown 散文） |
| `query-meal-assistance` | storeCode | 企业团餐助餐 |
| `order-list` / `query-order` | orderId | 历史订单 / 订单详情 |
| `query-survey-coupon` | orderId | 订单关联问卷券 |
| `query-promotions` | storeCode, orderType, beType | 促销（⚠️ 实测返回 **502**，勿放关键路径） |
| `mall-points-products` / `mall-product-detail` | spuId | 积分商城 |
| `mall-order-list` / `mall-order-detail` | orderId | 商城订单 |
| `query-lottery-info` / `query-my-prizes` | — | 抽奖信息 / 我的奖品 |

### 写操作（**必须用户明确确认后才可调用**）

| Tool | 必传参数 | 用途 |
|---|---|---|
| `calculate-price` | storeCode, orderType, beType | 价格试算，**不改状态**；支持 `items[].couponId/couponCode` |
| `create-order` | storeCode, orderType, beType | 创建订单，返回支付链接 |
| `cancel-order` | orderId, cancelReasonCode | 取消订单 |
| `party-order-create` | partyType | 主题活动下单 |
| `delivery-create-address` | address, addressDetail, city, contactName, phone | 新增配送地址 |
| `auto-bind-coupons` | — | 一键领取麦麦省全部可领券 |
| `mall-create-order` | skuId, spuCategory | 积分兑换下单 |
| `draw-lottery` | — | 积分抽奖（**会扣积分**） |

### 主题活动（级联，必须逐级取）

`query-party-city` → `query-party-store` → `query-party-store-date` → `query-party-store-session` →
`party-order-create`

⚠️ 注意这是 `query-party-store-date` / `query-party-store-session`，
**不是** `query-partystore-*`（早期文档写错过）。

---

## 五、推荐调用顺序

```text
now-time-info
      │
      ├─ 到店 ──> query-nearby-stores(searchType=2, city/keyword)
      │                       │
      └─ 外送 ──> delivery-query-addresses ──> delivery-query-stores
                              │                        │
                              └────────────────────────┤
                                                       ▼
                                     query-meals ──> list-nutrition-foods
                                                       │
                        query-store-coupons + query-my-coupons
                                                       │
                                       mcd_adapter.py（翻译成内部结构）
                                                       │
                                          optimize.py（组合优化）
                                                       │
                              calculate-price（权威核价，含 couponId）─┐
                                                       │              │
                                              【用户确认】────────────┘
                                                       │
                                                 create-order
```

**为什么必须要 `calculate-price` 这一环**：券的编码与菜单不在同一命名空间，
本地算不出券后价（见上）。把候选组合 + `couponId/couponCode` 交给它，
由服务端给出权威价格，再让用户确认下单。

---

## 六、WorkBuddy 接入方式

1. 左侧边栏 →【专家·技能·连接器】→【连接器】页签
2. 右上角【自定义连接器】→【配置 MCP】
3. 粘贴 `mcp-config.example.json` 的内容，把 `YOUR_MCP_TOKEN` 换成实际 Token
   - ⚠️ `Authorization` 的值必须写成 `"Bearer 你的Token"`，**带 `Bearer ` 前缀**。
     只填裸 token 会被判定未鉴权，表现为「需要认证」。
4. 保存后回到【自定义连接器】，把 `mcd-mcp` 启用 / 点「信任」
5. 验证连通：`python scripts/probe_mcp.py now-time-info`

---

## 七、合规提醒

麦当劳 MCP 服务面向中国大陆地区（不含港澳台）。Token 属于个人凭证，
`mcp-config.example.json` 只允许保留占位符，真实 Token 不得进入仓库或任何输出。
