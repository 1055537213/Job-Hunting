"""后台 token 和工具调用记录的分页保留规则。"""

from __future__ import annotations

from math import ceil

ADMIN_LEDGER_PAGE_SIZE = 100
ADMIN_LEDGER_MAX_PAGES = 5
ADMIN_LEDGER_MAX_RECORDS = ADMIN_LEDGER_PAGE_SIZE * ADMIN_LEDGER_MAX_PAGES


def admin_ledger_page_count(total_records: int) -> int:
    """按固定页大小计算总页数，并限制为最多 5 页。"""

    total = max(0, int(total_records))
    if total == 0:
        return 0
    return min(ADMIN_LEDGER_MAX_PAGES, ceil(total / ADMIN_LEDGER_PAGE_SIZE))
