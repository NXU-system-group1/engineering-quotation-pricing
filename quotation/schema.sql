-- ============================================================
-- 工程报价系统 · 奉的报价模块 —— SQLite 结构定义
-- 零第三方依赖；PRAGMA foreign_keys=ON 由 db.connect() 每个连接设置。
-- 红线 1：price_history 为「只追加」表，用触发器禁止 UPDATE / DELETE。
-- ============================================================

-- 1) 标准工程项目及同义名称（别名归一）
CREATE TABLE IF NOT EXISTS item_catalog (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT    NOT NULL,
    category       TEXT    NOT NULL DEFAULT '',
    material       TEXT    NOT NULL DEFAULT '',
    process        TEXT    NOT NULL DEFAULT '',
    spec           TEXT    NOT NULL DEFAULT '',
    aliases        TEXT    NOT NULL DEFAULT '[]',   -- JSON 数组文本
    created_at     TEXT    NOT NULL,
    UNIQUE (canonical_name, material, process, spec)
);

-- 2) 历史价格及来源（追加-only，旧价格永不覆盖 / 永不删除）
CREATE TABLE IF NOT EXISTS price_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id           INTEGER NOT NULL,
    price_type        TEXT    NOT NULL CHECK (price_type IN
                          ('material_cost', 'self_construction_cost',
                           'outsource_cost', 'customer_quote')),
    unit_price        NUMERIC NOT NULL CHECK (unit_price >= 0),
    pricing_unit      TEXT    NOT NULL,
    currency          TEXT    NOT NULL DEFAULT 'CNY',
    source_project    TEXT    NOT NULL DEFAULT '',
    quote_date        TEXT    NOT NULL,               -- YYYY-MM-DD
    source_type       TEXT    NOT NULL DEFAULT 'demo_seed' CHECK (source_type IN
                          ('demo_seed', 'import', 'quote_confirmation', 'manual')),
    source_ref        TEXT    NOT NULL DEFAULT '',
    construction_note TEXT    NOT NULL DEFAULT '',
    remark            TEXT    NOT NULL DEFAULT '',
    is_confirmed      INTEGER NOT NULL DEFAULT 0 CHECK (is_confirmed IN (0, 1)),
    is_demo           INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    created_at        TEXT    NOT NULL,
    FOREIGN KEY (item_id) REFERENCES item_catalog (id)
);
CREATE INDEX IF NOT EXISTS idx_price_history_lookup
    ON price_history (item_id, price_type, pricing_unit, is_confirmed, quote_date DESC);

CREATE TRIGGER IF NOT EXISTS trg_price_history_no_update
BEFORE UPDATE ON price_history
BEGIN
    SELECT RAISE(ABORT, 'price_history 为只追加表，禁止 UPDATE（旧价格永不覆盖）');
END;

CREATE TRIGGER IF NOT EXISTS trg_price_history_no_delete
BEFORE DELETE ON price_history
BEGIN
    SELECT RAISE(ABORT, 'price_history 为只追加表，禁止 DELETE（旧价格永不删除）');
END;

-- 3) 计价单位与经确认的换算规则（仅 is_confirmed=1 可被计算层使用）
CREATE TABLE IF NOT EXISTS pricing_rules (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id             INTEGER,                     -- NULL 表示通用规则
    from_quantity_type  TEXT    NOT NULL,
    from_unit           TEXT    NOT NULL,
    to_pricing_unit     TEXT    NOT NULL,
    conversion_factor   NUMERIC NOT NULL,
    rule_note           TEXT    NOT NULL DEFAULT '',
    is_confirmed        INTEGER NOT NULL DEFAULT 0 CHECK (is_confirmed IN (0, 1)),
    confirmed_by        TEXT    NOT NULL DEFAULT '',
    confirmed_at        TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL,
    FOREIGN KEY (item_id) REFERENCES item_catalog (id)
);
CREATE INDEX IF NOT EXISTS idx_pricing_rules_lookup
    ON pricing_rules (from_quantity_type, from_unit, to_pricing_unit, is_confirmed);

-- 4) 报价单主记录
CREATE TABLE IF NOT EXISTS quote_records (
    quote_id      TEXT    PRIMARY KEY,
    project_id    TEXT    NOT NULL DEFAULT '',
    project_name  TEXT    NOT NULL DEFAULT '',
    price_type    TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed')),
    generated_at  TEXT    NOT NULL,
    confirmed_by  TEXT    NOT NULL DEFAULT '',
    confirmed_at  TEXT    NOT NULL DEFAULT '',
    total_amount  NUMERIC,
    currency      TEXT    NOT NULL DEFAULT 'CNY',
    remark        TEXT    NOT NULL DEFAULT ''
);

-- 5) 报价明细行
CREATE TABLE IF NOT EXISTS quote_items (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id                TEXT    NOT NULL,
    item_id                 TEXT    NOT NULL DEFAULT '',   -- 识图结果的 item_id
    item_name               TEXT    NOT NULL DEFAULT '',
    material                TEXT    NOT NULL DEFAULT '',
    process                 TEXT    NOT NULL DEFAULT '',
    spec                    TEXT    NOT NULL DEFAULT '',
    quantity_value          NUMERIC,
    quantity_type           TEXT    NOT NULL DEFAULT '',
    quantity_unit           TEXT    NOT NULL DEFAULT '',
    review_status           TEXT    NOT NULL DEFAULT '',
    pricing_quantity        NUMERIC,
    pricing_unit            TEXT    NOT NULL DEFAULT '',
    recommended_unit_price  NUMERIC,
    final_unit_price        NUMERIC,
    price_history_id        INTEGER,
    match_status            TEXT    NOT NULL DEFAULT 'none',
    match_score             NUMERIC,
    amount                  NUMERIC,
    provisional_amount      NUMERIC,
    needs_confirmation      INTEGER NOT NULL DEFAULT 1 CHECK (needs_confirmation IN (0, 1)),
    messages                TEXT    NOT NULL DEFAULT '[]',  -- JSON 数组文本
    candidates              TEXT    NOT NULL DEFAULT '[]',  -- JSON 数组文本
    construction_note       TEXT    NOT NULL DEFAULT '',
    remark                  TEXT    NOT NULL DEFAULT '',
    raw_json                TEXT    NOT NULL DEFAULT '{}',
    created_at              TEXT    NOT NULL,
    FOREIGN KEY (quote_id) REFERENCES quote_records (quote_id),
    FOREIGN KEY (price_history_id) REFERENCES price_history (id)
);
CREATE INDEX IF NOT EXISTS idx_quote_items_quote ON quote_items (quote_id);
