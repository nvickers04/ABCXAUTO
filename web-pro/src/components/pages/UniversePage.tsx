import { useMemo, useState } from "react";
import {
  Check,
  Crosshair,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useAbcxStore } from "@/store/abcx-store";
import { Button } from "@/components/ui/button";
import type { Arena } from "@/lib/abcx-data";

type PoolRow = {
  symbol: string;
  arena: string;
  source: string;
  arenaId: string;
};

const GROUP_META: Record<
  Arena["group"],
  { title: string; blurb: string }
> = {
  caps: {
    title: "By size",
    blurb: "Market-cap bands the scanner can pull from.",
  },
  scans: {
    title: "By tape",
    blurb: "Live IBKR scan lanes — hot names, movers.",
  },
  sectors: {
    title: "By theme",
    blurb: "Seed baskets when the broker scan is offline.",
  },
  custom: {
    title: "Custom",
    blurb: "Your own lists.",
  },
};

const GROUP_ORDER: Arena["group"][] = ["caps", "scans", "sectors", "custom"];

function parseList(raw: string) {
  return raw
    .split(/[\s,]+/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
}

export function UniversePage() {
  const arenas = useAbcxStore((s) => s.arenas);
  const toggleArena = useAbcxStore((s) => s.toggleArena);
  const customSymbols = useAbcxStore((s) => s.customSymbols);
  const setCustomSymbols = useAbcxStore((s) => s.setCustomSymbols);
  const excludeSymbols = useAbcxStore((s) => s.excludeSymbols);
  const setExcludeSymbols = useAbcxStore((s) => s.setExcludeSymbols);
  const universeFilter = useAbcxStore((s) => s.universeFilter);
  const setUniverseFilter = useAbcxStore((s) => s.setUniverseFilter);
  const connected = useAbcxStore((s) => s.connected);
  const setFocusSymbol = useAbcxStore((s) => s.setFocusSymbol);
  const focusSymbol = useAbcxStore((s) => s.focusSymbol);

  const [draftInclude, setDraftInclude] = useState("");
  const [draftExclude, setDraftExclude] = useState("");
  const [syncing, setSyncing] = useState(false);
  const [lastSync, setLastSync] = useState<string | null>(null);
  const [selectedArena, setSelectedArena] = useState<string | "all">("all");

  const includes = useMemo(() => parseList(customSymbols), [customSymbols]);
  const excludes = useMemo(() => parseList(excludeSymbols), [excludeSymbols]);
  const enabledCount = arenas.filter((a) => a.enabled).length;

  const pool: PoolRow[] = useMemo(() => {
    const fromArenas: PoolRow[] = arenas
      .filter((a) => a.enabled)
      .flatMap((a) =>
        a.symbols.map((sym) => ({
          symbol: sym,
          arena: a.label,
          source: a.kind,
          arenaId: a.id,
        })),
      );

    const fromCustom: PoolRow[] = includes.map((symbol) => ({
      symbol,
      arena: "Always include",
      source: "custom",
      arenaId: "custom",
    }));

    const seen = new Set<string>();
    const rows: PoolRow[] = [];
    for (const row of [...fromArenas, ...fromCustom]) {
      if (excludes.includes(row.symbol)) continue;
      if (seen.has(row.symbol)) continue;
      seen.add(row.symbol);
      rows.push(row);
    }
    rows.sort((a, b) => a.symbol.localeCompare(b.symbol));
    return rows;
  }, [arenas, includes, excludes]);

  const q = universeFilter.trim().toLowerCase();
  const visible = pool.filter((row) => {
    if (selectedArena !== "all" && row.arenaId !== selectedArena) {
      if (!(selectedArena === "custom" && row.arenaId === "custom")) return false;
    }
    if (!q) return true;
    return (
      row.symbol.toLowerCase().includes(q) ||
      row.arena.toLowerCase().includes(q)
    );
  });

  const groups = GROUP_ORDER.map((g) => ({
    id: g,
    ...GROUP_META[g],
    items: arenas.filter((a) => a.group === g),
  })).filter((g) => g.items.length > 0);

  function addInclude() {
    const next = parseList(draftInclude);
    if (!next.length) return;
    const merged = Array.from(new Set([...includes, ...next]));
    setCustomSymbols(merged.join(","));
    setDraftInclude("");
  }

  function removeInclude(sym: string) {
    setCustomSymbols(includes.filter((s) => s !== sym).join(","));
  }

  function addExclude() {
    const next = parseList(draftExclude);
    if (!next.length) return;
    const merged = Array.from(new Set([...excludes, ...next]));
    setExcludeSymbols(merged.join(","));
    setDraftExclude("");
  }

  function removeExclude(sym: string) {
    setExcludeSymbols(excludes.filter((s) => s !== sym).join(","));
  }

  function syncFromBroker() {
    if (syncing) return;
    setSyncing(true);
    window.setTimeout(() => {
      setSyncing(false);
      setLastSync(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
      useAbcxStore.setState({
        toast: connected
          ? "Pool updated from IBKR scanners (sim)"
          : "Offline — used seed lists for enabled arenas",
      });
    }, 900);
  }

  return (
    <div className="flex min-h-full flex-col">
      <div className="border-b border-border px-4 py-4 sm:px-5">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="min-w-0">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted">
              Fence for Grok
            </p>
            <h2 className="mt-0.5 text-[22px] font-bold tracking-tight text-fg">
              Where the agent can look
            </h2>
            <p className="mt-1 max-w-lg text-[13px] leading-snug text-muted">
              Turn on arenas. Click a pool ticker to open Focus chart (levels + acts).
              Pool is not ranked.
            </p>
          </div>
          <div className="flex flex-col items-end gap-1">
            <div className="tabular text-[28px] font-bold leading-none tracking-tight text-fg">
              {pool.length}
            </div>
            <div className="text-[12px] text-muted">
              in pool · {enabledCount} arena{enabledCount === 1 ? "" : "s"} on
            </div>
          </div>
        </div>

        {pool.length === 1 && (
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-primary/30 bg-primary/10 px-3 py-2.5">
            <div className="text-[13px] text-fg">
              <span className="font-bold">Single-name fence</span>
              <span className="text-muted"> — ideal for Focus on {pool[0]!.symbol}</span>
            </div>
            <Button size="sm" onClick={() => setFocusSymbol(pool[0]!.symbol)}>
              <Crosshair className="h-3.5 w-3.5" />
              Open Focus
            </Button>
          </div>
        )}
      </div>

      <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
        <div className="space-y-6 border-b border-border px-4 py-5 sm:px-5 lg:border-b-0 lg:border-r">
          {groups.map((group) => (
            <section key={group.id}>
              <div className="mb-2.5">
                <h3 className="text-[13px] font-bold text-fg">{group.title}</h3>
                <p className="text-[12px] text-muted">{group.blurb}</p>
              </div>
              <div className="grid gap-2">
                {group.items.map((arena) => (
                  <ArenaCard
                    key={arena.id}
                    arena={arena}
                    onToggle={() => toggleArena(arena.id)}
                    onFocusPool={() =>
                      setSelectedArena((cur) =>
                        cur === arena.id ? "all" : arena.id,
                      )
                    }
                    focused={selectedArena === arena.id}
                  />
                ))}
              </div>
            </section>
          ))}

          <section>
            <div className="mb-2.5">
              <h3 className="text-[13px] font-bold text-fg">Always include</h3>
              <p className="text-[12px] text-muted">
                Names that stay in the pool even if no arena lists them.
              </p>
            </div>
            <ChipEditor
              chips={includes}
              draft={draftInclude}
              setDraft={setDraftInclude}
              onAdd={addInclude}
              onRemove={removeInclude}
              placeholder="e.g. PLTR CRWD"
              emptyHint="No always-include names"
            />
          </section>

          <section>
            <div className="mb-2.5">
              <h3 className="text-[13px] font-bold text-fg">Never trade</h3>
              <p className="text-[12px] text-muted">
                Hard exclude — stripped from the pool before Grok sees it.
              </p>
            </div>
            <ChipEditor
              chips={excludes}
              draft={draftExclude}
              setDraft={setDraftExclude}
              onAdd={addExclude}
              onRemove={removeExclude}
              placeholder="e.g. MEME penny names"
              emptyHint="No excludes"
              danger
            />
          </section>

          <section className="rounded-2xl border border-border bg-elevated/40 p-4">
            <div className="flex items-start gap-3">
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-bg ring-1 ring-border">
                <RefreshCw className={cn("h-4 w-4 text-primary", syncing && "animate-spin")} />
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-[14px] font-semibold text-fg">
                  Sync pool from IBKR
                </div>
                <p className="mt-0.5 text-[12px] leading-snug text-muted">
                  Re-runs the enabled scanners and rebuilds membership. Offline uses seed
                  lists (same as desktop when TWS is down).
                </p>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={syncing}
                    onClick={syncFromBroker}
                  >
                    {syncing ? (
                      <>
                        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Syncing…
                      </>
                    ) : (
                      <>
                        <RefreshCw className="h-3.5 w-3.5" /> Sync now
                      </>
                    )}
                  </Button>
                  <span className="text-[11px] text-muted">
                    {lastSync
                      ? `Last sync ${lastSync}`
                      : connected
                        ? "Ready · paper session"
                        : "Will use seeds offline"}
                  </span>
                </div>
              </div>
            </div>
          </section>
        </div>

        <div className="flex min-h-[420px] flex-col px-4 py-5 sm:px-5">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-[15px] font-bold text-fg">Hunt pool</h3>
              <p className="text-[12px] text-muted">
                Click a ticker → Focus chart. Not ranked.
              </p>
            </div>
            {selectedArena !== "all" && (
              <button
                type="button"
                onClick={() => setSelectedArena("all")}
                className="text-[12px] font-semibold text-primary hover:underline"
              >
                Show all arenas
              </button>
            )}
          </div>

          <div className="relative mb-3">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
            <input
              value={universeFilter}
              onChange={(e) => setUniverseFilter(e.target.value)}
              placeholder="Filter by symbol or arena"
              className="h-10 w-full rounded-full border border-border bg-elevated pl-10 pr-3 text-[13px] text-fg placeholder:text-muted focus:border-primary focus:outline-none"
            />
          </div>

          <div className="mb-4 flex flex-wrap gap-1.5">
            <FilterChip
              active={selectedArena === "all"}
              onClick={() => setSelectedArena("all")}
              label={`All · ${pool.length}`}
            />
            {arenas
              .filter((a) => a.enabled)
              .map((a) => {
                const count = pool.filter((p) => p.arenaId === a.id).length;
                return (
                  <FilterChip
                    key={a.id}
                    active={selectedArena === a.id}
                    onClick={() =>
                      setSelectedArena((cur) => (cur === a.id ? "all" : a.id))
                    }
                    label={`${a.label} · ${count}`}
                  />
                );
              })}
            {includes.length > 0 && (
              <FilterChip
                active={selectedArena === "custom"}
                onClick={() =>
                  setSelectedArena((cur) => (cur === "custom" ? "all" : "custom"))
                }
                label={`Always · ${includes.length}`}
              />
            )}
          </div>

          {visible.length === 0 ? (
            <div className="flex flex-1 flex-col items-center justify-center rounded-2xl border border-dashed border-border px-6 py-16 text-center">
              <p className="text-[15px] font-semibold text-fg">Empty pool</p>
              <p className="mt-1 max-w-xs text-[13px] text-muted">
                Enable at least one arena or add an always-include ticker.
              </p>
            </div>
          ) : (
            <div className="flex flex-wrap content-start gap-2">
              {visible.map((row) => (
                <button
                  key={row.symbol}
                  type="button"
                  title={`Focus ${row.symbol} · ${row.arena}`}
                  onClick={() => setFocusSymbol(row.symbol)}
                  className={cn(
                    "group inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-left transition-colors",
                    focusSymbol === row.symbol
                      ? "border-primary/50 bg-primary/10"
                      : "border-border bg-elevated/50 hover:border-primary/40 hover:bg-elevated",
                  )}
                >
                  <span className="text-[13px] font-bold tracking-tight text-fg">
                    {row.symbol}
                  </span>
                  <span className="max-w-[72px] truncate text-[10px] text-muted">
                    {row.arenaId === "custom" ? "pin" : row.source === "ibkr" ? "scan" : "seed"}
                  </span>
                </button>
              ))}
            </div>
          )}

          <div className="mt-auto border-t border-border pt-3 text-[11px] text-muted">
            Showing {visible.length}
            {visible.length !== pool.length ? ` of ${pool.length}` : ""} · click → Focus ·
            excludes: {excludes.length || "none"}
          </div>
        </div>
      </div>
    </div>
  );
}

function ArenaCard({
  arena,
  onToggle,
  onFocusPool,
  focused,
}: {
  arena: Arena;
  onToggle: () => void;
  onFocusPool: () => void;
  focused: boolean;
}) {
  const preview = arena.symbols.slice(0, 5);
  const more = Math.max(0, arena.symbols.length - preview.length);

  return (
    <div
      className={cn(
        "rounded-2xl border p-3 transition-colors",
        arena.enabled
          ? "border-border bg-elevated/50"
          : "border-border/80 bg-bg opacity-75",
        focused && arena.enabled && "border-primary/50 ring-1 ring-primary/20",
      )}
    >
      <div className="flex items-start gap-3">
        <button
          type="button"
          role="switch"
          aria-checked={arena.enabled}
          onClick={onToggle}
          className={cn(
            "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md border transition-colors",
            arena.enabled
              ? "border-primary bg-primary text-primary-fg"
              : "border-border bg-bg text-transparent hover:border-muted",
          )}
        >
          <Check className="h-3 w-3" strokeWidth={3} />
        </button>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={onFocusPool}
              className="text-left text-[14px] font-semibold text-fg hover:text-primary"
            >
              {arena.label}
            </button>
            <span
              className={cn(
                "rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                arena.kind === "ibkr"
                  ? "bg-primary/15 text-primary"
                  : "bg-elevated text-muted ring-1 ring-border",
              )}
            >
              {arena.kind === "ibkr" ? "IBKR" : "Seed"}
            </span>
          </div>

          {arena.enabled && preview.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {preview.map((s) => (
                <span
                  key={s}
                  className="rounded-md bg-bg px-1.5 py-0.5 font-mono text-[11px] text-muted ring-1 ring-border"
                >
                  {s}
                </span>
              ))}
              {more > 0 && (
                <span className="px-1 text-[11px] text-muted">+{more}</span>
              )}
            </div>
          )}

          {!arena.enabled && (
            <p className="mt-1 text-[12px] text-muted">Off — not in the pool</p>
          )}
        </div>
      </div>
    </div>
  );
}

function ChipEditor({
  chips,
  draft,
  setDraft,
  onAdd,
  onRemove,
  placeholder,
  emptyHint,
  danger,
}: {
  chips: string[];
  draft: string;
  setDraft: (v: string) => void;
  onAdd: () => void;
  onRemove: (s: string) => void;
  placeholder: string;
  emptyHint: string;
  danger?: boolean;
}) {
  return (
    <div className="rounded-2xl border border-border bg-elevated/30 p-3">
      {chips.length > 0 ? (
        <div className="mb-2.5 flex flex-wrap gap-1.5">
          {chips.map((s) => (
            <span
              key={s}
              className={cn(
                "inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[12px] font-semibold",
                danger
                  ? "bg-loss-soft text-loss"
                  : "bg-bg text-fg ring-1 ring-border",
              )}
            >
              {s}
              <button
                type="button"
                onClick={() => onRemove(s)}
                className="rounded-full p-0.5 opacity-70 hover:opacity-100"
                aria-label={`Remove ${s}`}
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
      ) : (
        <p className="mb-2.5 text-[12px] text-muted">{emptyHint}</p>
      )}
      <div className="flex gap-2">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              onAdd();
            }
          }}
          placeholder={placeholder}
          className="h-9 min-w-0 flex-1 rounded-full border border-border bg-bg px-3 text-[13px] text-fg placeholder:text-muted focus:border-primary focus:outline-none"
        />
        <Button size="sm" variant="outline" onClick={onAdd} disabled={!draft.trim()}>
          <Plus className="h-3.5 w-3.5" />
          Add
        </Button>
      </div>
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-full px-2.5 py-1 text-[11px] font-semibold transition-colors",
        active
          ? "bg-fg text-bg"
          : "bg-elevated text-muted ring-1 ring-border hover:text-fg",
      )}
    >
      {label}
    </button>
  );
}
