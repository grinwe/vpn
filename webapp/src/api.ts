// Thin client for /api/webapp/* endpoints.
//
// Token lifecycle: obtained from POST /auth using Telegram initData,
// kept in-memory only (sessionStorage is fine but not necessary — the
// page lives inside Telegram and is short-lived). On 401 the request()
// wrapper re-auths via initData transparently for ALL calls.

import { getTg } from "./telegram";

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
  // Токен устройства, чью ссылку показывает и бот (/config). У подписок до
  // 2026-09-28 бэк кладёт сюда legacy sub_token (он отдаёт креды ВСЕХ
  // устройств) — см. SubscriptionCard. Опционально: старый бэк поля не шлёт.
  link_token?: string | null;
  // Готовый URL link_token с доменом как у бота — см. subLinkUrl.
  link_url?: string | null;
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
  // Можно ли сразу потратить бонус на активацию плана. False, если у юзера
  // уже есть живая подписка — /subscriptions/activate снёс бы её (сменa
  // тарифа в single-sub модели). Опционально: старый бэк поля не отдаёт,
  // undefined читается как «нельзя» — безопасный дефолт на время деплоя.
  trial_autoactivate_allowed?: boolean;
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
  sub_url?: string | null;
}

// Саб-ссылка для показа. Бэк отдаёт готовый URL (домен выбран как у бота:
// 50/50 grn-ssync/grwr по токену) только у подписок с 2026-09-28 — тогда
// бот и кабинет показывают ОДИН И ТОТ ЖЕ URL. У старых собираем по-старому
// из SUB_LINK_BASE_URL: смена домена у уже импортированной ссылки дала бы
// при переимпорте профиль-дубль. Опционально: старый бэк полей не шлёт.
export function subLinkUrl(
  ready: string | null | undefined,
  token: string | null | undefined,
  base: string,
): string | null {
  if (ready) {
    return ready.startsWith("/") ? `${window.location.origin}${ready}` : ready;
  }
  if (!token) return null;
  return base ? `${base}/${token}` : `${window.location.origin}/api/sub/${token}`;
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

// Эндпоинт авторизации: на нём НЕ пытаемся переавторизоваться на 401,
// иначе reauth() (который сам дёргает /auth) уходит в рекурсию.
const AUTH_PATH = "/api/webapp/auth";
const FETCH_TIMEOUT_MS = 15000;

// Лёгкие breadcrumbs сетевых сбоев. Пользователю НЕ показываются (у него —
// friendlyError), нужны поддержке, разбирающей «кабинет не грузится»: в
// консоли webview видно метод/путь/статус/длительность и факт reauth.
// Префикс [webapp-net] — чтобы грепать в логах webview.
function netLog(event: string, data?: Record<string, unknown>): void {
  try {
    console.warn(`[webapp-net] ${event}`, data ?? "");
  } catch {
    // console может отсутствовать в экзотическом webview — диагностика
    // не должна ронять сам запрос.
  }
}

// Голый fetch с таймаутом и feature-detection AbortController.
//
// AbortSignal.timeout появился только в Safari 16 / Android WebView ~103.
// Telegram Mini App крутится в СИСТЕМНОМ WebView устройства (iOS 15,
// бюджетный Android), где его нет — прямой вызов кидал TypeError синхронно и
// ронял КАЖДЫЙ запрос, включая стартовый /auth (целая когорта устройств без
// доступа, как раз частая аудитория VPN). Поэтому таймаут строим через
// AbortController; если и его нет — деградируем до fetch без таймаута
// (лучше без ограничения, чем полностью неработающее приложение).
//
// На мобильных сетях TCP может висеть минутами без ответа — таймаут бросает
// "timeout", чтобы сработала ветка NETWORK_HINT в friendlyError вместо
// вечной «Загрузки…».
async function rawFetch(path: string, init: RequestInit): Promise<Response> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((init.headers as Record<string, string>) ?? {}),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const method = (init.method ?? "GET").toUpperCase();
  const started = Date.now();

  let signal = init.signal ?? null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  if (!signal && typeof AbortController !== "undefined") {
    const controller = new AbortController();
    signal = controller.signal;
    timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  }

  try {
    const res = await fetch(path, {
      ...init,
      headers,
      signal: signal ?? undefined,
    });
    if (!res.ok) {
      netLog("http-error", {
        method,
        path,
        status: res.status,
        ms: Date.now() - started,
      });
    }
    return res;
  } catch (e) {
    // TimeoutError/AbortError (наш таймаут или отмена вызывающим) → "timeout".
    if (
      e instanceof DOMException &&
      (e.name === "TimeoutError" || e.name === "AbortError")
    ) {
      netLog("timeout", { method, path, ms: Date.now() - started });
      throw new Error("timeout");
    }
    netLog("network-error", {
      method,
      path,
      ms: Date.now() - started,
      name: (e as Error)?.name,
    });
    throw e;
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

// Прозрачная переавторизация через Telegram initData. initData живёт весь
// сеанс Mini App, поэтому протухший токен чиним прозрачно, не заставляя юзера
// переоткрывать приложение. Раньше это жило ТОЛЬКО в App.tsx для /me, из-за
// чего мутации (заморозка, добавление устройства, пополнение…) падали сырым
// «401» при возврате в давно открытый кабинет. Теперь — на уровне общей
// обёртки, поэтому получают ВСЕ вызовы. С App.tsx не конфликтует: там reauth
// остаётся страховкой для стартового bootstrap, а рекурсию режет AUTH_PATH.
async function reauth(): Promise<boolean> {
  const tg = getTg();
  if (!tg || !tg.initData) return false;
  try {
    const auth = await authWithInitData(tg.initData);
    setToken(auth.token);
    netLog("reauth-ok");
    return true;
  } catch {
    netLog("reauth-failed");
    return false;
  }
}

// Текст ошибки для человека из того, что бросил request(): "502: {\"detail\":
// \"Платёжный сервис временно недоступен…\"}" → сама фраза. Коды и JSON
// пользователю не нужны; если detail не строка — общая формулировка.
export function humanError(e: unknown, fallback = "Не удалось выполнить операцию. Попробуй ещё раз или напиши в поддержку."): string {
  const raw = e instanceof Error ? e.message : String(e ?? "");
  const m = /^\d+:\s*([\s\S]+)$/.exec(raw);
  const body = (m ? m[1] : raw).trim();
  if (body.startsWith("{")) {
    try {
      const parsed = JSON.parse(body) as { detail?: unknown };
      if (typeof parsed.detail === "string" && parsed.detail.trim()) return parsed.detail;
    } catch {
      /* не JSON */
    }
    return fallback;
  }
  return body || fallback;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res = await rawFetch(path, init);

  // Токен протух → один раз прозрачно переавторизуемся и повторяем запрос.
  // Защита от рекурсии: сам /auth не переавторизуем (иначе reauth зациклится).
  if (
    (res.status === 401 || res.status === 403) &&
    path !== AUTH_PATH
  ) {
    netLog("reauth-trigger", { path, status: res.status });
    if (await reauth()) {
      res = await rawFetch(path, init);
    }
  }

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

// provider: "telegram_stars" | "lava_top" (карта РФ) | "lava_top_sbp" (СБП).
// Бэкенд принимает любое имя провайдера — новые добавляются без правки здесь.
export async function createTopup(amountKopecks: number, provider = "telegram_stars") {
  return request<TopupResponse>("/api/webapp/topup", {
    method: "POST",
    body: JSON.stringify({ amount_kopecks: amountKopecks, provider }),
  });
}

// Поллит /me, пока баланс не превысит baseline (платёж зачислён вебхуком),
// либо пока не выйдут попытки. Нужно для внешних платёжных страниц (карта РФ
// lava_top / СБП lava_top_sbp), у которых — в отличие от Telegram Stars
// openInvoice — нет синхронного callback. Возвращает true, если зачисление
// поймано.
export async function pollBalanceIncrease(
  baselineKopecks: number,
  {
    attempts,
    delayMs,
    shouldStop,
  }: { attempts: number; delayMs: number; shouldStop?: () => boolean },
): Promise<boolean> {
  for (let i = 0; i < attempts; i++) {
    await new Promise((r) => setTimeout(r, delayMs));
    // Отмена: вызывающий закрыл модалку/начал новый платёж — прекращаем,
    // чтобы отвязанный поллинг не дёргал UI-побочки постфактум.
    if (shouldStop?.()) return false;
    try {
      const fresh = await fetchMe();
      if (fresh.balance.balance_kopecks > baselineKopecks) return true;
    } catch {
      // Сетевой сбой при поллинге не критичен — пробуем ещё; источник
      // истины всё равно вебхук на бэке, баланс появится при следующем /me.
    }
  }
  return false;
}

export interface ActivateResponse {
  subscription_id: number;
  sub_token: string | null;
  sub_url?: string | null;
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

// ── Self-repair «VPN не работает» ────────────────────────────────────
//
// Кабинет ходит в то же ядро починки, что бот и страница на саб-домене
// (backend/app/services/self_repair.py): единый action, единый троттл и
// суточный потолок. Контракт — WebappRepairResponse в api_webapp.py.

// Исход одного шага починки. migrated / reshuffled / duplicated — что-то
// поменяли (дальше спрашиваем оператора и «помогло ли»); остальные —
// честный отказ с причиной.
export type RepairAction =
  | "migrated"
  | "reshuffled"
  | "duplicated"
  | "throttled"
  | "daily_limit"
  | "no_target"
  | "no_subscription"
  | "not_ready";

// device — тронули одно устройство, subscription — всю подписку
// («🔁 Все мои устройства»).
export type RepairScope = "device" | "subscription";

export interface RepairResponse {
  ok: boolean;
  action: RepairAction;
  report_id: number | null;
  new_node_name: string | null;
  new_node_region: string | null;
  task_id: number | null;
  // throttled / daily_limit: через сколько секунд можно снова.
  retry_after_sec: number | null;
  device_name: string | null;
  scope: RepairScope;
  // Совместимость: true для migrated / reshuffled / duplicated.
  migrated: boolean;
}

export interface RepairDevice {
  id: number;
  name: string;
  status: string;
}

// Пре-чек перед кнопкой: живые устройства первой активной подписки и надо
// ли ждать по единой политике повторов. subscription_id есть, а устройств
// нет — все они ещё pending (собираются).
export interface RepairState {
  devices: RepairDevice[];
  retry_after_sec: number | null;
  wait_reason: "throttled" | "daily_limit" | null;
  subscription_id: number | null;
}

export async function fetchRepairState() {
  return request<RepairState>("/api/webapp/repair-state");
}

// «Это устройство не работает» — один шаг лестницы для устройства
// (перетасовка протоколов → перенос → дубль), соседние не трогаем.
export async function reportBrokenDevice(deviceId: number) {
  return request<RepairResponse>("/api/webapp/report-broken-device", {
    method: "POST",
    body: JSON.stringify({ device_id: deviceId }),
  });
}

// «Все мои устройства» — перенос всей подписки.
export async function reportBrokenAll() {
  return request<RepairResponse>("/api/webapp/report-broken", {
    method: "POST",
  });
}

// Мобильные операторы (таксономия operator_routing_roadmap.md). value идёт
// на бэк, label показываем юзеру.
export const VPN_OPERATORS: { value: string; label: string }[] = [
  { value: "mts", label: "МТС" },
  { value: "beeline", label: "Билайн" },
  // Yota — MVNO на сети МегаФона, Т-Мобайл (бывш. Tinkoff) — на сети Tele2:
  // подписываем в скобках, чтобы их юзеры находили себя (синхрон с ботом).
  { value: "megafon", label: "МегаФон (Yota)" },
  { value: "tele2", label: "Tele2 (Т-Мобайл)" },
  { value: "home_wifi", label: "Домашний Wi-Fi" },
  { value: "other", label: "Другое" },
];

export async function setReportOperator(reportId: number, operator: string) {
  return request<{ report_id: number; operator: string }>(
    "/api/webapp/report-operator",
    { method: "POST", body: JSON.stringify({ report_id: reportId, operator }) },
  );
}

// Обратная связь после шага починки. «Всё равно не работает» → target-нода
// тоже fail (самый весомый сигнал для матрицы оператор×нода); «всё
// работает» → ok. Оба — по report_id, чужой репорт бэк отдаст 404.
export async function reportStillBroken(reportId: number) {
  return request<{ report_id: number; outcome: string }>(
    "/api/webapp/report-still-broken",
    { method: "POST", body: JSON.stringify({ report_id: reportId }) },
  );
}

export async function reportOk(reportId: number) {
  return request<{ report_id: number; outcome: string }>(
    "/api/webapp/report-ok",
    { method: "POST", body: JSON.stringify({ report_id: reportId }) },
  );
}
