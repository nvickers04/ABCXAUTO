/** Demo price path + trade levels for single-name Focus chart (paper-sim). */

export type FocusRange = "1D" | "5D" | "1M";

export type ChartLevelRole = "avg" | "entry" | "stop" | "target" | "last" | "planned";

export interface ChartLevel {
  role: ChartLevelRole;
  price: number;
  label: string;
}

export interface ChartEvent {
  id: string;
  t: string;
  price: number;
  kind: "fill" | "judge" | "act" | "gate" | "risk" | "planned";
  title: string;
  body?: string;
}

export interface FocusBar {
  t: string;
  price: number;
  /** price when above entry (for green segment optional) */
  priceGain?: number | null;
  priceLoss?: number | null;
  eventId?: string;
}

export type FocusSideKind = "position" | "planned";

export interface FocusSideItem {
  id: string;
  kind: FocusSideKind;
  symbol: string;
  /** long | short — drives PnL zone direction */
  side: "long" | "short";
  last: number;
  entry?: number;
  stop?: number;
  target?: number;
  qty?: number;
  uPnl?: number;
  protected?: boolean;
  label: string;
  detail: string;
}

const ROLE_COLOR: Record<ChartLevelRole, string> = {
  avg: "#71767b",
  entry: "#1d9bf0",
  planned: "#ffd400",
  stop: "#f4212e",
  target: "#00ba7c",
  last: "#e7e9ea",
};

export function levelColor(role: ChartLevelRole) {
  return ROLE_COLOR[role];
}

function seedFromSymbol(sym: string) {
  let h = 0;
  for (let i = 0; i < sym.length; i++) h = (h * 31 + sym.charCodeAt(i)) >>> 0;
  return h || 1;
}

function mulberry32(a: number) {
  return function next() {
    let t = (a += 0x6d2b79f5);
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function buildFocusSeries(
  symbol: string,
  range: FocusRange,
  opts: {
    last: number;
    avgCost?: number;
    stop?: number;
    target?: number;
    /** planned entry (no fill yet) */
    plannedEntry?: number;
    side?: "long" | "short";
    isPlanned?: boolean;
  },
): { bars: FocusBar[]; levels: ChartLevel[]; events: ChartEvent[]; entry: number | null; side: "long" | "short" } {
  const n = range === "1D" ? 78 : range === "5D" ? 65 : 42;
  const stepMin = range === "1D" ? 5 : range === "5D" ? 60 : 24 * 60;
  const rnd = mulberry32(seedFromSymbol(symbol) + n);
  const last = opts.last > 0 ? opts.last : 100;
  const side = opts.side ?? "long";
  const entryPx =
    opts.avgCost && opts.avgCost > 0
      ? opts.avgCost
      : opts.plannedEntry && opts.plannedEntry > 0
        ? opts.plannedEntry
        : null;

  const prices: number[] = [last];
  let p = last;
  for (let i = 1; i < n; i++) {
    const drift = (rnd() - 0.48) * (last * 0.004);
    p = Math.max(last * 0.85, p - drift);
    prices.unshift(p);
  }
  const scale = last / prices[prices.length - 1]!;
  const now = Date.now();
  const bars: FocusBar[] = prices.map((raw, i) => {
    const px = Math.round(raw * scale * 100) / 100;
    const ts = new Date(now - (n - 1 - i) * stepMin * 60_000);
    const t =
      range === "1D"
        ? ts.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        : range === "5D"
          ? ts.toLocaleString([], {
              weekday: "short",
              hour: "2-digit",
              minute: "2-digit",
            })
          : ts.toLocaleDateString([], { month: "short", day: "numeric" });
    // Split series for green/red path vs entry (long: above = gain)
    let priceGain: number | null = null;
    let priceLoss: number | null = null;
    if (entryPx != null) {
      const inGain = side === "long" ? px >= entryPx : px <= entryPx;
      if (inGain) priceGain = px;
      else priceLoss = px;
    }
    return { t, price: px, priceGain, priceLoss };
  });

  const levels: ChartLevel[] = [{ role: "last", price: last, label: "Last" }];
  if (opts.isPlanned && opts.plannedEntry && opts.plannedEntry > 0) {
    levels.push({ role: "planned", price: opts.plannedEntry, label: "Plan entry" });
  } else if (opts.avgCost && opts.avgCost > 0) {
    levels.push({ role: "entry", price: opts.avgCost, label: "Entry" });
  }
  if (opts.stop && opts.stop > 0) {
    levels.push({ role: "stop", price: opts.stop, label: "Stop" });
  }
  if (opts.target && opts.target > 0) {
    levels.push({ role: "target", price: opts.target, label: "Target" });
  }

  const events: ChartEvent[] = [];
  const mid = Math.floor(n * 0.45);
  const late = Math.floor(n * 0.72);
  const late2 = Math.floor(n * 0.88);

  if (opts.isPlanned && bars[mid] && opts.plannedEntry) {
    events.push({
      id: "ev-plan",
      t: bars[mid].t,
      price: opts.plannedEntry,
      kind: "planned",
      title: `PLANNED · entry ${symbol}`,
      body: "Working entry — not filled yet (sim).",
    });
  } else if (bars[mid] && entryPx) {
    events.push({
      id: "ev-entry",
      t: bars[mid].t,
      price: entryPx,
      kind: "fill",
      title: `FILL · entry ${symbol}`,
      body: "Bracket attached (sim).",
    });
  }
  if (!opts.isPlanned && bars[late]) {
    events.push({
      id: "ev-judge",
      t: bars[late].t,
      price: bars[late].price,
      kind: "judge",
      title: "JUDGE · manage",
      body: `Thesis: manage ${symbol}; stop geometry OK.`,
    });
  }
  if (!opts.isPlanned && opts.stop && bars[late2]) {
    events.push({
      id: "ev-act",
      t: bars[late2].t,
      price: opts.stop,
      kind: "act",
      title: `ACT · modify_stop ${symbol}`,
      body: `Stop → ${opts.stop.toFixed(2)} (sim).`,
    });
  }

  return { bars, levels, events, entry: entryPx, side };
}

export function parseStopTarget(details: string): { stop?: number; target?: number } {
  const stopM = details.match(/SL\s*([\d.]+)/i);
  const tpM = details.match(/TP\s*([\d.]+)/i);
  return {
    stop: stopM ? Number(stopM[1]) : undefined,
    target: tpM ? Number(tpM[1]) : undefined,
  };
}

/** Build right-rail tabs: open STK positions + working entry plans. */
export function buildFocusSideItems(
  positions: {
    conId: string;
    symbol: string;
    type: string;
    qty: number;
    avgCost: number;
    price: number;
    uPnl: number;
    details: string;
    protected: boolean;
  }[],
  orders: {
    id: string;
    symbol: string;
    side: string;
    type: string;
    qty: number;
    price: number;
    status: string;
    role: string;
  }[],
): FocusSideItem[] {
  const items: FocusSideItem[] = [];

  for (const p of positions) {
    if (p.type !== "STK") continue;
    const { stop, target } = parseStopTarget(p.details);
    const stopO = orders.find(
      (o) => o.symbol === p.symbol && (o.role === "stop" || o.type === "STP"),
    );
    const tgtO = orders.find((o) => o.symbol === p.symbol && o.role === "target");
    items.push({
      id: `pos-${p.conId}`,
      kind: "position",
      symbol: p.symbol,
      side: p.qty >= 0 ? "long" : "short",
      last: p.price,
      entry: p.avgCost,
      stop: stopO?.price ?? stop,
      target: tgtO?.price ?? target,
      qty: p.qty,
      uPnl: p.uPnl,
      protected: p.protected,
      label: p.symbol,
      detail: p.protected ? "open · protected" : "open · unprotected",
    });
  }

  for (const o of orders) {
    if (o.role !== "entry") continue;
    // skip if already have open position in that name
    if (items.some((it) => it.kind === "position" && it.symbol === o.symbol)) continue;
    items.push({
      id: `plan-${o.id}`,
      kind: "planned",
      symbol: o.symbol,
      side: o.side === "SELL" ? "short" : "long",
      last: o.price, // mid proxy until live quote
      entry: o.price,
      qty: o.qty,
      label: o.symbol,
      detail: `planned ${o.side} ${o.type} · ${o.status}`,
    });
  }

  return items;
}
