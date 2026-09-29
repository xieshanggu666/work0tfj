"""配额管理：年度配额分配与履约清缴。

并发安全保证（与交易服务一致）：
- 账户/企业级键锁 + 行锁串行化余额变更；
- 余额原子条件 UPDATE，清缴并发执行不会超额扣减；
- 配额表 (company_id, year) 唯一约束兜底，并发分配只入账一次；
- 履约记录按企业+年度唯一；已足额清缴（无缺口）后重复提交直接返回，
  不重复履约；缺口状态下仅允许按剩余缺口补缴，且累计清缴不超过核查排放量；
- 幂等键支持：同一清缴请求重试返回首次结果；
- 全部余额、流水、履约记录在同一事务中提交，异常统一回滚。
"""

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.ledger import (
    InsufficientBalanceError,
    account_lock_key,
    apply_balance_delta,
    company_clear_key,
    is_duplicate_submit,
    lock_rows_for_update,
    locked_accounts,
    transactional,
)
from app.models.allowance import (
    AllowanceAccount,
    AllowanceTransaction,
    ComplianceRecord,
    Quota,
)
from app.services.calculation_service import annual_total


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


def _find_existing_clear(
    db: Session, company_id: int, year: int, idempotency_key: str | None
) -> ComplianceRecord | None:
    q = db.query(ComplianceRecord).filter(
        ComplianceRecord.company_id == company_id,
        ComplianceRecord.year == year,
    )
    if idempotency_key:
        q = q.filter(ComplianceRecord.idempotency_key == idempotency_key)
    return q.first()


def clear_emission(
    db: Session,
    company_id: int,
    year: int,
    deadline: str,
    idempotency_key: str | None = None,
) -> ComplianceRecord:
    """履约清缴：从配额账户划转与排放量等额的配额，缺口记为 deficit。

    状态机与重复提交处理：
    - 携带相同幂等键的重试：返回首次履约记录，不重复扣减；
    - 已达标（compliant，缺口为 0）：重复清缴直接返回，杜绝重复履约；
    - 缺口（deficit）状态：允许补缴，但本次最多扣减“剩余缺口”，
      累计清缴不超过核查排放量，账户余额不足时只扣可用部分并更新缺口；
    - 账户不存在视为余额为 0，不再写入 account_id=0 的脏流水。
    """
    # 先取企业年度键（可能需创建履约记录），再取账户键，顺序固定避免死锁
    account = (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )
    keys = [company_clear_key(company_id, year)]
    if account:
        keys.append(account_lock_key(account.id))

    with locked_accounts(keys):
        if idempotency_key:
            existing = _find_existing_clear(db, company_id, year, idempotency_key)
            if existing:
                return existing

        record = (
            db.query(ComplianceRecord)
            .filter(ComplianceRecord.company_id == company_id, ComplianceRecord.year == year)
            .first()
        )

        try:
            with transactional(db):
                emission = annual_total(db, company_id, year)
                already_cleared = round(float(record.cleared_amount), 4) if record else 0.0

                # 已足额清缴：重复提交幂等返回，不再扣减、不产生重复流水
                if record and already_cleared >= emission and emission > 0:
                    return record

                # 补缴场景：本次最多扣减剩余缺口；首次清缴缺口为全部排放量
                remaining = round(emission - already_cleared, 4)
                if remaining <= 0:
                    if record:
                        return record
                    remaining = emission

                if account:
                    lock_rows_for_update(db, account.id)
                    balance_before = float(
                        db.get(AllowanceAccount, account.id).current_balance
                    )
                    deduct = round(min(balance_before, remaining), 4)
                else:
                    deduct = 0.0

                if account and deduct > 0:
                    # 原子条件 UPDATE：并发清缴/交易下余额不会被扣成负数
                    balance_after = apply_balance_delta(db, account.id, -deduct)
                    db.add(
                        AllowanceTransaction(
                            account_id=account.id,
                            company_id=company_id,
                            tx_type="clear",
                            amount=deduct,
                            counterparty="履约清缴",
                            price=None,
                            tx_date=deadline,
                            balance_after=balance_after,
                            remark=f"{year}年度履约清缴 {deduct} 吨配额",
                            idempotency_key=idempotency_key,
                        )
                    )
                else:
                    balance_after = float(account.current_balance) if account else 0.0

                cleared = round(already_cleared + deduct, 4)
                deficit = round(emission - cleared, 4)

                if record is None:
                    record = ComplianceRecord(
                        company_id=company_id,
                        year=year,
                        deadline=deadline,
                        idempotency_key=idempotency_key,
                    )
                    db.add(record)
                elif idempotency_key and not record.idempotency_key:
                    record.idempotency_key = idempotency_key
                if deadline:
                    record.deadline = deadline

                record.verified_emission = emission
                record.cleared_amount = cleared
                record.deficit = deficit
                record.status = "compliant" if deficit <= 0 else "deficit"
                record.cleared_at = datetime.utcnow()
                db.flush()
                db.refresh(record)
                if account:
                    db.refresh(account)
        except InsufficientBalanceError:
            # 键锁之后仍被并发改动的极端情况：原子更新兜底拒绝，事务已回滚
            raise ValueError("配额余额不足，清缴失败，请重试")
        except Exception as exc:
            # 与并发首笔清缴撞幂等键：回滚并返回首笔记录
            if idempotency_key and is_duplicate_submit(exc):
                db.rollback()
                existing = _find_existing_clear(db, company_id, year, idempotency_key)
                if existing:
                    return existing
            raise
        return record
