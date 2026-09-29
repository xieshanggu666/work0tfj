"""MRV 报告：年度监测报告生成、提交、核查批准（触发履约冻结）与冲正回滚。

跨模块闭环：
- approve：状态翻转与配额冻结在同一事务（freeze_service.freeze_on_approval），
  报告已批准即代表核查排放量锁定、对应配额已冻结，任一失败整体回滚；
- reverse：冲正后冻结解除/清缴退回、履约状态回滚，配额流水完整可溯；
- 已提交/已批准的报告不得直接重新生成，防止绕过核查与冻结链路。
"""

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.report import MrvReport
from app.services.calculation_service import scope_totals
from app.services.freeze_service import freeze_on_approval, reverse_approval
# 允许重新生成（重算数据）的状态：草稿与冲正退回；已提交需核查结果、已批准需先冲正
_REGENERABLE_STATES = {"draft", "pending"}


def _get_or_create(db: Session, company_id: int, year: int) -> MrvReport:
    report = db.query(MrvReport).filter(MrvReport.company_id == company_id, MrvReport.year == year).first()
    if not report:
        # 显式赋初值：列默认值仅在 INSERT 时应用，新建未 flush 对象读取为 None
        report = MrvReport(company_id=company_id, year=year, status="draft")
        db.add(report)
    return report


def generate_report(db: Session, company_id: int, year: int) -> MrvReport:
    """汇总年度核算结果生成/重建 MRV 报告。

    已提交待核查或已批准（已冻结）的报告不可重建：已批准必须先冲正，
    避免报告数据与已冻结的履约依据不一致。
    """
    report = _get_or_create(db, company_id, year)
    if report.status not in _REGENERABLE_STATES:
        if report.status == "approved":
            raise ValueError("报告已批准并冻结配额，请先冲正后再重新生成")
        raise ValueError("报告已提交待核查，暂不可重新生成")

    totals = scope_totals(db, company_id, year)
    detail = {
        "scope1": totals["1"],
        "scope2": totals["2"],
        "scope3": totals["3"],
        "total": round(totals["1"] + totals["2"] + totals["3"], 4),
    }
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
    """企业提交报告待核查（草稿或冲正退回状态均可提交）。"""
    if report.status not in ("draft", "pending"):
        raise ValueError("仅草稿或冲正退回状态的报告可提交")
    report.status = "submitted"
    db.commit()
    db.refresh(report)
    return report


def approve_report(db: Session, report: MrvReport, verifier_id: int):
    """核查员批准报告：状态翻转与履约冻结同一事务，返回 (报告, 履约记录)。

    状态校验、状态翻转、冻结、流水、履约记录全部在 freeze_on_approval
    的锁内事务完成；并发重复批准或冻结失败均整体回滚。
    """
    record = freeze_on_approval(db, report, verifier_id)
    db.refresh(report)
    return report, record


def reverse_report(db: Session, report: MrvReport, reason: str, user_id: int) -> MrvReport:
    """冲正已批准报告：解冻/退回配额并回退履约状态（详见 freeze_service）。"""
    return reverse_approval(db, report, reason, user_id)
