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
    compliant = db.query(ComplianceRecord).filter(ComplianceRecord.status == "compliant").count()
    deficit = db.query(ComplianceRecord).filter(ComplianceRecord.status == "deficit").count()

    return {
        "total_companies": total_companies,
        "total_activity": total_activity,
        "total_results": total_results,
        "emission_total": round(float(emission_total), 4),
        "quota_total": round(float(quota_total), 4),
        "cleared_total": round(float(cleared_total), 4),
        "compliance_counts": {"compliant": compliant, "deficit": deficit},
        "accounts": db.query(AllowanceAccount).count(),
    }
