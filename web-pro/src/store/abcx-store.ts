import { create } from "zustand";
import {
  ARENAS,
  DEFAULT_CONTROLS,
  DEFAULT_RISK,
  INITIAL_ACTIVITY,
  INITIAL_NEWS,
  INITIAL_ORDERS,
  INITIAL_POSITIONS,
  SUITE_TESTS,
  buildEquityCurve,
  nextCycleActivity,
  type ActivityItem,
  type AgentMode,
  type Arena,
  type ControlsState,
  type NewsItem,
  type Position,
  type RiskState,
  type ScorePoint,
  type SuiteTest,
  type TabId,
  type WorkingOrder,
} from "@/lib/abcx-data";

interface AbcxState {
  tab: TabId;
  mode: AgentMode;
  connected: boolean;
  xaiOk: boolean;
  mdaOk: boolean;
  cycles: number;
  equity: number;
  dayPnl: number;
  ret1w: number;
  ret3m: number;
  ret1y: number;
  positions: Position[];
  orders: WorkingOrder[];
  activity: ActivityItem[];
  news: NewsItem[];
  controls: ControlsState;
  risk: RiskState;
  arenas: Arena[];
  customSymbols: string;
  excludeSymbols: string;
  universeFilter: string;
  equityCurve: ScorePoint[];
  suite: SuiteTest[];
  suiteFilter: "all" | "stock" | "manage" | "options";
  compose: string;
  agentNow: string;
  judgment: string;
  proposal: string;
  pace: string;
  attention: string;
  pulseNarrative: string;
  toast: string | null;
  mobileRail: "main" | "nav" | "right";
  focusSymbol: string | null;
  cycleTimer: number | null;

  setTab: (t: TabId) => void;
  setCompose: (v: string) => void;
  setMobileRail: (r: "main" | "nav" | "right") => void;
  setControls: (p: Partial<ControlsState>) => void;
  setRisk: (p: Partial<RiskState>) => void;
  applyPosture: (p: RiskState["posture"]) => void;
  toggleArena: (id: string) => void;
  setCustomSymbols: (v: string) => void;
  setExcludeSymbols: (v: string) => void;
  setUniverseFilter: (v: string) => void;
  setFocusSymbol: (sym: string | null) => void;
  clearFocus: () => void;
  setSuiteFilter: (f: "all" | "stock" | "manage" | "options") => void;
  toggleConnect: () => void;
  toggleRun: () => void;
  pauseAgent: () => void;
  panicFlatten: () => void;
  manualHalt: () => void;
  resumeHalt: () => void;
  submitMandate: () => void;
  runSuiteTest: (id: string) => void;
  clearToast: () => void;
  tickSim: () => void;
  startCycleLoop: () => void;
  stopCycleLoop: () => void;
}

function sumUPnl(positions: Position[]) {
  return positions.reduce((a, p) => a + p.uPnl, 0);
}

function unprotectedCount(positions: Position[]) {
  return positions.filter((p) => p.type === "STK" && !p.protected).length;
}

export const useAbcxStore = create<AbcxState>((set, get) => ({
  tab: "overview",
  mode: "Safe",
  connected: false,
  xaiOk: true,
  mdaOk: true,
  cycles: 14,
  equity: 268_420,
  dayPnl: 1842.6,
  ret1w: 1.24,
  ret3m: 6.8,
  ret1y: 18.4,
  positions: INITIAL_POSITIONS,
  orders: INITIAL_ORDERS,
  activity: INITIAL_ACTIVITY,
  news: INITIAL_NEWS,
  controls: { ...DEFAULT_CONTROLS },
  risk: { ...DEFAULT_RISK },
  arenas: ARENAS.map((a) => ({ ...a })),
  customSymbols: "PLTR,CRWD",
  excludeSymbols: "",
  universeFilter: "",
  equityCurve: buildEquityCurve(),
  suite: SUITE_TESTS.map((t) => ({ ...t })),
  suiteFilter: "all",
  compose: "",
  agentNow: "Waiting for START — or run a demo cycle from the feed.",
  judgment: "Judgment: — (stance / thesis after Judge)",
  proposal: "No proposal yet — Start agent or wait for a cycle.",
  pace: "Pace: idle (demo)",
  attention: "Attention: protect META → then hunt",
  pulseNarrative: "Paper demo. Connect simulates TWS 7497. Autonomy is Pro START-driven.",
  toast: null,
  mobileRail: "main",
  focusSymbol: "NVDA",
  cycleTimer: null,

  setTab: (tab) => set({ tab, mobileRail: "main" }),
  setCompose: (compose) => set({ compose }),
  setMobileRail: (mobileRail) => set({ mobileRail }),
  setControls: (p) => set((s) => ({ controls: { ...s.controls, ...p } })),
  setRisk: (p) => set((s) => ({ risk: { ...s.risk, ...p } })),

  applyPosture: (posture) => {
    const presets: Record<RiskState["posture"], Partial<RiskState>> = {
      defensive: {
        posture,
        maxRiskPerTradePct: 0.75,
        dailyLossLimitPct: 2,
        maxPositionPct: 8,
        maxPeakDrawdownPct: 8,
        maxOptionPremiumPct: 2,
        definedRiskOnly: true,
      },
      balanced: {
        posture,
        maxRiskPerTradePct: 1.5,
        dailyLossLimitPct: 3,
        maxPositionPct: 12,
        maxPeakDrawdownPct: 12,
        maxOptionPremiumPct: 4,
        definedRiskOnly: true,
      },
      aggressive: {
        posture,
        maxRiskPerTradePct: 2.5,
        dailyLossLimitPct: 5,
        maxPositionPct: 18,
        maxPeakDrawdownPct: 18,
        maxOptionPremiumPct: 6,
        definedRiskOnly: false,
      },
    };
    set((s) => ({
      risk: { ...s.risk, ...presets[posture] },
      toast: `Risk posture → ${posture}`,
    }));
  },

  toggleArena: (id) =>
    set((s) => ({
      arenas: s.arenas.map((a) => (a.id === id ? { ...a, enabled: !a.enabled } : a)),
    })),

  setCustomSymbols: (customSymbols) => set({ customSymbols }),
  setExcludeSymbols: (excludeSymbols) => set({ excludeSymbols }),
  setUniverseFilter: (universeFilter) => set({ universeFilter }),

  setFocusSymbol: (sym) => {
    const focusSymbol = sym ? sym.toUpperCase().trim() : null;
    set({ focusSymbol, tab: focusSymbol ? "focus" : get().tab, mobileRail: "main" });
    if (focusSymbol) set({ toast: `Focus · ${focusSymbol}` });
  },
  clearFocus: () => set({ focusSymbol: null, toast: "Focus cleared" }),
  setSuiteFilter: (suiteFilter) => set({ suiteFilter }),

  toggleConnect: () => {
    const { connected, mode, stopCycleLoop } = get();
    if (connected) {
      stopCycleLoop();
      set({
        connected: false,
        mode: mode === "Running" ? "Safe" : mode,
        toast: "Disconnected from paper TWS (sim)",
        activity: [
          {
            id: `c-${Date.now()}`,
            kind: "connect",
            title: "CONNECT · down",
            body: "IBKR paper session closed. Positions remain at broker in real ops.",
            ts: new Date().toISOString(),
          },
          ...get().activity,
        ],
      });
    } else {
      set({
        connected: true,
        toast: "Connected · paper TWS 7497 (sim)",
        activity: [
          {
            id: `c-${Date.now()}`,
            kind: "connect",
            title: "CONNECT · up",
            body: "Paper account ready. NetLiq snapshot refreshed. Fail-closed gates armed.",
            ts: new Date().toISOString(),
          },
          ...get().activity,
        ],
      });
    }
  },

  toggleRun: () => {
    const { mode, connected, risk, startCycleLoop, stopCycleLoop } = get();
    if (risk.halt) {
      set({ toast: "Halted — resume from Risk before START" });
      return;
    }
    if (mode === "Running") {
      stopCycleLoop();
      set({
        mode: "Paused",
        toast: "Agent paused — positions stay open",
        agentNow: "Paused. Decisions stopped; book untouched.",
        activity: [
          {
            id: `r-${Date.now()}`,
            kind: "system",
            title: "AGENT · paused",
            body: "Stop agent = pause decisions only. Open risk book retained.",
            ts: new Date().toISOString(),
          },
          ...get().activity,
        ],
      });
      return;
    }
    if (!connected) {
      set({ toast: "Connect paper IBKR first" });
      return;
    }
    set({
      mode: "Running",
      toast: "START AUTONOMOUS — demo cycles running",
      agentNow: "Perceive → Judge → Act loop live (sim)",
      pace: "Pace: manage ~60s (open risk)",
      attention: "Attention: protect first, then capacity",
    });
    startCycleLoop();
  },

  pauseAgent: () => {
    get().stopCycleLoop();
    set({ mode: "Paused", toast: "Paused", agentNow: "Paused by operator." });
  },

  panicFlatten: () => {
    get().stopCycleLoop();
    set((s) => ({
      mode: "Halted",
      risk: {
        ...s.risk,
        halt: true,
        haltReason: "Close All Positions (panic)",
      },
      positions: [],
      orders: [],
      dayPnl: s.dayPnl + sumUPnl(s.positions) * 0.15,
      toast: "PANIC — flatten requested (sim)",
      agentNow: "Flattened. Halt latched until manual resume.",
      activity: [
        {
          id: `p-${Date.now()}`,
          kind: "risk",
          title: "FLATTEN · Close All Positions",
          body: "Panic path: cancel working, market close STK/OPT, halt latch set. Exits never blocked.",
          ts: new Date().toISOString(),
        },
        ...s.activity,
      ],
    }));
  },

  manualHalt: () => {
    get().stopCycleLoop();
    set((s) => ({
      mode: "Halted",
      risk: { ...s.risk, halt: true, haltReason: "Manual halt from Risk tab" },
      toast: "Halt latched",
      activity: [
        {
          id: `h-${Date.now()}`,
          kind: "risk",
          title: "HALT · manual",
          body: "New entries blocked. Exits still allowed.",
          ts: new Date().toISOString(),
        },
        ...s.activity,
      ],
    }));
  },

  resumeHalt: () => {
    set((s) => ({
      risk: { ...s.risk, halt: false, haltReason: "" },
      mode: s.mode === "Halted" ? "Safe" : s.mode,
      toast: "Halt cleared — Safe mode",
    }));
  },

  submitMandate: () => {
    const text = get().compose.trim();
    if (!text) return;
    const item: ActivityItem = {
      id: `u-${Date.now()}`,
      kind: "user",
      title: "OPERATOR · mandate",
      body: text,
      ts: new Date().toISOString(),
    };
    const reply: ActivityItem = {
      id: `a-${Date.now()}`,
      kind: "system",
      title: "SHELL · logged (unbiased)",
      body: `Mandate recorded. Taste stays on Controls dials / optional Card — shell will not invent stance. Grok will see this on next Judge when Running. “${text.slice(0, 140)}${text.length > 140 ? "…" : ""}”`,
      ts: new Date(Date.now() + 200).toISOString(),
    };
    set((s) => ({
      compose: "",
      activity: [reply, item, ...s.activity],
      toast: "Mandate posted to activity",
    }));
  },

  runSuiteTest: (id) => {
    set((s) => ({
      suite: s.suite.map((t) => (t.id === id ? { ...t, status: "running" as const } : t)),
    }));
    window.setTimeout(() => {
      set((s) => ({
        suite: s.suite.map((t) =>
          t.id === id
            ? {
                ...t,
                status: Math.random() > 0.12 ? ("pass" as const) : ("fail" as const),
                lastMs: 220 + Math.floor(Math.random() * 700),
              }
            : t,
        ),
        toast: `Suite test ${id} finished`,
      }));
    }, 700);
  },

  clearToast: () => set({ toast: null }),

  tickSim: () => {
    const { mode, positions, connected } = get();
    if (!connected) return;
    set((s) => ({
      positions: s.positions.map((p) => {
        const j = (Math.random() - 0.48) * 0.18;
        const price = Math.max(1, p.price * (1 + j / 100));
        const uPnl = (price - p.avgCost) * p.qty;
        return { ...p, price, uPnl };
      }),
      dayPnl:
        mode === "Running"
          ? s.dayPnl + (Math.random() - 0.42) * 12
          : s.dayPnl + (Math.random() - 0.5) * 4,
      equity: s.equity + (Math.random() - 0.45) * 40,
    }));
    if (mode === "Running" && unprotectedCount(positions) === 0) {
      // occasionally already handled in cycle
    }
  },

  startCycleLoop: () => {
    const existing = get().cycleTimer;
    if (existing) window.clearInterval(existing);

    const runOnce = () => {
      const s = get();
      if (s.mode !== "Running" || s.risk.halt) return;
      const cycle = s.cycles + 1;
      const item = nextCycleActivity(cycle);
      let positions = s.positions;
      let orders = s.orders;

      if (item.title.includes("META protected") || item.body.includes("Unprotected count")) {
        positions = positions.map((p) =>
          p.symbol === "META"
            ? {
                ...p,
                protected: true,
                details: "bracket · SL 498.5 · TP 540.0",
              }
            : p,
        );
        if (!orders.some((o) => o.symbol === "META" && o.role === "stop")) {
          orders = [
            {
              id: `o-m-${Date.now()}`,
              symbol: "META",
              side: "SELL",
              type: "STP",
              qty: 18,
              price: 498.5,
              status: "PreSubmitted",
              role: "stop",
            },
            {
              id: `o-mt-${Date.now()}`,
              symbol: "META",
              side: "SELL",
              type: "LMT",
              qty: 18,
              price: 540,
              status: "PreSubmitted",
              role: "target",
            },
            ...orders,
          ];
        }
      }

      set({
        cycles: cycle,
        activity: [item, ...s.activity].slice(0, 80),
        positions,
        orders,
        agentNow: item.title,
        judgment: item.kind === "judge" ? item.body : s.judgment,
        proposal: item.kind === "act" ? item.body : s.proposal,
        pulseNarrative:
          unprotectedCount(positions) > 0
            ? "Open risk: unprotected STK — hold forbidden until protected."
            : "Book protected. Capacity available for new risk under Controls.",
        attention:
          unprotectedCount(positions) > 0
            ? "Attention: protect first"
            : "Attention: hunt within universe",
      });
    };

    // first cycle soon, then interval
    window.setTimeout(runOnce, 1200);
    const timer = window.setInterval(runOnce, 6500);
    set({ cycleTimer: timer });
  },

  stopCycleLoop: () => {
    const t = get().cycleTimer;
    if (t) window.clearInterval(t);
    set({ cycleTimer: null });
  },
}));
