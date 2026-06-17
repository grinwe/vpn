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
  // `detail` preserves the raw FastAPI `detail` payload — string when the
  // backend sends a plain message, object when it sends structured info
  // (e.g. `{error, active_subs, message}` from DELETE /nodes/{id}).
  // Callers can narrow on `typeof detail === "object"` to branch on the
  // `error` code without parsing the message string.
  constructor(
    public status: number,
    message: string,
    public detail: unknown = message,
  ) {
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
    let message = res.statusText;
    let rawDetail: unknown = res.statusText;
    try {
      const payload = await res.json();
      if (payload?.detail !== undefined) {
        rawDetail = payload.detail;
        message =
          typeof payload.detail === "string"
            ? payload.detail
            : payload.detail?.message ?? JSON.stringify(payload.detail);
      }
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, message, rawDetail);
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
  banned_at: string | null;
}

export interface AdminTopupResponse {
  user_id: number;
  telegram_id: string | null;
  balance_kopecks: number;
  tx_id: number;
}

export interface BatchBanResult {
  action: "ban" | "unban";
  done: number[];
  skipped: number[];
  not_found: number[];
}

export function batchBanUsers(
  userIds: number[],
  action: "ban" | "unban",
  reason?: string,
): Promise<BatchBanResult> {
  return api.post<BatchBanResult>("/users/batch_ban", {
    user_ids: userIds,
    action,
    reason: reason ?? null,
  });
}

// Per-subscription failure entry shared by both bulk-subscription ops.
export interface BulkUserFailure {
  user_id: number;
  subscription_id: number;
  error: string;
}

// POST /subscriptions/bulk-regenerate-sublink — массовая ПЕРЕГЕНЕРАЦИЯ
// sub-link: каждому активному устройству выбранных юзеров выдаётся новая
// ссылка (в ЛК), старая остаётся живой, + Telegram-уведомление. sub_token
// МЕНЯЕТСЯ. Стоимость прежняя (extra_device_slots не трогаем). Бэкенд
// капит на 25 юзеров/запрос — UI чанкует.
export interface BulkRegenerateResult {
  done: number[];
  skipped: number[];
  not_found: number[];
  failed: BulkUserFailure[];
  notified: number[];
  subscriptions_regenerated: number;
  devices_created: number;
}

export function bulkRegenerateSublink(
  userIds: number[],
  notify: boolean = false,
): Promise<BulkRegenerateResult> {
  return api.post<BulkRegenerateResult>(
    "/subscriptions/bulk-regenerate-sublink",
    { user_ids: userIds, notify },
  );
}

// POST /subscriptions/bulk-rebuild-config — ТИХАЯ пересборка config_text
// из текущего VPNConfig: без ротации sub_token, нового устройства,
// ansible и пуша. Чинит вшитые URI после правки конфигов (напр. xhttp
// sni/port после DR) — клиент подтянет исправленный URI сам на рефреше.
export interface BulkRebuildResult {
  done: number[];
  skipped: number[];
  not_found: number[];
  failed: BulkUserFailure[];
  credentials_rebuilt: number;
}

export function bulkRebuildConfig(
  userIds: number[],
): Promise<BulkRebuildResult> {
  return api.post<BulkRebuildResult>("/subscriptions/bulk-rebuild-config", {
    user_ids: userIds,
  });
}

// ── Operator-aware routing (Phase 1, advisory) ───────────────────────
// Матрица «нода × оператор → ok/fail» из краудсорса юзерских «VPN не
// работает». choose_node это пока НЕ использует. См.
// docs/operations/operator_routing_roadmap.md.

export interface OperatorMatrixCell {
  node_id: number;
  node_name: string | null;
  operator: string;
  ok: number;
  fail: number;
  total: number;
  score: number | null;
  confident: boolean;
}

export interface OperatorMatrixOut {
  window_hours: number;
  min_devices: number;
  cells: OperatorMatrixCell[];
}

export function operatorRoutingMatrix(): Promise<OperatorMatrixOut> {
  return api.get<OperatorMatrixOut>("/admin/operator-routing/matrix");
}

export interface OperatorReportOut {
  id: number;
  user_id: number;
  subscription_id: number | null;
  operator: string | null;
  failed_node_id: number | null;
  failed_node_name: string | null;
  target_node_id: number | null;
  target_node_name: string | null;
  outcome: string;
  reported_at: string | null;
  resolved_at: string | null;
}

export function operatorRoutingReports(
  limit = 200,
): Promise<OperatorReportOut[]> {
  return api.get<OperatorReportOut[]>(
    `/admin/operator-routing/reports?limit=${limit}`,
  );
}

// POST /subscriptions/bulk-migrate-auto — массовый ПЕРЕЕЗД выбранных
// юзеров на свободные ноды (bulk-версия карточной migrate-auto). sub_token
// СОХРАНЯЕТСЯ, уведомления нет (профиль обновляется сам через alias).
export interface BulkMigrateResult {
  done: number[];
  skipped: number[];
  not_found: number[];
  failed: BulkUserFailure[];
  subscriptions_migrated: number;
}

export function bulkMigrateAuto(
  userIds: number[],
): Promise<BulkMigrateResult> {
  return api.post<BulkMigrateResult>("/subscriptions/bulk-migrate-auto", {
    user_ids: userIds,
  });
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

// Orphan-claim — transfers a placeholder-owned Subscription/Device/Credential
// bundle to a real user by UUID. See docs/operations/admin_claim_orphans.md
// and POSTMORTEM_2026-05-19.md for context.
export interface ClaimOrphanRequest {
  user_id?: number | null;
  telegram_id?: string | null;
  // Bare UUID OR full vless://… URL — backend extracts the UUID.
  uuid: string;
  plan_id?: number | null;
  // ISO-8601 string; null/undefined ⇒ backend uses now()+plan.duration_days.
  expires_at?: string | null;
  device_name?: string | null;
}

export interface ClaimedCredentialOut {
  id: number;
  proto: string;
}

export interface ClaimOrphanResponse {
  subscription_id: number;
  device_id: number;
  old_user_id: number;
  new_user_id: number;
  new_expires_at: string;
  claimed_credentials: ClaimedCredentialOut[];
}

export function claimOrphanSubscription(
  body: ClaimOrphanRequest,
): Promise<ClaimOrphanResponse> {
  return api.post<ClaimOrphanResponse>("/admin/claim-orphan", body);
}

// ── Relay-link diagnostics ────────────────────────────────────────────
// POST /exits/links/{link_id}/diagnose — структурированная проверка
// одного relay→exit WG-линка. Backend кладёт structured `checks`
// в task.result, UI рендерит карточками вместо raw stdout. Подробнее:
// docs/operations/diagnostics.md.

export type DiagnoseCheckStatus = "ok" | "warn" | "fail" | "skip" | "info";

export interface DiagnoseCheckEntry {
  name: string;
  status: DiagnoseCheckStatus;
  latency_ms?: number | null;
  message?: string;
  details?: Record<string, unknown>;
}

export interface DiagnoseMeta {
  link_id: number;
  exit_id: number;
  relay_id: number;
  wg_interface: string;
  requested_checks: string[];
  started_at?: string;
  finished_at?: string;
  exit_pubkey_prefix?: string;
}

export interface DiagnoseRelayLinkRequest {
  check_types?: string[] | null;
  xray_port?: number | null;
}

export interface DiagnoseRelayLinkResponse {
  link_id: number;
  relay_node_id: number;
  exit_id: number;
  task_id: number;
}

// Все check_types, поддерживаемые ansible-ролью diagnose_relay_link.
// Если бэк добавит новые — допиши сюда; роль игнорирует неизвестные
// имена молча (только запросит у себя в `when:` фильтре).
export const RELAY_LINK_CHECKS = [
  "peer_on_jump",
  "handshake_age",
  "ping_endpoint",
  "ping_internet_through",
  "xray_port",
  "listening_sockets",
  "peer_on_exit",
  "iptables_forward",
] as const;

export type RelayLinkCheckName = (typeof RELAY_LINK_CHECKS)[number];

export function diagnoseRelayLink(
  linkId: number,
  body?: DiagnoseRelayLinkRequest,
): Promise<DiagnoseRelayLinkResponse> {
  return api.post<DiagnoseRelayLinkResponse>(
    `/exits/links/${linkId}/diagnose`,
    body ?? {},
  );
}

export interface DeviceOut {
  id: number;
  name: string;
  status: string;
  config_id: number;
  access_username: string | null;
  connection_uri: string | null;
  node_id?: number | null;
  node_name?: string | null;
  node_region?: string | null;
  is_relay?: boolean;
  exit_id?: number | null;
  exit_name?: string | null;
}

export interface SubscriptionOut {
  id: number;
  plan_name: string;
  node: string;
  // Current node id. Used by the admin per-sub migrate dropdown to
  // exclude the sub's current node from the target list.
  node_id: number | null;
  region: string;
  expires_at: string;
  status: string;
  devices?: DeviceOut[];
  // True iff at least one live device has an open sharing-enforcer block
  // on the node. Computed server-side from AuditLog. Gates the
  // "снять sharing-бан" button in the Users page.
  sharing_blocked?: boolean;
  // Exit the sub currently egresses through (from Credential.exit_id).
  // NULL on legacy 1:1 relays where the outbound is implicit. Used by
  // the admin switch-exit dropdown to exclude the current exit.
  current_exit_id?: number | null;
  // Имя exit-ноды для current_exit_id — рядом с node в карточке
  // подписки, чтобы админ видел текущий выход без доп. запросов.
  current_exit_name?: string | null;
}

export interface SubscriptionMigrateIn {
  target_node_id: number;
}

export interface SubscriptionMigrateOut {
  subscription_id: number;
  old_node_id: number;
  old_node_name: string;
  new_node_id: number;
  new_node_name: string;
  provisioning_task_id: number | null;
  // Заполняется только авто-миграцией (/migrate-auto): забанили ли
  // старую ноду для юзера. null/undefined для ручной /migrate.
  banned_old_node?: boolean | null;
}

export interface SubscriptionSwitchExitIn {
  exit_id: number;
}

export interface SubscriptionSwitchExitOut {
  subscription_id: number;
  old_exit_id: number | null;
  new_exit_id: number;
  new_interface: string;
  task_ids: number[];
}

export interface DeviceMigrateIn {
  target_node_id: number;
}

export interface DeviceMigrateOut {
  old_device_id: number;
  device_id: number;
  old_node_id: number;
  old_node_name: string;
  new_node_id: number;
  new_node_name: string;
  provisioning_task_id: number | null;
}

export interface DeviceSwitchExitIn {
  exit_id: number;
}

export interface DeviceSwitchExitOut {
  device_id: number;
  old_exit_id: number | null;
  new_exit_id: number;
  new_interface: string;
  task_ids: number[];
}

// Диверсная подписка (DIVERSE_SUB_NODES>1): один device несёт активные
// creds на нескольких нодах. Это — набор тех нод («на каких RU-нодах сидит
// юзер»), по одной записи на ноду с её протоколами.
export interface DeviceNodeOut {
  node_id: number;
  name: string | null;
  region: string | null;
  status: string | null;
  protocols: string[];
}

export interface DeviceNodeSetOut {
  device_id: number;
  nodes: DeviceNodeOut[];
}

export interface DeviceNodeSwapOut {
  device_id: number;
  removed_node_id: number;
  added_nodes: number;
  nodes: DeviceNodeOut[];
}

export function getDeviceNodes(deviceId: number): Promise<DeviceNodeSetOut> {
  return api.get<DeviceNodeSetOut>(`/devices/${deviceId}/nodes`);
}

// Diverse-rotation: убрать ноду из набора device и добрать свежую взамен
// (sub_token не меняется). Возвращает новый набор нод.
export function swapDeviceNode(
  deviceId: number,
  nodeId: number,
): Promise<DeviceNodeSwapOut> {
  return api.post<DeviceNodeSwapOut>(
    `/devices/${deviceId}/nodes/${nodeId}/swap`,
    {},
  );
}

export interface StatsOut {
  users_total: number;
  subscriptions_active: number;
  subscriptions_total: number;
  invoices_pending: number;
  nodes_total: number;
  nodes_active: number;
  devices_active: number;
  // «Активны за 24ч» по реальному трафику (NodeTrafficSample).
  users_active_24h: number;
  devices_active_24h: number;
  orphans_active_24h: number;
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

export interface NodeExitLinkHealthMini {
  exit_id: number;
  exit_name: string;
  wg_interface_name: string;
  last_handshake_at: string | null;
  last_observed_at: string | null;
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
  active_users: number;
  blocked_regions: string[];
  cooldown_until: string | null;
  suspect_since: string | null;
  has_relay_config: boolean;
  exit_links: NodeExitLinkHealthMini[];
  last_ssh_at: string | null;
  // Reconciler: нода помечена dirty (desired > reconciled), прогон отложен на
  // тик. reconcile_due_at — когда тик её подхватит. Дефолты false/null.
  reconcile_pending: boolean;
  reconcile_due_at: string | null;
  // NULL = auto-trigger и Telegram-алёрты включены. Timestamp = mute.
  auto_diagnose_disabled_at?: string | null;
  // ── Diagnose-control state (см. api/diagnostics.py) ──
  // Все timestamp'ы — ISO-8601 или null. Заполняются бэком в VPNNodeOut.
  // disabled_at — hard-stop ВСЕХ diagnose-тасок ноды (отдельно от
  // auto_diagnose_disabled_at, который глушит только smart-триггер+алёрты).
  diagnostics_disabled_at?: string | null;
  // alerts_muted_until — Telegram-алёрты заглушены до этого момента
  // (forever = далёкое будущее). null = не заглушены.
  alerts_muted_until?: string | null;
  // diagnose_incident_open_at — открытый инцидент: пока стоит, бэк
  // авто-передиагностит по follow_mode. ack снимает авто-передиагностику.
  diagnose_incident_open_at?: string | null;
  diagnose_follow_mode?: string | null;
  diagnose_acked_at?: string | null;
  last_diagnosed_at?: string | null;
  // last_probe_* — лёгкий probe (ping/ssh), отдельно от полной диагностики.
  last_probe_at?: string | null;
  last_probe_status?: string | null;
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

// Региональные пулы reality-dest — зеркало REALITY_DEST_POOLS в
// backend/app/services/node_spawner.py. Suggestion'ы в refresh-dest модалке
// показываются ПО СТРАНЕ ноды. Хардкод вместо fetch'а осознанно: пул меняется
// редко, лишний роундтрип при открытии модалки не нужен.
export const REALITY_DEST_POOLS_BY_CC: Record<string, readonly string[]> = {
  ru: ["www.yandex.ru", "vk.ru", "mail.ru", "rutube.ru", "lenta.ru"],
  de: ["www.bmw.de", "www.mercedes-benz.com", "www.zalando.de"],
  nl: ["www.bol.com", "www.philips.com", "www.adyen.com"],
  fr: ["www.louisvuitton.com", "www.decathlon.fr", "www.sncf-connect.com"],
  cz: ["www.seznam.cz", "www.alza.cz"],
  fi: ["www.nokia.com", "www.kone.com", "www.fortum.com"],
  se: ["www.ikea.com", "www.volvocars.com"],
  gb: ["www.bbc.co.uk", "www.gov.uk", "www.bt.com"],
  es: ["www.zara.com", "www.bbva.es", "www.iberia.com"],
  at: ["www.redbull.com", "www.swarovski.com", "www.erstegroup.com"],
  pl: ["www.allegro.pl", "www.onet.pl"],
  ch: ["www.nestle.com", "www.swatch.com"],
  it: ["www.ferrari.com", "www.eni.com", "www.unicredit.it"],
};
// region (название страны) → cc. Зеркало _COUNTRY_CC на бэке.
const REALITY_REGION_CC: Record<string, string> = {
  russia: "ru", "россия": "ru", netherlands: "nl", "нидерланды": "nl",
  germany: "de", "германия": "de", france: "fr", "франция": "fr",
  czech: "cz", czechia: "cz", "czech republic": "cz", "чехия": "cz",
  finland: "fi", "финляндия": "fi", sweden: "se", "швеция": "se",
  "united kingdom": "gb", "great britain": "gb", britain: "gb", uk: "gb",
  england: "gb", "великобритания": "gb", spain: "es", "испания": "es",
  austria: "at", "австрия": "at", switzerland: "ch", "швейцария": "ch",
  italy: "it", "италия": "it", poland: "pl", "польша": "pl",
};
// Пул suggestion'ов по региону ноды (фолбэк — РУ, как на бэке).
export function realityPoolForRegion(
  region: string | null | undefined,
): readonly string[] {
  const cc = REALITY_REGION_CC[(region || "").trim().toLowerCase()];
  return REALITY_DEST_POOLS_BY_CC[cc] || REALITY_DEST_POOLS_BY_CC.ru;
}

export interface NodeRefreshDestIn {
  // null/undefined → бэкенд автоматически выберет из пула наименее
  // используемый домен.
  sni?: string | null;
}

export interface NodeRefreshDestOut {
  node_id: number;
  old_sni: string;
  new_sni: string;
  sub_count: number;
  failed_subs: number[];
  task_ids: number[];
}

export function refreshNodeRealityDest(
  nodeId: number,
  payload: NodeRefreshDestIn,
): Promise<NodeRefreshDestOut> {
  return api.post<NodeRefreshDestOut>(
    `/nodes/${nodeId}/refresh-reality-dest`,
    payload,
  );
}

// ── Cloud providers / order node (hoster API, напр. 4vps) ──

export interface CloudProviderOut {
  id: number;
  name: string;
  kind: string;
  default_image: string | null;
  default_region: string | null;
  default_plan: string | null;
  ssh_key_ids: string[] | null;
  is_active: boolean;
  created_at: string;
}

// Образ ОС внутри тарифа (у 4vps образы зависят от тарифа+ДЦ).
export interface OfferingImage {
  id: number | null;
  name: string;
}

export interface OfferingPlan {
  id: number | null;
  name: string;
  price?: number | null;
  cpu?: number | null;
  ram_mib?: number | null;
  rom?: number | null;
  images?: OfferingImage[];
}

export interface OfferingDatacenter {
  id: number | null;
  name: string;
  flag?: string | null;
  cpu_name?: string | null;
}

export interface ProviderOfferings {
  datacenters: OfferingDatacenter[];
  plans: OfferingPlan[];
  images: OfferingImage[];
}

export interface NodeSpawnIn {
  provider_id: number;
  name?: string | null; // пусто → бэкенд сгенерит «<хостер>-<cc>-<NN>»
  region: string; // datacenter id (строкой)
  plan: string; // tariff id (строкой)
  image?: string | null; // ostempl id (строкой)
  pool_id?: number | null;
  notes?: string | null;
}

export function listCloudProviders(): Promise<CloudProviderOut[]> {
  return api.get<CloudProviderOut[]>("/cloud/providers");
}

export function getProviderOfferings(
  providerId: number,
): Promise<ProviderOfferings> {
  return api.get<ProviderOfferings>(
    `/cloud/providers/${providerId}/offerings`,
  );
}

export function spawnNode(payload: NodeSpawnIn): Promise<VPNNodeOut> {
  return api.post<VPNNodeOut>("/nodes/spawn", payload);
}

// Заказ облачной WG-exit-ноды — зеркало NodeSpawnIn без pool_id (exit'ы не
// входят в choose_node-пул). Возврат не используется формой (она инвалидирует
// список), поэтому unknown.
export interface ExitSpawnIn {
  provider_id: number;
  name?: string | null; // пусто → бэкенд сгенерит «<хостер>-<cc>-<NN>»
  region: string; // datacenter (локация) id строкой
  plan: string; // tariff (preset) id строкой
  image?: string | null; // ostempl id строкой
  notes?: string | null;
}

export function spawnExit(payload: ExitSpawnIn): Promise<unknown> {
  return api.post("/exits/spawn", payload);
}

export function reinstallNode(
  nodeId: number,
  image?: string | null,
): Promise<VPNNodeOut> {
  return api.post<VPNNodeOut>(`/nodes/${nodeId}/reinstall`, { image });
}

export function renewNode(
  nodeId: number,
): Promise<{ node_id: number; renewed: boolean }> {
  return api.post<{ node_id: number; renewed: boolean }>(
    `/nodes/${nodeId}/renew`,
    {},
  );
}

// Правка дисплейных/маршрутных полей ноды (name/region/pool_id/notes).
// Бэк валидирует name как inventory-хост + уникальность; rename без
// re-bootstrap (ansible коннектится по host, name это alias).
export interface VPNNodeUpdateIn {
  name?: string;
  region?: string;
  pool_id?: number | null;
  notes?: string | null;
}

export function updateNode(
  nodeId: number,
  payload: VPNNodeUpdateIn,
): Promise<VPNNodeOut> {
  return api.patch<VPNNodeOut>(`/nodes/${nodeId}`, payload);
}

export interface ServerPoolMini {
  id: number;
  name: string;
}

export function listPools(): Promise<ServerPoolMini[]> {
  return api.get<ServerPoolMini[]>("/pools");
}

// Протоколы должны быть в синке с VPNConfigProtocol enum в
// backend/app/models.py — backend ругнётся 400 на неизвестный.
// shadowtls+shadowsocks и hysteria2 оставлены в типе, так как их всё
// ещё может вернуть бэк для легаси-нод.  Новые конфиги через UI не
// создаём (см. Nodes.tsx, PROTOCOL_DEFAULTS) — полное удаление в 0.4.
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

// Partial update — any field omitted is left untouched on the server.
// ``protocol`` is for client-side validation only (the backend rejects
// swapping the protocol of an existing config with 400).
export interface VPNConfigUpdateIn {
  name?: string | null;
  port?: number | null;
  sni?: string | null;
  public_key?: string | null;
  fallback?: string | null;
  settings?: Record<string, unknown> | null;
  is_enabled?: boolean | null;
  protocol?: VPNConfigProtocol | null;
}

export interface NodeActiveUserOut {
  access_username: string;
  device_id: number | null;
  device_name: string | null;
  subscription_id: number | null;
  user_id: number | null;
  user_telegram_id: string | null;
  plan_id: number | null;
  plan_name: string | null;
  protocols: string[];
  subscription_expires_at: string | null;
}

export interface NodeActiveUsersOut {
  node_id: number;
  observed_at: string | null;
  stale: boolean;
  users: NodeActiveUserOut[];
}

export interface NodeTrafficSamplePoint {
  observed_at: string;
  active_users: number;
  uplink_bytes: number;
  downlink_bytes: number;
}

export interface NodeTrafficHistoryOut {
  node_id: number;
  from_ts: string;
  to_ts: string;
  samples: NodeTrafficSamplePoint[];
}

export interface NodeRelayLinkOut {
  link_id: number;
  exit_id: number;
  exit_name: string;
  wg_interface_name: string;
  wg_client_address_v4: string;
  wg_client_public_key: string;
  credentials_count: number;
  created_at: string;
  // Filled by run_relay_link_health_tick → _auto_diagnose_stale_links
  // when a stale-handshake symptom was detected for this link. UI
  // renders a small "автодиагностика N мин назад" badge.
  last_auto_diagnose_at?: string | null;
  last_auto_diagnose_task_id?: number | null;
  last_auto_diagnose_symptom?: string | null;
}

// Node-level mute (заменяет link-level в миграции 0036). Глушит:
//   - smart-диагностику всех link'ов этой ноды
//   - Telegram-алёрты infra_ssh с этой нодой
//   - node-level smart-diagnose (consecutive SSH fails)
export function disableNodeAutoDiagnose(nodeId: number) {
  return api.post<{ node_id: number; auto_diagnose_disabled_at: string | null }>(
    `/nodes/${nodeId}/auto-diagnose/disable`,
    {},
  );
}

export function enableNodeAutoDiagnose(nodeId: number) {
  return api.post<{ node_id: number; auto_diagnose_disabled_at: string | null }>(
    `/nodes/${nodeId}/auto-diagnose/enable`,
    {},
  );
}

// ── Diagnose-control (api/diagnostics.py) ──────────────────────────────
// Унифицированный control-channel для нод и exit'ов. kind ∈ node|exit.
// disable/enable — hard-stop ВСЕХ diagnose-тасок цели; mute — глушит
// Telegram-алёрты на N часов (>0 N часов, <0 навсегда, 0 — снять mute).
// Возвращают обновлённое diagnose-состояние цели (поля как в VPNNodeOut).

export type DiagnosticsTargetKind = "node" | "exit";

export function diagnosticsDisable(kind: DiagnosticsTargetKind, id: number) {
  return api.post<Record<string, unknown>>(
    `/diagnostics/${kind}/${id}/disable`,
    {},
  );
}

export function diagnosticsEnable(kind: DiagnosticsTargetKind, id: number) {
  return api.post<Record<string, unknown>>(
    `/diagnostics/${kind}/${id}/enable`,
    {},
  );
}

export function diagnosticsMute(
  kind: DiagnosticsTargetKind,
  id: number,
  hours: number,
) {
  return api.post<Record<string, unknown>>(
    `/diagnostics/${kind}/${id}/mute`,
    { hours },
  );
}

// ── Client control channel (admin trigger) ────────────────────────────
// Имитирует client report от имени оператора — юзер написал в саппорт
// через второй канал, оператор кликает кнопку → backend мигрирует
// сабку на другую healthy ноду. См. docs/operations/control_channel_roadmap.md.

export type ClientReportKind =
  | "connect_failed"
  | "user_reported"
  | "health_check_failed";

export interface AdminReportFailureRequest {
  subscription_id: number;
  kind?: ClientReportKind;
}

export interface AdminReportFailureResponse {
  ok: boolean;
  subscription_id: number;
  retry_after_sec: number;
  target_node_id: number | null;
  target_node_name: string | null;
  task_id: number | null;
  action: string;
}

export function adminReportFailureForSubscription(
  body: AdminReportFailureRequest,
): Promise<AdminReportFailureResponse> {
  return api.post<AdminReportFailureResponse>(
    "/admin/client-control/report-for-subscription",
    body,
  );
}

// «Обновить подписку»: авто-выбор свободного сервера из пула (исключая
// текущую ноду и ноды из бан-листа юзера) + миграция + авто-бан старой
// ноды. sub_token сохраняется. В будущем тот же путь — в ЛК юзера.
export function migrateSubscriptionAuto(
  subId: number,
): Promise<SubscriptionMigrateOut> {
  return api.post<SubscriptionMigrateOut>(
    `/subscriptions/${subId}/migrate-auto`,
    {},
  );
}

// Per-node баны юзера — ноды, на которые авто-выбор его не селит.
export interface NodeUserBanOut {
  id: number;
  user_id: number;
  node_id: number;
  node_name: string | null;
  reason: string | null;
  created_by: string | null;
  created_at: string;
}

export function listUserNodeBans(userId: number): Promise<NodeUserBanOut[]> {
  return api.get<NodeUserBanOut[]>(`/users/${userId}/node-bans`);
}

export function removeUserNodeBan(
  userId: number,
  nodeId: number,
): Promise<{ status: string }> {
  return api.del<{ status: string }>(`/users/${userId}/node-bans/${nodeId}`);
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
  status: "pending" | "running" | "success" | "failed" | "cancelled" | string;
  payload: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  cancel_requested_at: string | null;
  telegram_id: string | null;
}

// ── Health-ping dashboard ──
// Источник данных — AuditLog rows с action IN (health_ping_request,
// health_ping_response, health_ping_opt_out). См. docs/data-model.md.

export interface HealthPingTotals {
  requests: number;
  responses: number;
  ok: number;
  bad: number;
  bad_prompted: number;
  bad_self_reported: number;
  opt_outs: number;
  response_rate: number;
}

export interface HealthPingPerNode {
  node_id: number | null;
  node_name: string | null;
  requests: number;
  ok: number;
  bad: number;
  bad_ratio: number;
}

export interface HealthPingTimeseriesPoint {
  bucket_ts: string;
  ok: number;
  bad: number;
}

export interface HealthPingSummaryOut {
  from_ts: string;
  to_ts: string;
  hours: number;
  bucket: "hour" | "day" | string;
  totals: HealthPingTotals;
  per_node: HealthPingPerNode[];
  timeseries: HealthPingTimeseriesPoint[];
}

export interface HealthPingRecentBadItem {
  created_at: string;
  telegram_id: string | null;
  user_id: number | null;
  node_id: number | null;
  node_name: string | null;
  subscription_id: number | null;
  plan_name: string | null;
  source: "prompted" | "self_reported" | string;
}

export interface HealthPingRecentBadOut {
  items: HealthPingRecentBadItem[];
}

export interface NodeHealthPingStatsOut {
  node_id: number;
  hours: number;
  requests: number;
  ok: number;
  bad: number;
  bad_ratio: number;
  last_bad_at: string | null;
}

// ---- Broadcasts ----

export type BroadcastStatus =
  | "queued"
  | "sending"
  | "completed"
  | "cancelled"
  | "failed";

export type BroadcastTargetFilter =
  | { type: "all" }
  | { type: "active" }
  | { type: "ids"; ids: number[] };

export interface BroadcastOut {
  id: number;
  created_at: string;
  created_by: string;
  text: string;
  target_filter: BroadcastTargetFilter;
  status: BroadcastStatus;
  total_recipients: number | null;
  sent_count: number;
  failed_count: number;
  last_user_id_cursor: number;
  started_at: string | null;
  completed_at: string | null;
  cancelled_reason: string | null;
}

export interface BroadcastListResponse {
  items: BroadcastOut[];
  total: number;
  has_more: boolean;
}

export function listBroadcasts(
  params: { limit?: number; offset?: number; status?: string } = {},
) {
  const qs = new URLSearchParams();
  if (params.limit !== undefined) qs.set("limit", String(params.limit));
  if (params.offset !== undefined) qs.set("offset", String(params.offset));
  if (params.status) qs.set("status", params.status);
  const suffix = qs.toString() ? `?${qs}` : "";
  return api.get<BroadcastListResponse>(`/broadcasts${suffix}`);
}

export function getBroadcast(id: number) {
  return api.get<BroadcastOut>(`/broadcasts/${id}`);
}

export function createBroadcast(body: {
  text: string;
  target_filter: BroadcastTargetFilter;
}) {
  return api.post<BroadcastOut>("/broadcasts", body);
}

export function previewBroadcast(body: {
  target_filter: BroadcastTargetFilter;
}) {
  return api.post<{ recipient_count: number }>("/broadcasts/preview", body);
}

export function cancelBroadcast(id: number, reason?: string) {
  return api.post<BroadcastOut>(`/broadcasts/${id}/cancel`, {
    reason: reason ?? null,
  });
}
