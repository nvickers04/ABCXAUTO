import type { ComponentType } from "react";
import {
  BarChart3,
  Crosshair,
  FlaskConical,
  LayoutDashboard,
  Pause,
  Play,
  Plug,
  Shield,
  SlidersHorizontal,
  Unplug,
  Wallet,
  Globe2,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { NAV, type TabId } from "@/lib/abcx-data";
import { useAbcxStore } from "@/store/abcx-store";
import { Button } from "@/components/ui/button";

const ICONS: Record<
  TabId,
  ComponentType<{ className?: string; strokeWidth?: number }>
> = {
  overview: LayoutDashboard,
  positions: Wallet,
  focus: Crosshair,
  controls: SlidersHorizontal,
  universe: Globe2,
  risk: Shield,
  scorecard: BarChart3,
  suite: FlaskConical,
};

export function LeftRail({ onClose }: { onClose?: () => void }) {
  const tab = useAbcxStore((s) => s.tab);
  const setTab = useAbcxStore((s) => s.setTab);
  const mode = useAbcxStore((s) => s.mode);
  const connected = useAbcxStore((s) => s.connected);
  const toggleConnect = useAbcxStore((s) => s.toggleConnect);
  const toggleRun = useAbcxStore((s) => s.toggleRun);
  const panicFlatten = useAbcxStore((s) => s.panicFlatten);
  const risk = useAbcxStore((s) => s.risk);
  const focusSymbol = useAbcxStore((s) => s.focusSymbol);

  const expanded = Boolean(onClose);

  return (
    <aside
      className={cn(
        "flex h-full w-full flex-col bg-bg px-2 py-2",
        expanded ? "w-[280px] px-3 py-3" : "sm:w-[76px] lg:w-[248px] lg:px-3 lg:py-3",
      )}
    >
      <div className="mb-1 flex items-center justify-between gap-2 px-2 py-2">
        <div className="flex min-w-0 items-center gap-3">
          <img
            src="/abcxauto_logo.png"
            alt="ABCXAUTO"
            className="h-10 w-10 shrink-0 rounded-full object-cover ring-1 ring-border"
          />
          <div className={cn("min-w-0", expanded ? "block" : "hidden lg:block")}>
            <div className="truncate text-[17px] font-bold tracking-tight text-fg">
              ABCXAUTO
            </div>
            <div className="truncate text-[11px] text-muted">
              {focusSymbol ? `Focus · ${focusSymbol}` : "Pro · paper"}
            </div>
          </div>
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="rounded-full p-2 text-muted hover:bg-elevated hover:text-fg"
            aria-label="Close menu"
          >
            <X className="h-5 w-5" />
          </button>
        )}
      </div>

      <nav className="mt-1 flex flex-1 flex-col gap-0.5">
        {NAV.map((item) => {
          const Icon = ICONS[item.id];
          const active = tab === item.id;
          return (
            <button
              key={item.id}
              type="button"
              title={item.label}
              onClick={() => {
                setTab(item.id);
                onClose?.();
              }}
              className={cn(
                "group flex items-center rounded-full px-3 py-2.5 text-left transition-colors",
                !expanded && "justify-center lg:justify-start lg:gap-3.5",
                expanded && "gap-3.5",
                active
                  ? "bg-elevated font-bold text-fg"
                  : "font-normal text-fg hover:bg-elevated/70",
              )}
            >
              <Icon
                className={cn(
                  "h-[22px] w-[22px] shrink-0",
                  !expanded && "mx-auto lg:mx-0",
                )}
                strokeWidth={active ? 2.35 : 1.75}
              />
              <span
                className={cn(
                  "text-[16px] leading-none tracking-tight",
                  expanded ? "inline" : "hidden lg:inline",
                )}
              >
                {item.label}
                {item.id === "focus" && focusSymbol ? (
                  <span className="ml-1 text-[12px] font-semibold text-primary">
                    {focusSymbol}
                  </span>
                ) : null}
              </span>
            </button>
          );
        })}
      </nav>

      <div className="mt-auto space-y-1.5 border-t border-border pt-3">
        <div
          className={cn(
            "items-center gap-2 px-2 pb-1 text-[11px] text-muted",
            expanded ? "flex" : "hidden lg:flex",
          )}
        >
          <span
            className={cn(
              "inline-block h-1.5 w-1.5 rounded-full",
              mode === "Running"
                ? "bg-gain"
                : mode === "Halted"
                  ? "bg-loss"
                  : mode === "Paused"
                    ? "bg-warn"
                    : "bg-muted",
            )}
          />
          <span className="font-medium text-fg">{mode}</span>
          {risk.halt && <span className="text-loss">halt</span>}
        </div>

        <Button
          variant={mode === "Running" ? "outline" : "secondary"}
          className="w-full"
          size="md"
          onClick={toggleRun}
          title={mode === "Running" ? "Stop agent" : "START AUTONOMOUS"}
        >
          {mode === "Running" ? (
            <>
              <Pause className="h-4 w-4" />
              <span className={expanded ? "inline" : "hidden lg:inline"}>Stop</span>
            </>
          ) : (
            <>
              <Play className="h-4 w-4" />
              <span className={expanded ? "inline" : "hidden lg:inline"}>Start</span>
            </>
          )}
        </Button>

        <button
          type="button"
          onClick={toggleConnect}
          className="flex w-full items-center justify-center gap-2 rounded-full px-3 py-2 text-[13px] font-semibold text-fg transition-colors hover:bg-elevated"
          title={connected ? "Disconnect" : "Connect IBKR"}
        >
          {connected ? (
            <Unplug className="h-4 w-4 text-muted" />
          ) : (
            <Plug className="h-4 w-4 text-primary" />
          )}
          <span className={expanded ? "inline" : "hidden lg:inline"}>
            {connected ? "Disconnect" : "Connect"}
          </span>
        </button>

        <button
          type="button"
          onClick={panicFlatten}
          className="flex w-full items-center justify-center gap-2 rounded-full px-3 py-2 text-[13px] font-semibold text-loss transition-colors hover:bg-loss-soft"
          title="Close all positions"
        >
          <span className={expanded ? "inline" : "hidden lg:inline"}>Close all</span>
          <span className={expanded ? "hidden" : "inline lg:hidden"}>×</span>
        </button>

        <div
          className={cn(
            "items-center gap-2 rounded-2xl px-2 py-2",
            expanded ? "flex" : "hidden lg:flex",
          )}
        >
          <div className="flex h-9 w-9 items-center justify-center rounded-full bg-elevated text-xs font-bold text-fg ring-1 ring-border">
            PM
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[13px] font-semibold text-fg">Operator</div>
            <div className="truncate text-[11px] text-muted">@abcxauto</div>
          </div>
        </div>
      </div>
    </aside>
  );
}
