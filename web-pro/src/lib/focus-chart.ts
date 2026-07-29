/** Demo price path + trade levels for single-name Focus chart (paper-sim). */

export type FocusRange = "1D" | "5D" | "1M";

export type ChartLevelRole = "avg" | "entry" | "stop" | "target" | "last";

export interface ChartLevel {
  role: ChartLevelRole;
  price: number;
  label: string;
}

export interface ChartEvent {
  id: string;
  t: string; // x key matching series
  price: number;
  kind: "fill" | "judge" | "act" | "gate" | "risk";
  title: string;
  body?: string;
}

export interface FocusBar {
  t: string;
  price: number;
  /** optional event mark for tooltip */
  eventId?: string;
}

const ROLE_COLOR: Record<ChartLevelRole, string> = {
  avg: "#71767b",
  entry: "#1d9bf0",
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
  },
): { bars: FocusBar[]; levels: ChartLevel[]; events: ChartEvent[] } {
  const n = range === "1D" ? 78 : range === "5D" ? 65 : 42;
  const stepMin = range === "1D" ? 5 : range === "5D" ? 60 : 24 * 60;
  const rnd = mulberry32(seedFromSymbol(symbol) + n);
  const last = opts.last > 0 ? opts.last : 100;
  // walk backward from last
  const prices: number[] = [last];
  let p = last;
  for (let i = 1; i < n; i++) {
    const drift = (rnd() - 0.48) * (last * 0.004);
    p = Math.max(last * 0.85, p - drift);
    prices.unshift(p);
  }
  // re-normalize end to last
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
    return { t, price: px };
  });

  const levels: ChartLevel[] = [{ role: "last", price: last, label: "Last" }];
  if (opts.avgCost && opts.avgCost > 0) {
    levels.push({ role: "avg", price: opts.avgCost, label: "Avg" });
  }
  if (opts.stop && opts.stop > 0) {
    levels.push({ role: "stop", price: opts.stop, label: "Stop" });
  }
  if (opts.target && opts.target > 0) {
    levels.push({ role: "target", price: opts.target, label: "Target" });
  }
  // entry mark near 55% of series if we have avg
  if (opts.avgCost && opts.avgCost > 0) {
    levels.push({ role: "entry", price: opts.avgCost, label: "Entry" });
  }

  const events: ChartEvent[] = [];
  const mid = Math.floor(n * 0.45);
  const late = Math.floor(n * 0.72);
  const late2 = Math.floor(n * 0.88);
  if (bars[mid]) {
    events.push({
      id: "ev-entry",
      t: bars[mid].t,
      price: opts.avgCost || bars[mid].price,
      kind: "fill",
      title: `FILL · entry ${symbol}`,
      body: "Bracket attached (sim).",
    });
  }
  if (bars[late]) {
    events.push({
      id: "ev-judge",
      t: bars[late].t,
      price: bars[late].price,
      kind: "judge",
      title: "JUDGE · manage",
      body: `Thesis: manage ${symbol}; stop geometry OK.`,
    });
  }
  if (opts.stop && bars[late2]) {
    events.push({
      id: "ev-act",
      t: bars[late2].t,
      price: opts.stop,
      kind: "act",
      title: `ACT · modify_stop ${symbol}`,
      body: `Stop → ${opts.stop.toFixed(2)} (sim).`,
    });
  }

  return { bars, levels, events };
}

export function parseStopTarget(details: string): { stop?: number; target?: number } {
  const stopM = details.match(/SL\s*([\d.]+)/i);
  const tpM = details.match(/TP\s*([\d.]+)/i);
  return {
    stop: stopM ? Number(stopM[1]) : undefined,
    target: tpM ? Number(tpM[1]) : undefined,
  };
}
