import {
  CartesianGrid,
  Line,
  LineChart,
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
};

export function FocusChart({
  bars,
  levels,
  events,
  className,
  height = 340,
}: {
  bars: FocusBar[];
  levels: ChartLevel[];
  events: ChartEvent[];
  className?: string;
  height?: number;
}) {
  const eventByT = new Map(events.map((e) => [e.t, e]));
  const data = bars.map((b) => ({
    ...b,
    _event: eventByT.get(b.t)?.kind as ChartEvent["kind"] | undefined,
  }));

  const shown = levels.filter((l, i, arr) => {
    if (l.role === "entry" && arr.some((x) => x.role === "avg" && x.price === l.price)) {
      return false;
    }
    return true;
  });

  return (
    <div className={cn("w-full", className)} style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 12, right: 12, left: 0, bottom: 4 }}>
          <CartesianGrid stroke="rgba(47,51,54,0.85)" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="t"
            tick={{ fill: "#71767b", fontSize: 10 }}
            tickLine={false}
            axisLine={{ stroke: "#2f3336" }}
            minTickGap={28}
          />
          <YAxis
            domain={["auto", "auto"]}
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
            formatter={(value: number | string) => [
              typeof value === "number" ? value.toFixed(2) : value,
              "Price",
            ]}
          />
          {shown.map((lv) => (
            <ReferenceLine
              key={`${lv.role}-${lv.price}`}
              y={lv.price}
              stroke={levelColor(lv.role)}
              strokeDasharray={lv.role === "avg" || lv.role === "entry" ? "4 4" : undefined}
              strokeWidth={lv.role === "last" ? 1 : 1.5}
              label={{
                value: `${lv.label} ${lv.price.toFixed(2)}`,
                fill: levelColor(lv.role),
                fontSize: 10,
                position: "insideTopRight",
              }}
            />
          ))}
          <Line
            type="monotone"
            dataKey="price"
            stroke="#1d9bf0"
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4, fill: "#1d9bf0" }}
            isAnimationActive={false}
          />
          {/* Event markers as second series with sparse points */}
          <Line
            type="monotone"
            dataKey="price"
            stroke="transparent"
            strokeWidth={0}
            isAnimationActive={false}
            legendType="none"
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

export function FocusLegend({ events }: { events: ChartEvent[] }) {
  return (
    <div className="flex flex-wrap gap-3 text-[11px] text-muted">
      <LegendSwatch color="#1d9bf0" label="Price" />
      <LegendSwatch color="#f4212e" label="Stop" />
      <LegendSwatch color="#00ba7c" label="Target" />
      <LegendSwatch color="#71767b" label="Avg / entry" dashed />
      {events.some((e) => e.kind === "fill") && (
        <LegendSwatch color="#1d9bf0" label="Fill" dot />
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
          className="inline-block w-4"
          style={{
            borderTop: `2px ${dashed ? "dashed" : "solid"} ${color}`,
          }}
        />
      )}
      {label}
    </span>
  );
}
