import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { cn } from "@/lib/utils";
import {
  levelColor,
  type ChartEvent,
  type ChartLevel,
  type FocusBar,
} from "@/lib/focus-chart";

const EVENT_DOT: Record<ChartEvent["kind"], string> = {
  fill: "#1d9bf0",
  judge: "#7856ff",
  act: "#00ba7c",
  gate: "#ffd400",
  risk: "#f4212e",
  planned: "#ffd400",
};

export function FocusChart({
  bars,
  levels,
  events,
  entry,
  side = "long",
  className,
  height = 340,
}: {
  bars: FocusBar[];
  levels: ChartLevel[];
  events: ChartEvent[];
  /** Entry / avg for PnL bands */
  entry?: number | null;
  side?: "long" | "short";
  className?: string;
  height?: number;
}) {
  const eventByT = new Map(events.map((e) => [e.t, e]));
  const data = bars.map((b) => ({
    ...b,
    _event: eventByT.get(b.t)?.kind as ChartEvent["kind"] | undefined,
  }));

  const prices = bars.map((b) => b.price);
  const yMin = Math.min(...prices, entry ?? Infinity, ...levels.map((l) => l.price));
  const yMax = Math.max(...prices, entry ?? -Infinity, ...levels.map((l) => l.price));
  const pad = (yMax - yMin) * 0.06 || 1;

  const shown = levels.filter((l) => {
    // prefer Entry label over duplicate avg
    if (l.role === "avg") return false;
    return true;
  });

  const last = bars[bars.length - 1]?.price;
  const inProfit =
    entry != null && last != null
      ? side === "long"
        ? last >= entry
        : last <= entry
      : null;

  // PnL zone: long → green above entry, red below
  const gainY1 = entry ?? 0;
  const gainY2 = side === "long" ? yMax + pad : yMin - pad;
  const lossY1 = entry ?? 0;
  const lossY2 = side === "long" ? yMin - pad : yMax + pad;

  return (
    <div className={cn("relative w-full", className)} style={{ height }}>
      {inProfit != null && (
        <div
          className={cn(
            "pointer-events-none absolute left-3 top-2 z-[1] rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide",
            inProfit ? "bg-gain/20 text-gain" : "bg-loss/20 text-loss",
          )}
        >
          {inProfit ? "In profit" : "In loss"}
        </div>
      )}
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 16, right: 12, left: 0, bottom: 4 }}>
          <CartesianGrid stroke="rgba(47,51,54,0.85)" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="t"
            tick={{ fill: "#71767b", fontSize: 10 }}
            tickLine={false}
            axisLine={{ stroke: "#2f3336" }}
            minTickGap={28}
          />
          <YAxis
            domain={[yMin - pad, yMax + pad]}
            orientation="right"
            tick={{ fill: "#71767b", fontSize: 10 }}
            tickLine={false}
            axisLine={false}
            width={52}
            tickFormatter={(v) => Number(v).toFixed(1)}
          />
          <Tooltip
            contentStyle={{
              background: "#16181c",
              border: "1px solid #2f3336",
              borderRadius: 12,
              fontSize: 12,
            }}
            labelStyle={{ color: "#71767b" }}
            formatter={(value: number | string, name: string) => {
              if (name !== "price" && name !== "Price") return [null, null];
              const n = typeof value === "number" ? value : Number(value);
              const vs =
                entry != null && Number.isFinite(n)
                  ? ` · ${n >= entry ? "+" : ""}${(((n - entry) / entry) * 100).toFixed(2)}% vs entry`
                  : "";
              return [`${n.toFixed(2)}${vs}`, "Price"];
            }}
          />

          {/* Green / red bands around entry */}
          {entry != null && (
            <>
              <ReferenceArea
                y1={Math.min(gainY1, gainY2)}
                y2={Math.max(gainY1, gainY2)}
                fill="#00ba7c"
                fillOpacity={0.1}
                ifOverflow="extendDomain"
              />
              <ReferenceArea
                y1={Math.min(lossY1, lossY2)}
                y2={Math.max(lossY1, lossY2)}
                fill="#f4212e"
                fillOpacity={0.1}
                ifOverflow="extendDomain"
              />
            </>
          )}

          {shown.map((lv) => (
            <ReferenceLine
              key={`${lv.role}-${lv.price}`}
              y={lv.price}
              stroke={levelColor(lv.role)}
              strokeDasharray={
                lv.role === "entry" || lv.role === "planned" || lv.role === "avg"
                  ? "5 4"
                  : undefined
              }
              strokeWidth={lv.role === "entry" || lv.role === "planned" ? 2 : lv.role === "last" ? 1 : 1.5}
              label={{
                value: `${lv.label} ${lv.price.toFixed(2)}`,
                fill: levelColor(lv.role),
                fontSize: 10,
                position: "insideTopRight",
              }}
            />
          ))}

          {/* Full price path — color by current PnL state */}
          <Line
            type="monotone"
            dataKey="price"
            stroke={inProfit === false ? "#f4212e" : inProfit === true ? "#00ba7c" : "#1d9bf0"}
            strokeWidth={2.25}
            dot={false}
            activeDot={{ r: 4 }}
            isAnimationActive={false}
            name="price"
          />

          {/* Event markers */}
          <Line
            type="monotone"
            dataKey="price"
            stroke="transparent"
            strokeWidth={0}
            isAnimationActive={false}
            legendType="none"
            name="_events"
            dot={(props: {
              cx?: number;
              cy?: number;
              payload?: { _event?: ChartEvent["kind"]; t?: string };
            }) => {
              const { cx, cy, payload } = props;
              if (cx == null || cy == null || !payload?._event) {
                return <g key={`empty-${payload?.t ?? cx}`} />;
              }
              const fill = EVENT_DOT[payload._event];
              return (
                <circle
                  key={payload.t}
                  cx={cx}
                  cy={cy}
                  r={5}
                  fill={fill}
                  stroke="#000"
                  strokeWidth={1.5}
                />
              );
            }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function FocusLegend({
  events,
  hasEntry,
}: {
  events: ChartEvent[];
  hasEntry?: boolean;
}) {
  return (
    <div className="flex flex-wrap gap-3 text-[11px] text-muted">
      <LegendSwatch color="#00ba7c" label="Profit zone" />
      <LegendSwatch color="#f4212e" label="Loss zone" />
      {hasEntry && <LegendSwatch color="#1d9bf0" label="Entry" dashed />}
      <LegendSwatch color="#f4212e" label="Stop" />
      <LegendSwatch color="#00ba7c" label="Target" />
      {events.some((e) => e.kind === "fill") && (
        <LegendSwatch color="#1d9bf0" label="Fill" dot />
      )}
      {events.some((e) => e.kind === "planned") && (
        <LegendSwatch color="#ffd400" label="Planned" dot />
      )}
      {events.some((e) => e.kind === "judge") && (
        <LegendSwatch color="#7856ff" label="Judge" dot />
      )}
      {events.some((e) => e.kind === "act") && (
        <LegendSwatch color="#00ba7c" label="Act" dot />
      )}
    </div>
  );
}

function LegendSwatch({
  color,
  label,
  dashed,
  dot,
}: {
  color: string;
  label: string;
  dashed?: boolean;
  dot?: boolean;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      {dot ? (
        <span className="h-2 w-2 rounded-full" style={{ background: color }} />
      ) : (
        <span
          className="inline-block h-2.5 w-3 rounded-sm opacity-80"
          style={{
            background: dashed ? "transparent" : color,
            borderTop: dashed ? `2px dashed ${color}` : undefined,
            opacity: dashed ? 1 : 0.35,
          }}
        />
      )}
      {label}
    </span>
  );
}
