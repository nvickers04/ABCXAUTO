/** Demo data + types for ABCXAUTO Pro web cockpit (paper-sim). */

export type TabId =
  | "overview"
  | "positions"
  | "focus"
  | "controls"
  | "universe"
  | "risk"
  | "scorecard"
  | "suite";

export type AgentMode = "Safe" | "Running" | "Paused" | "Halted";

export interface Position {
  conId: string;
  symbol: string;
  type: "STK" | "OPT";
  qty: number;
  avgCost: number;
  price: number;
  uPnl: number;
  details: string;
  protected: boolean;
}

export interface WorkingOrder {
  id: string;
  symbol: string;
  side: "BUY" | "SELL";
  type: string;
  qty: number;
  price: number;
  status: "Submitted" | "PreSubmitted" | "Filled" | "Cancelled";
  role: "entry" | "stop" | "target" | "exit";
}

export interface ActivityItem {
  id: string;
  kind: "judge" | "act" | "gate" | "fill" | "pace" | "connect" | "system" | "risk" | "user";
  title: string;
  body: string;
  ts: string;
  meta?: Record<string, string | number | boolean>;
}

export interface NewsItem {
  id: string;
  headline: string;
  source: string;
  related: string;
  ts: string;
}

export interface ControlsState {
  deliberation: number;
  budget: number;
  frequency: number;
  rotation: number;
  complexity: number;
  maxOpenPositions: number;
}

export interface RiskState {
  posture: "defensive" | "balanced" | "aggressive";
  gatesEnabled: boolean;
  autoPanic: boolean;
  definedRiskOnly: boolean;
  cashOnly: boolean;
  maxRiskPerTradePct: number;
  dailyLossLimitPct: number;
  maxPositionPct: number;
  maxPeakDrawdownPct: number;
  maxOptionPremiumPct: number;
  halt: boolean;
  haltReason: string;
}

export interface Arena {
  id: string;
  label: string;
  group: "caps" | "scans" | "sectors" | "custom";
  kind: "ibkr" | "mda_seed";
  enabled: boolean;
  symbols: string[];
}

export interface ScorePoint {
  date: string;
  equity: number;
  spy: number;
}

export interface SuiteTest {
  id: string;
  name: string;
  group: "stock" | "manage" | "options";
  status: "idle" | "running" | "pass" | "fail";
  lastMs?: number;
}

export const NAV: { id: TabId; label: string; desc: string }[] = [
  { id: "overview", label: "Dashboard", desc: "Live ops while the agent runs — facts only, shell does not rank." },
  { id: "positions", label: "Positions", desc: "Book table, working orders, and fills blotter." },
  { id: "focus", label: "Focus", desc: "Single-name chart — levels, fills, agent marks on one tape." },
  { id: "controls", label: "Controls", desc: "Attention + toolbox — disjoint from Risk and Universe." },
  {
    id: "universe",
    label: "Universe",
    desc: "Fence the hunt: arenas on/off, pins, excludes. Pool is not ranked.",
  },
  { id: "risk", label: "Risk", desc: "Capital survival gates and halt. Disjoint from Controls." },
  { id: "scorecard", label: "Scorecard", desc: "Forward paper evidence — P&L, gates, dispatches." },
  { id: "suite", label: "Test Suite", desc: "Paper place/cancel gym for order mechanics." },
];

export const DEFAULT_CONTROLS: ControlsState = {
  deliberation: 50,
  budget: 50,
  frequency: 45,
  rotation: 40,
  complexity: 35,
  maxOpenPositions: 6,
};

export const DEFAULT_RISK: RiskState = {
  posture: "balanced",
  gatesEnabled: true,
  autoPanic: true,
  definedRiskOnly: true,
  cashOnly: false,
  maxRiskPerTradePct: 1.5,
  dailyLossLimitPct: 3,
  maxPositionPct: 12,
  maxPeakDrawdownPct: 12,
  maxOptionPremiumPct: 4,
  halt: false,
  haltReason: "",
};

export const ARENAS: Arena[] = [
  { id: "mega_cap", label: "Mega cap", group: "caps", kind: "ibkr", enabled: true, symbols: ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "BRK.B", "AVGO", "JPM"] },
  { id: "large_cap", label: "Large cap", group: "caps", kind: "ibkr", enabled: true, symbols: ["AMD", "CRM", "COST", "NFLX", "ORCL", "ADBE", "PEP", "KO", "XOM", "CVX", "WMT", "V", "MA"] },
  { id: "mid_cap", label: "Mid cap", group: "caps", kind: "ibkr", enabled: false, symbols: ["DECK", "FIX", "CASY", "WSM", "TOL", "RCL", "DKNG", "ROKU"] },
  { id: "most_active", label: "Most active", group: "scans", kind: "ibkr", enabled: true, symbols: ["SPY", "QQQ", "IWM", "TSLA", "NVDA", "AMD", "SOFI", "PLTR"] },
  { id: "top_gainers", label: "Top % gainers", group: "scans", kind: "ibkr", enabled: false, symbols: ["SMCI", "ARM", "MSTR", "COIN"] },
  { id: "top_losers", label: "Top % losers", group: "scans", kind: "ibkr", enabled: false, symbols: [] },
  { id: "semis", label: "Semiconductors", group: "sectors", kind: "mda_seed", enabled: true, symbols: ["NVDA", "AVGO", "AMD", "TSM", "ASML", "QCOM", "AMAT", "LRCX"] },
  { id: "index_etfs", label: "Index ETFs", group: "sectors", kind: "mda_seed", enabled: true, symbols: ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF"] },
];

export const INITIAL_POSITIONS: Position[] = [
  { conId: "265598", symbol: "AAPL", type: "STK", qty: 80, avgCost: 198.4, price: 214.55, uPnl: 1292, details: "bracket · SL 204.2 · TP 228.0", protected: true },
  { conId: "272093", symbol: "MSFT", type: "STK", qty: 35, avgCost: 401.2, price: 428.9, uPnl: 969.5, details: "bracket · SL 410.0 · TP 455.0", protected: true },
  { conId: "4815747", symbol: "NVDA", type: "STK", qty: 60, avgCost: 118.4, price: 131.22, uPnl: 769.2, details: "bracket · SL 122.5 · TP 142.0", protected: true },
  { conId: "3691937", symbol: "SPY", type: "STK", qty: 40, avgCost: 532.1, price: 548.12, uPnl: 640.8, details: "bracket · SL 535.0 · TP 565.0", protected: true },
  { conId: "76792991", symbol: "META", type: "STK", qty: 18, avgCost: 478.0, price: 512.1, uPnl: 613.8, details: "unprotected — stop missing", protected: false },
];

export const INITIAL_ORDERS: WorkingOrder[] = [
  { id: "o-8821", symbol: "NVDA", side: "SELL", type: "STP", qty: 60, price: 122.5, status: "PreSubmitted", role: "stop" },
  { id: "o-8822", symbol: "NVDA", side: "SELL", type: "LMT", qty: 60, price: 142.0, status: "PreSubmitted", role: "target" },
  { id: "o-8910", symbol: "MSFT", side: "SELL", type: "STP", qty: 35, price: 410.0, status: "PreSubmitted", role: "stop" },
  { id: "o-8911", symbol: "MSFT", side: "SELL", type: "LMT", qty: 35, price: 455.0, status: "PreSubmitted", role: "target" },
  { id: "o-9001", symbol: "QQQ", side: "BUY", type: "LMT", qty: 15, price: 478.5, status: "Submitted", role: "entry" },
];

function ago(min: number) {
  return new Date(Date.now() - min * 60_000).toISOString();
}

export const INITIAL_ACTIVITY: ActivityItem[] = [
  {
    id: "a1",
    kind: "judge",
    title: "JUDGE · stance long_bias",
    body: "Thesis: AI capex still intact; prefer quality mega-cap over mid. Dismissed noisy mid-cap gappers. Focus: NVDA protection audit + SPY ballast.",
    ts: ago(4),
    meta: { stance: "long_bias", confidence: 0.72 },
  },
  {
    id: "a2",
    kind: "act",
    title: "ACT · modify_stop NVDA",
    body: "Raised stop 119.8 → 122.5 after partial strength. Live IBKR mid used for geometry. Share-lot OK.",
    ts: ago(3),
    meta: { symbol: "NVDA", type: "modify_stop" },
  },
  {
    id: "a3",
    kind: "gate",
    title: "GATE · allowed",
    body: "Risk gates passed: risk/trade 0.9% NL, position 11.4% NL, halt clear, universe allowlist hit.",
    ts: ago(3),
  },
  {
    id: "a4",
    kind: "fill",
    title: "FILL · STP size confirm",
    body: "Broker accepted modify_stop on NVDA. Working stop qty matches position.",
    ts: ago(2),
  },
  {
    id: "a5",
    kind: "risk",
    title: "PROTECT · META unprotected",
    body: "Monitor: META STK has no working stop. Hold forbidden until protected or flattened. Escalating next cycle.",
    ts: ago(12),
    meta: { symbol: "META" },
  },
  {
    id: "a6",
    kind: "judge",
    title: "JUDGE · manage + hold",
    body: "Book mostly protected. No new risk while META stop missing. Idle new-risk stream; open-risk stream only.",
    ts: ago(28),
  },
  {
    id: "a7",
    kind: "pace",
    title: "PACE · manage floor 60s",
    body: "Adaptive sleep: open risk present → manage cadence. Grok min interval 120s unless urgent wake.",
    ts: ago(30),
  },
  {
    id: "a8",
    kind: "system",
    title: "SESSION · paper TWS 7497",
    body: "Demo cockpit. Broker + xAI are simulated in this preview. Doctrine: risk > execution > monitoring > thin UI.",
    ts: ago(90),
  },
];

export const INITIAL_NEWS: NewsItem[] = [
  { id: "n1", headline: "Chip names firm into the open as AI server demand stays elevated", source: "MDA", related: "NVDA", ts: ago(18) },
  { id: "n2", headline: "Treasury yields steady; 10Y holds range ahead of data", source: "MDA", related: "SPY", ts: ago(42) },
  { id: "n3", headline: "Mega-cap earnings calendar: META, MSFT this week", source: "MDA", related: "META", ts: ago(95) },
  { id: "n4", headline: "Options: short-dated IV soft on SPX — hedges still cheap", source: "MDA", related: "SPY", ts: ago(140) },
];

export const SUITE_TESTS: SuiteTest[] = [
  { id: "stk_bracket", name: "Stock bracket place/cancel", group: "stock", status: "pass", lastMs: 420 },
  { id: "stk_mkt_bracket", name: "Market bracket geometry", group: "stock", status: "pass", lastMs: 380 },
  { id: "stk_partial", name: "Partial close + stop resize", group: "stock", status: "idle" },
  { id: "mng_modify_stop", name: "Modify stop live", group: "manage", status: "pass", lastMs: 510 },
  { id: "mng_trailing", name: "Trailing stop attach", group: "manage", status: "idle" },
  { id: "mng_oca", name: "OCA stop/target pair", group: "manage", status: "pass", lastMs: 640 },
  { id: "opt_vertical", name: "Vertical defined-risk", group: "options", status: "idle" },
  { id: "opt_csp", name: "Cash-secured put", group: "options", status: "idle" },
  { id: "opt_close", name: "close_option position check", group: "options", status: "pass", lastMs: 290 },
  { id: "opt_iron", name: "Iron condor allowlist", group: "options", status: "fail", lastMs: 880 },
];

export function buildEquityCurve(days = 60): ScorePoint[] {
  const points: ScorePoint[] = [];
  let eq = 250_000;
  let spy = 250_000;
  const now = new Date();
  for (let i = days; i >= 0; i--) {
    const d = new Date(now);
    d.setDate(d.getDate() - i);
    eq *= 1 + 0.00055 + Math.sin(i / 8) * 0.0011 + Math.cos(i / 5) * 0.0004;
    spy *= 1 + 0.00038 + Math.sin(i / 10) * 0.0007;
    points.push({
      date: d.toISOString().slice(0, 10),
      equity: Math.round(eq),
      spy: Math.round(spy),
    });
  }
  return points;
}

const CYCLE_SCRIPTS: Omit<ActivityItem, "id" | "ts">[] = [
  {
    kind: "judge",
    title: "JUDGE · protect first",
    body: "Unprotected META still open. Stance: manage. New-risk stream suppressed until stops restored.",
  },
  {
    kind: "act",
    title: "ACT · oca META",
    body: "Attaching stop 498.5 + target 540.0 OCA on META long. Live mid 512.1. Geometry R:R 1.9.",
    meta: { symbol: "META", type: "oca" },
  },
  {
    kind: "gate",
    title: "GATE · allowed",
    body: "Protection attach is exit-side; gates allow. Defined-risk N/A for stock stop/target.",
  },
  {
    kind: "fill",
    title: "FILL · META protected",
    body: "OCA working. Unprotected count → 0. Hold is valid again.",
  },
  {
    kind: "judge",
    title: "JUDGE · scan tape",
    body: "SCAN TAPE (unranked): mega + semis arenas. No forced top idea. Candidate pool size 48.",
  },
  {
    kind: "act",
    title: "ACT · hold",
    body: "Protected book, capacity 5/6. No structure met quality bar at current budget. Hold.",
  },
  {
    kind: "judge",
    title: "JUDGE · long_bias light",
    body: "QQQ limit still working. Thesis intact; size already booked on NVDA/AAPL.",
  },
  {
    kind: "pace",
    title: "PACE · idle floor stretch",
    body: "Flat-confirmed new risk idle → sleep stretch toward idle floor. API $ conserved.",
  },
];

export function nextCycleActivity(cycle: number): ActivityItem {
  const script = CYCLE_SCRIPTS[cycle % CYCLE_SCRIPTS.length]!;
  return {
    ...script,
    id: `cyc-${Date.now()}-${cycle}`,
    ts: new Date().toISOString(),
  };
}

export function postureLabel(p: RiskState["posture"]) {
  return p === "defensive" ? "Defensive" : p === "aggressive" ? "Aggressive" : "Balanced";
}
