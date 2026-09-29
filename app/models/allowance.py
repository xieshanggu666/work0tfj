from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint

from app.core.database import Base


class Quota(Base):
    """年度配额分配：免费配额与调整。"""

    __tablename__ = "quotas"
    __table_args__ = (
        # 同一企业同一年度只能有一条配额记录，并发分配由数据库兜底幂等
        UniqueConstraint("company_id", "year", name="uq_quota_company_year"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    baseline = Column(Numeric(18, 4), nullable=False, default=0)      # 历史基准排放
    allocation_amount = Column(Numeric(18, 4), nullable=False, default=0)  # 免费配额（tCO2）
    adjustment = Column(Numeric(18, 4), nullable=False, default=0)    # 调整量（可为负）
    total = Column(Numeric(18, 4), nullable=False, default=0)         # 最终配额
    status = Column(String(16), nullable=False, default="pending")    # pending/allocated/cleared
    allocated_at = Column(DateTime, nullable=True)


class AllowanceAccount(Base):
    """配额账户：企业年度配额持仓。

    余额不变量：
    - current_balance = 可用余额 + frozen_balance
    - 卖出/划出只能使用可用余额（current_balance - frozen_balance）；
    - 冻结只把可用部分转为冻结，不清偿；结算才把冻结转为已清缴；冲正则解冻退回。
    """

    __tablename__ = "allowance_accounts"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    opening_balance = Column(Numeric(18, 4), nullable=False, default=0)
    current_balance = Column(Numeric(18, 4), nullable=False, default=0)
    frozen_balance = Column(Numeric(18, 4), nullable=False, default=0)   # 履约冻结（已冻结未清缴）
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def available_balance(self) -> float:
        """可用余额：总余额扣除履约冻结部分，交易扣减以此为限。"""
        return round(float(self.current_balance) - float(self.frozen_balance), 4)


class AllowanceTransaction(Base):
    """配额划转与交易台账。

    tx_type 取值：
    - 增加：allocation（分配）/ buy（买入）/ transfer_in（划入）/ unfreeze（冻结解除退回）；
    - 扣减：sell（卖出）/ transfer_out（划出）/ offset（抵消）/ clear（直接清缴）/ settlement（冻结结算）；
    - 冻结：freeze（可用→冻结，current_balance 不变、frozen_balance 增加，记 0 快照不变量）。
    每条冻结/解除/结算流水均可通过 freeze_id 溯源到同一笔冻结记录。
    """

    __tablename__ = "allowance_transactions"
    __table_args__ = (
        # 客户端幂等键：同一账户重复提交（双击/重试/超时重发）只入账一次。
        # NULL 不参与唯一约束，未携带幂等键的请求不受影响。
        UniqueConstraint("account_id", "idempotency_key", name="uq_tx_account_idem"),
    )

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("allowance_accounts.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    tx_type = Column(String(16), nullable=False)
    amount = Column(Numeric(18, 4), nullable=False, default=0)
    counterparty = Column(String(128), nullable=False, default="")
    price = Column(Numeric(18, 2), nullable=True)
    tx_date = Column(String(10), nullable=False, default="")
    balance_after = Column(Numeric(18, 4), nullable=False, default=0)
    frozen_after = Column(Numeric(18, 4), nullable=False, default=0)  # 本笔后冻结余额快照
    remark = Column(String(256), nullable=False, default="")
    freeze_id = Column(Integer, ForeignKey("allowance_freezes.id"), nullable=True, index=True)
    idempotency_key = Column(String(64), nullable=True)  # 客户端去重键（UUID），同账户唯一
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AllowanceFreeze(Base):
    """履约冻结记录：报告批准后按核查排放量冻结配额。

    生命周期：
    - frozen：已冻结未清缴（frozen_amount 为当前冻结余额，原额在 approved_amount）；
    - settled：冻结配额已全部结算清缴（frozen_amount 归零）；
    - reversed：报告冲正，剩余冻结已解除并退回可用余额。
    缺口年度补缴先创建/补充冻结，再由清缴结算。
    """

    __tablename__ = "allowance_freezes"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("allowance_accounts.id"), nullable=True, index=True)
    report_id = Column(Integer, ForeignKey("mrv_reports.id"), nullable=True, index=True)
    approved_amount = Column(Numeric(18, 4), nullable=False, default=0)  # 批准时应冻结总额
    frozen_amount = Column(Numeric(18, 4), nullable=False, default=0)    # 当前仍冻结的额度
    settled_amount = Column(Numeric(18, 4), nullable=False, default=0)   # 已结算清缴累计
    reversed_amount = Column(Numeric(18, 4), nullable=False, default=0)  # 已冲正解除累计
    status = Column(String(16), nullable=False, default="frozen")        # frozen/settled/reversed
    reason = Column(String(256), nullable=False, default="")
    idempotency_key = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    settled_at = Column(DateTime, nullable=True)
    reversed_at = Column(DateTime, nullable=True)


class ComplianceRecord(Base):
    """年度履约记录：清缴配额抵扣实际排放。

    闭环状态：
    - pending：尚未批准/清缴；
    - deficit：存在未弥补缺口（已清缴 + 已冻结 < 核查排放量）；
    - frozen：报告已批准，缺口已全部冻结待结算；
    - compliant：清缴完成（cleared_amount >= verified_emission）。
    """

    __tablename__ = "compliance_records"
    __table_args__ = (
        UniqueConstraint("company_id", "year", name="uq_compliance_company_year"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    verified_emission = Column(Numeric(18, 4), nullable=False, default=0)  # 核查后排放量
    cleared_amount = Column(Numeric(18, 4), nullable=False, default=0)     # 已清缴配额
    frozen_amount = Column(Numeric(18, 4), nullable=False, default=0)      # 已冻结待结算
    deficit = Column(Numeric(18, 4), nullable=False, default=0)            # 缺口（未冻结且未清缴）
    status = Column(String(16), nullable=False, default="pending")         # pending/frozen/deficit/compliant
    deadline = Column(String(10), nullable=False, default="")
    idempotency_key = Column(String(64), nullable=True)  # 清缴请求去重键（全局唯一）
    approved_report_id = Column(Integer, ForeignKey("mrv_reports.id"), nullable=True)  # 已批准报告（冻结依据）
    cleared_at = Column(DateTime, nullable=True)
