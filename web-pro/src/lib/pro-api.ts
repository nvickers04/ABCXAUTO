/** Client for local Pro API (IBKR book + bars). Same-origin under desktop. */

export type DataMode = "live" | "demo" | "offline";

export interface ApiPosition {
  conId: string;
  symbol: string;
  type: "STK" | "OPT";
  qty: number;
  avgCost: number;
  price: number;
  uPnl: number;
  details: string;
  protected: boolean;
}

export interface ApiOrder {
  id: string;
  symbol: string;
  side: "BUY" | "SELL";
  type: string;
  qty: number;
  price: number;
  status: "Submitted" | "PreSubmitted" | "Filled" | "Cancelled";
  role: "entry" | "stop" | "target" | "exit";
}

export interface ApiAccount {
  netLiq: number;
  dayPnl: number;
  cash: number;
  unrealized?: number;
  accountId?: string;
  error?: string;
}

export interface BookResponse {
  live: boolean;
  source: string;
  positions: ApiPosition[];
  orders: ApiOrder[];
  account: ApiAccount;
  ts?: string;
}

export interface BarsResponse {
  symbol: string;
  range: string;
  source: string;
  bars: { t: string; c: number; o?: number | null; h?: number | null; l?: number | null }[];
}

export interface StatusResponse {
  ibkr_connected: boolean;
  mda_configured: boolean;
  xai_configured: boolean;
  trading_mode: string;
  ibkr_host?: string;
  ibkr_port?: number;
  data_mode?: string;
}

const BASE = (import.meta.env.VITE_PRO_API as string | undefined) || "";

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || body.error || detail;
    } catch {
      /* ignore */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json() as Promise<T>;
}

export async function apiHealth(): Promise<boolean> {
  try {
    const r = await json<{ ok: string }>("/api/health");
    return r.ok === "true" || Boolean(r.ok);
  } catch {
    return false;
  }
}

export async function apiStatus(): Promise<StatusResponse> {
  return json("/api/status");
}

export async function apiConnect(): Promise<StatusResponse & { ok?: boolean }> {
  return json("/api/connect", { method: "POST" });
}

export async function apiDisconnect(): Promise<StatusResponse & { ok?: boolean }> {
  return json("/api/disconnect", { method: "POST" });
}

export async function apiBook(): Promise<BookResponse> {
  return json("/api/book");
}

export async function apiBars(
  symbol: string,
  range: "1D" | "5D" | "1M" = "5D",
): Promise<BarsResponse> {
  return json(`/api/bars/${encodeURIComponent(symbol)}?range=${range}`);
}

export async function apiActivity(limit = 40): Promise<{
  items: {
    id: string;
    kind: string;
    title: string;
    body: string;
    ts: string;
    meta?: Record<string, string | number | boolean>;
  }[];
  live: boolean;
}> {
  return json(`/api/activity?limit=${limit}`);
}
