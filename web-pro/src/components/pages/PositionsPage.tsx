import { cn, formatUsd } from "@/lib/utils";
import { useAbcxStore } from "@/store/abcx-store";

export function PositionsPage() {
  const positions = useAbcxStore((s) => s.positions);
  const orders = useAbcxStore((s) => s.orders);

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
          <table className="w-full min-w-[640px] text-left text-[13px]">
            <thead className="bg-elevated/60 text-muted">
              <tr>
                {["Symbol", "Type", "Qty", "Price", "uPnL", "Protection", "Details"].map(
                  (h) => (
                    <th key={h} className="px-3 py-2.5 font-medium">
                      {h}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {positions.map((p) => (
                <tr key={p.conId} className="hover:bg-elevated/30">
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
                  <td className="max-w-[220px] truncate px-3 py-3 text-muted">
                    {p.details}
                  </td>
                </tr>
              ))}
              {positions.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-3 py-8 text-center text-muted">
                    Flat — no open risk at broker (sim).
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="border-b border-border px-4 py-4">
        <h2 className="text-sm font-bold text-fg">Working orders</h2>
        <ul className="mt-3 divide-y divide-border rounded-xl border border-border">
          {orders.map((o) => (
            <li
              key={o.id}
              className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5 text-[13px]"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-bold text-fg">{o.symbol}</span>
                <span
                  className={cn(
                    "font-semibold",
                    o.side === "BUY" ? "text-gain" : "text-loss",
                  )}
                >
                  {o.side}
                </span>
                <span className="text-muted">{o.type}</span>
                <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted">
                  {o.role}
                </span>
              </div>
              <div className="tabular text-muted">
                {o.qty} @ {formatUsd(o.price)} · {o.status}
              </div>
            </li>
          ))}
          {orders.length === 0 && (
            <li className="px-3 py-6 text-center text-sm text-muted">No working orders</li>
          )}
        </ul>
      </section>

      <section className="px-4 py-4">
        <h2 className="text-sm font-bold text-fg">Fills blotter</h2>
        <p className="mt-2 text-[13px] text-muted">
          Session fills appear here from the monitor. In this preview, protection attaches and
          panic flatten write activity on Dashboard.
        </p>
      </section>
    </div>
  );
}
