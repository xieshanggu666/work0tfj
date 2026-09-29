from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, Text

from app.core.database import Base


class MrvReport(Base):
    """年度 MRV 报告：监测、报告与核查成果。

    状态机：draft（草稿）→ submitted（已提交）→ approved（已批准，冻结配额）
                                          ↘ pending（冲正退回，待重新核查）→ submitted
    - approved：核查排放量锁定为履约依据，账户对应额度已冻结（不足部分记缺口）；
    - pending：冲正后冻结解除、履约记录同步回退，修订后可重新提交批准。
    """

    __tablename__ = "mrv_reports"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    total_emission = Column(Numeric(18, 4), nullable=False, default=0)
    scope1 = Column(Numeric(18, 4), nullable=False, default=0)
    scope2 = Column(Numeric(18, 4), nullable=False, default=0)
    scope3 = Column(Numeric(18, 4), nullable=False, default=0)
    report_json = Column(Text, nullable=False, default="{}")   # 明细数据
    status = Column(String(16), nullable=False, default="draft")  # draft/submitted/approved/pending
    generated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    reverse_reason = Column(String(256), nullable=True)        # 最近一次冲正原因
    reversed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reversed_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)       # 每次冲正退回后递增
