"""配额账本并发控制原语：账户锁定、原子余额更新与事务回滚。

设计目标（并发清缴 / 交易时保证余额与流水一致）：
1. 账户锁定：同一账户（或同一企业清缴/冻结键）的余额变更在进程内串行化，
   消除“读余额 → 判断 → 写余额”的竞态；跨账户操作按锁名排序获取，避免死锁；
   对支持行锁的数据库额外使用 SELECT ... FOR UPDATE。
2. 原子更新：
   - 交易扣减使用带 ``current_balance - frozen_balance >= amount`` 条件的
     单条 UPDATE，只能动用可用余额，已履约冻结的部分不会被卖出/划出占用；
   - 冻结使用 ``frozen_balance + amount <= current_balance`` 条件 UPDATE，
     冻结超额由数据库拒绝；结算同时扣减 current/frozen，条件 frozen_balance 充足；
   - 解冻仅减少 frozen_balance，current_balance 不变（额度退回可用）。
3. 事务边界：所有余额变更在单一事务中完成，异常统一 rollback，
   绝不出现“余额已扣、流水缺失”或“流水已写、余额未变”的半成品状态。

SQLite 不支持 SELECT ... FOR UPDATE，且默认写锁为库级锁，
因此进程内键锁是其主要的并发防线，原子条件 UPDATE 作为兜底；
切换到 PostgreSQL/MySQL 时键锁与行锁会同时生效。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Sequence

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.allowance import AllowanceAccount


class InsufficientBalanceError(ValueError):
    """可用配额不足，扣减被拒绝（事务已回滚，余额与流水均不变）。"""


class InsufficientFreezeError(ValueError):
    """可冻结配额不足，或冻结余额不足以结算/解除（事务已回滚）。"""


class DuplicateSubmitError(Exception):
    """幂等键冲突：同一业务请求已成功处理过（调用方应返回首次结果）。"""


_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def account_lock_key(account_id: int) -> str:
    return f"account:{account_id}"


def company_clear_key(company_id: int, year: int) -> str:
    """清缴键：企业 + 年度（覆盖该企业当年可能存在的全部账户变更）。"""
    return f"clear:{company_id}:{year}"


def company_freeze_key(company_id: int, year: int) -> str:
    """冻结键：企业 + 年度。

    报告批准冻结、冲正解冻、缺口补缴冻结共用此键串行化；
    与清缴键、账户键互不重名，业务流程中按固定顺序嵌套获取避免死锁。
    """
    return f"freeze:{company_id}:{year}"


def _get_lock(key: str) -> threading.RLock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


@contextmanager
def locked_accounts(keys: Sequence[str]) -> Iterator[None]:
    """按锁名排序后依次获取键锁，避免多账户操作时交叉等待形成死锁。

    使用可重入锁，清缴（先取企业键再取账户键）与嵌套调用安全。
    """
    acquired: list[threading.RLock] = []
    for key in sorted(set(keys)):
        lock = _get_lock(key)
        lock.acquire()
        acquired.append(lock)
    try:
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


@contextmanager
def transactional(db: Session) -> Iterator[None]:
    """统一事务边界：正常退出提交，任何异常回滚后原样抛出。

    业务层（分配/交易/清缴/冻结/冲正）统一通过此上下文提交，不再各自 commit，
    保证“余额 + 冻结 + 流水 + 业务记录”要么全部落库、要么全部撤销。
    """
    try:
        yield
        db.commit()
    except Exception:
        db.rollback()
        raise


def lock_rows_for_update(db: Session, account_id: int) -> None:
    """对支持行锁的数据库加 SELECT ... FOR UPDATE，并使本地缓存失效。

    加锁后使会话中该账户的属性过期，后续读取拿到的是锁内最新数据
    （避免 PostgreSQL 等数据库下 FOR UPDATE 与 ORM 快照不一致）。
    SQLite 不支持 FOR UPDATE，下为 no-op。
    """
    if db.bind is not None and db.bind.dialect.name != "sqlite":
        db.query(AllowanceAccount.id).filter(AllowanceAccount.id == account_id).with_for_update().first()
        db.expire_all()


def _reload_account(db: Session, account_id: int) -> AllowanceAccount:
    # UPDATE 绕开了 ORM 状态同步，必须使身份映射中的旧对象失效后再读，否则拿到的是旧余额
    db.expire_all()
    db.flush()
    return db.get(AllowanceAccount, account_id)


def _check_rowcount(result, error):
    if result.rowcount != 1:
        raise error


def apply_balance_delta(
    db: Session,
    account_id: int,
    delta: float,
) -> float:
    """对账户**可用余额**执行单条原子条件 UPDATE，返回更新后的总余额。

    - delta >= 0：入账，直接累加到 current_balance；
    - delta <  0：扣减，仅当 ``current_balance - frozen_balance >= amount``
      （可用余额充足）时 WHERE 条件成立，影响行数为 0 说明发生了并发超额
      扣减或动用了冻结额度，抛出 InsufficientBalanceError。
    结果由数据库计算，不依赖调用方先前读到的余额快照。
    """
    amount = round(abs(delta), 4)
    if delta < 0:
        stmt = (
            update(AllowanceAccount)
            .where(
                AllowanceAccount.id == account_id,
                AllowanceAccount.current_balance - AllowanceAccount.frozen_balance >= amount,
            )
            .values(current_balance=AllowanceAccount.current_balance - amount)
        )
    else:
        stmt = (
            update(AllowanceAccount)
            .where(AllowanceAccount.id == account_id)
            .values(current_balance=AllowanceAccount.current_balance + amount)
        )
    # 不做 ORM 内存态同步（Numeric 列是 Decimal，与 Python float 直接相减会报错），
    # 余额以数据库更新后重新读取的值为准
    result = db.execute(stmt.execution_options(synchronize_session=False))
    _check_rowcount(result, InsufficientBalanceError("可用配额余额不足"))
    refreshed = _reload_account(db, account_id)
    return round(float(refreshed.current_balance), 4)


def apply_freeze(db: Session, account_id: int, amount: float) -> float:
    """冻结可用配额：frozen_balance 增加，current_balance 不变。

    条件 ``current_balance - frozen_balance >= amount`` 保证不会超冻；
    返回更新后的冻结余额。
    """
    amount = round(amount, 4)
    stmt = (
        update(AllowanceAccount)
        .where(
            AllowanceAccount.id == account_id,
            AllowanceAccount.current_balance - AllowanceAccount.frozen_balance >= amount,
        )
        .values(frozen_balance=AllowanceAccount.frozen_balance + amount)
    )
    result = db.execute(stmt.execution_options(synchronize_session=False))
    _check_rowcount(result, InsufficientFreezeError("可冻结配额不足"))
    refreshed = _reload_account(db, account_id)
    return round(float(refreshed.frozen_balance), 4)


def apply_unfreeze(db: Session, account_id: int, amount: float) -> float:
    """解除冻结（冲正退回）：frozen_balance 减少，current_balance 不变。

    条件 ``frozen_balance >= amount`` 防止重复冲正/超解；
    返回更新后的冻结余额。
    """
    amount = round(amount, 4)
    stmt = (
        update(AllowanceAccount)
        .where(
            AllowanceAccount.id == account_id,
            AllowanceAccount.frozen_balance >= amount,
        )
        .values(frozen_balance=AllowanceAccount.frozen_balance - amount)
    )
    result = db.execute(stmt.execution_options(synchronize_session=False))
    _check_rowcount(result, InsufficientFreezeError("冻结配额不足，无法解除"))
    refreshed = _reload_account(db, account_id)
    return round(float(refreshed.frozen_balance), 4)


def apply_settlement(db: Session, account_id: int, amount: float) -> tuple[float, float]:
    """冻结配额结算清缴：current_balance 与 frozen_balance 同减。

    条件 ``frozen_balance >= amount`` 保证结算量不超过已冻结量；
    返回 (更新后的总余额, 更新后的冻结余额)。
    """
    amount = round(amount, 4)
    stmt = (
        update(AllowanceAccount)
        .where(
            AllowanceAccount.id == account_id,
            AllowanceAccount.frozen_balance >= amount,
        )
        .values(
            current_balance=AllowanceAccount.current_balance - amount,
            frozen_balance=AllowanceAccount.frozen_balance - amount,
        )
    )
    result = db.execute(stmt.execution_options(synchronize_session=False))
    _check_rowcount(result, InsufficientFreezeError("冻结配额不足，无法结算"))
    refreshed = _reload_account(db, account_id)
    return round(float(refreshed.current_balance), 4), round(float(refreshed.frozen_balance), 4)


def is_duplicate_submit(exc: IntegrityError, column: str = "idempotency_key") -> bool:
    """判断 IntegrityError 是否为幂等键唯一约束冲突（兼容各数据库报错文案）。"""
    message = str(exc.orig) if getattr(exc, "orig", None) is not None else str(exc)
    return column in message or "UNIQUE constraint failed" in message or "duplicate" in message.lower()
