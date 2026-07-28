import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { cn, formatUsd } from "@/lib/utils";
import { useAbcxStore } from "@/store/abcx-store";

export function ScorecardPage() {
  const equityCurve = useAbcxStore((s) => s.equityCurve);
  const activity = useAbcxStore((s) => s.activity);
  const dayPnl = useAbcxStore((s) => s.dayPnl);
  const cycles = useAbcxStore((s) => s.cycles);
  const equity = useAbcxStore((s) => s.equity);

  const proposals = activity.filter((a) => a.kind === "act" || a.kind === "judge").length;
  const allowed = activity.filter((a) => a.kind === "gate" && a.title.includes("allowed")).length;
  const rejected = activity.filter(
    (a) => a.kind === "gate" && a.title.toLowerCase().includes("reject"),
  ).length;
  const fills = activity.filter((a) => a.kind === "fill").length;

  const start = equityCurve[0]?.equity ?? equity;
  const vsSpy =
    equityCurve.length > 1
      ? (equityCurve[equityCurve.length - 1]!.equity / start - 1) * 100 -
        (equityCurve[equityCurve.length - 1]!.spy / (equityCurve[0]?.spy ?? 1) - 1) * 100
      : 0;

  const chartData = equityCurve.map((p) => ({
    date: p.date.slice(5),
    Book: p.equity,
    SPY: p.spy,
  }));

  return (
    <div className="px-4 py-4">
      <div className="mb-4">
        <h2 className="text-xl font-bold text-fg">Scorecard</h2>
        <p className="mt-1 max-w-xl text-[13px] text-muted">
          Forward paper evidence. Goal: return on startup cash exceeds cost of intelligence.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Metric label="Equity" value={formatUsd(equity, { compact: true })} />
        <Metric
          label="Day PnL"
          value={formatUsd(dayPnl, { signed: true, compact: true })}
          tone={dayPnl >= 0 ? "gain" : "loss"}
        />
        <Metric label="Cycles" value={String(cycles)} />
        <Metric
          label="vs SPY (60d sim)"
          value={`${vsSpy >= 0 ? "+" : ""}${vsSpy.toFixed(1)}%`}
          tone={vsSpy >= 0 ? "gain" : "loss"}
        />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Metric label="Proposals / judge" value={String(proposals)} />
        <Metric label="Gates allowed" value={String(allowed)} tone="gain" />
        <Metric label="Gates rejected" value={String(rejected)} tone="loss" />
        <Metric label="Fills" value={String(fills)} tone="gain" />
      </div>

      <div className="mt-6 rounded-2xl border border-border bg-elevated/30 p-4">
        <div className="mb-3 flex items-baseline justify-between">
          <h3 className="text-sm font-bold text-fg">Equity vs SPY</h3>
          <span className="text-[11px] text-muted">Simulated forward path</span>
        </div>
        <div className="h-56 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id="bookFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#1d9bf0" stopOpacity={0.35} />
                  <stop offset="100%" stopColor="#1d9bf0" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#2f3336" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="date"
                tick={{ fill: "#71767b", fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                minTickGap={24}
              />
              <YAxis
                tick={{ fill: "#71767b", fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                width={56}
                tickFormatter={(v) => `$${(Number(v) / 1000).toFixed(0)}k`}
              />
              <Tooltip
                contentStyle={{
                  background: "#16181c",
                  border: "1px solid #2f3336",
                  borderRadius: 12,
                  fontSize: 12,
                }}
                labelStyle={{ color: "#71767b" }}
                formatter={(value) => formatUsd(Number(value))}
              />
              <Area
                type="monotone"
                dataKey="SPY"
                stroke="#71767b"
                fill="transparent"
                strokeWidth={1.5}
                dot={false}
              />
              <Area
                type="monotone"
                dataKey="Book"
                stroke="#1d9bf0"
                fill="url(#bookFill)"
                strokeWidth={2}
                dot={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="mt-6">
        <h3 className="mb-2 text-sm font-bold text-fg">Recent dispatches</h3>
        <ul className="divide-y divide-border rounded-xl border border-border">
          {activity
            .filter((a) => a.kind === "act" || a.kind === "fill" || a.kind === "gate")
            .slice(0, 8)
            .map((a) => (
              <li key={a.id} className="px-3 py-2.5 text-[13px]">
                <div className="font-semibold text-fg">{a.title}</div>
                <div className="mt-0.5 line-clamp-2 text-muted">{a.body}</div>
              </li>
            ))}
        </ul>
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "gain" | "loss";
}) {
  return (
    <div className="rounded-xl border border-border bg-elevated/40 px-3 py-3">
      <div className="text-[11px] text-muted">{label}</div>
      <div
        className={cn(
          "mt-1 tabular text-xl font-bold tracking-tight",
          tone === "gain" && "text-gain",
          tone === "loss" && "text-loss",
          !tone && "text-fg",
        )}
      >
        {value}
      </div>
    </div>
  );
}
