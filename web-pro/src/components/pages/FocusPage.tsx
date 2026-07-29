import { useMemo, useState } from "react";
import { Crosshair, X } from "lucide-react";
import { cn, formatUsd, relativeTime } from "@/lib/utils";
import { useAbcxStore } from "@/store/abcx-store";
import {
  buildFocusSeries,
  parseStopTarget,
  type FocusRange,
} from "@/lib/focus-chart";
import { FocusChart, FocusLegend } from "@/components/charts/FocusChart";
import { Button } from "@/components/ui/button";
import { ActivityFeed } from "@/components/feed/ActivityFeed";

const RANGES: FocusRange[] = ["1D", "5D", "1M"];

export function FocusPage() {
  const focusSymbol = useAbcxStore((s) => s.focusSymbol);
  const setFocusSymbol = useAbcxStore((s) => s.setFocusSymbol);
  const clearFocus = useAbcxStore((s) => s.clearFocus);
  const positions = useAbcxStore((s) => s.positions);
  const orders = useAbcxStore((s) => s.orders);
  const activity = useAbcxStore((s) => s.activity);
  const mode = useAbcxStore((s) => s.mode);
  const [range, setRange] = useState<FocusRange>("5D");

  const sym = (focusSymbol || "").toUpperCase();

  const position = positions.find((p) => p.symbol === sym && p.type === "STK");
  const symOrders = orders.filter((o) => o.symbol === sym);
  const stopOrder = symOrders.find((o) => o.role === "stop" || o.type === "STP");
  const targetOrder = symOrders.find((o) => o.role === "target");
  const fromDetails = position ? parseStopTarget(position.details) : {};

  const last = position?.price ?? 100;
  const avgCost = position?.avgCost;
  const stop = stopOrder?.price ?? fromDetails.stop;
  const target = targetOrder?.price ?? fromDetails.target;

  const { bars, levels, events } = useMemo(
    () =>
      buildFocusSeries(sym || "DEMO", range, {
        last,
        avgCost,
        stop,
        target,
      }),
    [sym, range, last, avgCost, stop, target],
  );

  const filteredActivity = activity.filter((a) => {
    if (!sym) return false;
    const blob = `${a.title} ${a.body} ${JSON.stringify(a.meta || {})}`.toUpperCase();
    return blob.includes(sym);
  });

  const bookNames = positions
    .filter((p) => p.type === "STK")
    .map((p) => p.symbol)
    .filter((s, i, arr) => arr.indexOf(s) === i);

  if (!sym) {
    return (
      <div className="flex min-h-[480px] flex-col items-center justify-center px-6 py-16 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-elevated ring-1 ring-border">
          <Crosshair className="h-5 w-5 text-primary" />
        </div>
        <h2 className="mt-4 text-[18px] font-bold text-fg">Single-name Focus</h2>
        <p className="mt-2 max-w-sm text-[13px] leading-snug text-muted">
          Pick one symbol to watch agent levels and acts on the tape. Expand to the full
          book later — this is Focus only.
        </p>
        <div className="mt-6 flex flex-wrap justify-center gap-2">
          {bookNames.length > 0 ? (
            bookNames.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setFocusSymbol(s)}
                className="rounded-full border border-border bg-elevated px-3 py-1.5 text-[13px] font-bold text-fg transition-colors hover:border-primary/40"
              >
                {s}
              </button>
            ))
          ) : (
            <>
              {["NVDA", "AAPL", "SPY"].map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => setFocusSymbol(s)}
                  className="rounded-full border border-border bg-elevated px-3 py-1.5 text-[13px] font-bold text-fg transition-colors hover:border-primary/40"
                >
                  {s}
                </button>
              ))}
            </>
          )}
        </div>
        <p className="mt-4 text-[11px] text-muted">
          Or use Focus on a row in Positions / a chip in Universe.
        </p>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-col">
      {/* Header */}
      <div className="border-b border-border px-4 py-3 sm:px-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded-full bg-primary/15 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-primary">
                Focus
              </span>
              <h2 className="text-[22px] font-bold tracking-tight text-fg">{sym}</h2>
              <span
                className={cn(
                  "text-[12px] font-semibold",
                  mode === "Running" ? "text-gain" : "text-muted",
                )}
              >
                {mode}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap items-baseline gap-3">
              <span className="tabular text-[20px] font-bold text-fg">
                {formatUsd(last)}
              </span>
              {position && (
                <span
                  className={cn(
                    "tabular text-[13px] font-semibold",
                    position.uPnl >= 0 ? "text-gain" : "text-loss",
                  )}
                >
                  {formatUsd(position.uPnl, { signed: true })} uPnL
                </span>
              )}
              {!position && (
                <span className="text-[12px] text-muted">No open STK — levels from sim path</span>
              )}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex rounded-full border border-border bg-elevated p-0.5">
              {RANGES.map((r) => (
                <button
                  key={r}
                  type="button"
                  onClick={() => setRange(r)}
                  className={cn(
                    "rounded-full px-2.5 py-1 text-[11px] font-semibold transition-colors",
                    range === r ? "bg-fg text-bg" : "text-muted hover:text-fg",
                  )}
                >
                  {r}
                </button>
              ))}
            </div>
            <Button size="sm" variant="ghost" onClick={clearFocus} title="Clear focus">
              <X className="h-3.5 w-3.5" />
              Clear
            </Button>
          </div>
        </div>

        {/* Level chips */}
        <div className="mt-3 flex flex-wrap gap-2">
          {avgCost != null && (
            <LevelChip label="Avg" value={avgCost} tone="muted" />
          )}
          {stop != null && <LevelChip label="Stop" value={stop} tone="loss" />}
          {target != null && <LevelChip label="Target" value={target} tone="gain" />}
          {symOrders.length > 0 && (
            <span className="rounded-full border border-border px-2.5 py-1 text-[11px] text-muted">
              {symOrders.length} working order{symOrders.length === 1 ? "" : "s"}
            </span>
          )}
        </div>
      </div>

      {/* Chart */}
      <div className="border-b border-border px-2 py-3 sm:px-4">
        <FocusChart bars={bars} levels={levels} events={events} height={360} />
        <div className="mt-2 px-2">
          <FocusLegend events={events} />
          <p className="mt-1 text-[11px] text-muted">
            Demo path — levels from book / working orders. Marks are agent events (sim), not
            recommendations.
          </p>
        </div>
      </div>

      {/* Event strip + activity */}
      <div className="grid min-h-0 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
        <section className="border-b border-border px-4 py-4 lg:border-b-0 lg:border-r">
          <h3 className="text-[13px] font-bold text-fg">On chart</h3>
          <ul className="mt-3 space-y-2">
            {events.map((ev) => (
              <li
                key={ev.id}
                className="rounded-xl border border-border bg-elevated/40 px-3 py-2.5"
              >
                <div className="text-[13px] font-semibold text-fg">{ev.title}</div>
                {ev.body && (
                  <p className="mt-0.5 text-[12px] leading-snug text-muted">{ev.body}</p>
                )}
                <div className="mt-1 text-[11px] text-muted">
                  @ {ev.t} · {ev.price.toFixed(2)}
                </div>
              </li>
            ))}
            {events.length === 0 && (
              <li className="text-[13px] text-muted">No annotated events yet.</li>
            )}
          </ul>
        </section>

        <section className="min-h-0">
          <div className="border-b border-border px-4 py-2.5">
            <h3 className="text-[13px] font-bold text-fg">Activity · {sym}</h3>
            <p className="text-[11px] text-muted">
              Filtered feed — newest first
              {filteredActivity[0]
                ? ` · last ${relativeTime(filteredActivity[0].ts)}`
                : ""}
            </p>
          </div>
          {filteredActivity.length > 0 ? (
            <ActivityFeed items={filteredActivity} />
          ) : (
            <p className="px-4 py-8 text-center text-[13px] text-muted">
              No activity mentions {sym} yet — Start agent or pick a name in the book.
            </p>
          )}
        </section>
      </div>

      {/* Quick switch book names */}
      {bookNames.length > 1 && (
        <div className="border-t border-border px-4 py-3">
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted">
            Switch focus
          </div>
          <div className="flex flex-wrap gap-1.5">
            {bookNames.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setFocusSymbol(s)}
                className={cn(
                  "rounded-full px-2.5 py-1 text-[12px] font-semibold transition-colors",
                  s === sym
                    ? "bg-fg text-bg"
                    : "bg-elevated text-muted ring-1 ring-border hover:text-fg",
                )}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function LevelChip({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "muted" | "loss" | "gain";
}) {
  return (
    <span
      className={cn(
        "rounded-full border px-2.5 py-1 text-[11px] font-semibold tabular",
        tone === "loss" && "border-loss/30 text-loss",
        tone === "gain" && "border-gain/30 text-gain",
        tone === "muted" && "border-border text-muted",
      )}
    >
      {label} {value.toFixed(2)}
    </span>
  );
}
