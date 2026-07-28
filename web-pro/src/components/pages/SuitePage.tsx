import { cn } from "@/lib/utils";
import { useAbcxStore } from "@/store/abcx-store";
import { Button } from "@/components/ui/button";

const FILTERS = [
  { id: "all" as const, label: "All" },
  { id: "stock" as const, label: "Stock" },
  { id: "manage" as const, label: "Manage" },
  { id: "options" as const, label: "Options" },
];

export function SuitePage() {
  const suite = useAbcxStore((s) => s.suite);
  const suiteFilter = useAbcxStore((s) => s.suiteFilter);
  const setSuiteFilter = useAbcxStore((s) => s.setSuiteFilter);
  const runSuiteTest = useAbcxStore((s) => s.runSuiteTest);

  const rows = suite.filter((t) => suiteFilter === "all" || t.group === suiteFilter);

  return (
    <div className="px-4 py-4">
      <div className="mb-4">
        <h2 className="text-xl font-bold text-fg">Test Suite</h2>
        <p className="mt-1 max-w-xl text-[13px] text-muted">
          Paper place/cancel gym for order mechanics — not live curriculum trading. Dry-run by
          default on desktop.
        </p>
      </div>

      <div className="mb-4 flex flex-wrap gap-2">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            onClick={() => setSuiteFilter(f.id)}
            className={cn(
              "rounded-full border px-3 py-1.5 text-[13px] font-semibold transition-colors",
              suiteFilter === f.id
                ? "border-fg bg-fg text-bg"
                : "border-border text-fg hover:bg-elevated",
            )}
          >
            {f.label}
          </button>
        ))}
      </div>

      <ul className="divide-y divide-border rounded-xl border border-border">
        {rows.map((t) => (
          <li
            key={t.id}
            className="flex flex-wrap items-center justify-between gap-3 px-3 py-3"
          >
            <div>
              <div className="text-[14px] font-semibold text-fg">{t.name}</div>
              <div className="mt-0.5 flex items-center gap-2 text-[11px] text-muted">
                <span className="uppercase tracking-wide">{t.group}</span>
                <StatusBadge status={t.status} />
                {t.lastMs != null && <span className="tabular">{t.lastMs}ms</span>}
              </div>
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={t.status === "running"}
              onClick={() => runSuiteTest(t.id)}
            >
              {t.status === "running" ? "Running…" : "Run"}
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    pass: "bg-gain-soft text-gain",
    fail: "bg-loss-soft text-loss",
    running: "bg-primary/15 text-primary",
    idle: "bg-elevated text-muted",
  };
  return (
    <span className={cn("rounded-full px-2 py-0.5 font-semibold", map[status] ?? map.idle)}>
      {status}
    </span>
  );
}
