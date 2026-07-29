import { useEffect, useMemo, useState } from "react";
import { Crosshair, X } from "lucide-react";
import { cn, formatUsd, relativeTime } from "@/lib/utils";
import { useAbcxStore } from "@/store/abcx-store";
import {
  buildFocusSeries,
  buildFocusSideItems,
  type FocusRange,
  type FocusSideItem,
} from "@/lib/focus-chart";
import { FocusChart, FocusLegend } from "@/components/charts/FocusChart";
import { Button } from "@/components/ui/button";
import { ActivityFeed } from "@/components/feed/ActivityFeed";
import { apiBars } from "@/lib/pro-api";

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
  const [activeId, setActiveId] = useState<string | null>(null);
  const [liveBars, setLiveBars] = useState<{ t: string; c: number }[] | null>(null);
  const [barSource, setBarSource] = useState<string>("demo");
  const dataSource = useAbcxStore((s) => s.dataSource);

  const sideItems = useMemo(
    () => buildFocusSideItems(positions, orders),
    [positions, orders],
  );

  useEffect(() => {
    if (sideItems.length === 0) {
      setActiveId(null);
      return;
    }
    const bySym = focusSymbol
      ? sideItems.find((i) => i.symbol === focusSymbol.toUpperCase())
      : null;
    if (bySym) {
      setActiveId(bySym.id);
      return;
    }
    if (!activeId || !sideItems.some((i) => i.id === activeId)) {
      setActiveId(sideItems[0]!.id);
      if (!focusSymbol) setFocusSymbol(sideItems[0]!.symbol);
    }
  }, [sideItems, focusSymbol]); // eslint-disable-line react-hooks/exhaustive-deps

  const active: FocusSideItem | null =
    sideItems.find((i) => i.id === activeId) ?? sideItems[0] ?? null;

  const sym = active?.symbol ?? (focusSymbol || "").toUpperCase();

  useEffect(() => {
    let cancelled = false;
    if (!sym) {
      setLiveBars(null);
      setBarSource("demo");
      return;
    }
    void (async () => {
      try {
        const res = await apiBars(sym, range);
        if (cancelled) return;
        if (res.bars?.length) {
          setLiveBars(res.bars.map((b) => ({ t: b.t, c: b.c })));
          setBarSource(res.source || "live");
        } else {
          setLiveBars(null);
          setBarSource("demo");
        }
      } catch {
        if (!cancelled) {
          setLiveBars(null);
          setBarSource("demo");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sym, range, dataSource]);

    const { bars, levels, events, entry, side } = useMemo(() => {
    if (!active) {
      return buildFocusSeries(sym || "DEMO", range, { last: 100 });
    }
    return buildFocusSeries(active.symbol, range, {
      last: active.last,
      avgCost: active.kind === "position" ? active.entry : undefined,
      plannedEntry: active.kind === "planned" ? active.entry : undefined,
      stop: active.stop,
      target: active.target,
      side: active.side,
      isPlanned: active.kind === "planned",
      externalBars: liveBars ?? undefined,
    });
  }, [active, range, sym, liveBars]);

  const filteredActivity = activity.filter((a) => {
    if (!sym) return false;
    const blob = `${a.title} ${a.body} ${JSON.stringify(a.meta || {})}`.toUpperCase();
    return blob.includes(sym);
  });

  const pnlPct =
    active?.entry && active.entry > 0 && active.kind === "position"
      ? ((active.last - active.entry) / active.entry) * 100 * (active.side === "short" ? -1 : 1)
      : null;

  function selectItem(item: FocusSideItem) {
    setActiveId(item.id);
    setFocusSymbol(item.symbol);
  }

  if (sideItems.length === 0 && !sym) {
    return (
      <div className="flex min-h-[480px] flex-col items-center justify-center px-6 py-16 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-elevated ring-1 ring-border">
          <Crosshair className="h-5 w-5 text-primary" />
        </div>
        <h2 className="mt-4 text-[18px] font-bold text-fg">Focus</h2>
        <p className="mt-2 max-w-sm text-[13px] leading-snug text-muted">
          Open a position or queue an entry — tabs appear on the right for each name.
        </p>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-[640px] min-w-0 flex-col md:flex-row">
      {/* Chart + detail */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto scroll-thin border-b border-border md:border-b-0 md:border-r">
        <div className="border-b border-border px-4 py-3 sm:px-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className={cn(
                    "rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide",
                    active?.kind === "planned"
                      ? "bg-warn/15 text-warn"
                      : "bg-primary/15 text-primary",
                  )}
                >
                  {active?.kind === "planned" ? "Planned entry" : "Open"}
                </span>
                <h2 className="text-[22px] font-bold tracking-tight text-fg">{sym}</h2>
                {active && (
                  <span className="text-[11px] font-semibold uppercase text-muted">
                    {active.side}
                  </span>
                )}
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
                <span
                  className={cn(
                    "tabular text-[22px] font-bold",
                    pnlPct == null ? "text-fg" : pnlPct >= 0 ? "text-gain" : "text-loss",
                  )}
                >
                  {formatUsd(active?.last ?? 0)}
                </span>
                {active?.kind === "position" && active.uPnl != null && (
                  <span
                    className={cn(
                      "rounded-full px-2.5 py-0.5 tabular text-[14px] font-bold",
                      active.uPnl >= 0 ? "bg-gain/15 text-gain" : "bg-loss/15 text-loss",
                    )}
                  >
                    {formatUsd(active.uPnl, { signed: true })}
                    {pnlPct != null && (
                      <span className="ml-1 text-[12px] font-semibold opacity-90">
                        ({pnlPct >= 0 ? "+" : ""}
                        {pnlPct.toFixed(2)}%)
                      </span>
                    )}
                  </span>
                )}
                {active?.kind === "planned" && (
                  <span className="text-[12px] text-warn">Awaiting fill</span>
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
              </Button>
            </div>
          </div>

          <div className="mt-3 flex flex-wrap gap-2">
            {entry != null && (
              <LevelChip
                label={active?.kind === "planned" ? "Plan" : "Entry"}
                value={entry}
                tone="entry"
              />
            )}
            {active?.stop != null && (
              <LevelChip label="Stop" value={active.stop} tone="loss" />
            )}
            {active?.target != null && (
              <LevelChip label="Target" value={active.target} tone="gain" />
            )}
            {active?.qty != null && (
              <span className="rounded-full border border-border px-2.5 py-1 text-[11px] text-muted">
                qty {active.qty}
              </span>
            )}
          </div>
        </div>

        <div className="border-b border-border px-2 py-3 sm:px-4">
          <FocusChart
            bars={bars}
            levels={levels}
            events={events}
            entry={entry}
            side={side}
            height={380}
          />
          <div className="mt-2 px-2">
            <FocusLegend events={events} hasEntry={entry != null} />
            <p className="mt-1 text-[11px] text-muted">
              Green = profit vs entry · Red = loss · Bars:{" "}
              {barSource === "demo" ? "demo path" : barSource} · Book: {dataSource}
            </p>
          </div>
        </div>

        <div className="grid min-h-0 lg:grid-cols-2">
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
                </li>
              ))}
            </ul>
          </section>
          <section>
            <div className="border-b border-border px-4 py-2.5">
              <h3 className="text-[13px] font-bold text-fg">Activity · {sym}</h3>
              <p className="text-[11px] text-muted">
                {filteredActivity[0]
                  ? `Last ${relativeTime(filteredActivity[0].ts)}`
                  : "No matches yet"}
              </p>
            </div>
            {filteredActivity.length > 0 ? (
              <ActivityFeed items={filteredActivity} />
            ) : (
              <p className="px-4 py-8 text-center text-[13px] text-muted">
                No activity for {sym} yet.
              </p>
            )}
          </section>
        </div>
      </div>

      {/* Right position / planned tabs — always visible on md+ */}
      <aside className="flex w-full shrink-0 flex-col border-t border-border bg-bg md:w-[200px] md:border-l md:border-t-0 lg:w-[220px]">
        <div className="border-b border-border px-3 py-2.5">
          <div className="text-[11px] font-bold uppercase tracking-wide text-muted">
            Positions
          </div>
          <p className="text-[11px] text-muted">Open + planned</p>
        </div>
        <div className="flex gap-1.5 overflow-x-auto p-2 md:flex-col md:overflow-y-auto scroll-thin">
          {sideItems.map((item) => {
            const selected = item.id === (active?.id ?? activeId);
            const up =
              item.kind === "position" && item.uPnl != null ? item.uPnl >= 0 : null;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => selectItem(item)}
                className={cn(
                  "flex min-w-[132px] flex-col rounded-xl border px-3 py-2.5 text-left transition-colors md:min-w-0",
                  selected
                    ? "border-primary/50 bg-elevated ring-1 ring-primary/20"
                    : "border-border bg-bg hover:bg-elevated/50",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[14px] font-bold text-fg">{item.symbol}</span>
                  <span
                    className={cn(
                      "rounded-full px-1.5 py-0.5 text-[9px] font-bold uppercase",
                      item.kind === "planned"
                        ? "bg-warn/15 text-warn"
                        : "bg-elevated text-muted ring-1 ring-border",
                    )}
                  >
                    {item.kind === "planned" ? "plan" : "pos"}
                  </span>
                </div>
                <div className="mt-1 tabular text-[11px] text-muted">
                  {item.entry != null ? (
                    <span>
                      Entry <span className="text-primary">{item.entry.toFixed(2)}</span>
                    </span>
                  ) : (
                    "—"
                  )}
                </div>
                {item.kind === "position" && item.uPnl != null ? (
                  <div
                    className={cn(
                      "mt-1.5 rounded-md px-1.5 py-0.5 tabular text-[13px] font-bold",
                      up ? "bg-gain/15 text-gain" : "bg-loss/15 text-loss",
                    )}
                  >
                    {formatUsd(item.uPnl, { signed: true, compact: true })}
                  </div>
                ) : (
                  <div className="mt-1.5 text-[12px] font-semibold text-warn">Not filled</div>
                )}
              </button>
            );
          })}
        </div>
      </aside>
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
  tone: "muted" | "loss" | "gain" | "entry";
}) {
  return (
    <span
      className={cn(
        "rounded-full border px-2.5 py-1 text-[11px] font-semibold tabular",
        tone === "loss" && "border-loss/30 text-loss",
        tone === "gain" && "border-gain/30 text-gain",
        tone === "entry" && "border-primary/40 text-primary",
        tone === "muted" && "border-border text-muted",
      )}
    >
      {label} {value.toFixed(2)}
    </span>
  );
}
