// Thin client for /api/webapp/* endpoints.
//
// Token lifecycle: obtained from POST /auth using Telegram initData,
// kept in-memory only (sessionStorage is fine but not necessary — the
// page lives inside Telegram and is short-lived). On 401 the caller
// should re-auth via initData.

export interface Subscription {
  id: number;
  plan_name: string;
  plan_id: number;
  node: string;
  region: string;
  expires_at: string;
  status: string;
  auto_renew: boolean;
  sub_token: string | null;
  credentials: { proto: string; config_text: string }[];
  devices: unknown[];
}

export interface User {
  id: number;
  telegram_id: string | null;
  email: string | null;
  created_at: string;
  subscription_count: number;
}

export interface MeResponse {
  user: User;
  subscriptions: Subscription[];
}

let token: string | null = null;

export function setToken(t: string | null) {
  token = t;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((init.headers as Record<string, string>) ?? {}),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text || res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export async function authWithInitData(initData: string) {
  return request<{ token: string; expires_in: number; user_id: number }>(
    "/api/webapp/auth",
    {
      method: "POST",
      body: JSON.stringify({ init_data: initData }),
    },
  );
}

export async function fetchMe() {
  return request<MeResponse>("/api/webapp/me");
}

export interface WebAppPlan {
  id: number;
  name: string;
  tier: "Solo" | "Family" | "Pro";
  period: "month" | "year";
  duration_days: number;
  max_devices: number;
  price_rub: number;
  price_stars: number;
  badge: "popular" | null;
}

export async function fetchPlans() {
  return request<WebAppPlan[]>("/api/webapp/plans");
}

export interface CheckoutResponse {
  invoice_id: number;
  provider: string;
  pay_url: string;
  amount: number;
  currency: string;
}

export async function createCheckout(planId: number, provider = "telegram_stars") {
  return request<CheckoutResponse>("/api/webapp/checkout", {
    method: "POST",
    body: JSON.stringify({ plan_id: planId, provider }),
  });
}

export interface InvoiceStatusResponse {
  invoice_id: number;
  status: "pending" | "paid" | "failed";
  subscription_id: number | null;
  subscription_active: boolean;
  has_credentials: boolean;
}

export async function fetchInvoiceStatus(id: number) {
  return request<InvoiceStatusResponse>(`/api/webapp/invoices/${id}`);
}
