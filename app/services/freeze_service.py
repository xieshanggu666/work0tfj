"""履约冻结闭环：报告批准冻结 → 清缴结算 / 缺口补缴 → 冲正回滚。

与配额分配（quota_service）、交易（trading_service）共同构成跨模块闭环：

1. 冻结（``freeze_on_approval``）：MRV 报告被核查员批准后，以**报告锁定的
   核查排放量**为履约依据，把账户可用配额按“排放量 - 已清缴 - 已冻结”冻结，
   写入冻结记录与 freeze 流水，履约记录置 frozen（足额）/ deficit（不足）。
2. 结算（``settle_clearance``）：管理员执行清缴时，已冻结额度经
   current/frozen 同减的原子结算转为已清缴（settlement 流水）；
   缺口年度企业买入配额后再次清缴，会先补冻再即结（freeze + settlement
   成对流水），始终按剩余缺口结算、累计不超过核查排放量；
   报告尚未批准时沿用直接扣减可用余额的 clear 流水（向后兼容）。
3. 冲正（``reverse_approval``）：已批准报告被冲正回 pending 时，
   未结算冻结全部解除退回（unfreeze），已结算清缴的配额退回账户（reverse），
   冻结记录置 reversed、履约记录回退 pending，余额/冻结/流水/统计全程可溯源。

所有操作均在“企业冻结键/清缴键 + 账户键”内、单事务完成，异常统一回滚。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.ledger import (
    InsufficientBalanceError,
    InsufficientFreezeError,
    account_lock_key,
    apply_balance_delta,
    apply_freeze,
    apply_settlement,
    apply_unfreeze,
    company_clear_key,
    company_freeze_key,
    is_duplicate_submit,
    lock_rows_for_update,
    locked_accounts,
    transactional,
)
from app.models.allowance import (
    AllowanceAccount,
    AllowanceFreeze,
    AllowanceTransaction,
    ComplianceRecord,
)
from app.models.report import MrvReport
from app.services.calculation_service import annual_total


# ---------------------------------------------------------------- 内部工具

def _find_account(db: Session, company_id: int, year: int) -> AllowanceAccount | None:
    return (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )


def _add_tx(
    db: Session,
    *,
    account: AllowanceAccount,
    tx_type: str,
    amount: float,
    balance_after: float,
    frozen_after: float,
    remark: str,
    tx_date: str = "",
    counterparty: str = "履约清算",
    freeze_id: int | None = None,
    idempotency_key: str | None = None,
) -> AllowanceTransaction:
    """登记一笔履约链路流水（freeze/settlement/unfreeze/reverse/clear）。"""
    tx = AllowanceTransaction(
        account_id=account.id,
        company_id=account.company_id,
        tx_type=tx_type,
        amount=round(amount, 4),
        counterparty=counterparty,
        price=None,
        tx_date=tx_date,
        balance_after=round(balance_after, 4),
        frozen_after=round(frozen_after, 4),
        remark=remark,
        freeze_id=freeze_id,
        idempotency_key=idempotency_key,
    )
    db.add(tx)
    return tx


def _sync_record(
    record: ComplianceRecord,
    *,
    emission: float,
    cleared: float,
    frozen: float,
    fully_approved: bool,
) -> None:
    """按“已清缴 + 已冻结”与核查排放量的关系回写履约记录状态。

    - 已清缴达标：compliant；
    - 已批准报告且缺口已全部被冻结覆盖：frozen（待结算）；
    - 仍有未冻结、未清缴缺口：deficit；
    - 未批准且无任何动作：pending。
    """
    record.verified_emission = round(emission, 4)
    record.cleared_amount = round(cleared, 4)
    record.frozen_amount = round(frozen, 4)
    deficit = round(emission - cleared - frozen, 4)
    deficit = max(deficit, 0.0)
    record.deficit = deficit
    if cleared >= emission and emission > 0:
        record.status = "compliant"
        record.cleared_at = datetime.utcnow()
    elif frozen > 0 and deficit <= 0:
        record.status = "frozen"
    elif deficit > 0:
        record.status = "deficit"
    else:
        # 无缺口且无冻结：已批准视为达标，未批准为待处理
        record.status = "compliant" if fully_approved and emission >= 0 else "pending"


def _active_freezes(db: Session, company_id: int, year: int) -> list[AllowanceFreeze]:
    return (
        db.query(AllowanceFreeze)
        .filter(
            AllowanceFreeze.company_id == company_id,
            AllowanceFreeze.year == year,
            AllowanceFreeze.status == "frozen",
        )
        .order_by(AllowanceFreeze.id.asc())
        .all()
    )


# ---------------------------------------------------------------- 报告批准冻结

def freeze_on_approval(db: Session, report: MrvReport, verifier_id: int) -> ComplianceRecord:
    """批准报告时调用：翻转状态、按核查排放量冻结配额并回写履约记录（单事务）。

    状态校验放在企业冻结键锁内、并以数据库最新状态为准：并发重复批准时，
    首个成功者提交后，其余请求在锁内重新读取会看到 approved 而被拒绝，
    不会产生第二笔冻结。状态翻转与冻结任一失败整体回滚（报告不会停留在
    “已批准但未冻结”的半成品状态）。
    """
    company_id, year = report.company_id, report.year
    account = _find_account(db, company_id, year)
    keys = [company_freeze_key(company_id, year)]
    if account:
        keys.append(account_lock_key(account.id))

    with locked_accounts(keys):
        try:
            with transactional(db):
                # 锁内重读最新状态，杜绝并发批准重复冻结（传入对象可能是旧快照）
                db.expire(report)
                db.refresh(report)
                if report.status != "submitted":
                    raise ValueError("仅已提交的报告可批准")

                emission = round(float(report.total_emission), 4)
                report.status = "approved"
                report.approved_by = verifier_id
                report.approved_at = datetime.utcnow()

                record = (
                    db.query(ComplianceRecord)
                    .filter(
                        ComplianceRecord.company_id == company_id,
                        ComplianceRecord.year == year,
                    )
                    .first()
                )
                if record is None:
                    record = ComplianceRecord(company_id=company_id, year=year)
                    db.add(record)
                    db.flush()  # 使列默认值（0）生效，后续读取不会拿到 None

                already_cleared = round(float(record.cleared_amount), 4)
                already_frozen = round(float(record.frozen_amount), 4)
                need_freeze = round(emission - already_cleared - already_frozen, 4)

                frozen_now = 0.0
                frozen_after_total = already_frozen
                if account is not None and need_freeze > 0:
                    lock_rows_for_update(db, account.id)
                    available = account.available_balance
                    frozen_now = round(min(available, need_freeze), 4)
                    if frozen_now > 0:
                        frozen_after_total = apply_freeze(db, account.id, frozen_now)

                # 批准前可能已通过直接清缴（clear 流水，无冻结）缴过部分配额。
                # 将其纳入冻结记录视同已结算：冲正时随已结算部分统一退回，
                # 避免“先清缴 → 后批准 → 再冲正”时漏退已清缴配额。
                freeze_status = "frozen" if frozen_now > 0 else "settled"
                freeze_rec = AllowanceFreeze(
                    company_id=company_id,
                    year=year,
                    account_id=account.id if account else None,
                    report_id=report.id,
                    approved_amount=max(need_freeze, 0.0),
                    frozen_amount=frozen_now,
                    settled_amount=already_cleared,
                    reversed_amount=0,
                    status=freeze_status,
                    reason=f"MRV 报告 #{report.id} 批准冻结",
                )
                if already_cleared > 0 and frozen_now <= 0:
                    freeze_rec.settled_at = datetime.utcnow()
                db.add(freeze_rec)
                db.flush()

                if account is not None and frozen_now > 0:
                    _add_tx(
                        db,
                        account=account,
                        tx_type="freeze",
                        amount=frozen_now,
                        # 冻结不动总余额，仅增加冻结额；快照双写便于对账
                        balance_after=float(account.current_balance),
                        frozen_after=frozen_after_total,
                        remark=f"{year} 年度报告批准履约冻结 {frozen_now} 吨",
                        tx_date=datetime.utcnow().strftime("%Y-%m-%d"),
                        freeze_id=freeze_rec.id,
                    )

                record.approved_report_id = report.id
                if not record.deadline:
                    record.deadline = f"{year}-12-31"
                _sync_record(
                    record,
                    emission=emission,
                    cleared=already_cleared,
                    frozen=already_frozen + frozen_now,
                    fully_approved=True,
                )
                db.flush()
                db.refresh(report)
                db.refresh(record)
        except InsufficientFreezeError:
            raise ValueError("可冻结配额不足，报告批准失败")
        return record


# ---------------------------------------------------------------- 清缴结算

def _settle_existing_freezes(
    db: Session,
    account: AllowanceAccount,
    freezes: list[AllowanceFreeze],
    remaining: float,
    year: int,
    tx_date: str,
) -> tuple[float, float]:
    """把已冻结额度按冻结记录先后结算，返回 (本次结算量, 结算后剩余待清缴)。"""
    settled_total = 0.0
    for fz in freezes:
        if remaining <= 0:
            break
        part = round(min(float(fz.frozen_amount), remaining), 4)
        if part <= 0:
            continue
        balance_after, frozen_after = apply_settlement(db, account.id, part)
        fz.frozen_amount = round(float(fz.frozen_amount) - part, 4)
        fz.settled_amount = round(float(fz.settled_amount) + part, 4)
        if float(fz.frozen_amount) <= 0:
            fz.status = "settled"
            fz.settled_at = datetime.utcnow()
        _add_tx(
            db,
            account=account,
            tx_type="settlement",
            amount=part,
            balance_after=balance_after,
            frozen_after=frozen_after,
            remark=f"{year} 年度冻结配额结算清缴 {part} 吨",
            tx_date=tx_date,
            freeze_id=fz.id,
        )
        settled_total = round(settled_total + part, 4)
        remaining = round(remaining - part, 4)
    return settled_total, remaining


def _supplement_and_settle(
    db: Session,
    account: AllowanceAccount,
    company_id: int,
    year: int,
    report_id: int,
    remaining: float,
    tx_date: str,
) -> float:
    """缺口补缴：批准报告尚有缺口时，把可用余额补冻后立即结算。

    freeze 与 settlement 成对登记流水，补冻量受可用余额与剩余缺口双重约束。
    返回本次补缴结算量。
    """
    if remaining <= 0:
        return 0.0
    available = account.available_balance
    add = round(min(available, remaining), 4)
    if add <= 0:
        return 0.0

    fz = (
        db.query(AllowanceFreeze)
        .filter(
            AllowanceFreeze.company_id == company_id,
            AllowanceFreeze.year == year,
            AllowanceFreeze.report_id == report_id,
            AllowanceFreeze.status == "frozen",
        )
        .first()
    )
    if fz is None:
        fz = AllowanceFreeze(
            company_id=company_id,
            year=year,
            account_id=account.id,
            report_id=report_id,
            approved_amount=remaining,
            frozen_amount=0,
            settled_amount=0,
            reversed_amount=0,
            status="frozen",
            reason=f"MRV 报告 #{report_id} 缺口补缴冻结",
        )
        db.add(fz)
        db.flush()
    if fz.account_id is None:
        fz.account_id = account.id

    frozen_after = apply_freeze(db, account.id, add)
    fz.frozen_amount = round(float(fz.frozen_amount) + add, 4)
    _add_tx(
        db,
        account=account,
        tx_type="freeze",
        amount=add,
        balance_after=float(account.current_balance),
        frozen_after=frozen_after,
        remark=f"{year} 年度缺口补缴冻结 {add} 吨",
        tx_date=tx_date,
        freeze_id=fz.id,
    )

    balance_after, frozen_after = apply_settlement(db, account.id, add)
    fz.frozen_amount = round(float(fz.frozen_amount) - add, 4)
    fz.settled_amount = round(float(fz.settled_amount) + add, 4)
    if float(fz.frozen_amount) <= 0:
        fz.status = "settled"
        fz.settled_at = datetime.utcnow()
    _add_tx(
        db,
        account=account,
        tx_type="settlement",
        amount=add,
        balance_after=balance_after,
        frozen_after=frozen_after,
        remark=f"{year} 年度缺口补缴结算 {add} 吨",
        tx_date=tx_date,
        freeze_id=fz.id,
    )
    return add


def settle_clearance(
    db: Session,
    company_id: int,
    year: int,
    deadline: str,
    idempotency_key: str | None = None,
) -> ComplianceRecord:
    """履约清缴：冻结结算优先，缺口补冻即结，未批准则直接扣减可用余额。

    重复提交语义与历史版本一致：相同幂等键 / 已 compliant 返回首次记录，
    累计清缴不超过核查排放量；余额、冻结、流水、履约记录同事务提交。
    """
    account = _find_account(db, company_id, year)
    keys = [company_clear_key(company_id, year), company_freeze_key(company_id, year)]
    if account:
        keys.append(account_lock_key(account.id))

    with locked_accounts(keys):
        if idempotency_key:
            existing = (
                db.query(ComplianceRecord)
                .filter(
                    ComplianceRecord.company_id == company_id,
                    ComplianceRecord.year == year,
                    ComplianceRecord.idempotency_key == idempotency_key,
                )
                .first()
            )
            if existing:
                return existing

        record = (
            db.query(ComplianceRecord)
            .filter(ComplianceRecord.company_id == company_id, ComplianceRecord.year == year)
            .first()
        )

        try:
            with transactional(db):
                # 核查排放量：已批准报告锁定后以履约记录为准（重新核算不影响，要改先冲正）；
                # 尚未批准过报告时实时取核算结果（兼容直接清缴）
                approved_report_id = record.approved_report_id if record else None
                if record is not None and float(record.verified_emission) > 0 and approved_report_id:
                    emission = round(float(record.verified_emission), 4)
                else:
                    emission = annual_total(db, company_id, year)

                already_cleared = round(float(record.cleared_amount), 4) if record else 0.0
                already_frozen = round(float(record.frozen_amount), 4) if record else 0.0

                if record is None:
                    record = ComplianceRecord(
                        company_id=company_id,
                        year=year,
                        deadline=deadline,
                        idempotency_key=idempotency_key,
                    )
                    db.add(record)
                    db.flush()  # 使列默认值（0）生效，后续读取不会拿到 None
                elif idempotency_key and not record.idempotency_key:
                    record.idempotency_key = idempotency_key
                if deadline:
                    record.deadline = deadline

                # 已足额清缴：幂等返回，不产生任何流水
                if already_cleared >= emission and emission > 0:
                    db.flush()
                    db.refresh(record)
                    return record

                cleared_now = 0.0
                # 履约记录上的冻结余量；初始冻结结算后也必须同步核减，
                # 否则缺口补缴阶段“记录冻结 - 补冻”会出现负冻结
                frozen_now = already_frozen

                if account:
                    lock_rows_for_update(db, account.id)
                    remaining = round(emission - already_cleared, 4)

                    # 1) 已冻结额度优先结算
                    active = _active_freezes(db, company_id, year)
                    settled, remaining = _settle_existing_freezes(
                        db, account, active, remaining, year, deadline
                    )
                    cleared_now = round(cleared_now + settled, 4)
                    frozen_now = round(frozen_now - settled, 4)

                    # 2) 仍有缺口：已批准报告 → 补冻即结（冻结与结算等量，
                    #    记录冻结余量不变）；未批准 → 直接扣减可用余额
                    if remaining > 0:
                        if approved_report_id:
                            extra = _supplement_and_settle(
                                db, account, company_id, year, approved_report_id,
                                remaining, deadline,
                            )
                            cleared_now = round(cleared_now + extra, 4)
                        else:
                            direct = round(min(account.available_balance, remaining), 4)
                            if direct > 0:
                                balance_after = apply_balance_delta(db, account.id, -direct)
                                _add_tx(
                                    db,
                                    account=account,
                                    tx_type="clear",
                                    amount=direct,
                                    balance_after=balance_after,
                                    frozen_after=float(account.frozen_balance),
                                    remark=f"{year} 年度履约清缴 {direct} 吨配额",
                                    tx_date=deadline,
                                    idempotency_key=idempotency_key,
                                )
                                cleared_now = round(cleared_now + direct, 4)

                cleared = round(already_cleared + cleared_now, 4)
                _sync_record(
                    record,
                    emission=emission,
                    cleared=cleared,
                    frozen=frozen_now,
                    fully_approved=bool(approved_report_id),
                )
                db.flush()
                db.refresh(record)
                if account:
                    db.refresh(account)
        except InsufficientBalanceError:
            raise ValueError("配额余额不足，清缴失败，请重试")
        except InsufficientFreezeError as exc:
            raise ValueError(f"冻结结算失败：{exc}")
        except Exception as exc:
            if idempotency_key and is_duplicate_submit(exc):
                db.rollback()
                existing = (
                    db.query(ComplianceRecord)
                    .filter(
                        ComplianceRecord.company_id == company_id,
                        ComplianceRecord.year == year,
                        ComplianceRecord.idempotency_key == idempotency_key,
                    )
                    .first()
                )
                if existing:
                    return existing
            raise
        return record


# ---------------------------------------------------------------- 报告冲正回滚

def reverse_approval(
    db: Session,
    report: MrvReport,
    reason: str,
    user_id: int,
) -> MrvReport:
    """冲正已批准报告：解冻未结算冻结、退回已结算配额、履约记录回退 pending。

    - 冻结未清缴部分：frozen_balance 减少、current_balance 不变（unfreeze 流水）；
    - 已结算清缴部分：配额退回账户可用余额（reverse 流水），清缴累计清零；
    - 冻结记录置 reversed，履约记录回 pending 并解除与批准报告的锁定关系；
    - 报告回到 pending（可修订数据、重新核算后再次提交批准，version 递增）。
    全部在同一事务，任一步失败整体回滚。
    """
    if not reason or not reason.strip():
        raise ValueError("冲正必须填写原因")

    company_id, year = report.company_id, report.year
    account = _find_account(db, company_id, year)
    keys = [company_freeze_key(company_id, year)]
    if account:
        keys.append(account_lock_key(account.id))

    with locked_accounts(keys):
        with transactional(db):
            # 锁内重读，杜绝并发重复冲正导致二次退款
            db.expire(report)
            db.refresh(report)
            if report.status != "approved":
                raise ValueError("仅已批准的报告可冲正")

            # 该报告下全部冻结记录（含批准冻结与缺口补缴，状态可能为 frozen/settled），
            # 按记录分别解除剩余冻结、退回已结算额度
            freezes = (
                db.query(AllowanceFreeze)
                .filter(
                    AllowanceFreeze.company_id == company_id,
                    AllowanceFreeze.year == year,
                    AllowanceFreeze.report_id == report.id,
                )
                .order_by(AllowanceFreeze.id.asc())
                .all()
            )

            total_unfrozen = 0.0
            total_reversed = 0.0
            tx_date = datetime.utcnow().strftime("%Y-%m-%d")
            for fz in freezes:
                # 已冲正过的记录跳过：重复冲正不得二次退回配额
                if fz.status == "reversed":
                    continue
                still_frozen = round(float(fz.frozen_amount), 4)
                settled = round(float(fz.settled_amount), 4)

                if account is not None and still_frozen > 0:
                    frozen_after = apply_unfreeze(db, account.id, still_frozen)
                    _add_tx(
                        db,
                        account=account,
                        tx_type="unfreeze",
                        amount=still_frozen,
                        balance_after=float(account.current_balance),
                        frozen_after=frozen_after,
                        remark=f"报告 #{report.id} 冲正，解除冻结 {still_frozen} 吨",
                        tx_date=tx_date,
                        freeze_id=fz.id,
                    )
                    total_unfrozen = round(total_unfrozen + still_frozen, 4)

                if account is not None and settled > 0:
                    balance_after = apply_balance_delta(db, account.id, settled)
                    _add_tx(
                        db,
                        account=account,
                        tx_type="reverse",
                        amount=settled,
                        balance_after=balance_after,
                        frozen_after=float(account.frozen_balance),
                        remark=f"报告 #{report.id} 冲正，退回已清缴配额 {settled} 吨",
                        tx_date=tx_date,
                        freeze_id=fz.id,
                    )
                    total_reversed = round(total_reversed + settled, 4)

                fz.frozen_amount = 0
                fz.reversed_amount = round(
                    float(fz.reversed_amount) + still_frozen + settled, 4
                )
                fz.status = "reversed"
                fz.reversed_at = datetime.utcnow()

            record = (
                db.query(ComplianceRecord)
                .filter(
                    ComplianceRecord.company_id == company_id,
                    ComplianceRecord.year == year,
                )
                .first()
            )
            if record is not None and record.approved_report_id == report.id:
                # 回退履约状态：已清缴/已冻结清零（配额已退回），等待修订后重新批准
                record.approved_report_id = None
                record.cleared_amount = 0
                record.frozen_amount = 0
                record.deficit = 0
                record.status = "pending"
                record.cleared_at = None
                record.idempotency_key = None

            report.status = "pending"
            report.reverse_reason = reason.strip()[:256]
            report.reversed_by = user_id
            report.reversed_at = datetime.utcnow()
            report.version += 1
            report.approved_by = None
            report.approved_at = None
            db.flush()
            db.refresh(report)
    return report
