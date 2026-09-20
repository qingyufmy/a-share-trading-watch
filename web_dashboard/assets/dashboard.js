"use strict";

const APP_ASSET_VERSION = "v2-tracking-funnel-20260826";

const state = {
  index: [],
  report: null,
  runtime: null,
  filter: "all",
  search: "",
  theme: "light",
  autoRefresh: true,
  refreshTimer: null,
  refreshEveryMs: 15000,
  paperPositionSort: { key: null, direction: "desc" },
};

document.addEventListener("DOMContentLoaded", init);

async function init() {
  if (location.protocol === "file:") {
    renderFileRedirect();
    return;
  }
  const params = new URLSearchParams(location.search);
  state.theme = params.get("theme") || localStorage.getItem("aShareReportTheme") || "light";
  state.autoRefresh = params.get("auto") !== "0";
  document.documentElement.dataset.theme = state.theme;
  try {
    state.index = await fetchJson("./data/index.json");
    if (!state.index.length) throw new Error("暂无报告索引");
    const id = pickReportId(params);
    state.report = await fetchJson(`./data/reports/${encodeURIComponent(id)}.json`);
    state.runtime = await fetchRuntimeSnapshot();
    render();
    scheduleRefresh();
  } catch (error) {
    document.getElementById("app").innerHTML = renderError(error);
  }
}

async function fetchRuntimeSnapshot() {
  const [signals, health, preopenQuality] = await Promise.all([
    fetchJson("./data/runtime/latest_signals.json").catch(() => null),
    fetchJson("./data/runtime/signal_health.json").catch(() => null),
    fetchJson("./data/runtime/preopen_quality/latest.json").catch(() => null),
  ]);
  if (!signals && !health && !preopenQuality) return null;
  return {
    ...(signals || {}),
    runtime_status: health || signals?.health || {},
    preopen_quality: preopenQuality,
  };
}

function renderFileRedirect() {
  const target = `http://127.0.0.1:8765/web_dashboard/index.html${location.search || "?theme=light"}`;
  document.documentElement.dataset.theme = "light";
  document.getElementById("app").innerHTML = `
    <main class="loading-shell">
      <div class="loading-card">
        <h1>A股订盘报告工作台</h1>
        <p>正在切换到本地服务页面。</p>
        <p><a href="${escapeAttr(target)}">打开工作台</a></p>
      </div>
    </main>
  `;
  setTimeout(() => {
    location.replace(target);
  }, 250);
}

function pickReportId(params) {
  const explicit = params.get("report");
  if (explicit && state.index.some((x) => x.id === explicit)) return explicit;
  const date = params.get("date");
  const kind = params.get("kind") || params.get("type");
  const normalizedDate = date ? `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}` : "";
  const matched = state.index.find((x) => (!normalizedDate || x.date === normalizedDate) && (!kind || x.kind === kind));
  return (matched || state.index[0]).id;
}

async function fetchJson(url) {
  const resp = await fetch(url, { cache: "no-store" });
  if (!resp.ok) throw new Error(`${url} 读取失败：${resp.status}`);
  return resp.json();
}

function render() {
  const report = state.report;
  document.title = report.title || "A股订盘报告工作台";
  document.getElementById("app").innerHTML = `
    <div class="app-shell">
      ${renderTopbar(report)}
      ${renderHero(report)}
      ${renderRuntimeIntegrity(report)}
      ${renderPreopenQuality(report)}
      ${renderEntryAlerts(report)}
      ${renderGlobalRiskGate(report)}
      ${renderMiraGate(report)}
      ${renderPaperTrades(report)}
      ${renderPaperOrders(report)}
      ${renderPaperPositions(report)}
      ${renderMarketOpportunities(report)}
      ${renderStockSection(report)}
      ${renderFocusCards(report)}
      ${renderSections(report)}
      ${renderRaw(report)}
    </div>
  `;
  bindEvents();
  applyFilter();
}

function runtimeHealth(report) {
  return report.realtime_health || state.runtime?.runtime_status || state.runtime?.health || {};
}

function renderRuntimeIntegrity(report) {
  const freshness = report.report_freshness || {};
  const health = runtimeHealth(report);
  const stateName = health.status_state || health.engine_status || "unknown";
  const lastSuccess = health.last_success_at || "无成功信号记录";
  let tone = "ok";
  let title = "实时信号状态正常";
  let detail = `最后成功信号：${lastSuccess}`;

  if (freshness.stale_afterclose) {
    tone = "error";
    title = "当前盘后页不是当日复盘";
    detail = freshness.message || "该报告的基准交易日与页面日期不一致。";
  } else if (stateName === "engine_error") {
    tone = "error";
    title = "实时信号引擎异常";
    detail = `最后成功信号：${lastSuccess}；异常：${health.last_error || health.reason || "未记录"}`;
  } else if (stateName === "stale" || stateName === "missing") {
    tone = "warn";
    title = "实时信号数据已过期";
    detail = `最后成功信号：${lastSuccess}。条件卡片仅供复盘，不能作为盘中成交依据。`;
  } else if (stateName === "closed") {
    tone = "closed";
    title = "本交易日实时引擎已收盘";
    detail = `最后成功信号：${lastSuccess}。盘后页面保留该时点状态用于复盘。`;
  }
  return `
    <section class="runtime-integrity runtime-${escapeAttr(tone)}">
      <strong>${escapeHtml(title)}</strong>
      <span>${escapeHtml(detail)}</span>
    </section>
  `;
}

function renderPreopenQuality(report) {
  const quality = state.runtime?.preopen_quality;
  const liveKinds = new Set(["quality", "premarket", "auction", "intraday", "realtime"]);
  if (!quality || !liveKinds.has(report.kind) || String(quality.trading_date || "") !== String(report.date || "")) return "";
  const blockers = Array.isArray(quality.blockers) ? quality.blockers : [];
  const failed = Array.isArray(quality.checks) ? quality.checks.filter((item) => !item.passed) : [];
  const entryBlocked = Boolean(quality.entry_blocked);
  const tone = quality.status === "ok" ? "ok" : (entryBlocked ? "error" : "warn");
  const title = quality.status === "ok"
    ? "盘前系统质检通过"
    : entryBlocked
      ? "盘前关键执行依赖未通过，新增买入关闭"
      : `盘前系统质检存在 ${blockers.length} 项数据阻断`;
  const detail = quality.status === "ok"
    ? `计划源：${quality.plan_source || "-"}；订盘标的：${quality.levels ?? 0}只；检查时间：${quality.checked_at || "-"}`
    : (blockers.length ? blockers.slice(0, 3).map((item) => `${item.name}：${item.detail}`).join("；") : failed.slice(0, 3).map((item) => `${item.name}：${item.detail}`).join("；"));
  return `
    <section class="runtime-integrity runtime-${escapeAttr(tone)}">
      <strong>${escapeHtml(title)}</strong>
      <span>${escapeHtml(detail)}</span>
    </section>
  `;
}

function renderGlobalRiskGate(report) {
  const health = runtimeHealth(report);
  const risk = report.global_risk || health.global_risk;
  if (!risk) return "";
  const policy = risk.policy || {};
  const level = risk.risk_level || "green";
  const riskScope = risk.risk_scope || "broad";
  const technologyOnlyShock = level === "red" && riskScope === "technology";
  const liveKinds = new Set(["quality", "premarket", "auction", "intraday", "realtime"]);
  const qualityEntryBlocked = liveKinds.has(report.kind)
    && String(state.runtime?.preopen_quality?.trading_date || "") === String(report.date || "")
    && Boolean(state.runtime?.preopen_quality?.entry_blocked);
  const label = {
    red: "全球科技负反馈",
    yellow: "隔夜/竞价偏弱",
    green: "全球风险正常",
  }[level] || "全球风险";
  const reasons = (risk.reason || []).slice(0, 4);
  const path = risk.intraday_path || {};
  const localRegime = risk.a_share_market_regime || health.a_share_market_regime || {};
  const localAction = ["transition", "risk_off"].includes(localRegime.state)
    ? "科技链暂缓新增；非科技仅在板块情绪、量能和三周期同步确认后试错"
    : (localRegime.action || "");
  const kospiPath = path.markets?.kospi || {};
  const pathText = path.event_state === "deep_v_recovery"
    ? `盘中路径：韩国最低${formatPolicyNumber(kospiPath.min_pct)}% → 当前${formatPolicyNumber(kospiPath.current_pct)}%，反弹${formatPolicyNumber(kospiPath.rebound_from_low_pct)}%，确认深V`
    : path.event_state === "partial_recovery"
      ? `盘中路径：韩国最低${formatPolicyNumber(kospiPath.min_pct)}% → 当前${formatPolicyNumber(kospiPath.current_pct)}%，部分修复`
      : path.event_state === "shock_unrepaired"
        ? `盘中路径：韩国最低${formatPolicyNumber(kospiPath.min_pct)}%，当前冲击尚未修复`
        : "盘中路径：尚未形成可确认的全球科技反转路径";
  // `can_attack` is a live session flag: it is false before/after continuous
  // auction hours and must not override the report's risk policy on a static
  // report page. Otherwise a stale after-close snapshot falsely says buying is
  // globally disabled the next morning.
  const attackText = qualityEntryBlocked
    ? "新增买入：盘前质检阻断（风险退出仍可执行）"
    : technologyOnlyShock
    ? "新增买入：科技链关闭；非科技须板块情绪、量能和三周期确认"
    : policy.allow_core_attack_buy === false
    ? "新增买入：关闭"
    : "新增买入：按策略确认";
  const localRadarBlocked = ["data_stale"].includes(localRegime.state);
  const radarText = technologyOnlyShock
    ? "全市场机会池：科技链只观察；非科技可严格模拟"
    : policy.allow_market_opportunity_buy === false || localRadarBlocked
    ? "全市场机会池：只观察不买入"
    : "全市场机会池：允许严格模拟";
  const carryText = policy.opening_carry_de_risk_start
    ? `红灯早盘：${policy.opening_carry_de_risk_start}-${policy.opening_carry_de_risk_end || "09:50"}确认后减压`
    : "红灯早盘：按硬防守与结构确认";
  const recoveryText = policy.recovery_overnight_guard
    ? `深V修复隔夜：${policy.recovery_overnight_entry_cutoff || "13:45"}后新增仓上限 ${formatPolicyNumber(policy.recovery_overnight_entry_position_cap_pct)}%，T+1上限 ${formatPolicyNumber(policy.recovery_overnight_t1_cap_pct)}%`
    : "深V修复隔夜：按常规仓位纪律";
  return `
    <section class="global-risk-gate risk-${escapeAttr(level)}">
      <div class="global-risk-main">
        <span class="risk-pill">${escapeHtml(levelIcon(level))} ${escapeHtml(label)}</span>
        <strong>${escapeHtml(policy.notes || "-")}</strong>
        <p>${reasons.map(escapeHtml).join("；") || "暂无风险说明"}</p>
        <p class="global-risk-path">${escapeHtml(pathText)}</p>
        ${localRegime.label ? `<p class="global-risk-path">A股盘中门控：${escapeHtml(localRegime.label)}；${escapeHtml(localAction)}</p>` : ""}
      </div>
      <div class="global-risk-rules">
        <span>${escapeHtml(attackText)}</span>
        <span>${escapeHtml(radarText)}</span>
        <span>${escapeHtml(carryText)}</span>
        <span>${escapeHtml(recoveryText)}</span>
        <span>利润保护：浮盈≥${escapeHtml(formatPolicyNumber(policy.profit_guard_min_pnl_pct))}% 且日跌≤${escapeHtml(formatPolicyNumber(policy.profit_guard_day_loss_pct))}%</span>
        <span>组合风控：仓位≥${escapeHtml(formatPolicyNumber(policy.portfolio_risk_position_pct))}% / 日亏≤${escapeHtml(formatPolicyNumber(policy.portfolio_risk_day_loss_pct))}% / 软破≥${escapeHtml(formatPolicyNumber((policy.portfolio_risk_soft_break_ratio || 0) * 100))}%</span>
      </div>
    </section>
  `;
}

function levelIcon(level) {
  if (level === "red") return "🔴";
  if (level === "yellow") return "🟡";
  return "🟢";
}

function formatPolicyNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(Math.abs(n) >= 10 ? 0 : 1) : "-";
}

function renderEntryAlerts(report) {
  const alerts = buildEntryAlerts(report).slice(0, 10);
  if (!alerts.length) return renderV2WaitStatus(report);
  const green = alerts.filter((x) => x.tone === "green").length;
  const yellow = alerts.filter((x) => x.tone !== "green").length;
  return `
    <section class="section entry-alert-section" id="entryAlerts">
      <div class="section-head">
        <div>
          <h2 class="section-title">V2 执行事件</h2>
          <p class="section-note">只展示通过 120m、15m、Room/RR 与已收盘 5m 门控的 V2 首笔试错；没有固定预挂买入价。</p>
        </div>
        <div class="entry-summary">
          <span>重点 ${green}</span>
          <span>观察 ${yellow}</span>
        </div>
      </div>
      <div class="entry-alert-grid">
        ${alerts.map(renderEntryAlertCard).join("")}
        ${renderV2RejectedAuditCards(report)}
      </div>
    </section>
  `;
}

function v2RejectedOrders(report) {
  return (report.paper_orders || []).filter((order) => (
    String(order.scenario || "").startsWith("V2_E")
    && ["REJECTED", "REJECTED_ALREADY"].includes(String(order.status || ""))
  ));
}

function renderV2RejectedAuditCards(report) {
  const orders = v2RejectedOrders(report).slice(-3).reverse();
  if (!orders.length) return "";
  return orders.map((order) => `
    <article class="entry-card tone-yellow">
      <div class="entry-card-top">
        <div>
          <strong>${escapeHtml(order.name || "-")}</strong>
          <span>${escapeHtml(order.symbol || "")}</span>
        </div>
        <span class="entry-badge">V2 执行否决</span>
      </div>
      <p class="entry-level"><span>信号时间</span><strong>${escapeHtml(order.created_at || "-")}</strong></p>
      <p class="entry-confirm">${escapeHtml(order.reason || "执行层拒绝，原因已写入订单台账")}</p>
      <p class="action-text">审计记录，不是买点。新版本会把量能、VWAP 距离、框架证据和盘前仓位授权前置为 V2 等待条件。</p>
    </article>
  `).join("");
}

const V2_TRACKING_LABELS = {
  DISCOVERED: "已发现",
  QUALIFIED: "已入围",
  SETUP_FORMING: "结构成形",
  NEAR_TRIGGER: "临近触发",
  TRIGGERED: "已触发",
  ORDER_PENDING: "待成交",
  FILLED: "已成交",
  PARTIAL: "部分成交",
  UNFILLED: "未成交",
  DATA_BLOCKED: "数据阻断",
  LIMIT_LOCKED: "涨停锁定",
  EXPIRED: "时段失效",
  MANAGING: "持仓管理",
  EXIT: "退出触发",
  REDUCE: "减仓触发",
};

function v2TrackingState(signal) {
  const stored = String(signal.tracking?.state || signal.tracking_state || "").toUpperCase();
  if (stored) return stored;
  if (signal.external_status === "立即处理") return "TRIGGERED";
  if (signal.external_status === "接近触发") return "NEAR_TRIGGER";
  const setup = String(signal.timing_v2?.setup_15m || "WAIT");
  return setup !== "WAIT" && setup !== "MIXED_SETUP" ? "SETUP_FORMING" : "DISCOVERED";
}

function v2Opportunity(signal) {
  const rating = signal.opportunity_rating || {};
  const grade = String(signal.opportunity_grade || rating.grade || "D").toUpperCase();
  const rawScore = signal.opportunity_score ?? rating.score;
  const score = Number.isFinite(Number(rawScore)) ? Number(rawScore) : null;
  const hard = signal.opportunity_hard_veto || signal.hard_veto || rating.hard_veto || [];
  const hardVeto = Array.isArray(hard) ? hard : (hard ? [{ reason: String(hard) }] : []);
  const gaps = Array.isArray(rating.soft_gaps) ? rating.soft_gaps : (signal.tracking?.gaps || []);
  return { grade, score, hardVeto, gaps };
}

function isV2Executable(signal) {
  const scenario = String(signal.scenario || "");
  const isEntry = scenario.startsWith("V2_E");
  const isExit = ["V2_REDUCE", "V2_TAKE_PROFIT", "V2_STRUCTURAL_EXIT"].includes(scenario);
  const rating = v2Opportunity(signal);
  const contract = signal.signal_contract || {};
  if (!contract.sim_allowed || signal.external_status !== "立即处理") return false;
  if (isEntry) {
    return Boolean(signal.timing_v2?.entry_allowed)
      && ["A", "B"].includes(rating.grade)
      && !rating.hardVeto.length;
  }
  return isExit;
}

function v2ReasonText(item) {
  if (item && typeof item === "object") return item.reason || item.code || item.category || "";
  return String(item || "");
}

function renderV2WaitStatus(report) {
  const signals = state.runtime?.signals;
  const runtime = state.runtime?.runtime_status || {};
  const rejected = v2RejectedOrders(report);
  if (!Array.isArray(signals) && !rejected.length) return "";
  const safeSignals = Array.isArray(signals) ? signals : [];
  const v2Signals = safeSignals.filter((signal) => (
    signal.strategy_family === "V2_ONLY" || String(signal.scenario || "").startsWith("V2_")
  ));
  if (!v2Signals.length && !rejected.length) return "";
  const waits = v2Signals.filter((signal) => !isV2Executable(signal));
  const stateRank = { TRIGGERED: 0, NEAR_TRIGGER: 1, SETUP_FORMING: 2, QUALIFIED: 3, DISCOVERED: 4, DATA_BLOCKED: 5 };
  const visible = [...waits].sort((left, right) => {
    const stateDiff = (stateRank[v2TrackingState(left)] ?? 6) - (stateRank[v2TrackingState(right)] ?? 6);
    return stateDiff || (v2Opportunity(right).score ?? -1) - (v2Opportunity(left).score ?? -1);
  }).slice(0, 6);
  const updatedAt = runtime.last_success_at || state.runtime?.updated_at || "-";
  const status = runtime.engine_status === "ok" ? "引擎正常运行" : `引擎状态：${runtime.engine_status || "未知"}`;
  const stateCounts = v2Signals.reduce((acc, signal) => {
    const key = v2TrackingState(signal);
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});
  const gradeCounts = v2Signals.reduce((acc, signal) => {
    const key = v2Opportunity(signal).grade;
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});
  const dataBlocked = stateCounts.DATA_BLOCKED || 0;
  const cards = visible.map((signal) => {
    const timing = signal.timing_v2 || {};
    const opportunity = v2Opportunity(signal);
    const trackingState = v2TrackingState(signal);
    const hardReasons = opportunity.hardVeto.map(v2ReasonText).filter(Boolean);
    const gapReasons = opportunity.gaps.map(v2ReasonText).filter(Boolean);
    const fallback = Array.isArray(timing.blockers) && timing.blockers.length
      ? timing.blockers
      : (Array.isArray(signal.reasons) ? signal.reasons : ["等待多周期确认"]);
    const nextReasons = [...new Set([...hardReasons, ...gapReasons, ...fallback].filter(Boolean))];
    const distance = signal.tracking?.trigger_distance_pct;
    const distanceText = Number.isFinite(Number(distance)) ? `${Number(distance) >= 0 ? "+" : ""}${Number(distance).toFixed(2)}%` : "未形成可量化触发价";
    const tone = hardReasons.length || trackingState === "DATA_BLOCKED" ? "red" : "yellow";
    return `
      <article class="entry-card tone-${escapeAttr(tone)}">
        <div class="entry-card-top">
          <div>
            <strong>${escapeHtml(signal.name || "-")}</strong>
            <span>${escapeHtml(signal.symbol || "")}</span>
          </div>
          <span class="badge badge-${escapeAttr(tone)}">${escapeHtml(`${opportunity.grade}级｜${V2_TRACKING_LABELS[trackingState] || trackingState}`)}</span>
        </div>
        <p class="entry-level"><span>现价 / 距触发</span><strong>${escapeHtml(`${formatLiveLevel(signal.current_price, signal.current_price)} / ${distanceText}`)}</strong></p>
        <p class="entry-confirm">${escapeHtml(`评级 ${opportunity.score ?? "-"}分｜${timing.regime || "-"} / ${timing.location || "-"}｜15m ${timing.setup_15m || "WAIT"}｜5m ${timing.execution_5m || "WAIT"}`)}</p>
        <p class="action-text">${escapeHtml(`${hardReasons.length ? "硬拦截" : "下一关"}：${nextReasons.slice(0, 2).join("；") || "等待下一根已收盘K线重评"}`)}</p>
      </article>
    `;
  }).join("");
  return `
    <section class="section entry-alert-section" id="entryAlerts">
      <div class="section-head">
        <div>
          <h2 class="section-title">V2 实时执行状态</h2>
          <p class="section-note">${escapeHtml(status)}，最近成功计算 ${escapeHtml(updatedAt)}。候选会持续保留在跟踪漏斗中，只有 A/B 级、无硬拦截且冻结契约授权后才显示为可执行。</p>
        </div>
        <div class="entry-summary">
          <span>可执行 0</span>
          <span>临界 ${stateCounts.NEAR_TRIGGER || 0}</span>
          <span>成形 ${stateCounts.SETUP_FORMING || 0}</span>
          <span>数据阻断 ${dataBlocked}</span>
          <span>A/B ${(gradeCounts.A || 0) + (gradeCounts.B || 0)}</span>
          <span>等待 ${waits.length}</span>
        </div>
      </div>
      <div class="entry-alert-grid">
        ${renderV2RejectedAuditCards(report)}
        ${cards || `<article class="entry-card tone-yellow"><p class="action-text">V2 已完成本轮扫描；当前无候选进入跟踪漏斗，也没有首笔试错、减仓或退出事件。</p></article>`}
      </div>
    </section>
  `;
}

function buildEntryAlerts(report) {
  const signals = state.runtime?.signals;
  if (!Array.isArray(signals)) return [];
  return signals
    .filter((signal) => String(signal.scenario || "").startsWith("V2_E") && isV2Executable(signal))
    .map((signal) => {
      const timing = signal.timing_v2 || {};
      const room = timing.room_risk || {};
      const contract = signal.signal_contract || {};
      const opportunity = v2Opportunity(signal);
      return {
        code: signal.symbol,
        name: signal.name,
        priority: signal.priority || "P1",
        tone: "green",
        label: `${opportunity.grade}级 V2 首笔试错`,
        status: signal.scenario,
        current_price: formatLiveLevel(signal.current_price, signal.current_price),
        trigger_price: `${formatLiveLevel(contract.exec_low ?? room.execution_band_low, signal.current_price)}-${formatLiveLevel(contract.exec_high ?? room.execution_band_high, signal.current_price)}`,
        invalid_price: formatLiveLevel(timing.levels?.entry_invalidation ?? timing.levels?.structural_invalidation, signal.current_price),
        confirm: `${opportunity.score ?? "-"}分｜${V2_TRACKING_LABELS[v2TrackingState(signal)] || v2TrackingState(signal)}｜${timing.regime || "-"}｜${timing.location || "-"}｜${timing.setup_15m || "-"}｜${timing.execution_5m || "-"}`,
        action: signal.action,
        source: "V2 实时引擎",
      };
    });
}

function validPriceText(value) {
  const text = String(value || "").trim();
  if (!text || ["-", "—", "暂无", "无", "None", "null"].includes(text)) return false;
  return Number.isFinite(extractNumber(text));
}

function extractNumber(value) {
  const match = String(value || "").match(/-?\d+(?:\.\d+)?/);
  return match ? Number(match[0]) : NaN;
}

function priceDistance(currentText, triggerText) {
  const current = extractNumber(currentText);
  const trigger = extractNumber(triggerText);
  if (!Number.isFinite(current) || !Number.isFinite(trigger) || current <= 0) return "";
  const distance = (trigger - current) / current * 100;
  return `${distance >= 0 ? "+" : ""}${distance.toFixed(2)}%`;
}

function renderEntryAlertCard(alert) {
  const tone = alert.tone || "yellow";
  const badgeText = alert.label || (tone === "green" ? "重点入场/加仓" : "接近入场观察");
  const distance = alert.distance ? `距触发 ${alert.distance}` : "";
  return `
    <article class="entry-card tone-${escapeAttr(tone)}">
      <div class="entry-card-top">
        <div>
          <strong>${escapeHtml(alert.name || "-")}</strong>
          <span>${escapeHtml(alert.code || "")}</span>
        </div>
        <span class="badge badge-${escapeAttr(tone)}">${escapeHtml(badgeText)}</span>
      </div>
      <div class="entry-price-row">
        <div><span>现价</span><strong>${escapeHtml(alert.current_price || "-")}</strong></div>
        <div class="entry-trigger"><span>触发/加仓</span><strong>${escapeHtml(alert.trigger_price || "-")}</strong></div>
        <div><span>失效</span><strong>${escapeHtml(alert.invalid_price || "-")}</strong></div>
      </div>
      <div class="entry-meta">
        <span>${escapeHtml(alert.priority || "-")}</span>
        <span>${escapeHtml(alert.source || "")}</span>
        ${distance ? `<span>${escapeHtml(distance)}</span>` : ""}
      </div>
      <p class="entry-confirm">${escapeHtml(alert.confirm || "")}</p>
      <p class="action-text">${escapeHtml(alert.action || alert.status || "")}</p>
    </article>
  `;
}

function renderTopbar(report) {
  return `
    <header class="topbar">
      <div class="topbar-inner">
        <div>
          <h1 class="brand-title">A股订盘报告工作台</h1>
          <div class="brand-meta">${escapeHtml(report.date || "未识别日期")} · ${kindLabel(report.kind)} · ${escapeHtml(report.generated || report.published_at || "")}</div>
        </div>
        <div class="toolbar">
          <select class="select" id="reportSelect" aria-label="选择报告">
            ${state.index.map((item) => `<option value="${escapeAttr(item.id)}" ${item.id === report.id ? "selected" : ""}>${escapeHtml(optionLabel(item))}</option>`).join("")}
          </select>
          <button class="button ${state.autoRefresh ? "active" : ""}" id="autoRefreshButton" type="button">${state.autoRefresh ? "自动刷新" : "手动刷新"}</button>
          <button class="button" id="refreshNowButton" type="button">刷新</button>
          <button class="button" id="themeButton" type="button">${state.theme === "light" ? "深色" : "浅色"}</button>
        </div>
        <div class="brand-meta" id="refreshStatus">每 ${Math.round(state.refreshEveryMs / 1000)} 秒检查新报告</div>
      </div>
    </header>
  `;
}

function renderHero(report) {
  const rows = report.rows || [];
  const red = report.counts?.P0 || 0;
  const yellow = report.counts?.P1 || 0;
  const green = report.counts?.P2 || 0;
  const isV2LiveReport = ["intraday", "realtime"].includes(report.kind) && /V2/.test(`${report.title || ""} ${report.raw || ""}`);
  const v2Signals = isV2LiveReport
    ? (state.runtime?.signals || []).filter((signal) => signal.strategy_family === "V2_ONLY" || String(signal.scenario || "").startsWith("V2_"))
    : [];
  const v2Executable = v2Signals.filter((signal) => (
    ["V2_REDUCE", "V2_TAKE_PROFIT", "V2_STRUCTURAL_EXIT"].includes(signal.scenario)
    || (String(signal.scenario || "").startsWith("V2_E") && signal.timing_v2?.entry_allowed)
  )).length;
  const v2Waiting = v2Signals.filter((signal) => ["V2_WAIT", "V2_NO_ADD"].includes(signal.scenario)).length;
  const primary = pickPrimaryConclusion(report);
  return `
    <section class="hero">
      <div class="hero-main">
        <div>
          <div class="hero-kicker">${kindLabel(report.kind)} · ${escapeHtml(report.date || "")}</div>
          <h2 class="hero-title">${escapeHtml(report.title)}</h2>
          <p class="hero-copy">${escapeHtml(primary)}</p>
        </div>
        <div class="kpi-grid">
          ${isV2LiveReport
            ? `${renderKpi("V2标的", v2Signals.length, "")}
               ${renderKpi("V2执行", v2Executable, v2Executable ? "kpi-green" : "kpi-yellow")}
               ${renderKpi("V2等待", v2Waiting, "kpi-yellow")}
               ${renderKpi("模拟成交", report.paper_trade_counts?.total || 0, "kpi-green")}`
            : `${renderKpi("自选股", rows.length, "")}
               ${renderKpi("P0 风险", red, "kpi-red")}
               ${renderKpi("P1 盯盘", yellow, "kpi-yellow")}
               ${renderKpi("P2 观察", green, "kpi-green")}`}
        </div>
      </div>
      <aside class="side-panel">
        <h3 class="side-title">报告元信息</h3>
        <dl class="meta-list">
          ${(report.meta || []).slice(0, 6).map((item) => `
            <div class="meta-row">
              <dt class="meta-label">${escapeHtml(item.label)}</dt>
              <dd class="meta-value">${escapeHtml(item.value)}</dd>
            </div>
          `).join("")}
        </dl>
      </aside>
    </section>
  `;
}

function renderKpi(label, value, cls) {
  return `
    <div class="kpi-card">
      <span class="kpi-label">${escapeHtml(label)}</span>
      <strong class="kpi-value ${cls}">${escapeHtml(String(value ?? "-"))}</strong>
    </div>
  `;
}

function renderMiraGate(report) {
  const gate = report.mira_gate;
  if (!gate) return "";
  const evidenceLog = report.mira_evidence_log || {};
  const sources = gate.source_scope || [];
  const presentSources = sources.filter((x) => x.status === "present");
  const missingSources = sources.filter((x) => x.status !== "present");
  const evidence = gate.evidence_cards || [];
  const gaps = gate.blocking_gaps?.length ? gate.blocking_gaps : ["暂无阻塞缺口；继续按刷新条件复核。"];
  return `
    <section class="mira-gate">
      <div class="section-head">
        <div>
          <h2 class="section-title">证据与刷新门控</h2>
          <p class="section-note">吸收 Mira 研究流程：事实、市场定价、情绪和推断分离；订盘结论必须有刷新边界。</p>
        </div>
        <span class="mira-readiness ${readinessTone(gate.readiness_level)}">${escapeHtml(gate.readiness_label || gate.readiness_level || "-")}</span>
      </div>
      <div class="mira-grid">
        <article class="mira-card mira-card-wide">
          <span class="mira-label">可用性依据</span>
          <strong>${escapeHtml(gate.readiness_basis || "-")}</strong>
          <p>${escapeHtml(gate.action_boundary || "")}</p>
        </article>
        <article class="mira-card">
          <span class="mira-label">时间边界</span>
          <strong>${escapeHtml(gate.time_boundary || report.generated || "-")}</strong>
          <p>过期：${escapeHtml(gate.stale_after || "-")}</p>
        </article>
        <article class="mira-card">
          <span class="mira-label">证据覆盖</span>
          <strong>${escapeHtml(`${presentSources.length}/${sources.length || 0}`)}</strong>
          <p>${presentSources.slice(0, 4).map((x) => escapeHtml(x.label)).join("、") || "未识别"}</p>
        </article>
        <article class="mira-card">
          <span class="mira-label">研究台账</span>
          <strong>${escapeHtml(evidenceLog.total_claims != null ? `${evidenceLog.total_claims} 条 claim` : gate.evidence_log_status || "启发式")}</strong>
          <p>${escapeHtml(evidenceLog.schema || gate.evidence_log_status || "由报告文本推断")}</p>
        </article>
      </div>
      <div class="mira-columns">
        <div class="mira-panel">
          <h3>阻塞缺口</h3>
          <ul>${gaps.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>
        </div>
        <div class="mira-panel">
          <h3>必须刷新</h3>
          <ul>${(gate.must_refresh_if || []).slice(0, 5).map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>
        </div>
        <div class="mira-panel">
          <h3>证据姿态</h3>
          <div class="mira-evidence-list">
            ${evidence.map((item) => `
              <div class="mira-evidence">
                <span class="mira-pill">${escapeHtml(claimTypeLabel(item.claim_type))}</span>
                <strong>${escapeHtml(item.label || "")}</strong>
                <small>${escapeHtml(item.note || "")}</small>
              </div>
            `).join("")}
          </div>
        </div>
        ${renderMiraEvidenceLogPanel(evidenceLog)}
      </div>
      ${missingSources.length ? `<div class="mira-missing">未覆盖：${missingSources.map((x) => escapeHtml(x.label)).join("、")}</div>` : ""}
    </section>
  `;
}

function renderMiraEvidenceLogPanel(evidenceLog) {
  if (!evidenceLog || !evidenceLog.total_claims) return "";
  const treatment = evidenceLog.by_treatment || {};
  const metrics = evidenceLog.signal_metrics || {};
  const openItems = evidenceLog.open_items || [];
  const tradeAttempts = evidenceLog.paper_trade_attempt_count ?? 0;
  const fills = evidenceLog.paper_fill_count ?? 0;
  const rejected = evidenceLog.paper_rejected_count ?? 0;
  const cancelled = evidenceLog.paper_cancel_count ?? 0;
  return `
    <div class="mira-panel mira-panel-wide">
      <h3>每日研究台账</h3>
      <div class="mira-log-kpis">
        <span>claim ${escapeHtml(evidenceLog.total_claims)}</span>
        <span>降权 ${escapeHtml(treatment.haircut || 0)}</span>
        <span>开放项 ${escapeHtml(treatment.open_item || 0)}</span>
        <span>P0有效 ${escapeHtml(metrics.p0_effective || 0)}/${escapeHtml(metrics.p0_total || 0)}</span>
        <span>成交 ${escapeHtml(fills)}/${escapeHtml(tradeAttempts)}</span>
        <span>拦截 ${escapeHtml(rejected)}</span>
        <span>取消计划 ${escapeHtml(cancelled)}</span>
      </div>
      ${openItems.length ? `<ul>${openItems.slice(0, 3).map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>` : ""}
    </div>
  `;
}

function readinessTone(level) {
  if (level === "actionable_with_caveats" || level === "research_ready") return "ready";
  if (level === "working_view") return "working";
  if (level === "needs_refresh" || level === "not_actionable") return "stale";
  return "watch";
}

function claimTypeLabel(type) {
  return {
    fact: "事实",
    reported_metric: "指标",
    market_pricing: "定价",
    sentiment: "情绪",
    derived_calculation: "派生",
    opinion: "观点",
    interpretation: "推断",
    forecast: "预测",
  }[type] || type || "证据";
}

function renderPaperTrades(report) {
  const trades = report.paper_trades || [];
  const counts = report.paper_trade_counts || {};
  const showEmpty = report.kind === "afterclose" || /模拟盘成交/.test(report.raw || "");
  if (!trades.length && !showEmpty) return "";
  return `
    <section class="section" id="paperTrades">
      <div class="section-head">
        <div>
          <h2 class="section-title">今日模拟成交</h2>
          <p class="section-note">只统计盘中可成交的本地模拟订单；A股普通股票按 T+1，今日买入不计入当日可卖。</p>
        </div>
      </div>
      ${trades.length ? renderPaperTradeBody(trades, counts) : renderPaperTradeEmpty()}
    </section>
  `;
}

function isNoHoldingRejectedSell(order) {
  if (!order || order.side !== "SELL") return false;
  if (!["REJECTED", "NO_ORDER"].includes(order.status)) return false;
  const text = [
    order.reason,
    order._discipline_summary,
    order._failed_checks,
  ].filter(Boolean).join(" ");
  return /非模拟盘持仓|必须有可卖数量|无可卖数量/.test(text);
}

function visiblePaperOrders(orders) {
  return (orders || []).filter((order) => !isNoHoldingRejectedSell(order));
}

function paperOrderCountsFromRows(orders) {
  return {
    total: orders.length,
    filled: orders.filter((x) => ["FILLED", "PARTIAL_FILLED"].includes(x.status)).length,
    rejected: orders.filter((x) => x.status === "REJECTED").length,
    cancelled: orders.filter((x) => x.status === "CANCELLED").length,
    buy: orders.filter((x) => x.side === "BUY").length,
    sell: orders.filter((x) => x.side === "SELL").length,
  };
}

function renderPaperOrders(report) {
  const rawOrders = report.paper_orders || [];
  const orders = visiblePaperOrders(rawOrders);
  const counts = paperOrderCountsFromRows(orders);
  const show = report.kind === "realtime" || report.kind === "intraday" || report.kind === "afterclose";
  if (!rawOrders.length && !show) return "";
  return `
    <section class="section" id="paperOrders">
      <div class="section-head">
        <div>
          <h2 class="section-title">模拟订单流水</h2>
          <p class="section-note">展示成交、取消和有意义的纪律拒绝；方向颜色只表示买卖动作，实际盈亏单独按结果显示（盈利红、亏损绿）。</p>
        </div>
      </div>
      ${orders.length ? renderPaperOrderBody(orders, counts) : renderPaperOrderEmpty()}
    </section>
  `;
}

function renderPaperPositions(report) {
  const positions = report.paper_positions || [];
  const counts = report.paper_position_counts || {};
  const showEmpty = report.kind === "realtime" || report.kind === "intraday" || report.kind === "afterclose";
  if (!positions.length && !showEmpty) return "";
  return `
    <section class="section" id="paperPositions">
      <div class="section-head">
        <div>
          <h2 class="section-title">模拟账户持仓</h2>
          <p class="section-note">本地模拟盘账户和成交后的持仓盯市；清仓后仍展示账户表现，买入按 A股 T+1，今日买入不计入当日可卖。</p>
        </div>
      </div>
      ${positions.length ? renderPaperPositionBody(positions, counts) : renderPaperPositionEmpty(counts)}
    </section>
  `;
}

function renderMarketOpportunities(report) {
  const rows = report.market_opportunities || [];
  const counts = report.market_opportunity_counts || {};
  const showEmpty = (report.kind === "intraday" || report.kind === "realtime" || report.kind === "auction") && /全市场|机会池|雷达|候选/.test(report.raw || "");
  if (!rows.length && !showEmpty) return "";
  const title = report.kind === "auction" ? "全市场竞价候选" : report.kind === "premarket" ? "全市场盘前候选" : "全市场机会池";
  const note = report.kind === "auction"
    ? "集合竞价后按题材/板块共振、竞价价量和日线技术形态筛选；9:30 后必须再看开盘价、VWAP 和分钟量能。"
    : report.kind === "premarket"
      ? "盘前根据同花顺专题、政策题材、板块共振和日线技术形态准备候选；盘中未确认不介入。"
      : "只用于持仓票陷入弱势时寻找更强替代观察；必须等待自身回踩 VWAP 不破或放量站回触发价。";
  return `
    <section class="section" id="marketOpportunities">
      <div class="section-head">
        <div>
          <h2 class="section-title">${escapeHtml(title)}</h2>
          <p class="section-note">${escapeHtml(note)}</p>
        </div>
      </div>
      ${rows.length ? renderMarketOpportunityBody(rows, counts, report) : renderMarketOpportunityEmpty()}
    </section>
  `;
}

function renderMarketOpportunityBody(rows, counts, report) {
  const blocked = !marketOpportunityBuyAllowed(report);
  return `
    <div class="opportunity-summary">
      ${renderTradeKpi("候选数量", counts.total ?? rows.length, "")}
      ${renderTradeKpi("可替代观察", counts.green ?? rows.filter((x) => x._tone === "green").length, "positive")}
      ${renderTradeKpi("等待确认", counts.yellow ?? rows.filter((x) => x._tone === "yellow").length, "neutral")}
      ${renderTradeKpi("暂不介入", counts.red ?? rows.filter((x) => x._tone === "red").length, "negative")}
      ${renderTradeKpi("全局阻断", blocked ? rows.length : 0, blocked ? "negative" : "neutral")}
    </div>
    ${blocked ? `<p class="section-gate gate-blocked">当前全局门控禁止模拟买入。候选的绿色仅代表候选质量，不代表获准成交。</p>` : ""}
    <div class="stock-grid opportunity-card-grid">
      ${rows.slice(0, 6).map((row) => renderMarketOpportunityCard(row, report)).join("")}
    </div>
    <div class="table-shell opportunity-shell">
      <table class="stock-table opportunity-table">
        <thead>
          <tr>
            <th>股票</th>
            <th>状态</th>
            <th>模拟门控</th>
            <th>现价</th>
            <th>涨跌</th>
            <th>VWAP</th>
            <th>触发价</th>
            <th>失效价</th>
            <th>执行区</th>
            <th>成交额</th>
            <th>板块情绪/量能</th>
            <th>三轴</th>
            <th>框架依据</th>
            <th>动作</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row) => renderMarketOpportunityRow(row, report)).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function opportunityField(row, keys) {
  for (const key of keys) {
    const value = row[key];
    if (value !== undefined && value !== null && String(value).trim() !== "") return value;
  }
  return "";
}

function renderMarketOpportunityCard(row, report) {
  const code = opportunityField(row, ["机会代码", "code"]);
  const name = opportunityField(row, ["机会名称", "name"]) || "-";
  const tone = row._tone || row.tone || "yellow";
  const status = opportunityField(row, ["雷达状态", "status"]) || "-";
  const current = opportunityField(row, ["现价", "current_price"]);
  const trigger = opportunityField(row, ["触发价", "trigger_price"]);
  const invalid = opportunityField(row, ["失效价", "invalid_price"]);
  const gate = opportunityGateText(row, report);
  const band = opportunityExecutionBand(row);
  const amount = opportunityField(row, ["成交额", "amount_yi"]);
  const axes = opportunityField(row, ["三轴", "axes_text"]);
  const sector = opportunityField(row, ["板块情绪", "sector_emotion"]);
  const volume = opportunityField(row, ["板块/个股量能", "sector_volume"]);
  const focus = opportunityField(row, ["框架依据", "focus"]);
  const action = opportunityField(row, ["动作", "action"]);
  return `
    <article class="stock-card opportunity-card tone-${tone}">
      <div class="stock-card-top">
        <div class="stock-card-title">
          <strong>${escapeHtml(name)}</strong>
          <span>${escapeHtml(code)}</span>
        </div>
        <span class="badge badge-${tone}">${escapeHtml(status)}</span>
      </div>
      <div class="level-grid">
        <div class="level"><span>现价</span><strong>${escapeHtml(current || "-")}</strong></div>
        <div class="level"><span>触发</span><strong>${escapeHtml(trigger || "-")}</strong></div>
        <div class="level"><span>失效</span><strong>${escapeHtml(invalid || "-")}</strong></div>
        <div class="level"><span>成交额</span><strong>${escapeHtml(amount || "-")}</strong></div>
        <div class="level"><span>模拟门控</span><strong>${escapeHtml(gate || "-")}</strong></div>
        <div class="level"><span>执行区</span><strong>${escapeHtml(band || "-")}</strong></div>
      </div>
      <p class="action-text">${escapeHtml(focus || "")}</p>
      <p class="action-text muted">${escapeHtml([sector, volume ? `1m/5m ${volume}` : ""].filter(Boolean).join("；"))}</p>
      <p class="action-text muted">${escapeHtml(axes || "")}</p>
      <p class="action-text muted">${escapeHtml(action || "")}</p>
    </article>
  `;
}

function renderMarketOpportunityEmpty() {
  return `
    <div class="empty-card opportunity-empty">
      <strong>暂无满足框架门槛的全市场替代机会。</strong>
      <span>这不是坏事：持仓弱时先处理风险，没有更强替代就空等。</span>
    </div>
  `;
}

function renderMarketOpportunityRow(row, report) {
  const code = row["机会代码"] || row.code || "";
  const name = row["机会名称"] || row.name || "-";
  const tone = row._tone || row.tone || "yellow";
  const gate = opportunityGateText(row, report);
  const band = opportunityExecutionBand(row);
  return `
    <tr class="stock-row tone-${tone}">
      <td>
        <span class="name">${escapeHtml(name)}</span>
        <span class="muted trade-scenario">${escapeHtml(code)}</span>
      </td>
      <td><span class="badge badge-${tone}">${escapeHtml(row["雷达状态"] || row.status || "-")}</span></td>
      <td>${escapeHtml(row["模拟门控"] || gate || "-")}</td>
      <td><span class="num">${escapeHtml(row["现价"] || formatPrice(row.current_price))}</span></td>
      <td><span class="num">${escapeHtml(row["涨跌"] || formatPercent(row.pct))}</span></td>
      <td><span class="num">${escapeHtml(row["VWAP"] || formatPrice(row.vwap))}</span></td>
      <td><span class="num">${escapeHtml(row["触发价"] || formatPrice(row.trigger_price))}</span></td>
      <td><span class="num">${escapeHtml(row["失效价"] || formatPrice(row.invalid_price))}</span></td>
      <td><span class="num">${escapeHtml(band || "-")}</span></td>
      <td><span class="num">${escapeHtml(row["成交额"] || (row.amount_yi != null ? `${Number(row.amount_yi).toFixed(1)}亿` : "-"))}</span></td>
      <td>${escapeHtml([row["板块情绪"] || row.sector_emotion || "", row["板块/个股量能"] || row.sector_volume || ""].filter(Boolean).join("；"))}</td>
      <td>${escapeHtml(row["三轴"] || row.axes_text || "")}</td>
      <td>${escapeHtml(row["框架依据"] || row.focus || "")}</td>
      <td>${escapeHtml(row["动作"] || row.action || "")}</td>
    </tr>
  `;
}

function marketOpportunityBuyAllowed(report) {
  const health = runtimeHealth(report);
  const risk = report?.global_risk || health.global_risk || {};
  if (risk.risk_level === "red" && risk.risk_scope === "technology") return true;
  const policy = report?.global_risk?.policy || health.global_risk?.policy || {};
  return policy.allow_market_opportunity_buy !== false;
}

function opportunityGateText(row, report) {
  if (row.market_opportunity_gate_ok === true) {
    return row.market_opportunity_gate_stage === "SECTOR_ROTATION_ALLOWED"
      ? row.market_opportunity_gate_reason || "板块轮动确认"
      : "可模拟";
  }
  if (row.market_opportunity_gate_ok === false) {
    return row.market_opportunity_gate_reason || "等待确认";
  }
  if (!marketOpportunityBuyAllowed(report)) return "全局门控阻断：只观察";
  if (row.radar_gate_ok === true) return "可模拟";
  if (row.radar_gate_ok === false) return row.radar_gate_reason || "等待确认";
  return row["模拟门控"] || "";
}

function opportunityExecutionBand(row) {
  const low = row.execution_band_low;
  const high = row.execution_band_high;
  if (low != null || high != null) return `${formatPrice(low)}-${formatPrice(high)}`;
  return row["执行区"] || "";
}

function renderPaperTradeBody(trades, counts) {
  return `
    <div class="trade-summary">
      ${renderTradeKpi("成交笔数", counts.total ?? trades.length, "")}
      ${renderTradeKpi("买入", counts.buy ?? trades.filter((x) => x._side_label === "买入").length, "buy")}
      ${renderTradeKpi("卖出", counts.sell ?? trades.filter((x) => x._side_label === "卖出").length, "sell")}
      ${renderTradeKpi("15分钟均表现", formatSignedPct(counts.avg_pnl15), toneClassFromNumber(counts.avg_pnl15))}
    </div>
    <div class="table-shell trade-shell">
      <table class="stock-table trade-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>股票</th>
            <th>方向</th>
            <th>数量</th>
            <th>成交价</th>
            <th>5分钟表现</th>
            <th>15分钟表现</th>
            <th>交易质量</th>
            <th>复盘结论</th>
          </tr>
        </thead>
        <tbody>
          ${trades.map(renderPaperTradeRow).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderPaperOrderBody(orders, counts) {
  const latest = [...orders].slice(-24).reverse();
  return `
    <div class="trade-summary">
      ${renderTradeKpi("订单", counts.total ?? orders.length, "")}
      ${renderTradeKpi("成交", counts.filled ?? orders.filter((x) => ["FILLED", "PARTIAL_FILLED"].includes(x.status)).length, "sell")}
      ${renderTradeKpi("拒绝", counts.rejected ?? orders.filter((x) => x.status === "REJECTED").length, "watch")}
      ${renderTradeKpi("取消", counts.cancelled ?? orders.filter((x) => x.status === "CANCELLED").length, "watch")}
      ${renderTradeKpi("买入信号", counts.buy ?? orders.filter((x) => x.side === "BUY").length, "buy")}
      ${renderTradeKpi("卖出信号", counts.sell ?? orders.filter((x) => x.side === "SELL").length, "sell")}
    </div>
    <div class="table-shell trade-shell">
      <table class="stock-table trade-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>股票</th>
            <th>方向</th>
            <th>状态</th>
            <th>数量</th>
            <th>信号价</th>
            <th>成交价</th>
            <th>场景</th>
            <th>实际盈亏</th>
            <th>原因/纪律</th>
          </tr>
        </thead>
        <tbody>
          ${latest.map(renderPaperOrderRow).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderPaperOrderEmpty() {
  return `
    <div class="empty-card trade-empty">
      <strong>今日暂无模拟订单流水。</strong>
      <span>只有信号进入执行层或被纪律门控拒绝后，才会出现在这里。</span>
    </div>
  `;
}

function orderStatusLabel(status) {
  return {
    FILLED: "成交",
    PARTIAL_FILLED: "部分成交",
    REJECTED: "拒绝",
    CANCELLED: "取消",
    NO_ORDER: "无订单",
  }[status] || status || "-";
}

function orderTone(order) {
  if (order._tone) return order._tone;
  if (["FILLED", "PARTIAL_FILLED"].includes(order.status)) return order.side === "BUY" ? "red" : "green";
  if (order.status === "REJECTED" || order.status === "CANCELLED") return "yellow";
  return "gray";
}

function renderPaperOrderRow(order) {
  const tone = orderTone(order);
  const name = `${order.name || "-"} ${order.symbol || ""}`.trim();
  const discipline = order._discipline_summary || order.reason || "";
  const failed = order._failed_checks ? `｜${order._failed_checks}` : "";
  const realized = parseNumber(order.realized_pnl ?? order["实际盈亏"]);
  const realizedCell = realized !== null
    ? formatTradePnl(formatMoneySigned(realized), realized)
    : (order.side === "SELL" && ["FILLED", "PARTIAL_FILLED"].includes(order.status)
      ? `<span class="muted">待归因</span>`
      : `<span class="muted">-</span>`);
  return `
    <tr class="stock-row tone-${tone}">
      <td>${escapeHtml(order.created_at || "-")}</td>
      <td><span class="name">${escapeHtml(name)}</span></td>
      <td><span class="badge badge-${tone}">${escapeHtml(order._side_label || sideText(order.side))}</span></td>
      <td><span class="badge badge-${tone}">${escapeHtml(orderStatusLabel(order.status))}</span></td>
      <td><span class="num">${escapeHtml(order.qty ?? "-")}</span></td>
      <td><span class="num">${escapeHtml(formatPrice(order.signal_price))}</span></td>
      <td><span class="num">${escapeHtml(formatPrice(order.fill_price))}</span></td>
      <td>${escapeHtml(order.scenario || "-")}</td>
      <td>${realizedCell}</td>
      <td>${escapeHtml(`${discipline}${failed}`)}</td>
    </tr>
  `;
}

function sideText(side) {
  if (side === "BUY") return "买入";
  if (side === "SELL") return "卖出";
  if (side === "CANCEL") return "取消";
  return side || "-";
}

function renderPaperPositionBody(positions, counts) {
  const sortedPositions = sortPaperPositions(positions);
  return `
    ${renderPaperAccountKpis(counts, positions.length)}
    <div class="table-shell trade-shell">
      <table class="stock-table trade-table">
        <thead>
          <tr>
            ${paperPositionHeaders().map(renderPaperPositionHeader).join("")}
          </tr>
        </thead>
        <tbody>
          ${sortedPositions.map(renderPaperPositionRow).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderPaperAccountKpis(counts, fallbackPositionCount = 0) {
  const basisNote = counts.day_pnl_basis_note || (counts.day_pnl_source === "account_total_assets"
    ? "口径：总资产较上一交易日收盘变化"
    : "口径：无上一交易日账户快照，按持仓表现估算");
  return `
    <div class="trade-summary trade-summary-account">
      ${renderTradeKpi("持仓数", counts.total ?? fallbackPositionCount, "")}
      ${renderTradeKpi("总资产", formatMoney(counts.total_assets ?? counts.total_amount ?? counts.market_value), "")}
      ${renderTradeKpi("可用现金", formatMoney(counts.cash), "")}
      ${renderTradeKpi("持仓市值", formatMoney(counts.market_value), "")}
      ${renderTradeKpi("仓位", formatSignedPct(counts.position_pct).replace("+", ""), "")}
    </div>
    <div class="trade-summary trade-summary-performance">
      ${renderTradeKpi("账户当日盈亏", formatMoneySigned(counts.day_pnl), toneClassFromNumber(counts.day_pnl))}
      ${renderTradeKpi("账户当日收益率", formatSignedPct(counts.day_pnl_pct), toneClassFromNumber(counts.day_pnl_pct))}
      ${renderTradeKpi("持仓日盈亏", formatMoneySigned(counts.position_day_pnl), toneClassFromNumber(counts.position_day_pnl))}
      ${renderTradeKpi("持仓日收益", formatSignedPct(counts.position_day_pnl_pct), toneClassFromNumber(counts.position_day_pnl_pct))}
      ${renderTradeKpi("浮盈亏", formatMoneySigned(counts.unrealized_pnl), toneClassFromNumber(counts.unrealized_pnl))}
      ${renderTradeKpi("持仓收益率", formatSignedPct(counts.unrealized_pnl_pct), toneClassFromNumber(counts.unrealized_pnl_pct))}
    </div>
    <div class="account-basis-note">${escapeHtml(basisNote)}</div>
  `;
}

function paperPositionHeaders() {
  return [
    { key: "stock", label: "股票", type: "text" },
    { key: "quantity", label: "持仓", type: "number" },
    { key: "sellable", label: "可卖", type: "number" },
    { key: "cost", label: "成本", type: "number" },
    { key: "price", label: "现价", type: "number" },
    { key: "marketValue", label: "市值", type: "number" },
    { key: "dayPnl", label: "当日盈亏", type: "number" },
    { key: "dayPnlPct", label: "当日收益率", type: "number" },
    { key: "pnl", label: "浮盈亏", type: "number" },
    { key: "pnlPct", label: "收益率", type: "number" },
    { key: "holdingDays", label: "持仓天数", type: "number" },
  ];
}

function renderPaperPositionHeader(header) {
  const active = state.paperPositionSort.key === header.key;
  const direction = active ? state.paperPositionSort.direction : "";
  const arrow = active ? (direction === "asc" ? "↑" : "↓") : "↕";
  return `
    <th class="${active ? "is-sorted" : ""}">
      <button
        class="table-sort-button"
        type="button"
        data-paper-position-sort="${escapeAttr(header.key)}"
        aria-label="按${escapeAttr(header.label)}排序"
        aria-sort="${active ? (direction === "asc" ? "ascending" : "descending") : "none"}"
      >
        <span>${escapeHtml(header.label)}</span>
        <span class="sort-indicator">${escapeHtml(arrow)}</span>
      </button>
    </th>
  `;
}

function sortPaperPositions(positions) {
  const sort = state.paperPositionSort || {};
  if (!sort.key) return positions;
  const direction = sort.direction === "asc" ? 1 : -1;
  return [...positions].sort((a, b) => {
    const av = paperPositionSortValue(a, sort.key);
    const bv = paperPositionSortValue(b, sort.key);
    if (typeof av === "string" || typeof bv === "string") {
      return String(av ?? "").localeCompare(String(bv ?? ""), "zh-Hans-CN", { numeric: true }) * direction;
    }
    const an = Number.isFinite(av) ? av : -Infinity;
    const bn = Number.isFinite(bv) ? bv : -Infinity;
    if (an === bn) {
      return String(paperPositionSortValue(a, "stock")).localeCompare(String(paperPositionSortValue(b, "stock")), "zh-Hans-CN", { numeric: true });
    }
    return (an - bn) * direction;
  });
}

function paperPositionSortValue(row, key) {
  if (key === "stock") return `${row["名称"] || row.name || ""} ${row["代码"] || row.symbol || ""}`.trim();
  if (key === "quantity") return firstNumeric(row, ["_qty", "持仓", "quantity"]);
  if (key === "sellable") return firstNumeric(row, ["_sellable", "可卖", "sellable"]);
  if (key === "cost") return firstNumeric(row, ["_avg_cost", "成本", "avg_cost"]);
  if (key === "price") return firstNumeric(row, ["_last_price", "现价", "last_price", "current_price"]);
  if (key === "marketValue") return firstNumeric(row, ["_market_value", "市值", "market_value"]);
  if (key === "dayPnl") return firstNumeric(row, ["_day_pnl", "当日盈亏", "day_pnl"]);
  if (key === "dayPnlPct") return firstNumeric(row, ["_day_pnl_pct", "当日收益率", "day_pnl_pct"]);
  if (key === "pnl") return firstNumeric(row, ["_unrealized_pnl", "浮盈亏", "unrealized_pnl"]);
  if (key === "pnlPct") return firstNumeric(row, ["_unrealized_pnl_pct", "收益率", "unrealized_pnl_pct"]);
  if (key === "holdingDays") return firstNumeric(row, ["_holding_days", "持仓天数", "holding_days"]);
  return null;
}

function firstNumeric(row, keys) {
  for (const key of keys) {
    const value = row?.[key];
    if (value === undefined || value === null || value === "") continue;
    const parsed = parseNumber(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function renderPaperPositionEmpty(counts = {}) {
  const hasAccount = counts.total_assets != null || counts.cash != null || counts.day_pnl != null;
  return `
    ${hasAccount ? renderPaperAccountKpis(counts, 0) : ""}
    <div class="empty-card trade-empty">
      <strong>模拟账户暂无持仓。</strong>
      <span>今日若已完成卖出，会在“今日模拟成交”和“模拟订单流水”中保留记录；账户总资产和当日盈亏仍在上方持续跟踪。</span>
    </div>
  `;
}

function renderPaperPositionRow(row) {
  const name = `${row["名称"] || row.name || "-"} ${row["代码"] || row.symbol || ""}`.trim();
  const pnl = firstNumeric(row, ["_unrealized_pnl", "unrealized_pnl", "浮盈亏"]);
  const pnlPct = firstNumeric(row, ["_unrealized_pnl_pct", "unrealized_pnl_pct", "收益率"]);
  const dayPnl = firstNumeric(row, ["_day_pnl", "day_pnl", "当日盈亏"]);
  const dayPnlPct = firstNumeric(row, ["_day_pnl_pct", "day_pnl_pct", "当日收益率"]);
  return `
    <tr class="stock-row tone-${row._tone || toneClassFromNumber(pnl)}">
      <td><span class="name">${escapeHtml(name)}</span></td>
      <td><span class="num">${escapeHtml(row["持仓"] || row.quantity || "-")}</span></td>
      <td><span class="num">${escapeHtml(row["可卖"] || row.sellable || "0")}</span></td>
      <td><span class="num">${escapeHtml(row["成本"] || formatPrice(row.avg_cost))}</span></td>
      <td><span class="num">${escapeHtml(row["现价"] || formatPrice(row.last_price))}</span></td>
      <td><span class="num">${escapeHtml(row["市值"] || formatMoney(row.market_value))}</span></td>
      <td>${formatTradePnl(formatMoneySigned(dayPnl), dayPnl)}</td>
      <td>${formatTradePnl(formatSignedPct(dayPnlPct), dayPnlPct)}</td>
      <td>${formatTradePnl(formatMoneySigned(pnl), pnl)}</td>
      <td>${formatTradePnl(formatSignedPct(pnlPct), pnlPct)}</td>
      <td><span class="num">${escapeHtml(row["持仓天数"] || (row.holding_days != null ? `${row.holding_days}天` : "-"))}</span></td>
    </tr>
  `;
}

function renderPaperTradeEmpty() {
  return `
    <div class="empty-card trade-empty">
      <strong>今日无模拟成交。</strong>
      <span>信号若只停留在观察、接近触发、盘外时段或无可卖数量，会进入复盘记录，但不会算作成交。</span>
    </div>
  `;
}

function renderTradeKpi(label, value, tone) {
  return `
    <div class="trade-kpi">
      <span>${escapeHtml(label)}</span>
      <strong class="${tone ? `trade-${tone}` : ""}">${escapeHtml(String(value ?? "-"))}</strong>
    </div>
  `;
}

function renderPaperTradeRow(row) {
  const name = `${row["名称"] || row.name || "-"} ${row["代码"] || row.symbol || ""}`.trim();
  const side = row._side_label || row["方向"] || "-";
  const sideTone = side === "买入" ? "buy" : side === "卖出" ? "sell" : "cancel";
  return `
    <tr class="stock-row tone-${row._tone || "yellow"}">
      <td><span class="num">${escapeHtml(row["时间"] || row.created_at || "-")}</span></td>
      <td>
        <span class="name">${escapeHtml(name)}</span>
        <span class="muted trade-scenario">${escapeHtml(row["场景"] || row.scenario || "")}</span>
      </td>
      <td><span class="badge trade-side trade-side-${sideTone}">${escapeHtml(side)}</span></td>
      <td><span class="num">${escapeHtml(row["数量"] || row.qty || "-")}</span></td>
      <td><span class="num">${escapeHtml(row["成交价"] || row.fill_price || "-")}</span></td>
      <td>${formatTradePnl(row._pnl5m_text, row._pnl5m)}</td>
      <td>${formatTradePnl(row._pnl15m_text, row._pnl15m)}</td>
      <td><span class="badge badge-${row._tone || "yellow"}">${escapeHtml(row["交易质量"] || "-")}</span></td>
      <td>${escapeHtml(row["复盘结论"] || "")}</td>
    </tr>
  `;
}

function formatTradePnl(text, value) {
  if (!text) return `<span class="muted">-</span>`;
  return `<span class="trade-pnl trade-${toneClassFromNumber(value)}">${escapeHtml(text)}</span>`;
}

function parseNumber(value) {
  const n = Number(String(value ?? "").replace(/[,%]/g, ""));
  return Number.isFinite(n) ? n : null;
}

function formatMoney(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  return n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatMoneySigned(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  return `${n > 0 ? "+" : ""}${formatMoney(n)}`;
}

function toneClassFromNumber(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "neutral";
  if (num > 0) return "positive";
  if (num < 0) return "negative";
  return "neutral";
}

function formatSignedPct(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return `${num >= 0 ? "+" : ""}${num.toFixed(2)}%`;
}

function formatPrice(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(2) : "-";
}

function formatPercent(value) {
  const num = Number(value);
  return Number.isFinite(num) ? `${num.toFixed(2)}%` : "-";
}

function renderStockSection(report) {
  if (!report.rows?.length) {
    return `<section class="section"><div class="empty-card">这份报告没有识别到个股操作表。</div></section>`;
  }
  const rows = report.rows;
  const headers = usefulHeaders(report.headers);
  return `
    <section class="section" id="stocks">
      <div class="section-head">
        <div>
          <h2 class="section-title">核心操作表</h2>
          <p class="section-note">盘前框架先确定当日计划仓位；V2.0只在盘中确认120m/15m/5m择时与退出。价格均为条件参考，不是机械买卖指令。</p>
        </div>
      </div>
      <div class="filters">
        ${filterButton("all", "全部", rows.length)}
        ${filterButton("P0", "P0 风险", rows.filter((r) => r._priority === "P0").length)}
        ${filterButton("P1", "P1 盯盘", rows.filter((r) => r._priority === "P1").length)}
        ${filterButton("P2", "P2 观察", rows.filter((r) => r._priority === "P2").length)}
        <input class="search" id="stockSearch" placeholder="搜索代码、名称、题材或建议" autocomplete="off">
      </div>
      <div class="table-shell">
        <table class="stock-table">
          <thead><tr>${headers.map((h) => `<th>${escapeHtml(displayHeader(h))}</th>`).join("")}</tr></thead>
          <tbody>
            ${rows.map((row) => renderStockRow(row, headers)).join("")}
          </tbody>
        </table>
      </div>
    </section>
  `;
}

function usefulHeaders(headers) {
  const preferred = ["代码", "名称", "现价", "当前价", "收盘", "涨跌", "涨跌幅", "开盘", "状态", "操作指令", "优先级", "当日计划", "目标仓位", "单票上限", "V2单次", "VWAP", "日内强弱线", "支撑", "防守/止损", "防守", "风险减仓价", "第一反抽减仓价", "反抽减仓价", "条件加仓价", "加仓价", "减仓价", "失效价", "趋势防守", "趋势修复", "趋势压力", "修复", "修复/加仓条件", "压力", "压力/减仓", "同花顺专题映射", "专题", "社区温度", "盘前建议", "建议"];
  const out = preferred.filter((h) => headers.includes(h));
  return out.length ? out : headers.slice(0, 10);
}

function displayHeader(header) {
  return header
    .replace("当前价", "现价")
    .replace("同花顺专题映射", "专题")
    .replace("修复/加仓条件", "加仓条件")
    .replace("压力/减仓", "减仓条件")
    .replace("风险减仓价", "风险减仓")
    .replace("第一反抽减仓价", "第一反抽")
    .replace("反抽减仓价", "反抽减仓")
    .replace("条件加仓价", "条件加仓")
    .replace("V2单次", "V2单次试仓");
}

function renderStockRow(row, headers) {
  return `
    <tr class="stock-row tone-${row._tone}" data-priority="${escapeAttr(row._priority)}" data-search="${escapeAttr(row._search || "")}">
      ${headers.map((header) => `<td>${formatCell(header, row[header], row)}</td>`).join("")}
    </tr>
  `;
}

function formatCell(header, value, row) {
  const text = value ?? "";
  if (header === "代码") return `<span class="code">${escapeHtml(text)}</span>`;
  if (header === "名称") return `<span class="name">${escapeHtml(text)}</span>`;
  if (["压力", "压力/减仓", "减仓价", "风险减仓价", "第一反抽减仓价", "反抽减仓价"].includes(header)) {
    return `<span class="num">${escapeHtml(pressureCellText(header, row, text))}</span>`;
  }
  if (["现价", "当前价", "收盘", "涨跌", "涨跌幅", "开盘", "目标仓位", "单票上限", "V2单次", "支撑", "防守/止损", "防守", "修复", "压力", "加仓价", "减仓价", "修复/加仓条件", "压力/减仓", "VWAP", "日内强弱线", "风险减仓价", "第一反抽减仓价", "反抽减仓价", "条件加仓价", "失效价", "趋势防守", "趋势修复", "趋势压力"].includes(header)) {
    return `<span class="num">${escapeHtml(text)}</span>`;
  }
  if (header === "状态" || header === "优先级" || header === "操作指令") {
    return `<span class="badge badge-${row._tone}">${escapeHtml(text || row._priority)}</span>`;
  }
  return escapeHtml(text);
}

function renderFocusCards(report) {
  const sourceRows = focusCardSourceRows(report);
  const rankedRows = sourceRows.map((row, index) => ({
    row,
    index,
    priority: normalizedPriority(row),
  }));
  const focus = rankedRows
    .filter((item) => item.priority === "P0" || item.priority === "P1")
    .sort(compareFocusPriority)
    .slice(0, 9);
  const fallback = !focus.length;
  const cards = (fallback
    ? rankedRows.filter((item) => item.priority === "P2").slice(0, 6)
    : focus
  ).map((item) => ({ ...item.row, _priority: item.priority }));
  if (!cards.length) {
    return `
      <section class="section" id="focus">
        <div class="section-head">
          <div>
            <h2 class="section-title">重点盯盘卡片</h2>
            <p class="section-note">当前报告未返回可用的重点订盘数据；请检查实时引擎快照。</p>
          </div>
        </div>
      </section>
    `;
  }
  return `
    <section class="section" id="focus">
      <div class="section-head">
        <div>
          <h2 class="section-title">重点盯盘卡片</h2>
          <p class="section-note">${fallback ? "当前无 P0/P1，展示最接近触发的 P2 观察项。" : "把价位、状态和动作压缩到一屏内，适合盘中快速扫读。"}</p>
        </div>
      </div>
      <div class="stock-grid">
        ${cards.map(renderStockCard).join("")}
      </div>
    </section>
  `;
}

function focusCardSourceRows(report) {
  const liveSignals = state.runtime?.signals;
  if (!Array.isArray(liveSignals) || !liveSignals.length) return report.rows || [];
  return liveSignals.map(liveSignalCardRow);
}

function liveSignalCardRow(signal) {
  const priority = String(signal.priority || "P2").toUpperCase();
  const scenario = String(signal.scenario || "");
  const timing = signal.timing_v2 || {};
  const levelsV2 = timing.levels || {};
  const roomV2 = timing.room_risk || {};
  const marketData = timing.data_quality?.market_data || {};
  const opportunity = v2Opportunity(signal);
  const trackingState = v2TrackingState(signal);
  const contract = signal.signal_contract || {};
  const executionAuthorized = isV2Executable(signal);
  const hasTiming = Boolean(timing.version || timing.mode || timing.config_hash);
  const sourceState = marketData.history_source
    ? `｜行情 ${marketData.history_source}${marketData.status === "FULL" ? "已核验" : "降级"}`
    : "";
  const timingState = !hasTiming
    ? "等待下一次实时刷新写入V2状态"
    : timing.entry_allowed
      ? `${timing.mode || "V2"}｜${timing.regime || "-"}｜${timing.execution_5m || "-"}${sourceState}`
      : `等待｜${(timing.blockers || timing.reasons || ["V2多周期确认未完成"]).slice(0, 2).join("；")}${sourceState}`;
  const levels = [
    ["现价", signal.current_price],
    ["机会评级", `${opportunity.grade}级 ${opportunity.score ?? "-"}分`],
    ["跟踪阶段", V2_TRACKING_LABELS[trackingState] || trackingState],
    ["V2位置", timing.location || "等待"],
    ["15m Setup", timing.setup_15m || "WAIT"],
    ["5m执行", timing.execution_5m || "FAILURE"],
    ["入场失效", levelsV2.entry_invalidation ?? levelsV2.structural_invalidation],
    ["硬防守", levelsV2.structural_invalidation],
    ["上方阻力", levelsV2.nearest_resistance],
  ].map(([label, value]) => ({
    label,
    value: ["机会评级", "跟踪阶段", "V2位置", "15m Setup", "5m执行"].includes(label) ? String(value || "-") : formatLiveLevel(value, signal.current_price),
  }));
  const hardReasons = opportunity.hardVeto.map(v2ReasonText).filter(Boolean);
  const gapReasons = opportunity.gaps.map(v2ReasonText).filter(Boolean);
  const nextGate = hardReasons[0] || gapReasons[0] || (timing.blockers || [])[0] || "等待下一根已收盘K线重评";
  return {
    "代码": signal.symbol,
    "名称": signal.name,
    "状态": `${V2_TRACKING_LABELS[trackingState] || trackingState}｜${opportunity.grade}级`,
    "场景": scenario,
    "操作": signal.action,
    "确认": `${signal.confirm_rule || "-"}；多周期：${timingState}；下一关：${nextGate}`,
    "取消": signal.cancel_rule,
    "专题": signal.focus,
    "现价": formatLiveLevel(signal.current_price, signal.current_price),
    "V2模式": timing.mode || "等待刷新",
    "V2位置": timing.location || "-",
    "15m Setup": timing.setup_15m || "WAIT",
    "5m执行": timing.execution_5m || "FAILURE",
    "入场失效": formatLiveLevel(levelsV2.entry_invalidation ?? levelsV2.structural_invalidation, signal.current_price),
    "硬防守": formatLiveLevel(levelsV2.structural_invalidation, signal.current_price),
    "上方阻力": formatLiveLevel(levelsV2.nearest_resistance, signal.current_price),
    "Room/RR": `RoomATR ${roomV2.room_atr ?? "-"} / RR ${roomV2.reward_risk ?? "-"}`,
    "机会评级": `${opportunity.grade}级 ${opportunity.score ?? "-"}分`,
    "跟踪阶段": V2_TRACKING_LABELS[trackingState] || trackingState,
    "执行契约": executionAuthorized
      ? `已授权｜${formatLiveLevel(contract.exec_low, signal.current_price)}-${formatLiveLevel(contract.exec_high, signal.current_price)}`
      : `未授权｜${contract.sim_reason || nextGate}`,
    "加仓状态": !hasTiming
      ? "等待实时刷新V2状态"
      : executionAuthorized
        ? "V2冻结契约已授权"
        : `${opportunity.grade}级｜${V2_TRACKING_LABELS[trackingState] || trackingState}`,
    "加仓确认": `${signal.confirm_rule || "-"}；${timingState}`,
    "加仓取消": signal.cancel_rule,
    "操作指令": signal.execution_action || signal.internal_state || signal.external_status,
    "_priority": priority,
    "_tone": priority === "P0" ? "red" : priority === "P1" ? "yellow" : "green",
    "_live_levels": levels,
  };
}

function firstFinite(...values) {
  for (const value of values) {
    const num = Number(value);
    if (Number.isFinite(num) && num > 0) return num;
  }
  return null;
}

function formatLiveLevel(value, referencePrice) {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return "策略未启用";
  const decimals = Number(referencePrice) > 0 && Number(referencePrice) < 10 ? 3 : 2;
  return num.toFixed(decimals);
}

function normalizedPriority(row) {
  const raw = firstValue(row, ["_priority", "优先级", "priority"]);
  const match = String(raw || "").toUpperCase().match(/P[012]/);
  return match ? match[0] : "P2";
}

function compareFocusPriority(left, right) {
  const score = { P0: 0, P1: 1, P2: 2 };
  return (score[left.priority] ?? 3) - (score[right.priority] ?? 3) || left.index - right.index;
}

function firstValue(row, keys) {
  for (const key of keys) {
    const value = row[key];
    if (value !== undefined && value !== null && String(value).trim() !== "") return value;
  }
  return "";
}

function buildCardAction(row) {
  const parts = [];
  const add = firstValue(row, ["加仓触发", "条件加仓价", "加仓价", "修复/加仓条件", "修复", "趋势修复"]);
  const reduce = executableReduceText(row);
  const stop = firstValue(row, ["失效价", "防守/止损", "防守/止损（硬失效）", "防守", "趋势防守"]);
  if (add) parts.push(`加仓：${add}`);
  if (reduce) parts.push(`减仓：${reduce}`);
  if (stop) parts.push(`失效：${stop}`);
  return parts.join("；");
}

function nextSessionLimitUp(row) {
  const close = extractNumber(firstValue(row, ["收盘", "现价", "当前价"]));
  const code = String(row["代码"] || "");
  const name = String(row["名称"] || "").toUpperCase();
  if (!Number.isFinite(close) || close <= 0 || !code) return NaN;
  const rate = name.includes("ST") ? 0.05 : (code.startsWith("30") || code.startsWith("68")) ? 0.20 : 0.10;
  return Math.round(close * (1 + rate) * 100) / 100;
}

function cardPressure(row) {
  const rawText = firstValue(row, ["第一反抽减仓价", "反抽减仓价", "压力/减仓", "减仓价", "压力", "趋势压力"]);
  const raw = extractNumber(rawText);
  const suppliedKind = firstValue(row, ["压力说明", "压力类型"]);
  const suppliedLimit = extractNumber(firstValue(row, ["涨停边界", "当日涨停边界"]));
  const limitUp = Number.isFinite(suppliedLimit) ? suppliedLimit : nextSessionLimitUp(row);
  if (Number.isFinite(raw) && Number.isFinite(limitUp) && raw > limitUp + 0.005) {
    return {
      label: "涨停边界",
      value: formatPrice(limitUp),
      note: `趋势压力 ${formatPrice(raw)} 为跨日参考`,
    };
  }
  return {
    label: suppliedKind || "压力/减仓",
    value: rawText || "-",
    note: "",
  };
}

function pressureCellText(header, row, fallback) {
  const pressure = cardPressure(row);
  if (!pressure.note) return fallback;
  if (header === "压力") return `${pressure.label} ${pressure.value}`;
  const stop = firstValue(row, ["失效价", "防守/止损", "防守", "趋势防守"]);
  return `${pressure.label} ${pressure.value} 不追${stop ? `；跌破 ${stop} 再减仓` : ""}`;
}

function executableReduceText(row) {
  const raw = firstValue(row, ["风险减仓价", "第一反抽减仓价", "反抽减仓价", "减仓价", "压力/减仓"]);
  const pressure = cardPressure(row);
  if (!pressure.note) return raw;
  const stop = firstValue(row, ["失效价", "防守/止损", "防守", "趋势防守"]);
  return `${pressure.label} ${pressure.value} 不追；${pressure.note}${stop ? `；跌破 ${stop} 再按风控减仓` : ""}`;
}

function renderStockCard(row) {
  const action = firstValue(row, ["建议", "盘前建议", "操作建议", "操作"]);
  const topic = firstValue(row, ["专题", "同花顺专题映射", "场景", "社区温度"]);
  const command = firstValue(row, ["操作指令", "状态", "场景"]);
  const primaryPriceLabel = row["VWAP"] ? "VWAP" : (row["收盘"] ? "收盘" : "现价");
  const primaryPrice = firstValue(row, ["VWAP", "收盘", "现价", "当前价"]);
  const trigger = firstValue(row, ["加仓触发", "条件加仓价", "加仓价", "触发价", "修复/加仓条件", "修复", "趋势修复"]);
  const structureLabel = row["日内强弱线"] ? "日内强弱" : (row["支撑"] || row["支撑（回踩观察）"] ? "支撑" : "触发价");
  const structureLine = firstValue(row, ["日内强弱线", "支撑", "支撑（回踩观察）", "触发价"]);
  const riskReduce = firstValue(row, ["防守/止损", "防守/止损（硬失效）", "防守", "趋势防守", "风险减仓价"]);
  const pressure = cardPressure(row);
  const addPrice = trigger;
  const addMode = firstValue(row, ["加仓模式"]);
  const addConfirm = firstValue(row, ["加仓确认"]);
  const addCancel = firstValue(row, ["加仓取消"]);
  const invalidPrice = firstValue(row, ["失效价", "防守/止损", "防守/止损（硬失效）", "防守", "趋势防守"]);
  const actionText = action || buildCardAction(row) || row["状态"] || "";
  const levels = Array.isArray(row._live_levels)
    ? row._live_levels
    : [
      { label: primaryPriceLabel, value: primaryPrice || "策略未启用" },
      { label: structureLabel, value: structureLine || "策略未启用" },
      { label: "防守/风险", value: riskReduce || "策略未启用" },
      { label: pressure.label, value: pressure.value || "策略未启用" },
      { label: "加仓触发", value: addPrice || "当前不加仓" },
      { label: "失效", value: invalidPrice || "策略未启用" },
    ];
  return `
    <article class="stock-card tone-${row._tone}">
      <div class="stock-card-top">
        <div class="stock-card-title">
          <strong>${escapeHtml(row["名称"] || "-")}</strong>
          <span>${escapeHtml(row["代码"] || "")}</span>
        </div>
        <span class="badge badge-${row._tone}">${escapeHtml(normalizedPriority(row))}</span>
      </div>
      <div class="muted">${escapeHtml(command || topic || row["状态"] || "")}</div>
      <div class="level-grid">
        ${levels.map((level) => `<div class="level" title="${escapeAttr(level.note || "")}"><span>${escapeHtml(level.label)}</span><strong>${escapeHtml(level.value)}</strong></div>`).join("")}
      </div>
      <div class="muted">${escapeHtml(topic || "")}</div>
      ${addConfirm ? `<p class="entry-confirm">${escapeHtml(`${addMode ? `${addMode}：` : ""}${addConfirm}`)}</p>` : ""}
      ${addCancel ? `<p class="muted">${escapeHtml(`取消：${addCancel}`)}</p>` : ""}
      <p class="action-text">${escapeHtml(actionText)}</p>
    </article>
  `;
}

function renderSections(report) {
  const hasRows = Boolean(report.rows?.length);
  const hasPaperTrades = Boolean(report.paper_trades?.length);
  const skipExact = new Set(["核心操作表", "模拟盘成交质量复盘", "证据与刷新门控"]);
  const sections = (report.sections || []).filter((section) => {
    if (skipExact.has(section.title)) return false;
    if (hasRows && /订盘总览|个股盘后卡片/.test(section.title)) return false;
    if (hasPaperTrades && /模拟盘成交/.test(section.title)) return false;
    if (/全市场机会池/.test(section.title)) return false;
    return true;
  });
  return sections.map((section) => `
    <section class="section">
      <div class="section-head">
        <div>
          <h2 class="section-title">${escapeHtml(section.title)}</h2>
        </div>
      </div>
      <div class="section-panel">${section.html || ""}</div>
    </section>
  `).join("");
}

function renderRaw(report) {
  return `
    <section class="section">
      <div class="section-head">
        <div>
          <h2 class="section-title">原始报告</h2>
          <p class="section-note">保留完整 Markdown，便于核对字段和复制。</p>
        </div>
      </div>
      <pre class="raw-block">${escapeHtml(report.raw || "")}</pre>
    </section>
  `;
}

function filterButton(value, label, count) {
  return `<button class="button filter-button" type="button" data-filter="${escapeAttr(value)}">${escapeHtml(label)} ${count}</button>`;
}

function bindEvents() {
  document.getElementById("reportSelect")?.addEventListener("change", (event) => {
    const params = new URLSearchParams(location.search);
    params.set("report", event.target.value);
    params.set("theme", state.theme);
    location.search = params.toString();
  });
  document.getElementById("themeButton")?.addEventListener("click", () => {
    state.theme = state.theme === "light" ? "dark" : "light";
    localStorage.setItem("aShareReportTheme", state.theme);
    document.documentElement.dataset.theme = state.theme;
    document.getElementById("themeButton").textContent = state.theme === "light" ? "深色" : "浅色";
  });
  document.getElementById("autoRefreshButton")?.addEventListener("click", () => {
    state.autoRefresh = !state.autoRefresh;
    setRouteParam("auto", state.autoRefresh ? null : "0");
    render();
    scheduleRefresh();
  });
  document.getElementById("refreshNowButton")?.addEventListener("click", async () => {
    await checkForLatestReport({ force: true });
  });
  document.querySelectorAll(".filter-button").forEach((button) => {
    button.addEventListener("click", () => {
      state.filter = button.dataset.filter || "all";
      applyFilter();
    });
  });
  document.getElementById("stockSearch")?.addEventListener("input", (event) => {
    state.search = event.target.value.trim().toLowerCase();
    applyFilter();
  });
  document.querySelectorAll("[data-paper-position-sort]").forEach((button) => {
    button.addEventListener("click", () => {
      const key = button.dataset.paperPositionSort;
      const current = state.paperPositionSort || {};
      const sameKey = current.key === key;
      state.paperPositionSort = {
        key,
        direction: sameKey && current.direction === "desc" ? "asc" : "desc",
      };
      render();
    });
  });
}

function scheduleRefresh() {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  if (!state.autoRefresh) return;
  state.refreshTimer = setInterval(() => {
    checkForLatestReport({ force: false }).catch((error) => {
      const node = document.getElementById("refreshStatus");
      if (node) node.textContent = `自动刷新检查失败：${error.message || error}`;
    });
  }, state.refreshEveryMs);
}

async function checkForLatestReport({ force }) {
  if (await reloadIfAssetVersionChanged()) return;
  const latestIndex = await fetchJson(`./data/index.json?t=${Date.now()}`);
  if (!latestIndex.length) return;
  state.index = latestIndex;
  const latest = latestIndex[0];
  const current = state.report?.id;
  if (force || latest.id !== current) {
    const nextId = latest.id;
    state.report = await fetchJson(`./data/reports/${encodeURIComponent(nextId)}.json?t=${Date.now()}`);
    const params = new URLSearchParams(location.search);
    params.set("report", nextId);
    params.set("theme", state.theme);
    if (!state.autoRefresh) params.set("auto", "0");
    history.replaceState(null, "", `${location.pathname}?${params.toString()}`);
    render();
    scheduleRefresh();
    return;
  }
  const nextRuntime = await fetchRuntimeSnapshot();
  if (JSON.stringify(nextRuntime) !== JSON.stringify(state.runtime)) {
    state.runtime = nextRuntime;
    render();
    scheduleRefresh();
    return;
  }
  const node = document.getElementById("refreshStatus");
  if (node) {
    const now = new Date();
    node.textContent = `已检查，无新报告 · ${now.toLocaleTimeString("zh-CN", { hour12: false })}`;
  }
}

async function reloadIfAssetVersionChanged() {
  try {
    const resp = await fetch(`./index.html?t=${Date.now()}`, { cache: "no-store" });
    if (!resp.ok) return false;
    const html = await resp.text();
    const match = html.match(/dashboard\.js\?v=([^"'&]+)/);
    const latestVersion = match ? decodeURIComponent(match[1]) : "";
    if (latestVersion && latestVersion !== APP_ASSET_VERSION) {
      location.reload();
      return true;
    }
  } catch (_) {
    return false;
  }
  return false;
}

function setRouteParam(key, value) {
  const params = new URLSearchParams(location.search);
  if (value === null || value === undefined || value === "") params.delete(key);
  else params.set(key, value);
  params.set("theme", state.theme);
  if (state.report?.id) params.set("report", state.report.id);
  history.replaceState(null, "", `${location.pathname}?${params.toString()}`);
}

function applyFilter() {
  document.querySelectorAll(".filter-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.filter === state.filter);
  });
  document.querySelectorAll(".stock-row").forEach((row) => {
    const priorityOk = state.filter === "all" || row.dataset.priority === state.filter;
    const searchOk = !state.search || (row.dataset.search || "").toLowerCase().includes(state.search);
    row.classList.toggle("hidden", !(priorityOk && searchOk));
  });
}

function pickPrimaryConclusion(report) {
  const conclusion = (report.sections || []).find((x) => /结论|市场|开盘情绪/.test(x.title));
  if (conclusion?.plain) return conclusion.plain.replace(/\s+/g, " ").slice(0, 220);
  return "按情绪、筹码、时间和三周期框架组织；所有加仓价都是条件价，必须等待量价确认。";
}

function optionLabel(item) {
  const type = kindLabel(item.kind);
  return `${item.date || "未知日期"} ${type} ${item.generated || item.published_at || ""} · P0 ${item.p0 || 0} / P1 ${item.p1 || 0}`;
}

function kindLabel(kind) {
  return {
    realtime: "实时",
    auction: "竞价",
    quality: "质检",
    premarket: "盘前",
    intraday: "盘中",
    afterclose: "盘后",
    report: "报告",
  }[kind] || "报告";
}

function renderError(error) {
  return `
    <main class="loading-shell">
      <div class="loading-card">
        <h1>报告工作台加载失败</h1>
        <p>${escapeHtml(error.message || String(error))}</p>
        <p class="muted">请先运行报告脚本，或确认从项目根目录启动 HTTP 服务。</p>
      </div>
    </main>
  `;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}
