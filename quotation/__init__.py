"""工程报价系统 · 奉的报价计算内核（零第三方依赖，供胡直接 import 调用）。

典型用法：
    from quotation import generate_quote, confirm_quote, export_quote_excel
"""

from .api import (
    QuoteSaveError,
    confirm_quote,
    generate_quote,
    get_quote,
    update_quote_item_price,
)
from .models import (
    FUZZY_THRESHOLD,
    PRICE_TYPES,
    QUANTITY_TYPES,
    load_recognition_result,
    normalize_recognition_result,
    validate_recognition_result,
)
from .seed import init_database, table_counts
from .xlsx import export_quote_excel

__all__ = [
    "generate_quote",
    "get_quote",
    "update_quote_item_price",
    "confirm_quote",
    "export_quote_excel",
    "init_database",
    "table_counts",
    "load_recognition_result",
    "validate_recognition_result",
    "normalize_recognition_result",
    "QuoteSaveError",
    "PRICE_TYPES",
    "QUANTITY_TYPES",
    "FUZZY_THRESHOLD",
]

