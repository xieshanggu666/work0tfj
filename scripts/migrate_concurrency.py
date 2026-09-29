"""为已存在的数据库补齐并发/幂等/履约冻结改造所需的列、表与唯一约束。

用法：python scripts/migrate_concurrency.py

并发/幂等（首次迁移）：
- 新增 allowance_transactions.idempotency_key（同账户唯一）
- 新增 compliance_records.idempotency_key
- 新增 quotas / compliance_records 的 (company_id, year) 唯一约束

履约冻结闭环（报告批准冻结/结算/冲正）：
- 新建 allowance_freezes 冻结记录表
- allowance_transactions 新增 frozen_after、freeze_id
- compliance_records 新增 frozen_amount、approved_report_id
- mrv_reports 新增 reverse_reason/reversed_by/reversed_at/version

幂等：列/索引/表已存在时跳过；全新部署可直接用 init_db.py，无需执行本脚本。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402

from app.core.database import Base, engine  # noqa: E402
# 确保全部模型（含 AllowanceFreeze）已注册到 metadata，create_all 才能建新表
import app.models  # noqa: F401,E402


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
        if not _has_column(inspector, "allowance_transactions", "frozen_after"):
            statements.append(
                "ALTER TABLE allowance_transactions ADD COLUMN frozen_after NUMERIC(18, 4) DEFAULT 0"
            )
        if not _has_column(inspector, "allowance_transactions", "freeze_id"):
            statements.append(
                "ALTER TABLE allowance_transactions ADD COLUMN freeze_id INTEGER"
            )
        if not _has_index(inspector, "uq_tx_account_idem"):
            # SQLite 中 NULL 不参与唯一索引，未携带幂等键的历史/新请求不受影响
            statements.append(
                "CREATE UNIQUE INDEX uq_tx_account_idem "
                "ON allowance_transactions (account_id, idempotency_key)"
            )
        if not _has_index(inspector, "ix_allowance_transactions_freeze_id"):
            statements.append(
                "CREATE INDEX ix_allowance_transactions_freeze_id "
                "ON allowance_transactions (freeze_id)"
            )

    if "compliance_records" in inspector.get_table_names():
        if not _has_column(inspector, "compliance_records", "idempotency_key"):
            statements.append(
                "ALTER TABLE compliance_records ADD COLUMN idempotency_key VARCHAR(64)"
            )
        if not _has_column(inspector, "compliance_records", "frozen_amount"):
            statements.append(
                "ALTER TABLE compliance_records ADD COLUMN frozen_amount NUMERIC(18, 4) DEFAULT 0"
            )
        if not _has_column(inspector, "compliance_records", "approved_report_id"):
            statements.append(
                "ALTER TABLE compliance_records ADD COLUMN approved_report_id INTEGER"
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

    if "mrv_reports" in inspector.get_table_names():
        if not _has_column(inspector, "mrv_reports", "reverse_reason"):
            statements.append(
                "ALTER TABLE mrv_reports ADD COLUMN reverse_reason VARCHAR(256)"
            )
        if not _has_column(inspector, "mrv_reports", "reversed_by"):
            statements.append(
                "ALTER TABLE mrv_reports ADD COLUMN reversed_by INTEGER"
            )
        if not _has_column(inspector, "mrv_reports", "reversed_at"):
            statements.append(
                "ALTER TABLE mrv_reports ADD COLUMN reversed_at DATETIME"
            )
        if not _has_column(inspector, "mrv_reports", "version"):
            statements.append(
                "ALTER TABLE mrv_reports ADD COLUMN version INTEGER DEFAULT 1"
            )

    # 冻结记录表：全新建表（create_all 仅创建 metadata 中缺失的表，不影响既有表）
    Base.metadata.create_all(engine, tables=[app.models.AllowanceFreeze.__table__])

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
