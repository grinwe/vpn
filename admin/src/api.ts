// Thin fetch wrapper. Token is read from localStorage at call time so
// logging in/out doesn't require re-binding anything.
//
// In prod we are served from grinwer.online/admin and the API is on the
// same origin under /api, so an empty base just works. In dev Vite's
// proxy forwards /api to 127.0.0.1:8000 — same deal.

const TOKEN_KEY = "vpn_admin_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown
): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["X-Admin-Token"] = token;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const res = await fetch(`/api${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const payload = await res.json();
      if (payload?.detail) detail = payload.detail;
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};

// ---- Types mirrored from backend/app/schemas.py ----
// Kept hand-written for now; codegen can replace this later.

export interface UserOut {
  id: number;
  telegram_id: string | null;
  email: string | null;
  created_at: string;
  subscription_count: number;
  balance_kopecks: number;
}

export interface AdminTopupResponse {
  user_id: number;
  telegram_id: string | null;
  balance_kopecks: number;
  tx_id: number;
}

export function adminTopupByTelegram(
  telegramId: string,
  amountKopecks: number,
  note?: string,
): Promise<AdminTopupResponse> {
  return api.post<AdminTopupResponse>(
    `/users/by_telegram/${encodeURIComponent(telegramId)}/topup`,
    { amount_kopecks: amountKopecks, note: note || null },
  );
}

export interface DeviceOut {
  id: number;
  name: string;
  status: string;
  config_id: number;
  access_username: string | null;
  connection_uri: string | null;
}

export interface SubscriptionOut {
  id: number;
  plan_name: string;
  node: string;
  region: string;
  expires_at: string;
  status: string;
  devices?: DeviceOut[];
}

export interface StatsOut {
  users_total: number;
  subscriptions_active: number;
  subscriptions_total: number;
  invoices_pending: number;
  nodes_total: number;
  nodes_active: number;
  devices_active: number;
  provisioning_tasks_pending: number;
  provisioning_tasks_failed: number;
}

export interface InvoiceListItem {
  id: number;
  user_id: number;
  user_telegram_id: string | null;
  plan_id: number;
  plan_name: string;
  subscription_id: number | null;
  amount: number;
  currency: string;
  status: string;
  action: string;
  created_at: string;
}

export interface VPNNodeOut {
  id: number;
  name: string;
  region: string;
  host: string;
  ssh_port: number;
  pool_id: number | null;
  provider_id: number | null;
  notes: string | null;
  status: string;
  is_active: boolean;
  health_score: number;
  blocked_regions: string[];
  created_at: string;
  updated_at: string;
}

export interface NodeHealthOut {
  node_id: number;
  health_score: number;
  blocked_regions: string[];
  overall_success_rate: number;
  per_region: Record<string, number>;
  migrated_subscriptions: number[];
}

export interface VPNNodeCreateIn {
  name: string;
  region: string;
  host: string;
  ssh_port: number;
  pool_id: number | null;
  notes: string | null;
}

// Протоколы должны быть в синке с VPNConfigProtocol enum в
// backend/app/models.py — backend ругнётся 400 на неизвестный.
export type VPNConfigProtocol =
  | "shadowtls+shadowsocks"
  | "vless-reality"
  | "vless-ws-cdn"
  | "hysteria2"
  | "vless-xhttp";

export interface VPNConfigOut {
  id: number;
  node_id: number;
  name: string;
  protocol: VPNConfigProtocol;
  port: number;
  sni: string | null;
  public_key: string | null;
  fallback: string | null;
  settings: Record<string, unknown> | null;
  is_enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface VPNConfigCreateIn {
  name: string;
  protocol: VPNConfigProtocol;
  port: number;
  sni?: string | null;
  public_key?: string | null;
  fallback?: string | null;
  settings?: Record<string, unknown> | null;
  is_enabled?: boolean;
}

export interface PlanOut {
  id: number;
  name: string;
  duration_days: number;
  max_devices: number;
  price: number;
  traffic_limit_mb: number | null;
  is_visible: boolean;
}

export interface PlanCreateIn {
  name: string;
  duration_days: number;
  max_devices: number;
  price: number;
  traffic_limit_mb: number | null;
  is_visible: boolean;
}

export interface ApiTokenOut {
  id: number;
  name: string;
  scopes: string[];
  is_active: boolean;
  created_at: string;
  last_used_at: string | null;
}

export interface ApiTokenCreatedOut extends ApiTokenOut {
  token: string;
}

export interface ProvisioningTaskOut {
  id: number;
  target_type: string;
  target_id: number;
  action: string;
  status: "pending" | "running" | "success" | "failed" | string;
  payload: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  telegram_id: string | null;
}
