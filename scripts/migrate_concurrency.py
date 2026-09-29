"""为已存在的数据库补齐并发/幂等改造所需的列与唯一约束。

用法：python scripts/migrate_concurrency.py

- 新增 allowance_transactions.idempotency_key（同账户唯一）
- 新增 compliance_records.idempotency_key
- 新增 quotas / compliance_records 的 (company_id, year) 唯一约束
幂等：列/索引已存在时跳过；全新部署可直接用 init_db.py，无需执行本脚本。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402

from app.core.database import engine  # noqa: E402


def _has_column(inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_index(inspector, name: str) -> bool:
    return any(
        ix["name"] == name
        for table in inspector.get_table_names()
        for ix in inspector.get_indexes(table)
    )


def main():
    inspector = inspect(engine)
    statements: list[str] = []

    if "allowance_transactions" in inspector.get_table_names():
        if not _has_column(inspector, "allowance_transactions", "idempotency_key"):
            statements.append(
                "ALTER TABLE allowance_transactions ADD COLUMN idempotency_key VARCHAR(64)"
            )
        if not _has_index(inspector, "uq_tx_account_idem"):
            # SQLite 中 NULL 不参与唯一索引，未携带幂等键的历史/新请求不受影响
            statements.append(
                "CREATE UNIQUE INDEX uq_tx_account_idem "
                "ON allowance_transactions (account_id, idempotency_key)"
            )

    if "compliance_records" in inspector.get_table_names():
        if not _has_column(inspector, "compliance_records", "idempotency_key"):
            statements.append(
                "ALTER TABLE compliance_records ADD COLUMN idempotency_key VARCHAR(64)"
            )
        if not _has_index(inspector, "uq_compliance_company_year"):
            statements.append(
                "CREATE UNIQUE INDEX uq_compliance_company_year "
                "ON compliance_records (company_id, year)"
            )

    if "quotas" in inspector.get_table_names() and not _has_index(inspector, "uq_quota_company_year"):
        statements.append(
            "CREATE UNIQUE INDEX uq_quota_company_year ON quotas (company_id, year)"
        )

    if not statements:
        print("无需迁移：所有列与约束均已存在")
        return

    with engine.begin() as conn:
        for stmt in statements:
            print(f"执行：{stmt}")
            conn.execute(text(stmt))
    print(f"迁移完成：{len(statements)} 项变更")


if __name__ == "__main__":
    main()
