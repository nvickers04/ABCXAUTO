import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import { useAbcxStore } from "@/store/abcx-store";
import type { RiskState } from "@/lib/abcx-data";

const SLIDERS: {
  key: keyof Pick<
    RiskState,
    | "maxRiskPerTradePct"
    | "dailyLossLimitPct"
    | "maxPositionPct"
    | "maxPeakDrawdownPct"
    | "maxOptionPremiumPct"
  >;
  title: string;
  left: string;
  right: string;
  max: number;
  step: number;
}[] = [
  {
    key: "maxRiskPerTradePct",
    title: "Max risk / trade % NL",
    left: "0.25%",
    right: "6%",
    max: 6,
    step: 0.25,
  },
  {
    key: "dailyLossLimitPct",
    title: "Daily loss limit % NL",
    left: "1%",
    right: "15%",
    max: 15,
    step: 0.25,
  },
  {
    key: "maxPositionPct",
    title: "Max position % NL",
    left: "2%",
    right: "35%",
    max: 35,
    step: 0.5,
  },
  {
    key: "maxPeakDrawdownPct",
    title: "Peak drawdown %",
    left: "0=off",
    right: "35%",
    max: 35,
    step: 0.5,
  },
  {
    key: "maxOptionPremiumPct",
    title: "Max option premium %",
    left: "0=off",
    right: "12%",
    max: 12,
    step: 0.25,
  },
];

export function RiskPage() {
  const risk = useAbcxStore((s) => s.risk);
  const setRisk = useAbcxStore((s) => s.setRisk);
  const applyPosture = useAbcxStore((s) => s.applyPosture);
  const manualHalt = useAbcxStore((s) => s.manualHalt);
  const resumeHalt = useAbcxStore((s) => s.resumeHalt);

  return (
    <div className="px-4 py-4">
      <div className="mb-4">
        <h2 className="text-xl font-bold text-fg">Risk</h2>
        <p className="mt-1 max-w-xl text-[13px] text-muted">
          Capital survival gates and halt. Disjoint from Controls and Universe. LLM cannot talk
          past these.
        </p>
      </div>

      <div className="mb-4 rounded-2xl border border-border bg-elevated/40 p-4">
        <label className="block text-[12px] font-medium text-muted">
          Capital preset (seeds Risk sliders only)
        </label>
        <select
          value={risk.posture}
          onChange={(e) => applyPosture(e.target.value as RiskState["posture"])}
          className="mt-2 h-10 w-full max-w-xs rounded-xl border border-border bg-elevated px-3 text-[13px] text-fg focus:border-primary focus:outline-none"
        >
          <option value="defensive">Defensive</option>
          <option value="balanced">Balanced</option>
          <option value="aggressive">Aggressive</option>
        </select>
      </div>

      <div className="mb-4 grid gap-3 sm:grid-cols-2">
        <SwitchRow
          label="Hard risk gates"
          checked={risk.gatesEnabled}
          onChange={(v) => setRisk({ gatesEnabled: v })}
        />
        <SwitchRow
          label="Auto-panic on breach"
          checked={risk.autoPanic}
          onChange={(v) => setRisk({ autoPanic: v })}
        />
        <SwitchRow
          label="Reject naked / unlimited option risk"
          checked={risk.definedRiskOnly}
          onChange={(v) => setRisk({ definedRiskOnly: v })}
        />
        <SwitchRow
          label="Cash-only sizing"
          checked={risk.cashOnly}
          onChange={(v) => setRisk({ cashOnly: v })}
        />
      </div>

      <div className="space-y-5">
        {SLIDERS.map((row) => (
          <div key={row.key} className="rounded-2xl border border-border bg-elevated/40 p-4">
            <div className="mb-1 flex items-center justify-between gap-3">
              <div className="text-[13px] font-semibold text-fg">{row.title}</div>
              <div className="tabular text-sm font-bold text-fg">
                {risk[row.key].toFixed(2).replace(/\.?0+$/, "")}
              </div>
            </div>
            <Slider
              min={0}
              max={row.max}
              step={row.step}
              value={[risk[row.key]]}
              onValueChange={([v]) => setRisk({ [row.key]: v ?? 0 })}
            />
            <div className="mt-1 flex justify-between text-[11px] text-muted">
              <span>0 · {row.left}</span>
              <span>{row.right}</span>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-6 rounded-2xl border border-border p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-sm font-bold text-fg">Halt latch</div>
            <p className="text-[12px] text-muted">
              {risk.halt
                ? `Latched — ${risk.haltReason || "halted"}`
                : "Clear — new entries allowed when gates pass"}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="danger" size="sm" onClick={manualHalt}>
              Manual halt
            </Button>
            <Button variant="outline" size="sm" onClick={resumeHalt} disabled={!risk.halt}>
              Resume
            </Button>
          </div>
        </div>
      </div>

      <div className="mt-4">
        <Button
          variant="secondary"
          onClick={() =>
            useAbcxStore.setState({ toast: "Risk gates saved (demo local state)" })
          }
        >
          Save Risk gates
        </Button>
      </div>
    </div>
  );
}

function SwitchRow({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-2xl border border-border bg-elevated/40 px-4 py-3">
      <span className="text-[13px] font-medium text-fg">{label}</span>
      <Switch checked={checked} onCheckedChange={onChange} />
    </div>
  );
}
