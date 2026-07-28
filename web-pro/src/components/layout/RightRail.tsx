import { cn, formatPct, formatUsd, relativeTime } from "@/lib/utils";
import { useAbcxStore } from "@/store/abcx-store";
import { postureLabel } from "@/lib/abcx-data";

export function RightRail({ force = false }: { force?: boolean }) {
  const equity = useAbcxStore((s) => s.equity);
  const dayPnl = useAbcxStore((s) => s.dayPnl);
  const ret1w = useAbcxStore((s) => s.ret1w);
  const ret3m = useAbcxStore((s) => s.ret3m);
  const ret1y = useAbcxStore((s) => s.ret1y);
  const connected = useAbcxStore((s) => s.connected);
  const xaiOk = useAbcxStore((s) => s.xaiOk);
  const mdaOk = useAbcxStore((s) => s.mdaOk);
  const news = useAbcxStore((s) => s.news);
  const risk = useAbcxStore((s) => s.risk);
  const mode = useAbcxStore((s) => s.mode);
  const cycles = useAbcxStore((s) => s.cycles);
  const positions = useAbcxStore((s) => s.positions);

  const unprotected = positions.filter((p) => p.type === "STK" && !p.protected).length;

  return (
    <aside
      className={cn(
        "h-full w-full shrink-0 flex-col gap-3 overflow-y-auto scroll-thin bg-bg p-3",
        force ? "flex border-0" : "hidden w-[320px] border-l border-border xl:flex",
      )}
    >
      {/* Equity hero — denser than before */}
      <div className="rounded-2xl border border-border bg-elevated p-4">
        <div className="flex items-start justify-between gap-2">
          <div className="text-[13px] font-semibold text-muted">Net liq</div>
          <div className="flex items-center gap-1.5 text-[11px] text-muted">
            <Dot ok={connected} />
            <Dot ok={xaiOk} />
            <Dot ok={mdaOk} />
          </div>
        </div>
        <div className="mt-1 tabular text-[28px] font-bold leading-none tracking-tight text-fg">
          {formatUsd(equity, { compact: true })}
        </div>
        <div
          className={cn(
            "mt-2 tabular text-[13px] font-semibold",
            dayPnl >= 0 ? "text-gain" : "text-loss",
          )}
        >
          {formatUsd(dayPnl, { signed: true })} today
        </div>
        <div className="mt-3 grid grid-cols-3 gap-1 border-t border-border pt-3">
          <Ret label="1W" value={ret1w} />
          <Ret label="3M" value={ret3m} />
          <Ret label="1Y" value={ret1y} />
        </div>
      </div>

      {/* Book pulse — moved from cluttering dashboard top */}
      <div className="rounded-2xl border border-border bg-elevated p-4">
        <div className="text-[13px] font-bold text-fg">Book pulse</div>
        <dl className="mt-3 space-y-2 text-[13px]">
          <Row k="Mode" v={mode} />
          <Row k="Cycles" v={String(cycles)} />
          <Row
            k="Unprotected"
            v={String(unprotected)}
            tone={unprotected > 0 ? "loss" : "gain"}
          />
          <Row
            k="Halt"
            v={risk.halt ? "latched" : "clear"}
            tone={risk.halt ? "loss" : "gain"}
          />
          <Row k="Posture" v={postureLabel(risk.posture)} />
          <Row k="Open" v={`${positions.length} names`} />
        </dl>
      </div>

      {/* News — tighter */}
      <div className="rounded-2xl border border-border bg-elevated p-4">
        <div className="text-[13px] font-bold text-fg">What's happening</div>
        <ul className="mt-2 divide-y divide-border">
          {news.slice(0, 4).map((n) => (
            <li key={n.id} className="py-2.5 first:pt-1 last:pb-0">
              <div className="text-[13px] font-medium leading-snug text-fg">
                {n.headline}
              </div>
              <div className="mt-1 flex items-center gap-1.5 text-[11px] text-muted">
                <span className="font-semibold text-primary">{n.related}</span>
                <span>·</span>
                <span>{relativeTime(n.ts)}</span>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </aside>
  );
}

function Dot({ ok }: { ok: boolean }) {
  return (
    <span
      className={cn("h-2 w-2 rounded-full", ok ? "bg-gain" : "bg-loss")}
      title={ok ? "up" : "down"}
    />
  );
}

function Row({
  k,
  v,
  tone,
}: {
  k: string;
  v: string;
  tone?: "gain" | "loss";
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-muted">{k}</dt>
      <dd
        className={cn(
          "tabular font-semibold text-fg",
          tone === "gain" && "text-gain",
          tone === "loss" && "text-loss",
        )}
      >
        {v}
      </dd>
    </div>
  );
}

function Ret({ label, value }: { label: string; value: number }) {
  return (
    <div className="text-center">
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div
        className={cn(
          "tabular text-[13px] font-semibold",
          value >= 0 ? "text-gain" : "text-loss",
        )}
      >
        {formatPct(value)}
      </div>
    </div>
  );
}
