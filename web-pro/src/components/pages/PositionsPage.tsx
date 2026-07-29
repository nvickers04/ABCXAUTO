import { Crosshair } from "lucide-react";
import { cn, formatUsd } from "@/lib/utils";
import { useAbcxStore } from "@/store/abcx-store";

export function PositionsPage() {
  const positions = useAbcxStore((s) => s.positions);
  const orders = useAbcxStore((s) => s.orders);
  const setFocusSymbol = useAbcxStore((s) => s.setFocusSymbol);
  const focusSymbol = useAbcxStore((s) => s.focusSymbol);

  const totalU = positions.reduce((a, p) => a + p.uPnl, 0);

  return (
    <div className="space-y-0">
      <section className="border-b border-border px-4 py-4">
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 className="text-sm font-bold text-fg">Book</h2>
            <p className="text-[12px] text-muted">
              {positions.length === 0
                ? "No open positions"
                : `${positions.length} open · uPnL ${formatUsd(totalU, { signed: true })}`}
            </p>
          </div>
        </div>

        <div className="mt-3 overflow-x-auto scroll-thin rounded-xl border border-border">
          <table className="w-full min-w-[720px] text-left text-[13px]">
            <thead className="bg-elevated/60 text-muted">
              <tr>
                {["Symbol", "Type", "Qty", "Price", "uPnL", "Protection", "Details", ""].map(
                  (h) => (
                    <th key={h || "actions"} className="px-3 py-2.5 font-medium">
                      {h}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {positions.map((p) => (
                <tr
                  key={p.conId}
                  className={cn(
                    "hover:bg-elevated/30",
                    focusSymbol === p.symbol && "bg-primary/5",
                  )}
                >
                  <td className="px-3 py-3 font-bold text-fg">{p.symbol}</td>
                  <td className="px-3 py-3 text-muted">{p.type}</td>
                  <td className="tabular px-3 py-3 text-fg">{p.qty}</td>
                  <td className="tabular px-3 py-3 text-fg">{formatUsd(p.price)}</td>
                  <td
                    className={cn(
                      "tabular px-3 py-3 font-semibold",
                      p.uPnl >= 0 ? "text-gain" : "text-loss",
                    )}
                  >
                    {formatUsd(p.uPnl, { signed: true })}
                  </td>
                  <td className="px-3 py-3">
                    <span
                      className={cn(
                        "rounded-full px-2 py-0.5 text-[11px] font-semibold",
                        p.protected
                          ? "bg-gain-soft text-gain"
                          : "bg-loss-soft text-loss",
                      )}
                    >
                      {p.protected ? "protected" : "unprotected"}
                    </span>
                  </td>
                  <td className="max-w-[200px] truncate px-3 py-3 text-muted">
                    {p.details}
                  </td>
                  <td className="px-3 py-3">
                    {p.type === "STK" && (
                      <button
                        type="button"
                        onClick={() => setFocusSymbol(p.symbol)}
                        className="inline-flex items-center gap-1 rounded-full border border-border px-2.5 py-1 text-[11px] font-semibold text-fg transition-colors hover:border-primary/40 hover:bg-elevated"
                        title={`Focus ${p.symbol}`}
                      >
                        <Crosshair className="h-3 w-3 text-primary" />
                        Focus
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {positions.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-3 py-8 text-center text-muted">
                    Flat — no open risk at broker (sim).
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="px-4 py-4">
        <h2 className="text-sm font-bold text-fg">Working orders</h2>
        <ul className="mt-3 divide-y divide-border rounded-xl border border-border">
          {orders.map((o) => (
            <li
              key={o.id}
              className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5 text-[13px]"
            >
              <div className="flex items-center gap-2">
                <span className="font-bold text-fg">{o.symbol}</span>
                <span className="text-muted">
                  {o.side} {o.type}
                </span>
                <span className="rounded-full bg-elevated px-2 py-0.5 text-[10px] uppercase text-muted">
                  {o.role}
                </span>
              </div>
              <div className="flex items-center gap-3">
                <span className="tabular text-fg">
                  {o.qty} @ {formatUsd(o.price)}
                </span>
                <span className="text-muted">{o.status}</span>
                <button
                  type="button"
                  onClick={() => setFocusSymbol(o.symbol)}
                  className="text-[11px] font-semibold text-primary hover:underline"
                >
                  Focus
                </button>
              </div>
            </li>
          ))}
          {orders.length === 0 && (
            <li className="px-3 py-6 text-center text-sm text-muted">No working orders</li>
          )}
        </ul>
      </section>
    </div>
  );
}
