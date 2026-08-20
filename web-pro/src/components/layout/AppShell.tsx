import { useEffect } from "react";
import { useAbcxStore } from "@/store/abcx-store";
import { LeftRail } from "@/components/layout/LeftRail";
import { RightRail } from "@/components/layout/RightRail";
import { CenterHeader } from "@/components/layout/CenterHeader";
import { DashboardPage } from "@/components/pages/DashboardPage";
import { PositionsPage } from "@/components/pages/PositionsPage";
import { ControlsPage } from "@/components/pages/ControlsPage";
import { UniversePage } from "@/components/pages/UniversePage";
import { RiskPage } from "@/components/pages/RiskPage";
import { ScorecardPage } from "@/components/pages/ScorecardPage";
import { SuitePage } from "@/components/pages/SuitePage";

export function AppShell() {
  const tab = useAbcxStore((s) => s.tab);
  const toast = useAbcxStore((s) => s.toast);
  const clearToast = useAbcxStore((s) => s.clearToast);
  const mobileRail = useAbcxStore((s) => s.mobileRail);
  const setMobileRail = useAbcxStore((s) => s.setMobileRail);
  const tickSim = useAbcxStore((s) => s.tickSim);
  const connected = useAbcxStore((s) => s.connected);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(clearToast, 2600);
    return () => window.clearTimeout(t);
  }, [toast, clearToast]);

  useEffect(() => {
    if (!connected) return;
    const t = window.setInterval(tickSim, 2500);
    return () => window.clearInterval(t);
  }, [connected, tickSim]);

  return (
    <div className="flex h-full min-h-0 justify-center bg-bg text-fg">
      {/* Wider shell — more room for the feed */}
      <div className="flex h-full w-full max-w-[1360px] min-h-0">
        <div className="hidden h-full shrink-0 border-r border-border sm:block">
          <LeftRail />
        </div>

        {mobileRail === "nav" && (
          <div className="fixed inset-0 z-40 flex sm:hidden">
            <button
              type="button"
              className="absolute inset-0 bg-bg/70"
              aria-label="Close"
              onClick={() => setMobileRail("main")}
            />
            <div className="relative z-10 h-full w-[min(100%,280px)] border-r border-border bg-bg shadow-soft">
              <LeftRail onClose={() => setMobileRail("main")} />
            </div>
          </div>
        )}

        <main className="flex min-h-0 min-w-0 flex-1 flex-col border-x border-border bg-bg sm:border-l-0">
          <CenterHeader />
          <div className="min-h-0 flex-1 overflow-y-auto scroll-thin">
            {tab === "overview" && <DashboardPage />}
            {tab === "positions" && <PositionsPage />}
            {tab === "controls" && <ControlsPage />}
            {tab === "universe" && <UniversePage />}
            {tab === "risk" && <RiskPage />}
            {tab === "scorecard" && <ScorecardPage />}
            {tab === "suite" && <SuitePage />}
          </div>
        </main>

        <RightRail />

        {mobileRail === "right" && (
          <div className="fixed inset-0 z-40 flex justify-end xl:hidden">
            <button
              type="button"
              className="absolute inset-0 bg-bg/70"
              aria-label="Close"
              onClick={() => setMobileRail("main")}
            />
            <div className="relative z-10 h-full w-[min(100%,340px)] overflow-y-auto border-l border-border bg-bg shadow-soft">
              <div className="flex items-center justify-between border-b border-border px-3 py-2">
                <span className="text-sm font-bold text-fg">Account</span>
                <button
                  type="button"
                  className="text-sm font-semibold text-primary"
                  onClick={() => setMobileRail("main")}
                >
                  Done
                </button>
              </div>
              <RightRail force />
            </div>
          </div>
        )}
      </div>

      {toast && (
        <div
          className="fixed bottom-5 left-1/2 z-50 -translate-x-1/2 rounded-full border border-border bg-elevated px-4 py-2 text-[13px] font-medium text-fg shadow-soft"
          role="status"
        >
          {toast}
        </div>
      )}
    </div>
  );
}
