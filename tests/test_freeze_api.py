"""履约冻结闭环 API 测试：报告批准/冲正、冻结记录查询的权限边界。

覆盖：
- 批准仅 verifier/admin；冲正仅 verifier/admin，且必须带原因；
- enterprise 不能批准/冲正；未登录 401；
- 企业只能查看本企业冻结记录，跨企业 403（与账户/流水同一归属边界）；
- 批准响应含冻结量/缺口，冲正后履约记录回 pending、配额退回；
- 已提交/已批准报告不可重新生成的接口约束。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import app
from app.models import (
    ActivityData,
    AllowanceAccount,
    AllowanceTransaction,
    CalculationMethod,
    Company,
    ComplianceRecord,
    EmissionFactor,
    EmissionScope,
    MrvReport,
    User,
)


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)

    db = TestingSession()
    pwd_hash, salt = hash_password("123456")
    c1 = Company(code="E-001", name="企业甲", industry="电力", region="华东")
    c2 = Company(code="E-002", name="企业乙", industry="水泥", region="华北")
    db.add_all([c1, c2])
    db.flush()

    db.add_all([
        User(username="ent1", display_name="企业甲用户", role="enterprise",
             company_id=c1.id, password_hash=pwd_hash, salt=salt),
        User(username="ent2", display_name="企业乙用户", role="enterprise",
             company_id=c2.id, password_hash=pwd_hash, salt=salt),
        User(username="admin", display_name="监管员", role="admin",
             password_hash=pwd_hash, salt=salt),
        User(username="verifier", display_name="核查员", role="verifier",
             password_hash=pwd_hash, salt=salt),
    ])

    scope = EmissionScope(company_id=c1.id, scope="2", category="外购电力", name="厂区用电")
    db.add(scope)
    db.flush()
    db.add(CalculationMethod(method_code="ELEC", name="电力因子法", scope="2",
                             formula_type="activity_factor"))
    db.add(EmissionFactor(factor_code="ELEC-GRID", name="外购电力", scope="2",
                          unit="tCO2/MWh", value=0.5, source="电网",
                          valid_from="2024-01-01", valid_to=None))
    db.flush()
    db.add(ActivityData(company_id=c1.id, scope_id=scope.id, year=2025, period="monthly",
                        activity_type="外购电力", unit="MWh", quantity=1000,
                        data_source="台账", verified=1))
    db.commit()

    from app.services.calculation_service import recalc_company_year
    from app.services.quota_service import allocate_quota
    from app.services.mrv_service import generate_report

    recalc_company_year(db, c1.id, 2025)
    allocate_quota(db, c1.id, 2025, baseline=1000, allocation_amount=1000, adjustment=0)
    report = generate_report(db, c1.id, 2025)
    ids = {"company1": c1.id, "company2": c2.id, "report_id": report.id}
    db.close()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client, ids
    app.dependency_overrides.clear()
    engine.dispose()


def login(client, username):
    client.post("/api/auth/login", json={"username": username, "password": "123456"})


def _enterprise_submit(client, ids):
    """报告提交属企业动作：以企业甲身份登录并提交，结束后仍为企业会话。

    需要核查员身份的用例随后自行 login(client, "verifier") 覆盖会话即可。
    """
    login(client, "ent1")
    return client.post(f"/api/reports/{ids['report_id']}/submit")


# ---------- 权限边界 ----------

def test_approve_requires_verifier_role(ctx):
    client, ids = ctx
    login(client, "ent1")
    _enterprise_submit(client, ids)
    # enterprise 不能批准
    res = client.post(f"/api/reports/{ids['report_id']}/approve")
    assert res.status_code == 403
    # 未登录 401
    client.cookies.clear()
    res = client.post(f"/api/reports/{ids['report_id']}/approve")
    assert res.status_code == 401


def test_reverse_requires_verifier_role(ctx):
    client, ids = ctx
    _enterprise_submit(client, ids)
    login(client, "verifier")
    client.post(f"/api/reports/{ids['report_id']}/approve")
    client.cookies.clear()

    login(client, "ent1")
    res = client.post(f"/api/reports/{ids['report_id']}/reverse", json={"reason": "x"})
    assert res.status_code == 403


def test_approve_and_reverse_full_cycle(ctx):
    client, ids = ctx
    _enterprise_submit(client, ids)
    login(client, "verifier")

    res = client.post(f"/api/reports/{ids['report_id']}/approve")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "approved"
    assert body["compliance_status"] == "frozen"
    assert body["frozen_amount"] == pytest.approx(500.0)
    assert body["deficit"] == 0

    # 已批准不能直接重新生成（企业侧）
    client.cookies.clear()
    login(client, "ent1")
    res = client.post(f"/api/companies/{ids['company1']}/reports/generate?year=2025")
    assert res.status_code == 400
    assert "冲正" in res.json()["detail"]

    # 冲正必须带原因：空串被 schema 拒绝（422），纯空白被服务层拒绝（400）
    client.cookies.clear()
    login(client, "verifier")
    res = client.post(f"/api/reports/{ids['report_id']}/reverse", json={"reason": ""})
    assert res.status_code == 422
    res = client.post(f"/api/reports/{ids['report_id']}/reverse", json={"reason": "   "})
    assert res.status_code == 400

    res = client.post(f"/api/reports/{ids['report_id']}/reverse", json={"reason": "因子取值有误"})
    assert res.status_code == 200
    assert res.json()["status"] == "pending"
    assert res.json()["version"] == 2

    # 履约记录已回退
    res = client.get("/api/compliance?year=2025")
    rec = [r for r in res.json() if r["company_id"] == ids["company1"]][0]
    assert rec["status"] == "pending"
    assert rec["frozen_amount"] == 0
    assert rec["cleared_amount"] == 0

    # 冲正后企业可重新生成
    client.cookies.clear()
    login(client, "ent1")
    res = client.post(f"/api/companies/{ids['company1']}/reports/generate?year=2025")
    assert res.status_code == 200
    assert res.json()["status"] == "draft"


def test_freeze_list_boundary(ctx):
    """冻结记录受企业归属边界保护：跨企业 403，本企业可读。"""
    client, ids = ctx
    _enterprise_submit(client, ids)
    login(client, "verifier")
    client.post(f"/api/reports/{ids['report_id']}/approve")

    res = client.get(f"/api/companies/{ids['company1']}/freezes?year=2025")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["frozen_amount"] == pytest.approx(500.0)
    assert data[0]["status"] == "frozen"

    client.cookies.clear()
    login(client, "ent2")
    res = client.get(f"/api/companies/{ids['company1']}/freezes?year=2025")
    assert res.status_code == 403

    # 本企业可看
    login(client, "ent1")
    res = client.get(f"/api/companies/{ids['company1']}/freezes?year=2025")
    assert res.status_code == 200

    # 未登录
    client.cookies.clear()
    res = client.get(f"/api/companies/{ids['company1']}/freezes")
    assert res.status_code == 401


def test_account_exposes_available_balance(ctx):
    client, ids = ctx
    _enterprise_submit(client, ids)
    login(client, "verifier")
    client.post(f"/api/reports/{ids['report_id']}/approve")

    login(client, "ent1")
    res = client.get(f"/api/companies/{ids['company1']}/account?year=2025")
    assert res.status_code == 200
    body = res.json()
    assert body["current_balance"] == 1000.0
    assert body["frozen_balance"] == 500.0
    assert body["available_balance"] == 500.0

    # 试图卖出超过可用余额（动用冻结）被拒
    res = client.post(
        f"/api/accounts/{body['id']}/transfer",
        json={"amount": 600, "tx_type": "sell", "tx_date": "2025-06-01"},
    )
    assert res.status_code == 400
    assert "余额不足" in res.json()["detail"]
