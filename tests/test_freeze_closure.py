"""报告批准冻结 → 清缴结算 → 缺口补缴 → 冲正回滚 的跨模块闭环测试。

覆盖：
- 批准即冻结：账户 current/frozen/available、freeze 流水双快照、履约状态联动；
- 冻结额度不得被卖出占用（交易权限边界为“可用余额”）；
- 清缴走冻结结算（settlement），结算后达标；
- 缺口批准（deficit）→ 买入补缴 → 补冻即结 → compliant，累计不超排放量；
- 冲正：未结算冻结解除退回（unfreeze），已结算清缴退回（reverse），
  履约记录回 pending，修订后可重新提交批准；
- 幂等：清缴幂等键在冻结结算路径同样去重；
- 异常回滚：结算中途注入失败，冻结/余额/流水/履约记录全部不变；
- 统计对账：账户侧冻结合计 = 履约侧冻结合计。
"""

import pytest

from app.models import (
    ActivityData,
    AllowanceAccount,
    AllowanceFreeze,
    AllowanceTransaction,
    ComplianceRecord,
    MrvReport,
)
from app.services.calculation_service import annual_total, recalc_company_year
from app.services.freeze_service import freeze_on_approval, reverse_approval, settle_clearance
from app.services.mrv_service import approve_report, generate_report, reverse_report, submit_report
from app.services.quota_service import allocate_quota, clear_emission
from app.services.stats_service import dashboard_stats
from app.services.trading_service import transfer

TX_SIGN = {
    "allocation": 1, "buy": 1, "transfer_in": 1, "unfreeze": 0, "reverse": 1,
    "sell": -1, "transfer_out": -1, "offset": -1,
    "freeze": 0, "settlement": -1, "clear": -1,
}
FROZEN_SIGN = {
    "freeze": 1, "settlement": -1, "unfreeze": -1,
}


def approx(value, rel=1e-6):
    return pytest.approx(float(value), rel=rel)


def _prepare(db, seed, qty=2000, quota=10000):
    db.add(
        ActivityData(
            company_id=seed["company"].id, scope_id=seed["scope2"].id, year=2025,
            period="monthly", activity_type="外购电力", unit="MWh",
            quantity=qty, data_source="台账", verified=1,
        )
    )
    db.commit()
    recalc_company_year(db, seed["company"].id, 2025)
    if quota is not None:
        allocate_quota(db, seed["company"].id, 2025, baseline=1000, allocation_amount=quota, adjustment=0)
    return annual_total(db, seed["company"].id, 2025)


def _approve_flow(db, company_id, year=2025, quota_verifier=1):
    report = generate_report(db, company_id, year)
    submit_report(db, report)
    report, record = approve_report(db, report, verifier_id=quota_verifier)
    return report, record


def _assert_ledger_consistent(db, account_id):
    """双快照链：总余额链与冻结链逐笔一致，末笔快照等于账户现值。"""
    txs = (
        db.query(AllowanceTransaction)
        .filter(AllowanceTransaction.account_id == account_id)
        .order_by(AllowanceTransaction.id.asc())
        .all()
    )
    expect_balance = 0.0
    expect_frozen = 0.0
    for tx in txs:
        expect_balance = round(expect_balance + TX_SIGN[tx.tx_type] * float(tx.amount), 4)
        expect_frozen = round(
            expect_frozen + FROZEN_SIGN.get(tx.tx_type, 0) * float(tx.amount), 4
        )
        assert float(tx.balance_after) == approx(expect_balance), (
            f"流水 #{tx.id}({tx.tx_type}) 总余额快照 {tx.balance_after} != 推算 {expect_balance}"
        )
        assert float(tx.frozen_after or 0) == approx(expect_frozen), (
            f"流水 #{tx.id}({tx.tx_type}) 冻结快照 {tx.frozen_after} != 推算 {expect_frozen}"
        )
    account = db.get(AllowanceAccount, account_id)
    assert float(account.current_balance) == approx(expect_balance)
    assert float(account.frozen_balance) == approx(expect_frozen)
    return txs


class TestApprovalFreeze:
    def test_approve_freezes_emission_quota(self, db, seed):
        """批准后：冻结=排放量，总余额不变，可用余额减少，履约 frozen。"""
        emission = _prepare(db, seed, qty=2000, quota=10000)
        report, record = _approve_flow(db, seed["company"].id)

        assert report.status == "approved"
        account = db.query(AllowanceAccount).first()
        assert float(account.frozen_balance) == approx(emission)
        assert float(account.current_balance) == approx(10000)
        assert account.available_balance == approx(10000 - emission)
        assert record.status == "frozen"
        assert float(record.frozen_amount) == approx(emission)
        assert float(record.cleared_amount) == approx(0)
        assert float(record.deficit) == approx(0)
        assert record.approved_report_id == report.id

        freeze = db.query(AllowanceFreeze).one()
        assert float(freeze.frozen_amount) == approx(emission)
        assert freeze.status == "frozen"

        fz_tx = db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "freeze").one()
        assert float(fz_tx.amount) == approx(emission)
        assert float(fz_tx.balance_after) == approx(10000)
        assert float(fz_tx.frozen_after) == approx(emission)
        assert fz_tx.freeze_id == freeze.id
        _assert_ledger_consistent(db, account.id)

    def test_frozen_quota_cannot_be_sold(self, db, seed):
        """冻结后卖出只能动用可用余额，试图超用冻结部分必须被拒绝。"""
        emission = _prepare(db, seed, qty=2000, quota=2000)
        account = db.query(AllowanceAccount).first()
        _approve_flow(db, seed["company"].id)
        db.refresh(account)
        available = account.available_balance
        assert available == approx(2000 - emission)

        # 可卖可用部分
        transfer(db, account, available, "sell", tx_date="2025-06-01")
        # 再卖 1 吨即触及冻结额度，拒绝且无副作用
        with pytest.raises(ValueError, match="余额不足"):
            transfer(db, account, 1, "sell", tx_date="2025-06-01")
        db.refresh(account)
        assert account.available_balance == approx(0)
        assert float(account.frozen_balance) == approx(emission)
        _assert_ledger_consistent(db, account.id)

    def test_approve_with_insufficient_quota_marks_deficit(self, db, seed):
        """配额不足时批准：冻结全部可用，缺口记 deficit，冻结记录留痕。"""
        emission = _prepare(db, seed, qty=2000, quota=800)
        _, record = _approve_flow(db, seed["company"].id)
        account = db.query(AllowanceAccount).first()
        assert record.status == "deficit"
        assert float(record.frozen_amount) == approx(800)
        assert float(record.deficit) == approx(emission - 800)
        assert account.available_balance == approx(0)
        _assert_ledger_consistent(db, account.id)

    def test_approve_without_account_records_full_deficit(self, db, seed):
        """无账户时批准：全额缺口，不产生脏流水/脏冻结。"""
        emission = _prepare(db, seed, qty=1000, quota=None)
        _, record = _approve_flow(db, seed["company"].id)
        assert record.status == "deficit"
        assert float(record.deficit) == approx(emission)
        assert float(record.frozen_amount) == approx(0)
        assert db.query(AllowanceTransaction).count() == 0
        freeze = db.query(AllowanceFreeze).one()
        assert freeze.account_id is None
        assert float(freeze.frozen_amount) == approx(0)

    def test_duplicate_approve_rejected(self, db, seed):
        """非 submitted 状态重复批准被拒，且不产生第二笔冻结。"""
        _prepare(db, seed)
        report, _ = _approve_flow(db, seed["company"].id)
        with pytest.raises(ValueError, match="仅已提交"):
            approve_report(db, report, verifier_id=1)
        assert db.query(AllowanceFreeze).count() == 1


class TestSettlementAndTopUp:
    def test_clear_settles_frozen_quota(self, db, seed):
        """批准后清缴：冻结结算（settlement），frozen/settled/履约状态同步。"""
        emission = _prepare(db, seed, qty=2000, quota=10000)
        account = db.query(AllowanceAccount).first()
        _, record = _approve_flow(db, seed["company"].id)

        cleared = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert cleared.status == "compliant"
        assert float(cleared.cleared_amount) == approx(emission)
        assert float(cleared.frozen_amount) == approx(0)
        db.refresh(account)
        assert float(account.frozen_balance) == approx(0)
        assert float(account.current_balance) == approx(10000 - emission)

        freeze = db.query(AllowanceFreeze).one()
        assert freeze.status == "settled"
        assert float(freeze.settled_amount) == approx(emission)
        assert float(freeze.frozen_amount) == approx(0)
        txs = _assert_ledger_consistent(db, account.id)
        assert sum(1 for t in txs if t.tx_type == "settlement") == 1
        assert not [t for t in txs if t.tx_type == "clear"]

    def test_clear_idempotent_after_compliant(self, db, seed):
        """结算达标后重复清缴：无新流水、无变化（含相同/不同幂等键）。"""
        emission = _prepare(db, seed, qty=1000, quota=10000)
        account = db.query(AllowanceAccount).first()
        _approve_flow(db, seed["company"].id)
        first = clear_emission(db, seed["company"].id, 2025, "2025-12-31", idempotency_key="k1")
        again = clear_emission(db, seed["company"].id, 2025, "2025-12-31", idempotency_key="k1")
        third = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert first.id == again.id == third.id
        db.refresh(account)
        assert float(account.current_balance) == approx(10000 - emission)
        txs = db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "settlement").all()
        assert len(txs) == 1

    def test_deficit_then_buy_topup_closes_gap(self, db, seed):
        """缺口批准 → 买入 → 再次清缴：补冻即结，累计不超过排放量。"""
        emission = _prepare(db, seed, qty=2000, quota=800)
        account = db.query(AllowanceAccount).first()
        _, record = _approve_flow(db, seed["company"].id)
        assert record.status == "deficit"

        # 尚未买入时清缴：已冻结的 800 先结算，剩余仍为缺口
        partial = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert float(partial.cleared_amount) == approx(800)
        assert partial.status == "deficit"
        assert float(partial.deficit) == approx(emission - 800)

        # 市场买入缺口配额后补缴：freeze + settlement 成对，仅补剩余缺口
        transfer(db, account, 600, "buy", counterparty="交易所", tx_date="2025-12-20")
        final = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert final.status == "compliant"
        assert float(final.cleared_amount) == approx(emission)
        assert float(final.frozen_amount) == approx(0)

        db.refresh(account)
        gap = emission - 800
        assert float(account.current_balance) == approx(800 + 600 - 800 - gap)
        assert float(account.frozen_balance) == approx(0)
        txs = _assert_ledger_consistent(db, account.id)
        topup_freeze = [t for t in txs if "缺口补缴冻结" in (t.remark or "")]
        topup_settle = [t for t in txs if "缺口补缴结算" in (t.remark or "")]
        assert sum(float(t.amount) for t in topup_freeze) == approx(gap)
        assert sum(float(t.amount) for t in topup_settle) == approx(gap)
        total_settled = sum(float(t.amount) for t in txs if t.tx_type == "settlement")
        assert total_settled == approx(emission)

    def test_topup_partial_then_remaining(self, db, seed):
        """分批补缴：先补一部分仍 deficit，再补剩余后 compliant。"""
        emission = _prepare(db, seed, qty=2000, quota=800)
        account = db.query(AllowanceAccount).first()
        _approve_flow(db, seed["company"].id)
        clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        gap = emission - 800

        transfer(db, account, gap - 100, "buy", counterparty="交易所", tx_date="2025-12-20")
        rec = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert rec.status == "deficit"
        assert float(rec.cleared_amount) == approx(emission - 100)

        transfer(db, account, 200, "buy", counterparty="交易所", tx_date="2025-12-25")
        rec = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert rec.status == "compliant"
        assert float(rec.cleared_amount) == approx(emission)
        _assert_ledger_consistent(db, account.id)

    def test_clear_without_approval_keeps_legacy_path(self, db, seed):
        """报告未批准直接清缴：沿用 clear 流水扣可用余额路径。"""
        emission = _prepare(db, seed, qty=1000, quota=1000)
        account = db.query(AllowanceAccount).first()
        rec = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert rec.status == "compliant"
        assert rec.approved_report_id is None
        db.refresh(account)
        assert float(account.frozen_balance) == approx(0)
        assert float(account.current_balance) == approx(1000 - emission)
        assert db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "clear").count() == 1
        _assert_ledger_consistent(db, account.id)


class TestReversal:
    def test_reverse_before_settlement_unfreezes(self, db, seed):
        """未清缴即冲正：冻结解除退回，履约回 pending，配额可再次交易。"""
        emission = _prepare(db, seed, qty=2000, quota=10000)
        account = db.query(AllowanceAccount).first()
        report, _ = _approve_flow(db, seed["company"].id)

        reverse_report(db, report, reason="因子修订，排放量重算", user_id=2)
        assert report.status == "pending"
        assert report.version == 2
        assert report.reverse_reason == "因子修订，排放量重算"
        db.refresh(account)
        assert float(account.frozen_balance) == approx(0)
        assert float(account.current_balance) == approx(10000)
        assert account.available_balance == approx(10000)

        record = db.query(ComplianceRecord).one()
        assert record.status == "pending"
        assert float(record.cleared_amount) == approx(0)
        assert float(record.frozen_amount) == approx(0)
        assert record.approved_report_id is None
        freeze = db.query(AllowanceFreeze).one()
        assert freeze.status == "reversed"
        assert float(freeze.reversed_amount) == approx(emission)

        txs = _assert_ledger_consistent(db, account.id)
        assert any(t.tx_type == "unfreeze" for t in txs)
        # 冲正后可重新提交批准（新冻结记录）
        recalc_company_year(db, seed["company"].id, 2025)
        report2 = generate_report(db, seed["company"].id, 2025)
        assert report2.id == report.id and report2.status == "draft"
        submit_report(db, report2)
        report2, record2 = approve_report(db, report2, verifier_id=1)
        assert report2.version == 2
        assert record2.status == "frozen"
        assert db.query(AllowanceFreeze).filter(AllowanceFreeze.status == "frozen").count() == 1
        _assert_ledger_consistent(db, account.id)

    def test_reverse_after_settlement_refunds_cleared_quota(self, db, seed):
        """清缴达标后冲正：已清缴配额以 reverse 流水退回，履约回退 pending。"""
        emission = _prepare(db, seed, qty=2000, quota=10000)
        account = db.query(AllowanceAccount).first()
        report, _ = _approve_flow(db, seed["company"].id)
        clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        db.refresh(account)
        assert float(account.current_balance) == approx(10000 - emission)

        reverse_report(db, report, reason="核查结论撤销", user_id=2)
        db.refresh(account)
        assert float(account.current_balance) == approx(10000)
        assert float(account.frozen_balance) == approx(0)
        record = db.query(ComplianceRecord).one()
        assert record.status == "pending"
        assert float(record.cleared_amount) == approx(0)

        freeze = db.query(AllowanceFreeze).one()
        assert freeze.status == "reversed"
        assert float(freeze.settled_amount) == approx(emission)
        assert float(freeze.reversed_amount) == approx(emission)
        txs = _assert_ledger_consistent(db, account.id)
        refund = [t for t in txs if t.tx_type == "reverse"]
        assert sum(float(t.amount) for t in refund) == approx(emission)

    def test_reverse_partial_settlement(self, db, seed):
        """部分结算后冲正：剩余冻结 unfreeze + 已结算 reverse，配额全部退回。"""
        emission = _prepare(db, seed, qty=2000, quota=800)
        account = db.query(AllowanceAccount).first()
        report, _ = _approve_flow(db, seed["company"].id)
        # 800 冻结全部结算，仍有缺口；之后买入补缴 200 并结算
        clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        transfer(db, account, 500, "buy", counterparty="交易所", tx_date="2025-12-20")
        clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        db.refresh(account)
        assert float(account.current_balance) == approx(800 + 500 - emission)

        reverse_report(db, report, reason="数据错误", user_id=2)
        db.refresh(account)
        # 退回全部已清缴 = emission；买入的剩余仍在账户
        assert float(account.current_balance) == approx(1300)
        assert float(account.frozen_balance) == approx(0)
        record = db.query(ComplianceRecord).one()
        assert record.status == "pending"
        _assert_ledger_consistent(db, account.id)

    def test_reverse_requires_reason(self, db, seed):
        """冲正必须填写原因，空原因拒绝且状态不变。"""
        _prepare(db, seed)
        report, _ = _approve_flow(db, seed["company"].id)
        with pytest.raises(ValueError, match="原因"):
            reverse_approval(db, report, "   ", user_id=2)
        db.refresh(report)
        assert report.status == "approved"
        assert db.query(AllowanceFreeze).filter(AllowanceFreeze.status == "frozen").count() == 1

    def test_reverse_non_approved_rejected(self, db, seed):
        """草稿/已提交报告不可冲正。"""
        _prepare(db, seed)
        report = generate_report(db, seed["company"].id, 2025)
        with pytest.raises(ValueError, match="已批准"):
            reverse_approval(db, report, "x", user_id=2)

    def test_direct_clear_then_approve_then_reverse_refunds(self, db, seed):
        """先直接清缴（无报告）→ 事后批准 → 冲正：已清缴配额必须退回。

        回归：直接 clear 流水无 freeze 关联，批准时将其纳入冻结记录
        “视同已结算”，冲正时随已结算部分统一 reverse 退回。
        """
        emission = _prepare(db, seed, qty=1000, quota=1000)
        account = db.query(AllowanceAccount).first()

        # 报告尚未生成/批准，直接清缴达标
        rec = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert rec.status == "compliant"
        db.refresh(account)
        assert float(account.current_balance) == approx(1000 - emission)

        # 事后补报告并批准：无需再冻结，仍达标
        report = generate_report(db, seed["company"].id, 2025)
        submit_report(db, report)
        report, rec2 = approve_report(db, report, verifier_id=1)
        assert rec2.status == "compliant"
        assert float(rec2.frozen_amount) == approx(0)
        assert float(rec2.cleared_amount) == approx(emission)
        fz = db.query(AllowanceFreeze).one()
        assert float(fz.settled_amount) == approx(emission)
        assert float(fz.frozen_amount) == approx(0)
        assert fz.status == "settled"

        # 冲正：已直接清缴的配额通过 reverse 退回，履约回 pending
        reverse_report(db, report, reason="事后核查撤销", user_id=2)
        db.refresh(account)
        assert float(account.current_balance) == approx(1000)
        assert float(account.frozen_balance) == approx(0)
        record = db.query(ComplianceRecord).one()
        assert record.status == "pending"
        assert float(record.cleared_amount) == approx(0)
        refunds = db.query(AllowanceTransaction).filter(
            AllowanceTransaction.tx_type == "reverse"
        ).all()
        assert sum(float(t.amount) for t in refunds) == approx(emission)
        _assert_ledger_consistent(db, account.id)


class TestRollbackAndStats:
    def test_settlement_failure_rolls_back_everything(self, db, seed, monkeypatch):
        """结算途中异常：冻结/余额/流水/履约记录整体回滚，无半成品。"""
        emission = _prepare(db, seed, qty=2000, quota=10000)
        account = db.query(AllowanceAccount).first()
        _, record_before = _approve_flow(db, seed["company"].id)

        import app.services.freeze_service as fs

        real = fs.apply_settlement
        calls = {"n": 0}

        def flaky(db_, account_id, amount):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟结算下游故障")
            return real(db_, account_id, amount)

        monkeypatch.setattr(fs, "apply_settlement", flaky)
        with pytest.raises(RuntimeError, match="模拟结算下游故障"):
            settle_clearance(db, seed["company"].id, 2025, "2025-12-31")
        monkeypatch.undo()

        db.expire_all()
        account = db.query(AllowanceAccount).first()
        record = db.query(ComplianceRecord).one()
        assert float(account.frozen_balance) == approx(emission)
        assert float(account.current_balance) == approx(10000)
        assert record.status == "frozen"
        assert float(record.cleared_amount) == approx(0)
        # 仅批准时的 freeze 流水，无 settlement 残留
        assert db.query(AllowanceTransaction).filter(
            AllowanceTransaction.tx_type == "settlement"
        ).count() == 0
        _assert_ledger_consistent(db, account.id)

        # 回滚后可正常重试并成功
        rec = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert rec.status == "compliant"
        _assert_ledger_consistent(db, account.id)

    def test_stats_include_frozen_and_reconcile(self, db, seed):
        """仪表盘统计：冻结/缺口/可用字段齐全，账户侧与履约侧冻结合计一致。"""
        emission = _prepare(db, seed, qty=2000, quota=10000)
        _approve_flow(db, seed["company"].id)
        stats = dashboard_stats(db)
        assert stats["frozen_total"] == approx(emission)
        assert stats["account_frozen_total"] == approx(emission)
        assert stats["available_total"] == approx(10000 - emission)
        assert stats["outstanding_deficit"] == approx(0)
        assert stats["compliance_counts"]["frozen"] == 1

        clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        stats = dashboard_stats(db)
        assert stats["cleared_total"] == approx(emission)
        assert stats["frozen_total"] == approx(0)
        assert stats["compliance_counts"]["compliant"] == 1
