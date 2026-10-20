// Typed fetch wrapper for the ProHarness REST API.
// Token is read from localStorage (set on first load via ?token=...).

const TOKEN_KEY = "proharness_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(t: string | null) {
  if (t) localStorage.setItem(TOKEN_KEY, t);
  else localStorage.removeItem(TOKEN_KEY);
}

// Capture ?token=... on first load.
(() => {
  const params = new URLSearchParams(window.location.search);
  const t = params.get("token");
  if (t) {
    setToken(t);
    // Clean the URL.
    params.delete("token");
    const clean = params.toString();
    window.history.replaceState({}, "", clean ? `?${clean}` : window.location.pathname);
  }
})();

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const h: Record<string, string> = { ...extra };
  const t = getToken();
  if (t) h["authorization"] = `Bearer ${t}`;
  return h;
}

export async function api<T = any>(
  path: string,
  opts: RequestInit = {},
): Promise<T> {
  const extra: Record<string, string> = {};
  if (opts.body && !(opts.body instanceof FormData)) {
    extra["content-type"] = "application/json";
  }
  const init: RequestInit = {
    ...opts,
    headers: headers(extra),
  };
  const r = await fetch(path, init);
  if (!r.ok) {
    const detail = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(typeof detail.detail === "string" ? detail.detail : JSON.stringify(detail.detail));
  }
  return r.status === 204 ? (undefined as T) : ((await r.json()) as T);
}

// ── typed surfaces ──────────────────────────────────────────────────────────
export interface Memory {
  id: string;
  text: string;
  type: string;
  tags: string[];
  importance: number;
  created_at: number;
  last_accessed: number;
  access_count: number;
  source: string;
  metadata: Record<string, unknown>;
}

export interface RecallHit {
  id: string;
  text: string;
  type: string;
  score: number;
  recency: number;
  importance: number;
  relevance: number;
}

export interface RouterAlpha {
  current_alpha: number;
  recommended_alpha: number | null;
  guardrails: { min: number; max: number; max_step: number; autotune_enabled: boolean; min_decisions: number };
  n_scored: number;
  n_total: number;
  note: string;
  sweep_curve: { alpha: number; local: number; cloud: number; est_cost: number; local_bad_rate: number }[];
}

export interface Cost {
  total_tokens: number;
  total_cost_usd: number;
  per_model: Record<string, { in_tokens: number; out_tokens: number }>;
}

export interface SettingsResponse {
  runtime_tunable: Record<string, unknown>;
  startup_only: Record<string, unknown>;
}

export interface EvalResult {
  behavior_pass: number;
  behavior_total: number;
  behavior_pct: number;
  results: { id: string; difficulty: string; passed: boolean; signals: { name: string; passed: boolean; detail: string }[] }[];
}

// ── endpoints ──────────────────────────────────────────────────────────────
export const Mem = {
  list: (limit = 200, type?: string) =>
    api<Memory[]>(`/api/memory?limit=${limit}${type ? `&type=${type}` : ""}`),
  count: () => api<{ count: number }>("/api/memory/count"),
  get: (id: string) => api<Memory>(`/api/memory/${id}`),
  add: (body: { text: string; type?: string; importance?: number; tags?: string[] }) =>
    api<Memory>("/api/memory", { method: "POST", body: JSON.stringify(body) }),
  patch: (id: string, body: Partial<Memory>) =>
    api<Memory>(`/api/memory/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  delete: (id: string) => api(`/api/memory/${id}`, { method: "DELETE" }),
  search: (q: string, limit = 8) =>
    api<{ query: string; count: number; hits: { id: string; score: number; preview: string; type: string; importance: number }[] }>(
      `/api/memory/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  recall: (q: string, limit = 5) =>
    api<{ query: string; count: number; hits: RecallHit[] }>(`/api/memory/recall?q=${encodeURIComponent(q)}&limit=${limit}`),
  reflectionGauge: () =>
    api<{ pending: number; threshold: number; last_reflection_ts: number; ratio: number }>(
      "/api/memory/reflection/gauge",
    ),
};

export const Telemetry = {
  routerDecisions: (limit = 200) =>
    api<{ count: number; decisions: any[] }>(`/api/router/decisions?limit=${limit}`),
  alpha: () => api<RouterAlpha>("/api/router/alpha"),
  cost: () => api<Cost>("/api/cost"),
  eval: (filter?: string) =>
    api<EvalResult>(`/api/eval${filter ? `?filter=${encodeURIComponent(filter)}` : ""}`),
};

export const Feedback = {
  // Explicit turn feedback (2026-08-31 #3): rating ±1, optional correction
  // text stored as a Mem0 contradiction candidate.
  rate: (rating: number, correction?: string) =>
    api<{ recorded: boolean; decision_id: number | null }>("/api/feedback", {
      method: "POST",
      body: JSON.stringify({ rating, correction }),
    }),
};

export const Settings = {
  get: () => api<SettingsResponse>("/api/settings"),
  patch: (body: Record<string, unknown>) =>
    api<{ applied: Record<string, unknown> }>("/api/settings", { method: "PATCH", body: JSON.stringify(body) }),
};

export const Files = {
  list: (path = "") => api<any>(`/api/files/list?path=${encodeURIComponent(path)}`),
  read: (path: string) => api<{ path: string; content: string; truncated: boolean }>(`/api/files/read?path=${encodeURIComponent(path)}`),
  write: (path: string, content: string) =>
    api<{ path: string; bytes: number }>("/api/files/write", { method: "PUT", body: JSON.stringify({ path, content }) }),
};
