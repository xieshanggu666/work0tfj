views.DashboardView = () => {
  const [stats, setStats] = React.useState(null);
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    api.get("/api/dashboard/stats").then(setStats).catch((e) => setError(e.message));
  }, []);

  if (error) return html`<div class="msg err">${error}</div>`;
  if (!stats) return html`<div class="empty">加载中...</div>`;

  const cc = stats.compliance_counts || {};
  const resolved = cc.compliant || 0;
  const totalRec = resolved + (cc.deficit || 0) + (cc.frozen || 0);
  const complianceRate =
    totalRec > 0
      ? ((resolved / totalRec) * 100).toFixed(1)
      : "-";

  return html`
    <div class="cards">
      <div class="card"><div class="label">控排企业</div><div class="value">${stats.total_companies}</div><div class="sub">平台注册企业</div></div>
      <div class="card"><div class="label">活动数据</div><div class="value">${stats.total_activity}</div><div class="sub">台账记录总数</div></div>
      <div class="card"><div class="label">核算结果</div><div class="value">${stats.total_results}</div><div class="sub">排放量明细条数</div></div>
      <div class="card"><div class="label">累计排放量</div><div class="value">${fmtNum(stats.emission_total)} tCO2e</div><div class="sub">核算结果汇总</div></div>
      <div class="card"><div class="label">配额总量</div><div class="value">${fmtNum(stats.quota_total)} t</div><div class="sub">年度免费配额</div></div>
      <div class="card"><div class="label">已清缴配额</div><div class="value">${fmtNum(stats.cleared_total)} t</div><div class="sub">累计履约清缴</div></div>
      <div class="card"><div class="label">冻结待结算</div><div class="value">${fmtNum(stats.frozen_total || 0)} t</div><div class="sub">报告批准锁定</div></div>
      <div class="card"><div class="label">待补缺口</div><div class="value">${fmtNum(stats.outstanding_deficit || 0)} t</div><div class="sub">未冻结且未清缴</div></div>
      <div class="card"><div class="label">履约达标率</div><div class="value">${complianceRate}%</div><div class="sub">达标 ${resolved} · 冻结 ${cc.frozen || 0} · 缺口 ${cc.deficit || 0}</div></div>
    </div>
    <div class="panel">
      <h3>平台概况</h3>
      <p style=${{color: "var(--text-dim)", lineHeight: "1.9"}}>
        本平台面向控排企业提供活动数据采集、排放核算、配额管理与履约清缴、配额交易台账及年度 MRV 报告等能力。
        核算引擎按《核算方法与报告指南》公式计算：排放量 = 活动量 × 排放因子；燃料燃烧类采用综合因子 × 碳氧化率 × 44/12。
        MRV 报告批准后按核查排放量冻结配额（仅可用余额可交易），清缴时冻结结算、缺口可买入补缴，
        核查结论撤销时可冲正：解除冻结、退回已清缴配额，履约状态同步回滚，余额流水与统计全程一致。
      </p>
    </div>
  `;
};
