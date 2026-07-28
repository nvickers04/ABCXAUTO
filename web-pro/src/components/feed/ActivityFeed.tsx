import type { ComponentType } from "react";
import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  CircleDot,
  Gavel,
  Plug,
  ShieldAlert,
  User,
  Zap,
} from "lucide-react";
import { cn, relativeTime } from "@/lib/utils";
import type { ActivityItem } from "@/lib/abcx-data";
import { useAbcxStore } from "@/store/abcx-store";
import { Button } from "@/components/ui/button";

const KIND_META: Record<
  ActivityItem["kind"],
  { icon: ComponentType<{ className?: string }>; accent: string }
> = {
  judge: { icon: Bot, accent: "text-primary" },
  act: { icon: Zap, accent: "text-gain" },
  gate: { icon: Gavel, accent: "text-warn" },
  fill: { icon: CheckCircle2, accent: "text-gain" },
  pace: { icon: Activity, accent: "text-muted" },
  connect: { icon: Plug, accent: "text-primary" },
  system: { icon: CircleDot, accent: "text-muted" },
  risk: { icon: ShieldAlert, accent: "text-loss" },
  user: { icon: User, accent: "text-fg" },
};

export function ComposeBox() {
  const compose = useAbcxStore((s) => s.compose);
  const setCompose = useAbcxStore((s) => s.setCompose);
  const submitMandate = useAbcxStore((s) => s.submitMandate);

  return (
    <div className="border-b border-border px-4 py-3">
      <div className="flex gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-elevated text-xs font-bold ring-1 ring-border">
          PM
        </div>
        <div className="min-w-0 flex-1">
          <textarea
            value={compose}
            onChange={(e) => setCompose(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                submitMandate();
              }
            }}
            rows={1}
            placeholder="Mandate (optional) — Controls dials first"
            className="max-h-28 min-h-[40px] w-full resize-none bg-transparent py-2 text-[15px] leading-snug text-fg placeholder:text-muted focus:outline-none"
          />
          <div className="flex items-center justify-end">
            <Button size="sm" disabled={!compose.trim()} onClick={submitMandate}>
              Post
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function ActivityFeed({ items }: { items: ActivityItem[] }) {
  return (
    <div className="divide-y divide-border">
      {items.map((item) => (
        <ActivityRow key={item.id} item={item} />
      ))}
      {items.length === 0 && (
        <div className="flex items-center gap-2 px-4 py-10 text-sm text-muted">
          <AlertTriangle className="h-4 w-4" />
          No activity yet — Start the agent to stream cycles.
        </div>
      )}
    </div>
  );
}

function ActivityRow({ item }: { item: ActivityItem }) {
  const meta = KIND_META[item.kind];
  const Icon = meta.icon;
  return (
    <article className="flex gap-3 px-4 py-3 transition-colors hover:bg-elevated/35">
      <div
        className={cn(
          "mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-elevated ring-1 ring-border",
          meta.accent,
        )}
      >
        <Icon className="h-[18px] w-[18px]" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
          <span className="text-[14px] font-bold text-fg">{item.title}</span>
          <span className="text-[12px] text-muted">· {relativeTime(item.ts)}</span>
        </div>
        <p className="mt-0.5 text-[14px] leading-snug text-fg/90">{item.body}</p>
        {item.meta && Object.keys(item.meta).length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1">
            {Object.entries(item.meta).map(([k, v]) => (
              <span
                key={k}
                className="rounded-full border border-border bg-bg px-2 py-0.5 text-[10px] text-muted"
              >
                {k} <span className="text-fg">{String(v)}</span>
              </span>
            ))}
          </div>
        )}
      </div>
    </article>
  );
}
