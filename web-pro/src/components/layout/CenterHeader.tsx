import { Menu, PanelRight } from "lucide-react";
import { NAV } from "@/lib/abcx-data";
import { useAbcxStore } from "@/store/abcx-store";
import { cn } from "@/lib/utils";

export function CenterHeader() {
  const tab = useAbcxStore((s) => s.tab);
  const setMobileRail = useAbcxStore((s) => s.setMobileRail);
  const mode = useAbcxStore((s) => s.mode);
  const connected = useAbcxStore((s) => s.connected);
  const risk = useAbcxStore((s) => s.risk);
  const meta = NAV.find((n) => n.id === tab) ?? NAV[0]!;

  return (
    <header className="sticky top-0 z-10 border-b border-border bg-bg/90 backdrop-blur-md">
      <div className="flex h-14 items-center gap-3 px-3 sm:px-4">
        <button
          type="button"
          className="rounded-full p-2 text-fg hover:bg-elevated lg:hidden"
          onClick={() => setMobileRail("nav")}
          aria-label="Open navigation"
        >
          <Menu className="h-5 w-5" />
        </button>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-[19px] font-bold tracking-tight text-fg">
              {meta.label}
            </h1>
            {tab === "overview" && (
              <span
                className={cn(
                  "hidden items-center gap-1.5 rounded-full border border-border px-2 py-0.5 text-[11px] font-semibold sm:inline-flex",
                  mode === "Running" && "border-gain/30 text-gain",
                  mode === "Halted" && "border-loss/30 text-loss",
                  mode === "Paused" && "border-warn/30 text-warn",
                  mode === "Safe" && "text-muted",
                )}
              >
                <span
                  className={cn(
                    "h-1.5 w-1.5 rounded-full",
                    mode === "Running" && "bg-gain",
                    mode === "Halted" && "bg-loss",
                    mode === "Paused" && "bg-warn",
                    mode === "Safe" && "bg-muted",
                  )}
                />
                {mode}
              </span>
            )}
          </div>
        </div>

        <div className="hidden items-center gap-3 text-[12px] text-muted md:flex">
          <span className={cn("font-medium", connected ? "text-gain" : "text-muted")}>
            {connected ? "IBKR paper" : "Offline"}
          </span>
          {risk.halt && <span className="font-semibold text-loss">Halt</span>}
        </div>

        <button
          type="button"
          className="rounded-full p-2 text-fg hover:bg-elevated xl:hidden"
          onClick={() => setMobileRail("right")}
          aria-label="Open account panel"
        >
          <PanelRight className="h-5 w-5" />
        </button>
      </div>
    </header>
  );
}
