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

export interface BalanceInfo {
  balance_kopecks: number;
  balance_rub: number;
  min_days_remaining: number | null;
  has_active_balance_sub: boolean;
  trial_available: boolean;
  trial_amount_kopecks: number;
}

export interface TrialActivateResponse {
  trial_amount_kopecks: number;
  referral_bonus_kopecks: number;
  balance_kopecks: number;
  trial_expires_at: string;
}

export async function activateTrial() {
  return request<TrialActivateResponse>("/api/webapp/trial/activate", {
    method: "POST",
  });
}

export interface DeviceSummary {
  id: number;
  name: string;
  status: string;
  sub_token: string | null;
  created_at: string | null;
}

export interface SubscriptionExtra {
  subscription_id: number;
  plan_name: string | null;
  plan_price_kopecks: number;
  plan_duration_days: number;
  expires_at: string | null;
  auto_renew: boolean;
  frozen_until: string | null;
  can_freeze: boolean;
  device_count: number;
  bundled_devices: number;
  extra_device_slots: number;
  extra_device_monthly_kopecks: number;
  next_extra_fee_kopecks: number;
  period?: "month" | "year";
  total_per_period_kopecks?: number;
  total_monthly_kopecks: number;
  devices: DeviceSummary[];
}

export interface AddDeviceResponse {
  subscription_id: number;
  device_id: number;
  device_count: number;
  new_daily_cost_kopecks: number;
}

export async function addDevice(subscriptionId: number) {
  return request<AddDeviceResponse>(
    `/api/webapp/subscriptions/${subscriptionId}/devices`,
    { method: "POST" },
  );
}

export interface RenameDeviceResponse {
  device_id: number;
  name: string;
}

export async function renameDevice(deviceId: number, name: string) {
  return request<RenameDeviceResponse>(`/api/webapp/devices/${deviceId}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}

export interface RemoveDeviceResponse {
  device_id: number;
  device_count: number;
  new_daily_cost_kopecks: number;
}

export async function removeDevice(deviceId: number) {
  return request<RemoveDeviceResponse>(`/api/webapp/devices/${deviceId}`, {
    method: "DELETE",
  });
}

export interface MeResponse {
  user: User;
  subscriptions: Subscription[];
  balance: BalanceInfo;
  subscription_extras: SubscriptionExtra[];
  sub_link_base_url: string;
  bot_username: string;
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

// ── Balance billing ──────────────────────────────────────────────────

export interface TopupResponse {
  invoice_id: number;
  provider: string;
  pay_url: string;
  amount: number;
  currency: string;
}

export async function createTopup(amountKopecks: number, provider = "telegram_stars") {
  return request<TopupResponse>("/api/webapp/topup", {
    method: "POST",
    body: JSON.stringify({ amount_kopecks: amountKopecks, provider }),
  });
}

export interface ActivateResponse {
  subscription_id: number;
  sub_token: string | null;
  expires_at: string;
  balance_kopecks: number;
  plan_price_kopecks: number;
  plan_duration_days: number;
}

export interface InsufficientBalanceDetail {
  code: "insufficient_balance";
  balance_kopecks: number;
  required_kopecks: number;
  suggested_topup_kopecks: number;
}

export async function activateSubscription(planId: number) {
  return request<ActivateResponse>("/api/webapp/subscriptions/activate", {
    method: "POST",
    body: JSON.stringify({ plan_id: planId }),
  });
}

export interface FreezeResponse {
  subscription_id: number;
  status: string;
  frozen_until: string | null;
  can_freeze_again: boolean;
}

export async function freezeSubscription(id: number) {
  return request<FreezeResponse>(`/api/webapp/subscriptions/${id}/freeze`, {
    method: "POST",
  });
}

export interface UnfreezeResponse {
  subscription_id: number;
  status: string;
  expires_at: string | null;
}

export async function unfreezeSubscription(id: number) {
  return request<UnfreezeResponse>(`/api/webapp/subscriptions/${id}/unfreeze`, {
    method: "POST",
  });
}

export interface CancelSubscriptionResponse {
  subscription_id: number;
  auto_renew: boolean;
  expires_at: string | null;
}

export async function cancelSubscription(id: number) {
  return request<CancelSubscriptionResponse>(
    `/api/webapp/subscriptions/${id}/cancel`,
    { method: "POST" },
  );
}

export interface AutoRenewToggleResponse {
  subscription_id: number;
  auto_renew: boolean;
}

export async function toggleAutoRenew(id: number, autoRenew: boolean) {
  return request<AutoRenewToggleResponse>(
    `/api/webapp/subscriptions/${id}/auto_renew`,
    {
      method: "POST",
      body: JSON.stringify({ auto_renew: autoRenew }),
    },
  );
}

export interface ChangePlanResponse {
  subscription_id: number;
  new_plan_name: string;
  expires_at: string;
  refunded_kopecks: number;
  charged_kopecks: number;
  balance_kopecks: number;
}

export async function changePlan(subscriptionId: number, planId: number) {
  return request<ChangePlanResponse>(
    `/api/webapp/subscriptions/${subscriptionId}/change_plan`,
    {
      method: "POST",
      body: JSON.stringify({ plan_id: planId }),
    },
  );
}

// ── History + referral ───────────────────────────────────────────────

export interface TransactionRow {
  id: number;
  amount_kopecks: number;
  kind: "topup" | "spend" | "refund" | "bonus" | "adjust";
  reference: string | null;
  note: string | null;
  created_at: string;
}

export interface TransactionsResponse {
  items: TransactionRow[];
  has_more: boolean;
}

export async function fetchTransactions(limit = 50, offset = 0) {
  return request<TransactionsResponse>(
    `/api/webapp/transactions?limit=${limit}&offset=${offset}`,
  );
}

export interface ReferralInfo {
  code: string | null;
  bonus_kopecks: number;
  invited_count: number;
  earned_kopecks: number;
  share_url: string | null;
}

export async function fetchReferral() {
  return request<ReferralInfo>("/api/webapp/referral");
}

// ── Self-report «VPN не работает» ────────────────────────────────────
//
// Запись в тот же AuditLog, что и плановые health-ping'и бота, но с
// extra.source = "self_reported" — админка выделяет такие жалобы
// отдельной красной карточкой как более сильный сигнал.

export interface HealthPingReportResponse {
  ok: boolean;
  subscription_id: number | null;
  node_id: number | null;
}

export async function reportVpnBroken() {
  return request<HealthPingReportResponse>(
    "/api/webapp/health-ping-report",
    { method: "POST" },
  );
}
