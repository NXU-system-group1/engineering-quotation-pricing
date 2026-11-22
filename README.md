# 工程报价系统 · 奉的报价模块
胡（图纸识别 + 统一前端）→ **奉（本模块 = 报价计算内核）** → 蒋（系统整合 / 业务规则裁定）。
本模块零第三方依赖、不提供前端/GUI，只暴露可被胡直接 import 的 Python 接口。
闭环：图纸识别 → 工程量确认 → 历史价格匹配 → 合价计算 → 报价 Excel → 人工确认 → 历史价格回写。

## 零依赖快速开始
```bash
python examples/demo.py     # 仅需 Python >= 3.9 标准库，无需 pip install
```
预期：`total_amount=5538.12` / `provisional_amount=1680.0` / `pending_items=4` / 草案 `price_history=27` → 确认后 `31`（新增 4 条、旧价未覆盖）/ 退出码 0。
产物：`output/quote_demo.db`、`output/报价明细_草案.xlsx`、`output/报价明细_已确认.xlsx`。

## 目录结构
`quotation/`：8 个 .py（__init__/api/db/seed/models/matching/pricing/xlsx）+ schema.sql；`data/seed_prices.json`：模拟脱敏种子；`examples/`：demo.py + recognition_demo.json。
根目录：README.md / INTERFACE.md / requirements.txt / `docs/待确认问题与已知限制.md`；`output/`：3 个运行产物。

## 数据库结构（5 张表逐字段）
| 表 | 字段 | 类型 | 含义与约束 |
|---|---|---|---|
| item_catalog | id | INTEGER PK | 自增主键 |
| item_catalog | canonical_name | TEXT NOT NULL | 标准项目名；参与 UNIQUE |
| item_catalog | category | TEXT | 分类（装饰面层/吊顶工程…） |
| item_catalog | material | TEXT | 材料；参与 UNIQUE |
| item_catalog | process | TEXT | 工艺；参与 UNIQUE |
| item_catalog | spec | TEXT | 规格；参与 UNIQUE |
| item_catalog | aliases | TEXT | 别名 JSON 数组文本，默认 `'[]'` |
| item_catalog | created_at | TEXT | 创建时间 ISO 秒 |
| price_history | id | INTEGER PK | 自增主键；**只追加** |
| price_history | item_id | INTEGER FK | → item_catalog.id |
| price_history | price_type | TEXT CHECK | 四选一（见下节） |
| price_history | unit_price | NUMERIC CHECK(>=0) | 单价（Decimal 字符串语义） |
| price_history | pricing_unit | TEXT | 计价单位 m2/m3/m/套/个… |
| price_history | currency | TEXT | 默认 `'CNY'` |
| price_history | source_project | TEXT | 来源项目名（种子均含「模拟/脱敏样例」） |
| price_history | quote_date | TEXT | 报价日期 `YYYY-MM-DD` |
| price_history | source_type | TEXT CHECK | demo_seed / import / quote_confirmation / manual |
| price_history | source_ref | TEXT | 来源引用（确认回写时为 quote_id） |
| price_history | construction_note | TEXT | 施工说明 |
| price_history | remark | TEXT | 备注 |
| price_history | is_confirmed | INTEGER 0/1 | 是否人工确认 |
| price_history | is_demo | INTEGER 0/1 | 是否模拟数据 |
| price_history | created_at | TEXT | 创建时间 ISO 秒 |
| pricing_rules | id | INTEGER PK | 自增主键 |
| pricing_rules | item_id | INTEGER FK NULL | NULL = 通用规则 |
| pricing_rules | from_quantity_type | TEXT | area / volume / length / count |
| pricing_rules | from_unit | TEXT | 源单位（cm2 / cm / m） |
| pricing_rules | to_pricing_unit | TEXT | 目标计价单位（m2 / m） |
| pricing_rules | conversion_factor | NUMERIC | 换算系数（0.0001 / 1 / 0.01） |
| pricing_rules | rule_note | TEXT | 规则说明 |
| pricing_rules | is_confirmed | INTEGER 0/1 | **仅 =1 可被计算层使用** |
| pricing_rules | confirmed_by | TEXT | 确认人 |
| pricing_rules | confirmed_at | TEXT | 确认时间；未确认时为空 |
| pricing_rules | created_at | TEXT | 创建时间 |
| quote_records | quote_id | TEXT PK | `Q-YYYYMMDDHHMMSS-uuid8` |
| quote_records | project_id | TEXT | 项目 ID |
| quote_records | project_name | TEXT | 项目名称 |
| quote_records | price_type | TEXT | 四类之一 |
| quote_records | status | TEXT CHECK | draft / confirmed |
| quote_records | generated_at | TEXT | 生成时间 ISO 秒 |
| quote_records | confirmed_by | TEXT | 确认人 |
| quote_records | confirmed_at | TEXT | 确认时间 |
| quote_records | total_amount | NUMERIC | 正式总价（不含相似匹配暂估） |
| quote_records | currency | TEXT | 默认 `'CNY'` |
| quote_records | remark | TEXT | 确认备注 |
| quote_items | id | INTEGER PK | 自增主键 |
| quote_items | quote_id | TEXT FK | → quote_records.quote_id |
| quote_items | item_id | TEXT | 识图结果的 item_id |
| quote_items | item_name | TEXT | 项目名称 |
| quote_items | material | TEXT | 材料 |
| quote_items | process | TEXT | 工艺 |
| quote_items | spec | TEXT | 规格 |
| quote_items | quantity_value | NUMERIC | 原始工程量（保留原始精度） |
| quote_items | quantity_type | TEXT | area / volume / length / count |
| quote_items | quantity_unit | TEXT | 归一后单位（m2/cm2/m3/m/cm/个…） |
| quote_items | review_status | TEXT | confirmed / modified / unconfirmed / pending |
| quote_items | pricing_quantity | NUMERIC | 换算后计价工程量 |
| quote_items | pricing_unit | TEXT | 计价单位 |
| quote_items | recommended_unit_price | NUMERIC | 推荐单价（无匹配为 NULL） |
| quote_items | final_unit_price | NUMERIC | 最终单价（人工确认后） |
| quote_items | price_history_id | INTEGER FK | 原推荐价追溯（manual 行仍保留） |
| quote_items | match_status | TEXT | exact / fuzzy / none / manual |
| quote_items | match_score | NUMERIC | 相似度；exact=1.0 |
| quote_items | amount | NUMERIC | 合价；挂起行为 NULL |
| quote_items | provisional_amount | NUMERIC | 相似匹配暂估（不计入总价） |
| quote_items | needs_confirmation | INTEGER 0/1 | 是否需人工确认 |
| quote_items | messages | TEXT | 提示/拦截原因 JSON 数组文本 |
| quote_items | candidates | TEXT | 候选价 JSON 数组文本 |
| quote_items | construction_note | TEXT | 施工说明 |
| quote_items | remark | TEXT | 备注 |
| quote_items | raw_json | TEXT | 识图原始 item（保留未知字段） |
| quote_items | created_at | TEXT | 创建时间 |

约束与索引：`item_catalog` UNIQUE(canonical_name, material, process, spec)；`idx_price_history_lookup(item_id, price_type, pricing_unit, is_confirmed, quote_date DESC)` / `idx_pricing_rules_lookup` / `idx_quote_items_quote`；每连接 `PRAGMA foreign_keys=ON`。
**只追加触发器**：`trg_price_history_no_update`(BEFORE UPDATE)、`trg_price_history_no_delete`(BEFORE DELETE) 均 `RAISE(ABORT, '只追加表，禁止 …')`；应用层写回也只 INSERT。

## 四类 price_type
`material_cost`=材料成本；`self_construction_cost`=自施工成本；`outsource_cost`=外包成本；`customer_quote`=对客报价（演示默认）。由 `price_history.price_type` 的 CHECK 约束强制四选一。

## 三态匹配与取价策略
- **exact**：别名归一后 canonical_name + material + process + spec + pricing_unit + price_type 全等，且 `is_confirmed=1`；多条时按 `quote_date DESC, id DESC` 取最新。返回完整 `price_source`。
- **fuzzy**：`0.7×名称最高 SequenceMatcher 相似度 + 0.3×材料相似度 >= 0.60`，取 top3；**仅参考价**，`needs_confirmation=True`、`amount=None`、只给 `provisional_amount`。
- **none**：`recommended_unit_price=None`、`amount=None`、`messages=["暂无可靠价格，等待人工填写"]`；同名但计价单位不一致时给候选价并要求人工确认或补充已确认规则，**不自动推荐**。
- 严禁均值/趋势/插值/AI 预测造价。取价策略与阈值均待蒋确认（见 docs/）。

## 单位校验规则
同维度同单位 → factor=1 直通；同维度不同倍率 → 仅 `pricing_rules.is_confirmed=1` 的规则可换算（cm2→m2 = 0.0001）；跨维度（m³ 对 元/㎡）直接拦截并提示「不属于同一维度，禁止直接相乘，请人工确认」；数量类单位之间默认不互通。

## 合价计算
`合价 = 计价工程量 × 单价`，全程 `decimal.Decimal`、金额 `quantize(0.01, ROUND_HALF_UP)`、工程量保留原始精度；示例 `53.7812 × 100.00 = 5378.12`、`50000 cm2 × 0.0001 = 5.0 m2 × 32.00 = 160.00`。

## 人工确认与回写流程
`generate_quote`(draft) → `export_quote_excel` → 前端人工改价 → `update_quote_item_price`（仅改 draft 明细，**绝不写 price_history**）→ `confirm_quote`。
`confirm_quote` 在**单个事务**内完成：更新 quote_items → 更新 quote_records(status/confirmed_by/total_amount) → 为每条有最终单价的行**只 INSERT** 一条新 `price_history`（`source_type='quote_confirmation'`, `source_ref=quote_id`, `is_confirmed=1`, `is_demo=0`）。
任何失败 rollback 并抛 `QuoteSaveError("数据库保存失败：…；本次确认已回滚，历史价格未写入")`；重复确认、仍有未填价（`allow_unpriced=False`）抛 `ValueError`。

## 对外接口示例
```python
from quotation import generate_quote, confirm_quote, export_quote_excel, init_database
db = init_database('output/quote_demo.db')          # 幂等，非 force 不重建
q = generate_quote(recognition_result, 'customer_quote', db)
export_quote_excel(q, 'output/报价明细_草案.xlsx')
confirm_quote(q['quote_id'], None, '老板', db, allow_unpriced=True)
```

## generate_quote 输出字段
| 字段 | 说明 |
|---|---|
| quote_id / project_id / project_name | 报价单号与项目标识 |
| price_type / price_type_label | 价格类型及其标签 |
| generated_at / status / currency | 生成时间、draft/confirmed、币种 |
| items[] | 完整明细行（见 INTERFACE.md） |
| total_amount | 仅可计价且价格已确认可用行的合价之和 |
| provisional_amount | 相似参考价暂估，**不计入 total_amount** |
| pending_items[] | 挂起项：item_id/item_name/reason/message/match_status |
| warnings[] | 模拟数据声明、待蒋确认规则、校验汇总 |
| database_path | 数据库绝对路径 |

## 8 条红线
1. 历史价格只 INSERT，禁止 UPDATE/DELETE（触发器 + 应用层）；2. 无匹配不自行生成/推算价格；
3. 相似匹配只作参考价，必须 `needs_confirmation=True`，不计入正式总价；
4. 每个推荐价可追溯（price_history_id / 来源项目 / 日期 / is_demo 等）；
5. 面积/体积/长度/数量不混用，跨维度拦截，换算需已确认规则；
6. `review_status != 'confirmed'` 的工程量不参与计价；
7. 取整/损耗/跨视图合并/相似阈值/多历史价取价策略一律不猜，显式挂起；
8. 未确认价格不写正式历史价；确认回写单事务，失败 rollback 并抛 `QuoteSaveError`。

## 模拟数据声明与已知限制
`data/seed_prices.json` 全部为**自造的模拟/脱敏样例**，每条 `is_demo=1`、`source_type='demo_seed'`，来源项目名带「模拟/脱敏样例」；**非真实商业报价，仅用于联调演示**。
**不得上传真实商业报价库，不得将附件 `报价.zip` 中 1.xlsx / 3.xlsx 的真实商业报价数据入库或写入任何交付物**；附件只用于参考表头结构。
已知限制：不提供前端；`modified`/`review_status`、取整、损耗、跨视图合并、含税与币种口径、有效期等均未启用（见 `docs/待确认问题与已知限制.md`）；xlsx 由标准库手写 OOXML，不支持图表/宏/公式；SQLite 单机原型，无并发与权限控制。
