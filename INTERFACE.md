# INTERFACE.md · 给胡的对接契约（字段未冻结，以蒋裁定为准）
模块：`quotation`（零第三方依赖）。`generate_quote(recognition_result: dict, price_type: str, database_path: str) -> dict` **签名冻结**。
顶层可 `from quotation import ...`；完整示例见 `examples/recognition_demo.json` 与 `examples/demo.py`。

## 输入 JSON schema（逐字段）
| 字段 | 类型 | 必填 | 示例 | 说明 |
|---|---|---|---|---|
| project_id | str | 是 | `"demo001"` | 项目 ID，缺失会进 validation_errors |
| project_name | str | 否 | `"联调演示项目（模拟）"` | 项目名称，确认回写时作为来源项目名 |
| items | list[dict] | 是 | 见下 | 非 list 会报错 |
| items[].item_id | str | 是 | `"item001"` | 识图项唯一标识，改价/确认按此定位 |
| items[].item_name | str | 是 | `"墙面PVC饰面"` | 项目名称，别名归一后匹配标准项目 |
| items[].material | str | 否 | `"PVC"` | 材料；完全匹配要求与 catalog 全等 |
| items[].process | str | 否 | `"贴面"` | 工艺；同上 |
| items[].spec | str | 否 | `""` | 规格；同上 |
| items[].quantity | dict | 是 | `{"value":53.7812,"type":"area","unit":"m2"}` | 缺失时可用顶层折平 `value/type/unit` 兜底（会给提示） |
| items[].quantity.value | int/float | 是 | `53.7812` | 必须 >=0；非数值/负数进 validation_errors |
| items[].quantity.type | str | 是 | `"area"` | area/volume/length/count 或 面积/体积/长度/数量 |
| items[].quantity.unit | str | 是 | `"m2"` | 见单位映射表；未知进 validation_errors |
| items[].review_status | str | 否 | `"confirmed"` | 缺省视为 `unconfirmed`（挂起）；可写在 item 顶层或 quantity 内 |
| items[].dimensions | dict | 否 | `{}` | 原样保留到 `raw`，不参与计算 |
| items[].evidence | list | 否 | `[]` | 原样保留到 `raw` |
| items[].施工部位 / location | str | 否 | `"一层展厅墙面"` | Excel「施工部位」列读取；不影响匹配 |
| 其它未知字段 | any | 否 | — | 一律保留在 `item.raw` / `quote_items.raw_json` |

单位映射：`m2/㎡/平方米/m^2/M2/平米 → (area,m2)`；`cm2/cm²/平方厘米 → (area,cm2)`；
`m3/m³/立方米/方 → (volume,m3)`；`m/米/延米/M → (length,m)`；`cm/厘米 → (length,cm)`；
`个/套/件/项/台/樘/处/次/块 → (count, 原单位)`。数量类单位之间默认不互通。
`review_status`：`confirmed`=可用；`modified`=人工修改（**本版仍挂起**）；`unconfirmed`/`pending`=挂起。

## 输出 dict（generate_quote / get_quote 同结构）
| 字段 | 类型 | 说明 |
|---|---|---|
| quote_id / project_id / project_name | str | 报价单号与项目标识 |
| price_type / price_type_label | str | 四类之一及中文标签 |
| generated_at / status / currency | str | 生成时间、`draft`/`confirmed`、`CNY` |
| total_amount | float | **仅可计价行合价之和**（Decimal 累加后转 float） |
| provisional_amount | float | 相似匹配暂估金额，**不计入 total_amount** |
| items[] | list[dict] | 完整明细行（下表） |
| pending_items[] | list[dict] | `{item_id,item_name,reason,message,match_status}` |
| warnings[] | list[str] | 模拟数据声明、待蒋确认规则、校验汇总 |
| database_path | str | 数据库绝对路径 |
| items[].item_id/item_name/material/process/spec | str | 项目标识 |
| items[].quantity | dict | `{value,type,unit,review_status}` |
| items[].pricing_quantity / pricing_unit | float|None / str | 换算后计价工程量与计价单位 |
| items[].recommended_unit_price | float|None | 推荐单价；无匹配为 None |
| items[].final_unit_price / amount | float|None | 最终单价 / 合价（挂起时 None） |
| items[].provisional_amount | float|None | 仅 fuzzy 行有值 |
| items[].price_history_id | int|None | 原推荐价 id；manual 行仍保留 |
| items[].price_source | dict|None | `{price_history_id,source_project,quote_date,price_type,price_type_label,pricing_unit,unit_price,is_confirmed,is_demo,source_type,source_ref,construction_note,remark,currency}` |
| items[].match_status | str | `exact` / `fuzzy` / `none` / `manual` |
| items[].match_score / candidates[] | float / list | 相似度与 top3 候选（含各自 price_source） |
| items[].needs_confirmation | bool | True 表示该行挂起、amount 不进总价 |
| items[].messages[] | list[str] | 中文提示/拦截原因，直接展示给用户 |
| items[].construction_note / remark / raw | str / dict | 施工说明、备注、原始识图字段 |
| items[].validation_errors[] | list[str] | 该项结构化校验错误 |

## 可复制调用示例
```python
from quotation import (init_database, load_recognition_result, validate_recognition_result,
                       generate_quote, get_quote, update_quote_item_price,
                       confirm_quote, export_quote_excel, QuoteSaveError)
db = init_database('output/quote_demo.db')                 # 幂等；force=True 才重建
rec = load_recognition_result('examples/recognition_demo.json')  # 也支持 dict / JSON 字符串
assert validate_recognition_result(rec) == []            # 先校验，错误列表直接展示
q = generate_quote(rec, 'customer_quote', db)            # 签名冻结
qid = q['quote_id']
q2 = get_quote(qid, db)                                   # 从 DB 重建同结构 dict
q3 = update_quote_item_price(qid, 'item003', 55.0, db, remark='人工填写')  # 仅 draft 可改
export_quote_excel(q3, 'output/报价明细_草案.xlsx')
try:
    c = confirm_quote(qid, None, '老板', db, allow_unpriced=False)
except ValueError as e:
    print(e)            # 列出仍无单价的 item_id
c = confirm_quote(qid, [{'item_id':'item002','final_unit_price':180.0,'remark':'核定'}],
                  '老板', db, allow_unpriced=True, note='确认备注')
print(c['status'], c['history_records_added'])           # confirmed / 4
export_quote_excel(get_quote(qid, db), 'output/报价明细_已确认.xlsx')
```
`confirm_quote` 的 `items` 中 `final_unit_price=None` 表示沿用当前值；`items=None/[]` 表示全部沿用。

## 异常类型与错误信息样例
| 类型 | 触发 | 信息样例 |
|---|---|---|
| ValueError | price_type 非法 | `price_type 非法：'x'；合法值为：material_cost, self_construction_cost, outsource_cost, customer_quote` |
| ValueError | 报价已确认后改价 | `报价已确认，不能修改明细价格` |
| ValueError | 重复确认 | `报价已确认，禁止重复确认（避免重复写入历史价格）` |
| ValueError | 仍有未填价 | `以下项目仍无最终单价，需人工填写或传 allow_unpriced=True：item004, item005` |
| ValueError | 单价非 >0 数值 / item_id 不存在 / quote_id 不存在 | `unit_price 必须是 >0 的数值…` |
| QuoteSaveError | 保存阶段任何 DB 异常（含非法路径） | `数据库保存失败：…；本次确认已回滚，历史价格未写入` |
| TypeError | 识图输入类型不支持 | `识图结果输入类型不支持：int` |
| FileNotFoundError | 给了 `.json` 路径但文件不存在 | `识图结果文件不存在：…` |

## 确认回写与历史价格
`confirm_quote` 成功后 `quote_records.status='confirmed'`，并为每条有最终单价的明细**只 INSERT** 一条新 `price_history`：`source_type='quote_confirmation'`、`source_ref=quote_id`、`is_confirmed=1`、`is_demo=0`，返回值附 `history_records_added`。旧记录永不覆盖（`price_history` 上有 BEFORE UPDATE / BEFORE DELETE 触发器）；草案态 `update_quote_item_price` 绝不写历史价格库。
计价单位换算只认 `pricing_rules` 中 `is_confirmed=1` 的已确认规则，未确认规则一律拦截并提示人工确认。

## 前端联调建议
先 `validate_recognition_result` → 展示 `recommended_unit_price + price_source + match_status + messages`（fuzzy 必须标「仅参考、需人工确认」，none 显示「暂无可靠价格」）
→ 人工改价（`update_quote_item_price` 或汇总后一次性传给 `confirm_quote`）→ `confirm_quote`（未填价默认阻止）→ `export_quote_excel`。
`total_amount` 可直接显示合计；`provisional_amount` 单独标「暂估，不计入合计」；`pending_items` 建议做红点/待办列表。

## 待与胡 / 蒋确认的字段清单
1. `item_name` 与 `material` 的传递口径（是否含品牌/规格前缀）；2. `quantity.type` 枚举与单位写法是否冻结为上述映射；
3. `review_status=modified` + `modified_by` 能否直接计价；4. `dimensions/evidence` 是否需要展开展示；
5. `施工部位` 字段名是否统一（当前兼容 `施工部位`/`location`）；6. `spec` 为空时的匹配口径；
7. 币种 / 含税 / 价格有效期是否进契约；8. 计数类单位（个/套/项）之间是否允许已确认换算规则；
9. 相似匹配阈值 0.60 与多历史价取价策略；10. 人工改价是逐条调用还是一次性提交数组。
以上以蒋的裁定为准，本版全部做显式挂起或宽容解析，未擅自猜测。
