"""碳排放系统业务逻辑测试：核算引擎 / 配额履约 / 交易台账 / MRV 报告。"""

import pytest

from app.models import ActivityData, EmissionResult
from app.services.calculation_service import (
    annual_total,
    get_factor_for_year,
    recalc_company_year,
    scope_totals,
)
from app.services.mrv_service import approve_report, generate_report, submit_report
from app.services.quota_service import allocate_quota, clear_emission
from app.services.trading_service import transfer


def approx(value, rel=1e-6):
    """Numeric 列返回 Decimal，统一转为 float 后近似比较。"""
    return pytest.approx(float(value), rel=rel)


def _add_activity(db, seed, scope, year, atype, qty, unit="t"):
    act = ActivityData(
        company_id=seed["company"].id,
        scope_id=scope.id,
        year=year,
        period="monthly",
        activity_type=atype,
        unit=unit,
        quantity=qty,
        data_source="测试台账",
        verified=1,
    )
    db.add(act)
    db.commit()
    return act


class TestCalculationEngine:
    def test_activity_factor_method(self, db, seed):
        """外购电力：排放量 = 活动量 × 因子值。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        count = recalc_company_year(db, seed["company"].id, 2025)
        assert count == 1
        result = db.query(EmissionResult).first()
        assert float(result.emission_amount) == approx(2000 * 0.5703, rel=1e-6)

    def test_fuel_combustion_method(self, db, seed):
        """燃煤：排放量 = 燃料量 × 综合系数 × 碳氧化率 × 44/12。"""
        _add_activity(db, seed, seed["scope1"], 2025, "燃煤消耗", 100)
        recalc_company_year(db, seed["company"].id, 2025)
        result = db.query(EmissionResult).first()
        expected = 100 * 2.6 * 0.98 * 44 / 12
        assert float(result.emission_amount) == approx(expected, rel=1e-6)

    def test_scope_totals_grouping(self, db, seed):
        """范围一与范围二分别汇总，年度合计正确。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        _add_activity(db, seed, seed["scope1"], 2025, "燃煤消耗", 100)
        recalc_company_year(db, seed["company"].id, 2025)
        totals = scope_totals(db, seed["company"].id, 2025)
        assert totals["2"] == approx(2000 * 0.5703, rel=1e-6)
        assert totals["1"] == approx(100 * 2.6 * 0.98 * 44 / 12, rel=1e-6)
        assert annual_total(db, seed["company"].id, 2025) == approx(totals["1"] + totals["2"], rel=1e-6)

    def test_factor_picked_by_year(self, db, seed):
        """因子按年度生效区间取当期版本。"""
        from app.models import EmissionFactor

        new_factor = EmissionFactor(
            factor_code="COAL-PWR-2025", name="燃煤消耗", scope="1", unit="tC/t", value=2.8,
            source="2025 修订", valid_from="2025-01-01", valid_to=None,
        )
        db.add(new_factor)
        db.commit()

        f_2024 = get_factor_for_year(db, "燃煤消耗", 2024)
        f_2025 = get_factor_for_year(db, "燃煤消耗", 2025)
        assert float(f_2024.value) == approx(2.6)
        assert float(f_2025.value) == approx(2.8)

    def test_recalc_is_idempotent(self, db, seed):
        """重复核算不产生重复结果。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        recalc_company_year(db, seed["company"].id, 2025)
        first = db.query(EmissionResult).count()
        recalc_company_year(db, seed["company"].id, 2025)
        assert db.query(EmissionResult).count() == first == 1


class TestQuotaAndCompliance:
    def test_allocation_total_and_account(self, db, seed):
        """配额总额 = 分配量 + 调整量，账户余额入账并登记流水。"""
        quota = allocate_quota(db, seed["company"].id, 2025, baseline=1000, allocation_amount=800, adjustment=-50)
        assert quota.total == approx(750)
        assert quota.status == "allocated"
        from app.models import AllowanceAccount, AllowanceTransaction

        account = db.query(AllowanceAccount).filter(AllowanceAccount.company_id == seed["company"].id).first()
        assert account.opening_balance == approx(750)
        assert account.current_balance == approx(750)
        tx = db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "allocation").first()
        assert tx.amount == approx(750)
        assert tx.balance_after == approx(750)

    def test_duplicate_allocation_returns_existing(self, db, seed):
        """同一企业同年份重复分配返回已有配额，不重复入账。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, -50)
        again = allocate_quota(db, seed["company"].id, 2025, 1000, 800, -50)
        assert db.query(type(again)).count() == 1
        from app.models import AllowanceAccount

        account = db.query(AllowanceAccount).first()
        assert account.current_balance == approx(750)

    def test_clear_compliant_when_quota_sufficient(self, db, seed):
        """配额充足时清缴后状态为 compliant，缺口为 0。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000, "MWh")
        recalc_company_year(db, seed["company"].id, 2025)
        emission = annual_total(db, seed["company"].id, 2025)
        allocate_quota(db, seed["company"].id, 2025, baseline=1000, allocation_amount=1000, adjustment=0)
        record = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert record.status == "compliant"
        assert float(record.verified_emission) == approx(emission)
        assert float(record.cleared_amount) == approx(emission)
        assert float(record.deficit) == approx(0)

    def test_clear_deficit_when_quota_insufficient(self, db, seed):
        """配额不足时清缴后状态为 deficit，缺口正确。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        recalc_company_year(db, seed["company"].id, 2025)
        emission = annual_total(db, seed["company"].id, 2025)
        allocate_quota(db, seed["company"].id, 2025, baseline=1000, allocation_amount=800, adjustment=0)
        record = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert record.status == "deficit"
        assert float(record.cleared_amount) == approx(800)
        assert float(record.deficit) == approx(emission - 800)


class TestTrading:
    def test_sell_reduces_balance(self, db, seed):
        """卖出扣减余额并记录余额快照。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        from app.models import AllowanceAccount

        account = db.query(AllowanceAccount).first()
        tx = transfer(db, account, 300, "sell", counterparty="某碳资产管理公司", price=80, tx_date="2025-06-01", remark="挂牌卖出")
        assert account.current_balance == approx(500)
        assert tx.balance_after == approx(500)

    def test_insufficient_balance_raises(self, db, seed):
        """余额不足时拒绝卖出且余额不变。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        from app.models import AllowanceAccount

        account = db.query(AllowanceAccount).first()
        with pytest.raises(ValueError, match="余额不足"):
            transfer(db, account, 900, "sell", tx_date="2025-06-01")
        assert account.current_balance == approx(800)

    def test_buy_increases_balance(self, db, seed):
        """买入增加余额。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        from app.models import AllowanceAccount

        account = db.query(AllowanceAccount).first()
        transfer(db, account, 200, "buy", counterparty="交易所", price=85, tx_date="2025-06-02", remark="大宗买入")
        assert account.current_balance == approx(1000)


class TestMrvReport:
    def test_generate_submit_approve_workflow(self, db, seed):
        """报告生成 → 提交 → 批准完整流转。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        recalc_company_year(db, seed["company"].id, 2025)
        total = annual_total(db, seed["company"].id, 2025)

        report = generate_report(db, seed["company"].id, 2025)
        assert report.status == "draft"
        assert float(report.total_emission) == approx(total)

        submit_report(db, report)
        assert report.status == "submitted"

        approve_report(db, report, verifier_id=1)
        assert report.status == "approved"

    def test_approve_without_submit_rejected(self, db, seed):
        """草稿不可直接批准。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        recalc_company_year(db, seed["company"].id, 2025)
        report = generate_report(db, seed["company"].id, 2025)
        with pytest.raises(ValueError, match="仅已提交"):
            approve_report(db, report, verifier_id=1)

    def test_regenerate_resets_to_draft(self, db, seed):
        """重新生成报告重置为草稿。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        recalc_company_year(db, seed["company"].id, 2025)
        report = generate_report(db, seed["company"].id, 2025)
        submit_report(db, report)
        report = generate_report(db, seed["company"].id, 2025)
        assert report.status == "draft"
