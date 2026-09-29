# 碳排放核算与交易管理系统

面向控排企业的碳管理平台：活动数据采集、排放核算、配额分配、配额交易台账、履约清缴与年度 MRV 报告。

## 技术栈

- **后端**：Python 3.10+ / FastAPI / SQLAlchemy ORM / SQLite / JWT（Cookie 认证）
- **前端**：React 18（本地 UMD 运行时 + htm 模板引擎，无需构建工具，完全离线可用）
- **测试**：pytest（66 项全部通过，含 16 项多线程并发一致性测试）

## 快速开始

```bash
pip install -r requirements.txt
python scripts/init_db.py      # 初始化数据库与演示数据（已有旧库先执行迁移脚本）
uvicorn app.main:app --reload  # 启动服务
```

> 升级旧库（新增幂等键列与唯一约束）：`python scripts/migrate_concurrency.py`，可重复执行。

访问 http://127.0.0.1:8000

### 演示账号（密码均为 `123456`）

| 用户名    | 角色       | 说明                       |
|-----------|------------|----------------------------|
| `admin`   | 监管管理员 | 企业/因子/配额/清缴全权限   |
| `verifier`| 核查员     | 核验活动数据、批准 MRV 报告 |
| `elec`    | 控排企业   | 绿能电力集团（边界受限）    |
| `cement`  | 控排企业   | 恒固水泥股份（边界受限）    |

## 功能模块

1. **核算边界管理**：企业注册行业/地区/核算边界说明，范围一/二/三边界配置
2. **排放因子库**：因子编号、有效期、数据来源；修订自动记录版本历史
3. **活动数据台账**：企业按年度/周期录入活动量，核查员核验标记
4. **排放核算引擎**：
   - `activity_factor`：排放量 = 活动量 × 因子值
   - `fuel_combustion`：排放量 = 燃料量 × 综合系数 × 碳氧化率 × 44/12
   - 因子按年度生效区间取值，重复核算幂等（先清后算）
5. **配额管理**：免费配额分配（基准 + 分配量 + 调整量）、配额账户余额（总额/冻结/可用三段式）
6. **配额交易台账**：买入/卖出/划转，实时校验**可用余额**（冻结配额不可交易），逐笔记录余额与冻结双快照
7. **履约清缴与冻结闭环**：
   - **批准即冻结**：MRV 报告批准后以核查排放量为依据冻结配额（freeze 流水），不足部分记缺口（deficit），足额冻结为 frozen
   - **冻结结算**：清缴时冻结额度 current/frozen 同减转为已清缴（settlement 流水），达标为 compliant
   - **缺口补缴**：缺口年度可先买入配额，再次清缴自动“补冻即结”（freeze+settlement 成对流水），累计清缴不超过核查排放量
   - **冲正回滚**：已批准报告可由核查员冲正（须填原因）：未结算冻结解除退回（unfreeze）、已结算清缴退回（reverse），履约记录回 pending，修订后重新提交批准
   - 报告未批准时清缴沿用直接扣减可用余额（clear 流水）路径
8. **MRV 报告**：年度范围一二三汇总生成，草稿/冲正退回 → 提交 → 批准状态流转；已提交、已批准不可直接重建（须先冲正），版本随冲正递增

### 跨模块冻结闭环（批准 / 清缴 / 补缴 / 冲正）

```
草稿 draft ──提交──▶ 已提交 submitted ──批准(核查员)──▶ 已批准 approved ──冲正(原因)──▶ 冲正退回 pending
                          │                              │                              │
                          │                       冻结配额 freeze                 解冻 unfreeze
                          │                     （可用→冻结，锁定）            退回已清缴 reverse
                          │                              │                              │
                          ▼                              ▼                              ▼
                     （不影响账户）               清缴结算 settlement          履约记录回 pending
                                                     冻结→已清缴                可修订后重新提交批准
                     缺口 deficit ──买入 buy──▶ 再次清缴：补冻即结 freeze+settlement ──▶ compliant
```

### 并发一致性保障（冻结 / 清缴 / 交易）

- **账户锁定**：进程内按键（账户 / 企业+年度清缴键 `clear:` / 企业+年度冻结键 `freeze:`）串行化余额变更，多键按序加锁防死锁；PostgreSQL/MySQL 额外加 `SELECT … FOR UPDATE` 行锁，SQLite 设置 `busy_timeout` 等待写锁
- **状态锁内校验**：报告批准/冲正的状态翻转在企业冻结键锁内以数据库最新状态判定，并发重复批准只冻结一次、重复冲正不会二次退款
- **原子条件更新**：交易扣减带 `current_balance - frozen_balance >= amount` 条件（只能动用可用余额）；冻结带可冻余额条件；结算/解冻带冻结余额条件，均为单条 UPDATE，由数据库兜底防超额
- **重复提交**：流水与履约记录支持幂等键（请求体 `idempotency_key` 或 `Idempotency-Key` 请求头），双击 / 超时重试只入账一次；前端提交期间禁用按钮并自动生成幂等键
- **事务边界**：批准翻转 + 冻结 + 流水 + 履约记录、清缴结算 + 补冻 + 流水、冲正解冻/退回 + 状态回滚，均为单事务，任一步异常统一回滚，杜绝“已批准未冻结/已扣减无流水”等半成品
- **数据库兜底约束**：`quotas` / `compliance_records` 的 (企业, 年度) 唯一约束防止并发分配/清缴产生重复主记录

## 数据表（14 张）

`users` `companies` `emission_scopes` `activity_data` `emission_factors` `factor_versions` `calculation_methods` `emission_results` `quotas` `allowance_accounts` `allowance_transactions` `allowance_freezes` `compliance_records` `mrv_reports`

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录（Cookie 会话） |
| GET | `/api/dashboard/stats` | 平台统计 |
| GET/POST | `/api/companies` | 企业列表/创建（admin） |
| POST | `/api/companies/{id}/scopes` | 添加核算边界（admin） |
| GET | `/api/companies/{id}/totals?year=` | 年度范围汇总 |
| GET/POST | `/api/activity` | 活动数据列表/录入 |
| POST | `/api/activity/{id}/verify` | 核验（verifier/admin） |
| GET/POST | `/api/factors` | 因子列表/创建（admin） |
| PUT | `/api/factors/{id}` | 修订因子并记版本（admin） |
| POST | `/api/companies/{id}/calculate?year=` | 触发核算 |
| GET | `/api/companies/{id}/results?year=` | 核算明细 |
| GET/POST | `/api/quotas` | 配额列表/分配（admin） |
| GET | `/api/companies/{id}/account?year=` | 账户余额（总额/冻结/可用） |
| GET | `/api/companies/{id}/freezes?year=` | 履约冻结记录（冻结/结算/冲正） |
| POST | `/api/accounts/{id}/transfer` | 配额交易（仅扣可用余额） |
| POST | `/api/companies/{id}/clear` | 履约清缴（冻结结算/缺口补缴，admin） |
| GET | `/api/compliance` | 履约记录（pending/frozen/deficit/compliant） |
| POST | `/api/companies/{id}/reports/generate` | 生成 MRV 报告（已批准须先冲正） |
| POST | `/api/reports/{id}/submit` / `/approve` / `/reverse` | 提交/批准（触发冻结）/冲正（解冻退回） |

## 测试

```bash
python -m pytest tests/ -v   # 66 passed
```

覆盖：核算引擎两种公式、因子按年取值、核算幂等、配额分配幂等、清缴达标/缺口与补缴、交易余额校验、MRV 状态机、API 冒烟、越权防护；**冻结闭环**（批准即冻结、冻结额度不可交易、冻结结算、缺口买入补冻即结、冲正解冻/退回/状态回退、结算故障整体回滚、统计对账、批准/冲正 API 权限边界）；以及多线程并发交易/清缴/批准冻结/结算（无超额扣减、余额+冻结双快照链一致、幂等键去重、失败整体回滚、清缴与交易并发三方一致）。
