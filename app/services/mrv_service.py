"""MRV 报告：年度监测报告生成、提交与核查批准。"""

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.report import MrvReport
from app.services.calculation_service import scope_totals


def generate_report(db: Session, company_id: int, year: int) -> MrvReport:
    """汇总年度核算结果生成 MRV 报告（已存在则重建为草稿）。"""
    totals = scope_totals(db, company_id, year)
    detail = {
        "scope1": totals["1"],
        "scope2": totals["2"],
        "scope3": totals["3"],
        "total": round(totals["1"] + totals["2"] + totals["3"], 4),
    }
    report = db.query(MrvReport).filter(MrvReport.company_id == company_id, MrvReport.year == year).first()
    if not report:
        report = MrvReport(company_id=company_id, year=year)
        db.add(report)
    report.scope1 = detail["scope1"]
    report.scope2 = detail["scope2"]
    report.scope3 = detail["scope3"]
    report.total_emission = detail["total"]
    report.report_json = json.dumps(detail, ensure_ascii=False)
    report.status = "draft"
    report.generated_at = datetime.utcnow()
    db.commit()
    db.refresh(report)
    return report


def submit_report(db: Session, report: MrvReport) -> MrvReport:
    """企业提交报告待核查。"""
    if report.status != "draft":
        raise ValueError("仅草稿状态的报告可提交")
    report.status = "submitted"
    db.commit()
    db.refresh(report)
    return report


def approve_report(db: Session, report: MrvReport, verifier_id: int) -> MrvReport:
    """核查员批准报告。"""
    if report.status != "submitted":
        raise ValueError("仅已提交的报告可批准")
    report.status = "approved"
    report.approved_by = verifier_id
    report.approved_at = datetime.utcnow()
    db.commit()
    db.refresh(report)
    return report
