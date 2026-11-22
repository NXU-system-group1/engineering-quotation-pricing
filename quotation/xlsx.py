"""标准库手写 OOXML(.xlsx) 导出：零第三方依赖，仅 zipfile + xml。

不使用 openpyxl / xlsxwriter / pandas。生成合法 xlsx 包并在写完后
用 zipfile + ElementTree 自检所有部件。
"""

from __future__ import annotations

import math
import re
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from xml.sax.saxutils import escape

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# xl/styles.xml 中 cellXfs 的样式索引
STYLE_TEXT = 0
STYLE_HEADER = 1
STYLE_MONEY = 2
STYLE_QUANTITY = 3
STYLE_WRAP = 4
STYLE_TOTAL_TEXT = 5
STYLE_TOTAL_MONEY = 6

_KIND_STYLE: dict[str, int] = {
    "text": STYLE_WRAP,
    "number": STYLE_QUANTITY,
    "money": STYLE_MONEY,
}

_MATCH_LABELS: dict[str, str] = {
    "exact": "完全匹配",
    "fuzzy": "相似匹配(待确认)",
    "none": "无匹配",
    "manual": "人工填写",
}

_DETAIL_COLUMNS: list[dict[str, Any]] = [
    {"title": "序号", "width": 7, "kind": "text"},
    {"title": "施工部位", "width": 14, "kind": "text"},
    {"title": "施工项目", "width": 24, "kind": "text"},
    {"title": "材料", "width": 12, "kind": "text"},
    {"title": "工艺", "width": 10, "kind": "text"},
    {"title": "规格", "width": 10, "kind": "text"},
    {"title": "工程量", "width": 13, "kind": "number"},
    {"title": "单位", "width": 9, "kind": "text"},
    {"title": "计价工程量", "width": 15, "kind": "number"},
    {"title": "计价单位", "width": 11, "kind": "text"},
    {"title": "推荐单价", "width": 13, "kind": "money"},
    {"title": "确认单价", "width": 13, "kind": "money"},
    {"title": "合价", "width": 15, "kind": "money"},
    {"title": "匹配状态", "width": 19, "kind": "text"},
    {"title": "价格来源", "width": 38, "kind": "text"},
    {"title": "是否需人工确认", "width": 16, "kind": "text"},
    {"title": "施工说明", "width": 28, "kind": "text"},
    {"title": "备注", "width": 52, "kind": "text"},
]


def _xml_escape(value: Any) -> str:
    """转义 XML 文本并剔除非法控制字符。"""
    return escape(_ILLEGAL_XML.sub("", str(value)))


def _attr_escape(value: Any) -> str:
    """转义 XML 属性值（含引号）。"""
    return escape(
        _ILLEGAL_XML.sub("", str(value)),
        {'"': "&quot;", "'": "&apos;"},
    )


def _col_letter(index: int) -> str:
    """1-based 列号转 Excel 列字母（1→A, 27→AA）。"""
    letters = ""
    current = index
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _safe_sheet_name(name: Any, index: int) -> str:
    """清洗 sheet 名：去掉 Excel 非法字符并截断到 31 字符。"""
    text = re.sub(r"[\\/*?:\[\]]", "_", str(name or "")).strip()
    return (text or f"Sheet{index}")[:31]


def _unique_sheet_names(raw_names: list[Any]) -> list[str]:
    """生成不重名的 sheet 名列表。"""
    used: dict[str, int] = {}
    result: list[str] = []
    for index, raw in enumerate(raw_names, start=1):
        base = _safe_sheet_name(raw, index)
        if base in used:
            used[base] += 1
            name = f"{base[:27]}({used[base]})"
        else:
            used[base] = 0
            name = base
        result.append(name)
    return result


def _inline_str_cell(ref: str, style: int, text: str) -> str:
    """生成 inlineStr 字符串单元格。"""
    return (
        f'<c r="{ref}" s="{style}" t="inlineStr">'
        f'<is><t xml:space="preserve">{_xml_escape(text)}</t></is></c>'
    )


def _numeric_text(value: Any) -> str | None:
    """把数值转成 xlsx <v> 可用的文本；非有限浮点返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else None
    return None


def _cell_xml(col_index: int, row_index: int, value: Any, style: int) -> str:
    """生成单个 <c> 单元格：None→空，数值→<v>，其余→inlineStr。"""
    ref = f"{_col_letter(col_index)}{row_index}"
    if value is None:
        return f'<c r="{ref}" s="{style}"/>'
    if isinstance(value, bool):
        return _inline_str_cell(ref, style, "是" if value else "否")
    numeric = _numeric_text(value)
    if numeric is not None:
        return f'<c r="{ref}" s="{style}"><v>{numeric}</v></c>'
    if isinstance(value, float):
        return f'<c r="{ref}" s="{style}"/>'
    return _inline_str_cell(ref, style, str(value))


def _sheet_xml(sheet: dict[str, Any]) -> str:
    """生成单个 worksheet XML。"""
    columns = list(sheet.get("columns") or [])
    rows = list(sheet.get("rows") or [])
    total_row = sheet.get("total_row")
    freeze_header = bool(sheet.get("freeze_header", True))

    parts: list[str] = [_XML_DECL, f'<worksheet xmlns="{_MAIN_NS}">']
    if freeze_header:
        parts.append(
            '<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
            "</sheetView></sheetViews>"
        )
    else:
        parts.append('<sheetViews><sheetView workbookViewId="0"/></sheetViews>')
    parts.append('<sheetFormatPr defaultRowHeight="15"/>')

    if columns:
        parts.append("<cols>")
        for index, column in enumerate(columns, start=1):
            width = int(column.get("width") or 12)
            parts.append(
                f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
            )
        parts.append("</cols>")

    parts.append('<sheetData><row r="1" ht="30" customHeight="1">')
    for index, column in enumerate(columns, start=1):
        parts.append(
            _cell_xml(index, 1, str(column.get("title") or ""), STYLE_HEADER)
        )
    parts.append("</row>")

    for row_index, raw_row in enumerate(rows, start=2):
        row = raw_row if isinstance(raw_row, (list, tuple)) else [raw_row]
        parts.append(f'<row r="{row_index}">')
        for col_index, column in enumerate(columns, start=1):
            value = row[col_index - 1] if col_index - 1 < len(row) else None
            kind = str(column.get("kind") or "text")
            parts.append(
                _cell_xml(
                    col_index,
                    row_index,
                    value,
                    _KIND_STYLE.get(kind, STYLE_WRAP),
                )
            )
        parts.append("</row>")

    if total_row is not None:
        row_index = len(rows) + 2
        row = total_row if isinstance(total_row, (list, tuple)) else [total_row]
        parts.append(f'<row r="{row_index}">')
        for col_index, column in enumerate(columns, start=1):
            value = row[col_index - 1] if col_index - 1 < len(row) else None
            kind = str(column.get("kind") or "text")
            style = (
                STYLE_TOTAL_MONEY
                if kind in ("money", "number")
                else STYLE_TOTAL_TEXT
            )
            parts.append(_cell_xml(col_index, row_index, value, style))
        parts.append("</row>")

    parts.append("</sheetData>")
    parts.append(
        '<pageMargins left="0.7" right="0.7" top="0.75" '
        'bottom="0.75" header="0.3" footer="0.3"/>'
    )
    parts.append("</worksheet>")
    return "".join(parts)


def _content_types_xml(sheet_count: int) -> str:
    """生成 [Content_Types].xml。"""
    parts: list[str] = [
        _XML_DECL,
        f'<Types xmlns="{_CT_NS}">',
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.styles+xml"/>',
    ]
    for index in range(1, sheet_count + 1):
        parts.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.worksheet+xml"/>'
        )
    parts.append(
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    )
    parts.append(
        '<Override PartName="/docProps/app.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'extended-properties+xml"/>'
    )
    parts.append("</Types>")
    return "".join(parts)


def _root_rels_xml() -> str:
    """生成 _rels/.rels。"""
    return (
        _XML_DECL
        + f'<Relationships xmlns="{_PKG_REL_NS}">'
        + f'<Relationship Id="rId1" Type="{_REL_NS}/officeDocument" '
        'Target="xl/workbook.xml"/>'
        + '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/package/2006/'
        'relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        + f'<Relationship Id="rId3" Type="{_REL_NS}/extended-properties" '
        'Target="docProps/app.xml"/>'
        + "</Relationships>"
    )


def _workbook_xml(sheet_names: list[str]) -> str:
    """生成 xl/workbook.xml。"""
    parts: list[str] = [
        _XML_DECL,
        f'<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}"><sheets>',
    ]
    for index, name in enumerate(sheet_names, start=1):
        parts.append(
            f'<sheet name="{_attr_escape(name)}" sheetId="{index}" '
            f'r:id="rId{index}"/>'
        )
    parts.append("</sheets></workbook>")
    return "".join(parts)


def _workbook_rels_xml(sheet_count: int) -> str:
    """生成 xl/_rels/workbook.xml.rels。"""
    parts: list[str] = [
        _XML_DECL,
        f'<Relationships xmlns="{_PKG_REL_NS}">',
    ]
    for index in range(1, sheet_count + 1):
        parts.append(
            f'<Relationship Id="rId{index}" Type="{_REL_NS}/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
    parts.append(
        f'<Relationship Id="rId{sheet_count + 1}" '
        f'Type="{_REL_NS}/styles" Target="styles.xml"/>'
    )
    parts.append("</Relationships>")
    return "".join(parts)


def _styles_xml() -> str:
    """生成 xl/styles.xml（表头粗体+底色、边框、金额/工程量格式、自动换行）。"""
    return (
        _XML_DECL
        + f'<styleSheet xmlns="{_MAIN_NS}">'
        + '<numFmts count="2">'
        + '<numFmt numFmtId="164" formatCode="#,##0.00"/>'
        + '<numFmt numFmtId="165" formatCode="#,##0.####"/>'
        + "</numFmts>"
        + '<fonts count="3">'
        + '<font><sz val="11"/><name val="Calibri"/></font>'
        + '<font><b/><sz val="11"/><color rgb="FF1F3864"/>'
        + '<name val="Calibri"/></font>'
        + '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        + "</fonts>"
        + '<fills count="3">'
        + '<fill><patternFill patternType="none"/></fill>'
        + '<fill><patternFill patternType="gray125"/></fill>'
        + '<fill><patternFill patternType="solid">'
        + '<fgColor rgb="FFDCE6F1"/><bgColor indexed="64"/>'
        + "</patternFill></fill>"
        + "</fills>"
        + '<borders count="2">'
        + "<border><left/><right/><top/><bottom/><diagonal/></border>"
        + '<border><left style="thin"><color rgb="FFB0B0B0"/></left>'
        + '<right style="thin"><color rgb="FFB0B0B0"/></right>'
        + '<top style="thin"><color rgb="FFB0B0B0"/></top>'
        + '<bottom style="thin"><color rgb="FFB0B0B0"/></bottom>'
        + "<diagonal/></border>"
        + "</borders>"
        + '<cellStyleXfs count="1">'
        + '<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
        + "</cellStyleXfs>"
        + '<cellXfs count="7">'
        + '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        + '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" '
        'applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        + '<alignment horizontal="center" vertical="center" wrapText="1"/>'
        + "</xf>"
        + '<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" '
        'applyNumberFormat="1" applyBorder="1" applyAlignment="1">'
        + '<alignment horizontal="right"/>'
        + "</xf>"
        + '<xf numFmtId="165" fontId="0" fillId="0" borderId="1" xfId="0" '
        'applyNumberFormat="1" applyBorder="1"/>'
        + '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" '
        'applyBorder="1" applyAlignment="1">'
        + '<alignment vertical="top" wrapText="1"/>'
        + "</xf>"
        + '<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" '
        'applyFont="1" applyFill="1" applyBorder="1"/>'
        + '<xf numFmtId="164" fontId="2" fillId="2" borderId="1" xfId="0" '
        'applyNumberFormat="1" applyFont="1" applyFill="1" '
        'applyBorder="1" applyAlignment="1">'
        + '<alignment horizontal="right"/>'
        + "</xf>"
        + "</cellXfs>"
        + '<cellStyles count="1">'
        + '<cellStyle name="Normal" xfId="0" builtinId="0"/>'
        + "</cellStyles>"
        + "</styleSheet>"
    )


def _core_xml() -> str:
    """生成 docProps/core.xml。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        _XML_DECL
        + '<cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        + "<dc:title>工程报价明细</dc:title>"
        + "<dc:creator>奉的报价模块</dc:creator>"
        + "<cp:lastModifiedBy>奉的报价模块</cp:lastModifiedBy>"
        + f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        + f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        + "</cp:coreProperties>"
    )


def _app_xml() -> str:
    """生成 docProps/app.xml。"""
    return (
        _XML_DECL
        + '<Properties '
        'xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        + "<Application>QuotationModule</Application>"
        + "<AppVersion>1.0</AppVersion>"
        + "</Properties>"
    )


def _self_check(path: Path, required_parts: list[str]) -> None:
    """写完后重新打开 zip：部件齐全且每个 XML 均可被 ElementTree 解析。"""
    with zipfile.ZipFile(str(path), "r") as zf:
        present = set(zf.namelist())
        missing = [name for name in required_parts if name not in present]
        if missing:
            raise ValueError(f"xlsx 自检失败，缺少部件：{missing}")
        for name in required_parts:
            ElementTree.fromstring(zf.read(name))


def write_xlsx(sheets: list[dict], output_path: str) -> str:
    """用标准库手写 OOXML 生成 .xlsx 文件并返回绝对路径。

    Args:
        sheets: 每项为 {'name', 'columns':[{'title','width','kind'}...],
            'rows': [[值...]], 'freeze_header': bool, 'total_row': [值...](可选)}。
        output_path: 输出 .xlsx 路径，父目录自动创建。

    Returns:
        output_path 的绝对路径字符串。

    Raises:
        ValueError: sheets 为空，或写后自检不通过。
    """
    if not isinstance(sheets, list) or not sheets:
        raise ValueError("sheets 必须是至少含 1 个 sheet 的 list")

    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_count = len(sheets)
    sheet_names = _unique_sheet_names([s.get("name") for s in sheets])

    parts: dict[str, str] = {
        "[Content_Types].xml": _content_types_xml(sheet_count),
        "_rels/.rels": _root_rels_xml(),
        "xl/workbook.xml": _workbook_xml(sheet_names),
        "xl/_rels/workbook.xml.rels": _workbook_rels_xml(sheet_count),
        "xl/styles.xml": _styles_xml(),
        "docProps/core.xml": _core_xml(),
        "docProps/app.xml": _app_xml(),
    }
    for index, sheet in enumerate(sheets, start=1):
        parts[f"xl/worksheets/sheet{index}.xml"] = _sheet_xml(sheet)

    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in parts.items():
            zf.writestr(name, content.encode("utf-8"))

    _self_check(path, list(parts.keys()))
    return str(path)


def _as_number(value: Any) -> Any:
    """尽量把金额转成数值，保证 Excel 单元格为数值型而非文本。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _source_text(source: Any) -> str:
    """把价格来源拼成「来源项目 / 报价日期 / #历史价ID」。"""
    if not isinstance(source, dict) or not source:
        return "—"
    project = str(source.get("source_project") or "")
    quote_date = str(source.get("quote_date") or "")
    history_id = source.get("price_history_id")
    suffix = f"#{history_id}" if history_id is not None else "#"
    return f"{project} / {quote_date} / {suffix}"


def _match_label(status: Any) -> str:
    """匹配状态转中文标签。"""
    key = str(status or "")
    return _MATCH_LABELS.get(key, key)


def _messages_text(item: dict[str, Any]) -> str:
    """把 messages 列表拼成备注文本。"""
    return "；".join(str(m) for m in (item.get("messages") or []))


def _detail_sheet(quote_result: dict[str, Any]) -> dict[str, Any]:
    """构造「报价明细」sheet。"""
    items = list(quote_result.get("items") or [])
    total_amount = _as_number(quote_result.get("total_amount"))
    rows: list[list[Any]] = []
    for index, item in enumerate(items, start=1):
        raw = item.get("raw") or {}
        if not isinstance(raw, dict):
            raw = {}
        location = raw.get("施工部位") or raw.get("location") or ""
        quantity = item.get("quantity") or {}
        if not isinstance(quantity, dict):
            quantity = {}
        rows.append(
            [
                index,
                location,
                item.get("item_name") or "",
                item.get("material") or "",
                item.get("process") or "",
                item.get("spec") or "",
                _as_number(quantity.get("value")),
                quantity.get("unit") or "",
                _as_number(item.get("pricing_quantity")),
                item.get("pricing_unit") or "",
                _as_number(item.get("recommended_unit_price")),
                _as_number(item.get("final_unit_price")),
                _as_number(item.get("amount")),
                _match_label(item.get("match_status")),
                _source_text(item.get("price_source")),
                "是" if item.get("needs_confirmation") else "否",
                item.get("construction_note") or "",
                _messages_text(item),
            ]
        )

    total_row: list[Any] = [None] * len(_DETAIL_COLUMNS)
    total_row[0] = "合计"
    total_row[12] = total_amount
    total_row[17] = (
        "仅统计可计价行；相似匹配暂估价不计入；本次使用模拟/脱敏价格数据，"
        "非真实商业报价"
    )
    return {
        "name": "报价明细",
        "columns": _DETAIL_COLUMNS,
        "rows": rows,
        "freeze_header": True,
        "total_row": total_row,
    }


def _pending_sheet(quote_result: dict[str, Any]) -> dict[str, Any]:
    """构造「待确认项目」sheet。"""
    columns: list[dict[str, Any]] = [
        {"title": "序号", "width": 7, "kind": "text"},
        {"title": "item_id", "width": 18, "kind": "text"},
        {"title": "项目名称", "width": 26, "kind": "text"},
        {"title": "原因", "width": 32, "kind": "text"},
        {"title": "提示", "width": 56, "kind": "text"},
        {"title": "匹配状态", "width": 19, "kind": "text"},
        {"title": "是否需人工确认", "width": 16, "kind": "text"},
    ]
    pending_items = list(quote_result.get("pending_items") or [])
    rows: list[list[Any]] = []
    if pending_items:
        for index, pending in enumerate(pending_items, start=1):
            if isinstance(pending, dict):
                rows.append(
                    [
                        index,
                        pending.get("item_id", ""),
                        pending.get("item_name", ""),
                        pending.get("reason", ""),
                        pending.get("message", ""),
                        _match_label(pending.get("match_status", "")),
                        "是",
                    ]
                )
            else:
                rows.append([index, "", "", "", str(pending), "", "是"])
    else:
        for index, item in enumerate(quote_result.get("items") or [], start=1):
            if not item.get("needs_confirmation"):
                continue
            messages = list(item.get("messages") or [])
            rows.append(
                [
                    len(rows) + 1,
                    item.get("item_id", ""),
                    item.get("item_name", ""),
                    messages[0] if messages else "需人工确认",
                    _messages_text(item),
                    _match_label(item.get("match_status", "")),
                    "是",
                ]
            )
    if not rows:
        rows.append(["", "", "", "", "本次无待确认项目", "", ""])
    return {
        "name": "待确认项目",
        "columns": columns,
        "rows": rows,
        "freeze_header": True,
    }


def _notes_sheet(quote_result: dict[str, Any]) -> dict[str, Any]:
    """构造「说明」sheet（数据来源、红线摘要、待确认业务规则）。"""
    columns: list[dict[str, Any]] = [
        {"title": "项目", "width": 26, "kind": "text"},
        {"title": "内容", "width": 104, "kind": "text"},
    ]
    rows: list[list[Any]] = [
        [
            "数据来源",
            "全部为模拟/脱敏样例数据，非真实商业报价，仅用于联调演示；"
            "所有种子价格均为自造数值，不代表任何真实工程报价。",
        ],
        ["报价单号", quote_result.get("quote_id", "")],
        ["项目ID", quote_result.get("project_id", "")],
        ["项目名称", quote_result.get("project_name", "")],
        [
            "价格类型",
            f"{quote_result.get('price_type_label', '')}"
            f"（{quote_result.get('price_type', '')}）",
        ],
        ["生成时间", quote_result.get("generated_at", "")],
        ["报价状态", quote_result.get("status", "")],
        ["总价（元）", _as_number(quote_result.get("total_amount"))],
        ["暂估金额（元）", _as_number(quote_result.get("provisional_amount"))],
        [
            "匹配状态口径",
            "exact=完全匹配；fuzzy=相似匹配，仅参考价、必须人工确认且不计入正式总价；"
            "none=无匹配，暂无可靠价格等待人工填写；manual=人工填写。",
        ],
        [
            "红线1 历史价格只追加",
            "price_history 建 BEFORE UPDATE / BEFORE DELETE 触发器 RAISE(ABORT)；"
            "应用层写回只 INSERT，旧价格永不覆盖。",
        ],
        [
            "红线2 无匹配不生成价格",
            "显示「暂无可靠价格，等待人工填写」，推荐单价与合价均为 None；"
            "禁止均值/趋势/插值/预测造价。",
        ],
        [
            "红线3 相似匹配仅参考",
            "fuzzy 行 needs_confirmation=True，不计入 total_amount，"
            "只给 provisional_amount 暂估金额。",
        ],
        [
            "红线4 推荐价可追溯",
            "返回 price_history_id / source_project / quote_date / price_type / "
            "is_confirmed / construction_note / remark / is_demo。",
        ],
        [
            "红线5 单位维度不混用",
            "面积/体积/长度/数量跨维度直接拦截（提示「不属于同一维度，禁止直接相乘，"
            "请人工确认」）；同维度不同倍率必须有 pricing_rules 中 "
            "is_confirmed=1 的已确认规则才允许换算。",
        ],
        [
            "红线6 未确认工程量不计价",
            "review_status != confirmed 的工程量挂起，amount=None，"
            "提示「工程量未经人工确认，暂不参与计价」。",
        ],
        [
            "红线7 业务规则不猜测",
            "工程量取整、损耗系数、跨视图合并、相似匹配阈值、同名多历史价取价策略"
            "均做成显式挂起/可配置但默认关闭，本版未启用。",
        ],
        [
            "红线8 确认回写单事务",
            "未确认价格不写入正式历史价；确认时 quote_items 更新 + quote_records 状态 + "
            "price_history 新增在同一事务内，失败 rollback 并抛 QuoteSaveError。",
        ],
        [
            "待确认业务规则（待蒋裁定）",
            "取整规则、损耗系数、跨视图合并、相似匹配阈值 0.60、"
            "多历史价取价策略（is_confirmed 优先→quote_date DESC→id DESC）、"
            "含税/币种口径、人工修改后打标方式等均待确认；"
            "业务规则以蒋的裁定为准，本版未擅自猜测。",
        ],
        [
            "免责声明",
            "本文件仅用于系统联调演示，不得作为对外报价、结算或商务依据。",
        ],
    ]
    return {
        "name": "说明",
        "columns": columns,
        "rows": rows,
        "freeze_header": True,
    }


def export_quote_excel(quote_result: dict, output_path: str) -> str:
    """把 generate_quote() 结果导出为含 3 个 sheet 的 .xlsx。

    Sheets：报价明细（含合计行）、待确认项目、说明。
    金额单元格一律为数值型，便于 Excel 求和。

    Args:
        quote_result: generate_quote() 返回的结果 dict。
        output_path: 输出 .xlsx 路径。

    Returns:
        output_path 的绝对路径字符串。
    """
    if not isinstance(quote_result, dict):
        raise TypeError(
            f"quote_result 必须是 dict，实际为 {type(quote_result).__name__}"
        )
    sheets = [
        _detail_sheet(quote_result),
        _pending_sheet(quote_result),
        _notes_sheet(quote_result),
    ]
    return write_xlsx(sheets, output_path)
