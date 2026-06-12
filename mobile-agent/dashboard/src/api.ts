import { useEffect, useState } from "react";

// ---- types (mirror bot/dashboard_api.py response shapes) -------------------

export interface TaskRow {
  id: number;
  description: string;
  state: string;
  started_at: string;
  ended_at: string | null;
  final_summary: string | null;
  failure_reason: string | null;
  step_count: number;
  tokens: { in: number; out: number };
  duration_seconds: number | null;
  launch_package: string | null;
  app_id: string | null;
  app_name: string;
  emoji: string;
  category: string | null;
}

export interface StepRow {
  idx: number;
  action_type: string | null;
  note: string;
  result: string | null;
  rejected: boolean;
  coords: {
    tap?: { x: number; y: number };
    swipe?: { x1: number; y1: number; x2: number; y2: number };
    text?: string;
  };
  screenshot_url: string;
  timestamp: string | null;
}

export interface AppStat {
  app_id: string | null;
  app_name: string;
  emoji: string;
  category: string | null;
  launch_package: string | null;
  run_count: number;
  success_count: number;
  success_rate: number;
  last_used_at: string | null;
  avg_steps: number;
}

export interface SavedRow {
  id: number;
  slug: string;
  label: string;
  description: string;
  run_count: number;
  last_run_at: string | null;
  app_name: string;
  emoji: string;
}

export interface ScheduleRow {
  id: number;
  name: string;
  freq: string;
  at_minute: number;
  weekday: number | null;
  day_of_month: number | null;
  next_run_at: string;
  last_run_at: string | null;
  enabled: boolean;
  pay_automatically: boolean;
  description: string;
  app_name: string;
  emoji: string;
  action_kind: string;
  payload?: Record<string, unknown>;
}

export interface ComparisonRow {
  id: number;
  query: string;
  category: string;
  ranking_key: string;
  winner_app_id: string | null;
  chosen_app_id: string | null;
  created_at: string;
  ordered_at: string | null;
  quotes: Array<Record<string, unknown>>;
}

export interface GoogleStatus {
  configured: boolean;
  connected: boolean;
  email: string | null;
  scopes: string[];
  connected_at: string | null;
}

export interface CloudActionRow {
  id: number;
  kind: string;
  status: string;
  error: string | null;
  created_at: string;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
}

export interface ContactRow {
  id: number;
  name: string;
  email: string;
  created_at: string;
}

// ---- auth + fetch ----------------------------------------------------------

const TOKEN_KEY = "atlas_dash_token";
export const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
export const setToken = (t: string) => localStorage.setItem(TOKEN_KEY, t);

/** Append the token as a query param (for <img> and EventSource, which can't
 * set an Authorization header). */
export function withToken(url: string): string {
  const t = getToken();
  if (!t) return url;
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(t)}`;
}

export async function api<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (res.status === 401) throw new Error("unauthorized");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken()}`,
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

export async function apiDelete<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

/** Tiny data hook: fetch on mount + when `deps` change. */
export function useApi<T>(path: string, deps: unknown[] = []): {
  data: T | null;
  error: string | null;
  loading: boolean;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    api<T>(path)
      .then((d) => alive && (setData(d), setError(null)))
      .catch((e) => alive && setError(String(e.message || e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading };
}
