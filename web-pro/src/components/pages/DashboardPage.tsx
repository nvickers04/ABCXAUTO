import { RefreshCw } from "lucide-react";
import { cn, formatUsd } from "@/lib/utils";
import { useAbcxStore } from "@/store/abcx-store";
import { ActivityFeed, ComposeBox } from "@/components/feed/ActivityFeed";

/**
 * Dashboard hierarchy (ops, not marketing):
 * 1. Status strip — one glance
 * 2. Now + last cycle — what the agent is doing
 * 3. Compose — optional mandate
 * 4. Activity feed — primary surface (X timeline)
 *
 * Book metrics live on the right rail so the feed isn't buried.
 */
export function DashboardPage() {
  const mode = useAbcxStore((s) => s.mode);
  const cycles = useAbcxStore((s) => s.cycles);
  const dayPnl = useAbcxStore((s) => s.dayPnl);
  const positions = useAbcxStore((s) => s.positions);
  const risk = useAbcxStore((s) => s.risk);
  const agentNow = useAbcxStore((s) => s.agentNow);
  const judgment = useAbcxStore((s) => s.judgment);
  const proposal = useAbcxStore((s) => s.proposal);
  const pace = useAbcxStore((s) => s.pace);
  const attention = useAbcxStore((s) => s.attention);
  const pulseNarrative = useAbcxStore((s) => s.pulseNarrative);
  const activity = useAbcxStore((s) => s.activity);
  const tickSim = useAbcxStore((s) => s.tickSim);

  const unprotected = positions.filter((p) => p.type === "STK" && !p.protected).length;
  const alert = unprotected > 0 || risk.halt;

  return (
    <div className="flex min-h-0 flex-col">
      {/* Compact status strip */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border px-4 py-2.5 text-[12px]">
        <StripItem label="Mode" value={mode} />
        <StripItem label="Cycles" value={String(cycles)} mono />
        <StripItem
          label="Day"
          value={formatUsd(dayPnl, { signed: true, compact: true })}
          tone={dayPnl >= 0 ? "gain" : "loss"}
          mono
        />
        <StripItem
          label="Unprot"
          value={String(unprotected)}
          tone={unprotected > 0 ? "loss" : "gain"}
          mono
        />
        <StripItem
          label="Halt"
          value={risk.halt ? "on" : "off"}
          tone={risk.halt ? "loss" : "gain"}
        />
        <button
          type="button"
          onClick={tickSim}
          className="ml-auto inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-muted transition-colors hover:bg-elevated hover:text-fg"
          title="Refresh snapshot"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">Refresh</span>
        </button>
      </div>

      {/* Now — single hero block (merged formerly split sections) */}
      <section
        className={cn(
          "border-b border-border px-4 py-4",
          alert && "bg-loss-soft/40",
        )}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-muted">
            Now
          </div>
          <div className="max-w-[55%] truncate text-right text-[11px] text-muted">
            {pace.replace(/^Pace:\s*/i, "")}
          </div>
        </div>
        <p className="mt-1.5 text-[18px] font-bold leading-snug tracking-tight text-fg">
          {agentNow}
        </p>
        <p className="mt-2 text-[13px] leading-snug text-muted">{attention}</p>
        {pulseNarrative && (
          <p className="mt-1 text-[13px] leading-snug text-fg/85">{pulseNarrative}</p>
        )}
        <div className="mt-3 space-y-1.5 rounded-xl border border-border bg-elevated/40 px-3 py-2.5">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-muted">
            Last cycle
          </div>
          <p className="text-[13px] leading-snug text-muted">{judgment}</p>
          <p className="text-[14px] leading-snug text-fg">{proposal}</p>
        </div>
      </section>

      <ComposeBox />

      <section className="min-h-0 flex-1">
        <div className="sticky top-0 z-[1] flex items-center justify-between border-b border-border bg-bg/90 px-4 py-2.5 backdrop-blur-md">
          <h2 className="text-[15px] font-bold text-fg">Activity</h2>
          <span className="text-[11px] text-muted">Newest first</span>
        </div>
        <ActivityFeed items={activity} />
      </section>
    </div>
  );
}

function StripItem({
  label,
  value,
  tone,
  mono,
}: {
  label: string;
  value: string;
  tone?: "gain" | "loss";
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-muted">{label}</span>
      <span
        className={cn(
          "font-semibold text-fg",
          mono && "tabular",
          tone === "gain" && "text-gain",
          tone === "loss" && "text-loss",
        )}
      >
        {value}
      </span>
    </div>
  );
}
