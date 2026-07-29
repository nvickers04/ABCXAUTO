import { Slider } from "@/components/ui/slider";
import { Button } from "@/components/ui/button";
import { useAbcxStore } from "@/store/abcx-store";

const DIALS: {
  key:
    | "deliberation"
    | "budget"
    | "frequency"
    | "rotation"
    | "entrySurface"
    | "complexity";
  title: string;
  left: string;
  right: string;
}[] = [
  {
    key: "deliberation",
    title: "Deliberation (System 1 ↔ System 2)",
    left: "S1 lean / quiet when protected",
    right: "S2 mega-worker / require Act",
  },
  {
    key: "budget",
    title: "Intelligence budget",
    left: "protect API $",
    right: "more frequent Grok",
  },
  {
    key: "frequency",
    title: "Trade frequency",
    left: "patient — few entries / quality",
    right: "higher rate OK — process/streams",
  },
  {
    key: "rotation",
    title: "Capital rotation",
    left: "hold protected book OK",
    right: "redeploy / free cash for better setups",
  },
  {
    key: "entrySurface",
    title: "Entry surface (restrict)",
    left: "stock brackets only",
    right: "options only — no stock entries",
  },
  {
    key: "complexity",
    title: "Option complexity (add)",
    left: "defined-risk options",
    right: "full multi-leg toolbox",
  },
];

export function ControlsPage() {
  const controls = useAbcxStore((s) => s.controls);
  const setControls = useAbcxStore((s) => s.setControls);

  return (
    <div className="px-4 py-4">
      <div className="mb-4">
        <h2 className="text-xl font-bold text-fg">Controls</h2>
        <p className="mt-1 max-w-xl text-[13px] text-muted">
          Attention + toolbox + book capacity. Entry surface restricts stock vs options;
          option complexity adds shapes. Disjoint from Risk. Taste lives here — shell never
          invents stance.
        </p>
      </div>

      <div className="space-y-6">
        {DIALS.map((d) => (
          <div key={d.key} className="rounded-2xl border border-border bg-elevated/40 p-4">
            <div className="mb-1 flex items-center justify-between gap-3">
              <div className="text-[13px] font-semibold text-fg">{d.title}</div>
              <div className="tabular text-sm font-bold text-fg">{controls[d.key]}</div>
            </div>
            <Slider
              min={0}
              max={100}
              step={5}
              value={[controls[d.key]]}
              onValueChange={([v]) => setControls({ [d.key]: v ?? 0 })}
            />
            <div className="mt-1 flex justify-between gap-4 text-[11px] text-muted">
              <span>0 · {d.left}</span>
              <span className="text-right">{d.right} · 100</span>
            </div>
          </div>
        ))}

        <div className="rounded-2xl border border-border bg-elevated/40 p-4">
          <div className="mb-1 flex items-center justify-between gap-3">
            <div className="text-[13px] font-semibold text-fg">
              Book capacity (max open positions)
            </div>
            <div className="tabular text-sm font-bold text-fg">
              {controls.maxOpenPositions === 0 ? "∞" : controls.maxOpenPositions}
            </div>
          </div>
          <Slider
            min={0}
            max={25}
            step={1}
            value={[controls.maxOpenPositions]}
            onValueChange={([v]) => setControls({ maxOpenPositions: v ?? 0 })}
          />
          <div className="mt-1 flex justify-between text-[11px] text-muted">
            <span>0 · unlimited</span>
            <span>hard cap 25</span>
          </div>
        </div>
      </div>

      <div className="mt-6 flex flex-wrap gap-2">
        <Button
          variant="secondary"
          onClick={() =>
            useAbcxStore.setState({ toast: "Controls saved (local demo state)" })
          }
        >
          Save Controls
        </Button>
        <p className="self-center text-[12px] text-muted">
          In the desktop app this writes risk_settings.json (Controls keys only).
        </p>
      </div>
    </div>
  );
}
