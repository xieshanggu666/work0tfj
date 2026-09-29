const api = {
  // 生成客户端幂等键：同一次提交在双击/重试/超时重发时只生效一次
  idemKey() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "idem-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  },
  async request(method, url, body, idemKey) {
    const headers = { "Content-Type": "application/json" };
    // 写操作携带幂等键，服务端据此去重，避免重复扣减/重复履约
    if (idemKey) headers["Idempotency-Key"] = idemKey;
    const res = await fetch(url, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
    });
    if (res.status === 401) {
      location.hash = "#/login";
      throw new Error("未登录");
    }
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.detail || "请求失败");
    }
    return res.json();
  },
  get(url) { return this.request("GET", url); },
  post(url, body, idemKey) { return this.request("POST", url, body, idemKey); },
  put(url, body) { return this.request("PUT", url, body); },
};

const fmtNum = (v, digits = 2) =>
  v === null || v === undefined ? "-" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });

const StatusBadge = (s, overrides) => {
  const map = {
    active: ["active", "正常"],
    inactive: ["muted", "停用"],
    pending: ["warn", "待处理"],
    allocated: ["info", "已分配"],
    cleared: ["ok", "已清缴"],
    frozen: ["info", "冻结待缴"],
    settled: ["ok", "已结算"],
    reversed: ["muted", "已冲正"],
    compliant: ["ok", "履约达标"],
    deficit: ["danger", "配额缺口"],
    draft: ["muted", "草稿"],
    submitted: ["info", "已提交"],
    approved: ["ok", "已批准"],
  };
  const found = overrides && overrides[s] ? overrides[s] : map[s];
  const [cls, label] = found || ["muted", s];
  return `<span class="badge ${cls}">${label}</span>`;
};

// MRV 报告状态：pending 在报告语境表示“冲正退回”
const reportStatusOverrides = { pending: ["warn", "冲正退回"] };

const scopeLabel = (s) => ({ 1: "范围一", 2: "范围二", 3: "范围三" }[s] || s);
const txLabel = {
  allocation: "配额分配",
  buy: "买入",
  sell: "卖出",
  transfer_in: "转入",
  transfer_out: "转出",
  offset: "抵消",
  clear: "履约清缴",
  freeze: "履约冻结",
  settlement: "冻结结算",
  unfreeze: "解除冻结",
  reverse: "冲正退回",
};
