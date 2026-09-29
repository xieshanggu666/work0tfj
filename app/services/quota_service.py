"""配额管理：年度配额分配与履约清缴。

并发安全保证（与交易服务一致）：
- 账户/企业级键锁 + 行锁串行化余额变更；
- 余额原子条件 UPDATE，清缴并发执行不会超额扣减；
- 配额表 (company_id, year) 唯一约束兜底，并发分配只入账一次；
- 履约记录按企业+年度唯一；已足额清缴（无缺口）后重复提交直接返回，
  不重复履约；缺口状态下仅允许按剩余缺口补缴，且累计清缴不超过核查排放量；
- 报告批准后的冻结结算 / 缺口补冻即结 / 冲正回滚逻辑统一由
  freeze_service.settle_clearance 承担，本模块仅做兼容转发；
- 幂等键支持：同一清缴请求重试返回首次结果；
- 全部余额、冻结、流水、履约记录在同一事务中提交，异常统一回滚。
"""

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.ledger import (
    account_lock_key,
    apply_balance_delta,
    is_duplicate_submit,
    locked_accounts,
    transactional,
)
from app.models.allowance import (
    AllowanceAccount,
    AllowanceTransaction,
    ComplianceRecord,
    Quota,
)
from app.services.freeze_service import settle_clearance


def allocate_quota(
    db: Session,
    company_id: int,
    year: int,
    baseline: float,
    allocation_amount: float,
    adjustment: float = 0.0,
) -> Quota:
    """免费配额分配：写入配额、初始化账户、登记划入流水。

    同一企业同一年度重复分配返回已有配额，不重复入账；
    并发提交由唯一约束 + 账户锁保证只有一笔生效。
    """
    account = (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )
    lock_key = account_lock_key(account.id) if account else f"quota:{company_id}:{year}"

    with locked_accounts([lock_key]):
        existing = (
            db.query(Quota)
            .filter(Quota.company_id == company_id, Quota.year == year)
            .first()
        )
        if existing:
            return existing

        total = round(allocation_amount + adjustment, 4)
        try:
            with transactional(db):
                quota = Quota(
                    company_id=company_id,
                    year=year,
                    baseline=round(baseline, 4),
                    allocation_amount=round(allocation_amount, 4),
                    adjustment=round(adjustment, 4),
                    total=total,
                    status="allocated",
                    allocated_at=datetime.utcnow(),
                )
                db.add(quota)

                if account:
                    # 已存在账户：原子加记配额与期初值
                    balance_after = apply_balance_delta(db, account.id, total)
                    db.execute(
                        update(AllowanceAccount)
                        .where(AllowanceAccount.id == account.id)
                        .values(opening_balance=AllowanceAccount.opening_balance + total)
                        .execution_options(synchronize_session=False)
                    )
                    db.flush()
                else:
                    account = AllowanceAccount(
                        company_id=company_id,
                        year=year,
                        opening_balance=total,
                        current_balance=total,
                        frozen_balance=0,
                    )
                    db.add(account)
                    db.flush()
                    balance_after = total

                db.add(
                    AllowanceTransaction(
                        account_id=account.id,
                        company_id=company_id,
                        tx_type="allocation",
                        amount=total,
                        counterparty="主管部门",
                        price=None,
                        tx_date=datetime.utcnow().strftime("%Y-%m-%d"),
                        balance_after=balance_after,
                        frozen_after=float(account.frozen_balance) if account else 0.0,
                        remark=f"{year}年度免费配额分配",
                    )
                )
                db.flush()
                db.refresh(quota)
        except IntegrityError as exc:
            # 并发分配竞态：另一请求已插入同年配额，回滚后返回已有记录
            if is_duplicate_submit(exc, "uq_quota_company_year"):
                db.rollback()
                return (
                    db.query(Quota)
                    .filter(Quota.company_id == company_id, Quota.year == year)
                    .one()
                )
            raise
        return quota


def clear_emission(
    db: Session,
    company_id: int,
    year: int,
    deadline: str,
    idempotency_key: str | None = None,
) -> ComplianceRecord:
    """履约清缴（兼容入口）：委托冻结闭环服务统一结算。

    - 报告未批准：直接扣减可用余额（clear 流水），不足部分记 deficit；
    - 报告已批准：先结算已冻结额度（settlement 流水），缺口年度买入后
      再次清缴自动“补冻即结”，累计不超过核查排放量。
    详见 app.services.freeze_service.settle_clearance。
    """
    return settle_clearance(db, company_id, year, deadline, idempotency_key=idempotency_key)
