"""仪表盘统计。"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.allowance import AllowanceAccount, ComplianceRecord, Quota
from app.models.company import Company
from app.models.emission import ActivityData, EmissionResult


def dashboard_stats(db: Session, year: int | None = None) -> dict:
    total_companies = db.query(Company).count()
    total_activity = db.query(ActivityData).count()
    total_results = db.query(EmissionResult).count()

    emission_total = (
        db.query(func.coalesce(func.sum(EmissionResult.emission_amount), 0)).scalar() or 0
    )
    quota_total = (
        db.query(func.coalesce(func.sum(Quota.total), 0)).scalar() or 0
    )
    cleared_total = (
        db.query(func.coalesce(func.sum(ComplianceRecord.cleared_amount), 0)).scalar() or 0
    )
    # 已冻结待结算配额：报告批准后锁定、尚未实际清缴的部分
    frozen_total = (
        db.query(func.coalesce(func.sum(ComplianceRecord.frozen_amount), 0)).scalar() or 0
    )
    outstanding_deficit = (
        db.query(func.coalesce(func.sum(ComplianceRecord.deficit), 0)).scalar() or 0
    )
    # 账户侧：总持仓、冻结、可用三者由流水实时推导，与履约侧冻结互为对账
    holding_total = (
        db.query(func.coalesce(func.sum(AllowanceAccount.current_balance), 0)).scalar() or 0
    )
    account_frozen_total = (
        db.query(func.coalesce(func.sum(AllowanceAccount.frozen_balance), 0)).scalar() or 0
    )

    status_rows = (
        db.query(ComplianceRecord.status, func.count(ComplianceRecord.id))
        .group_by(ComplianceRecord.status)
        .all()
    )
    counts = {"pending": 0, "frozen": 0, "compliant": 0, "deficit": 0}
    for status, cnt in status_rows:
        counts[status] = cnt

    return {
        "total_companies": total_companies,
        "total_activity": total_activity,
        "total_results": total_results,
        "emission_total": round(float(emission_total), 4),
        "quota_total": round(float(quota_total), 4),
        "cleared_total": round(float(cleared_total), 4),
        "frozen_total": round(float(frozen_total), 4),
        "outstanding_deficit": round(float(outstanding_deficit), 4),
        "holding_total": round(float(holding_total), 4),
        "account_frozen_total": round(float(account_frozen_total), 4),
        "available_total": round(float(holding_total) - float(account_frozen_total), 4),
        "compliance_counts": counts,
        "accounts": db.query(AllowanceAccount).count(),
    }
