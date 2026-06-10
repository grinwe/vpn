import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, List, Optional
from pydantic import BaseModel, Field, PlainSerializer, field_validator


def _iso_utc_z(v: datetime) -> str:
    """Сериализуем naive/aware datetime как ISO 8601 UTC с хвостовым ``Z``.

    В БД колонки DateTime naive (см. ``time_utils.utcnow`` — хранится UTC
    без tzinfo). Pydantic по умолчанию отдаёт naive как строку без суффикса,
    а ``new Date("...")`` в браузере трактует такой ISO как local-time —
    и admin UI видит возраст на ``TZ_offset`` минут больше реального, что
    ломает арифметические проверки вроде ``observedAgeMin > 15``.
    """
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.isoformat().replace("+00:00", "Z")


UTCDateTime = Annotated[datetime, PlainSerializer(_iso_utc_z, when_used="json")]


class CredentialOut(BaseModel):
    id: int
    proto: str
    config_text: str
    device_id: int | None = None
    config_id: int | None = None

    class Config:
        from_attributes = True

    @classmethod
    def from_orm(cls, obj):  # type: ignore[override]
        # Transparently decrypt config_text on read.
        from .security import decrypt

        return cls(
            id=obj.id,
            proto=obj.proto,
            config_text=decrypt(obj.config_text) or "",
            device_id=getattr(obj, "device_id", None),
            config_id=getattr(obj, "config_id", None),
        )


class DeviceOut(BaseModel):
    id: int
    name: str
    status: str
    config_id: int | None = None
    access_username: str | None = None
    connection_uri: str | None = None
    # Per-device placement context for the admin UI. Populated by the
    # admin serializer (_subscriptions_for_user in api/users.py) from
    # the sub's already-loaded ``node`` + the first active cred's
    # ``exit_id``. Left as None/False for non-admin callers (bot
    # ``_subscriptions_for_user`` in api.py, webapp flows) so the field
    # additions don't leak sub-level data onto user-facing endpoints.
    node_id: int | None = None
    node_name: str | None = None
    node_region: str | None = None
    # True iff the sub's VPNNode has ``relay_config`` set — admin UI
    # renders a red "relay" badge and shows the exit alongside so ops
    # can tell at a glance which devices tunnel out via WG.
    is_relay: bool = False
    # Exit the sub currently egresses through (same value as
    # SubscriptionOut.current_exit_id; duplicated onto each device so
    # per-device cards in the admin UI stay self-contained).
    exit_id: int | None = None
    exit_name: str | None = None

    class Config:
        from_attributes = True

    @classmethod
    def from_orm(
        cls,
        obj,  # type: ignore[override]
        *,
        node_id: int | None = None,
        node_name: str | None = None,
        node_region: str | None = None,
        is_relay: bool = False,
        exit_id: int | None = None,
        exit_name: str | None = None,
    ):
        from .security import decrypt

        return cls(
            id=obj.id,
            name=obj.name,
            status=obj.status.value if hasattr(obj.status, "value") else obj.status,
            config_id=obj.config_id,
            access_username=obj.access_username,
            connection_uri=decrypt(obj.connection_uri),
            node_id=node_id,
            node_name=node_name,
            node_region=node_region,
            is_relay=is_relay,
            exit_id=exit_id,
            exit_name=exit_name,
        )


class DeviceStatusOut(DeviceOut):
    credentials: list[CredentialOut] = []
    provisioning_task_id: int | None = None


class VPNConfigCreate(BaseModel):
    name: str
    protocol: str
    port: int
    sni: str | None = None
    public_key: str | None = None
    fallback: str | None = None
    settings: dict[str, Any] | None = None
    is_enabled: bool = True


class VPNConfigOut(VPNConfigCreate):
    id: int
    node_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class VPNConfigUpdate(BaseModel):
    # In-place edit of an existing VPNConfig. ``protocol`` сюда приходит
    # read-only echo'ом из UI — менять протокол нельзя (это другой конфиг),
    # эндпоинт лишь отвергает запрос, если присланный protocol != текущего.
    # Поле ОБЯЗАНО быть объявлено: update_config читает ``payload.protocol``,
    # а в Pydantic v2 доступ к необъявленному (extra-ignored) полю кидает
    # AttributeError → весь PUT падал в 500 ДО применения sni/прочих полей,
    # т.е. редактирование конфига вообще не сохранялось. Все поля optional;
    # пропущенные — не трогаются. ``settings`` мёржится в существующий JSONB,
    # чтобы UI мог обновить один под-ключ, не пересылая зашифрованные секреты.
    name: str | None = None
    port: int | None = None
    sni: str | None = None
    public_key: str | None = None
    fallback: str | None = None
    settings: dict[str, Any] | None = None
    is_enabled: bool | None = None
    protocol: str | None = None
    # Optional — if sent, backend verifies it matches the existing
    # protocol (defensive; the UI passes it for readability).
    protocol: str | None = None


class NodeActiveUserOut(BaseModel):
    access_username: str
    device_id: int | None = None
    device_name: str | None = None
    subscription_id: int | None = None
    user_id: int | None = None
    user_telegram_id: str | None = None
    plan_id: int | None = None
    plan_name: str | None = None
    protocols: list[str] = Field(default_factory=list)
    subscription_expires_at: datetime | None = None


class NodeActiveUsersOut(BaseModel):
    node_id: int
    observed_at: UTCDateTime | None = None
    # ``True`` if the latest NodeTrafficSample is older than 15 minutes
    # (or doesn't exist) — UI should surface "нет свежих данных".
    stale: bool
    users: list[NodeActiveUserOut] = Field(default_factory=list)


class NodeTrafficSamplePoint(BaseModel):
    observed_at: UTCDateTime
    active_users: int
    uplink_bytes: int
    downlink_bytes: int


class NodeTrafficHistoryOut(BaseModel):
    node_id: int
    from_ts: UTCDateTime
    to_ts: UTCDateTime
    samples: list[NodeTrafficSamplePoint] = Field(default_factory=list)


class VPNNodeCreate(BaseModel):
    name: str
    region: str
    host: str
    ssh_port: int = 22
    pool_id: int | None = None
    notes: str | None = None


class VPNNodeWithConfigsCreate(BaseModel):
    """Composite create-node-and-configs: за один запрос делаем INSERT
    ноды + N INSERT'ов VPN-конфигов + один общий bootstrap.

    Заменяет старый flow «создать ноду → bootstrap → добавить config →
    bootstrap → …», который плодил N+1 таску на каждое добавление
    протокола.

    Best-effort атомарность: при exception во время создания configs
    бэк rollback'ает уже-созданную ноду + успешные configs (см.
    create_node_with_configs). Полностью atomic'ный flow требовал бы
    рефакторинга ensure_reality_config/ensure_shadowtls_config'ов
    (они сейчас сами commit'ят) — оставлено на потом.
    """
    node: VPNNodeCreate
    configs: list[VPNConfigCreate] = []


class NodeExitLinkHealthMini(BaseModel):
    """Mini-view одного relay→exit линка для отрисовки health-dots в строке
    таблицы Nodes (симметрично ``ExitLinkHealthMini``, но с точки зрения relay).
    """
    exit_id: int
    exit_name: str
    wg_interface_name: str
    last_handshake_at: UTCDateTime | None = None
    last_observed_at: UTCDateTime | None = None


class VPNNodeOut(VPNNodeCreate):
    id: int
    provider_id: int | None = None
    status: str
    is_active: bool
    health_score: int | None = None
    blocked_regions: list[str] = []
    # Until this timestamp ``choose_node`` skips the node even with
    # ``is_active=True``. Surfaced so the admin UI can warn operators —
    # otherwise a node that's toggled "active" but still cooling down
    # looks eligible and silently gets no traffic.
    cooldown_until: UTCDateTime | None = None
    suspect_since: UTCDateTime | None = None
    # True iff node.relay_config is populated (i.e. it's a relay attached
    # to some wg_exit_node). Private key lives encrypted in the link row;
    # we only expose the boolean so the admin UI can filter attachable
    # nodes without learning the tunnel metadata.
    has_relay_config: bool = False
    # Health-dots для relay-нод. Дефолт пустой, потому что из семи
    # call-сайтов ``from_orm`` только ``list_nodes`` реально bulk-load'ит
    # линки — остальные используют VPNNodeOut как ответ после мутации
    # одной ноды, где dots не нужны.
    exit_links: list[NodeExitLinkHealthMini] = []
    # Время последнего успешного SSH-тика ``run_traffic_stats_tick`` на
    # эту ноду — прокси для «нода жива». Тик сам пишет NodeTrafficSample
    # при каждом успешном ``xray api statsquery``, значит max(observed_at)
    # это timestamp последнего доказательства что SSH дошёл и xray отдал
    # stats. Без клика по diagnose. Дефолт None (``list_nodes`` bulk-load,
    # остальные call-сайты VPNNodeOut его не проставляют — им неактуально).
    last_ssh_at: UTCDateTime | None = None
    # Текущее число активных юзеров — active_users из ПОСЛЕДНЕГО
    # NodeTrafficSample (тем же per-node lookup, что и last_ssh_at). Нужно
    # админке, чтобы отличать idle-туннель (0 юзеров → WG без трафика не делает
    # handshake → серый) от реального обрыва (юзеры есть, а handshake протух →
    # красный). Дефолт 0; кроме list_nodes другие call-сайты не проставляют.
    active_users: int = 0
    # Reconciler-видимость: desired_generation > reconciled_generation, т.е.
    # ноде нужен прогон, но он отложен на reconcile-тик (defer-модель). Без
    # этого флага операторское действие при включённом RECONCILER_ENABLED
    # выглядит как «ничего не произошло» — таска материализуется только когда
    # тик сойдёт ноду. reconcile_pending derived (ставится в list_nodes, как
    # active_users); reconcile_due_at маппится из ORM-колонки автоматически.
    reconcile_pending: bool = False
    reconcile_due_at: UTCDateTime | None = None
    # NULL = auto-trigger и Telegram-алерты на эту ноду работают.
    # Timestamp = оператор замьютил (legacy combined-флаг, до migration 0039).
    auto_diagnose_disabled_at: UTCDateTime | None = None
    # Diagnostics overhaul (migration 0039) — два независимых тумблера +
    # per-incident state для admin UI (две разные кнопки в строке ноды).
    diagnostics_disabled_at: UTCDateTime | None = None
    alerts_muted_until: UTCDateTime | None = None
    diagnose_incident_open_at: UTCDateTime | None = None
    diagnose_follow_mode: str | None = None
    diagnose_acked_at: UTCDateTime | None = None
    last_diagnosed_at: UTCDateTime | None = None
    last_probe_at: UTCDateTime | None = None
    last_probe_status: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("blocked_regions", mode="before")
    @classmethod
    def _coerce_blocked_regions(cls, v: Any) -> list[str]:
        return v if v is not None else []

    class Config:
        from_attributes = True


class SubscriptionCreate(BaseModel):
    telegram_id: str
    plan_id: int
    email: Optional[str] = None
    node_id: int | None = None
    device_name: str | None = Field(default=None, description="Human readable device label")


class PlanOut(BaseModel):
    id: int
    name: str
    duration_days: int
    max_devices: int
    price: float
    traffic_limit_mb: int | None = None
    is_visible: bool = True

    class Config:
        from_attributes = True

    @classmethod
    def from_orm(cls, obj):  # type: ignore[override]
        # price is Numeric(10,2) → Decimal; coerce explicitly so Pydantic
        # doesn't choke on the type mismatch.
        return cls(
            id=obj.id,
            name=obj.name,
            duration_days=obj.duration_days,
            max_devices=obj.max_devices,
            price=float(obj.price) if obj.price is not None else 0.0,
            traffic_limit_mb=obj.traffic_limit_mb,
            is_visible=bool(obj.is_visible) if obj.is_visible is not None else True,
        )


class PlanCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    duration_days: int = Field(..., gt=0)
    max_devices: int = Field(1, ge=1)
    price: float = Field(..., ge=0)
    traffic_limit_mb: int | None = Field(None, ge=0)
    is_visible: bool = True


class PlanUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    duration_days: int | None = Field(None, gt=0)
    max_devices: int | None = Field(None, ge=1)
    price: float | None = Field(None, ge=0)
    traffic_limit_mb: int | None = Field(None, ge=0)
    is_visible: bool | None = None


class SubscriptionOut(BaseModel):
    id: int
    plan_name: str
    plan_id: int = 0
    node: str
    # Exposed so the admin UI can exclude the current node from the
    # per-sub migrate dropdown and highlight it in node lists. Nullable
    # defensively — a sub without a node wouldn't round-trip anyway,
    # but the existing schema never promised non-null and some legacy
    # data paths may still hit this code before node_id is set.
    node_id: int | None = None
    region: str
    expires_at: datetime
    status: str
    auto_renew: bool = False
    sub_token: str | None = None
    credentials: List[CredentialOut]
    devices: List[DeviceOut] = []
    # True iff at least one of this subscription's live device emails
    # has a more recent `sharing_block` audit than `sharing_unblock`.
    # Used by the admin UI to gate the "снять sharing-бан" button so
    # it only shows when there's actually something to unblock.
    sharing_blocked: bool = False
    # Current exit (pinned via Credential.exit_id) — first active
    # cred's exit_id. Lets the UI exclude it from the switch-exit
    # dropdown and show which exit the sub egresses through today.
    # NULL for legacy 1:1 relays and warm-pool bundles.
    current_exit_id: int | None = None
    # Имя exit-ноды для current_exit_id — рядом с node в админке,
    # чтобы было видно куда юзер выходит после туннеля. NULL в тех же
    # случаях, что и current_exit_id (плюс если exit-нода удалена).
    current_exit_name: str | None = None


class SubscriptionMigrateIn(BaseModel):
    # Target node id. Admin override: skips pool/health/cooldown checks,
    # only validates is_active=True. Required — auto-selection is what
    # the mass migrate-off-node endpoint is for.
    target_node_id: int


class SubscriptionMigrateOut(BaseModel):
    subscription_id: int
    old_node_id: int
    old_node_name: str
    new_node_id: int
    new_node_name: str
    provisioning_task_id: int | None
    # Заполняется только авто-миграцией (/migrate-auto): добавили ли
    # старую ноду в бан-лист юзера. None для ручной /migrate.
    banned_old_node: bool | None = None


class NodeUserBanCreate(BaseModel):
    node_id: int
    reason: str | None = None


class NodeUserBanOut(BaseModel):
    id: int
    user_id: int
    node_id: int
    node_name: str | None = None
    reason: str | None = None
    created_by: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class SubscriptionSwitchExitIn(BaseModel):
    exit_id: int


class SubscriptionSwitchExitOut(BaseModel):
    subscription_id: int
    old_exit_id: int | None
    new_exit_id: int
    new_interface: str
    task_ids: list[int]


class DeviceMigrateIn(BaseModel):
    target_node_id: int


class DeviceMigrateOut(BaseModel):
    # old_device_id is the row revoked by the migrate (now status=disabled
    # but kept in DB for sub_token aliasing); device_id is the freshly
    # provisioned row on the target node.
    old_device_id: int
    device_id: int
    old_node_id: int
    old_node_name: str
    new_node_id: int
    new_node_name: str
    provisioning_task_id: int | None


class DeviceSwitchExitIn(BaseModel):
    exit_id: int


class DeviceSwitchExitOut(BaseModel):
    device_id: int
    old_exit_id: int | None
    new_exit_id: int
    new_interface: str
    task_ids: list[int]


class NodeBulkMigrateFailure(BaseModel):
    subscription_id: int
    error: str


class NodeBulkMigrateOut(BaseModel):
    from_node_id: int
    to_node_id: int
    considered_count: int
    migrated: list[int] = Field(default_factory=list)
    failed: list[NodeBulkMigrateFailure] = Field(default_factory=list)
    task_ids: list[int] = Field(default_factory=list)
    revoke_task_ids: list[int] = Field(default_factory=list)
    device_task_ids: list[int] = Field(default_factory=list)
    resync_task_ids: list[int] = Field(default_factory=list)


class NodeRefreshDestIn(BaseModel):
    sni: str | None = None


class NodeRefreshDestOut(BaseModel):
    node_id: int
    old_sni: str
    new_sni: str
    sub_count: int
    failed_subs: list[int] = Field(default_factory=list)
    task_ids: list[int] = Field(default_factory=list)


class TickStatusItem(BaseModel):
    """Снимок одного worker-tick'а.

    ``job_status`` — стандартный RQ enum или ``missing`` если Job
    вообще нет (никогда не запускался после чистого Redis) / ``unknown``
    если Redis отсутствует. ``overdue_by_seconds`` > 0 значит
    scheduler должен был уже запустить этот тик, но не запустил —
    сильный сигнал что RQScheduler-форк умер.
    """
    tick_id: str
    func_name: str
    interval_seconds: int
    job_status: str
    enqueued_at: UTCDateTime | None = None
    started_at: UTCDateTime | None = None
    ended_at: UTCDateTime | None = None
    scheduled_for: UTCDateTime | None = None
    overdue_by_seconds: int | None = None
    last_exc_type: str | None = None


class WorkerInfo(BaseModel):
    name: str
    state: str
    last_heartbeat: UTCDateTime | None = None
    current_job_id: str | None = None


class TicksStatusOut(BaseModel):
    queue_available: bool
    workers: list[WorkerInfo] = Field(default_factory=list)
    ticks: list[TickStatusItem] = Field(default_factory=list)


class WorkerRestartOut(BaseModel):
    """Кого удалось пнуть shutdown'ом через pubsub, кого — нет.

    Docker restart policy поднимет контейнер заново; воркер гарантированно
    доделает текущий job до выхода (warm shutdown RQ).
    """
    signalled: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)


class WorkerScaleRequest(BaseModel):
    """Желаемое число worker-реплик (1..20, как в scripts/workers.sh)."""
    replicas: int = Field(ge=1, le=20)


class WorkerScaleOut(BaseModel):
    """Результат скейла. ``status``: applied | failed | enqueued.

    Скейл реально делает worker по SSH на mgmt (у API-образа нет ssh/ключа),
    поэтому API энкьюит job и коротко ждёт результат. ``enqueued`` — job
    взяли, но за окно ожидания он не успел; счётчик воркеров подтянется
    в виджете сам.
    """
    replicas: int
    status: str
    detail: str | None = None


class ExitEvacuateOut(BaseModel):
    """Результат массового переезда подписок с exit A на exit B.

    В отличие от node-миграции, тут меняется только ``Credential.exit_id``
    и гоняется один relay_tunnel apply на каждый уникальный relay, где
    жили evacuated сабы. Сабы сами остаются на тех же relay-нодах.

    ``failed_relays`` содержит те relay'и, к которым не прикреплён
    target exit — их сабы не переехали, админ должен сначала прикрепить
    exit или выбрать другой target.
    """
    from_exit_id: int
    to_exit_id: int
    considered_count: int
    migrated: list[int] = Field(default_factory=list)
    failed: list[NodeBulkMigrateFailure] = Field(default_factory=list)
    task_ids: list[int] = Field(default_factory=list)
    failed_relays: list[int] = Field(default_factory=list)


class DisableRequest(BaseModel):
    reason: str | None = None


class SubscriptionStatusOut(BaseModel):
    plan_name: str
    server_name: str
    expires_at: datetime
    is_active: bool
    proto_configs: List[CredentialOut]


class SubscriptionProvisionResponse(BaseModel):
    subscription_id: int
    status: str
    expires_at: datetime
    node_id: int
    plan_id: int
    device: DeviceStatusOut
    provisioning_task_id: int


class SubscriptionTrafficUpdate(BaseModel):
    used_mb: int = Field(..., ge=0, description="Traffic to add to the subscription usage in MB")


class SubscriptionTrafficOut(BaseModel):
    subscription_id: int
    status: str
    traffic_used_mb: int
    traffic_limit_mb: int | None = None
    over_limit: bool = False
    revocation_task_ids: list[int] = Field(default_factory=list)


class PaymentCreate(BaseModel):
    subscription_id: int
    amount: float
    currency: str = "USD"
    status: str = "pending"
    provider: str = "manual"
    external_id: str | None = None


class InvoiceCreate(BaseModel):
    user_id: int | None = None
    telegram_id: str | None = None
    plan_id: int
    subscription_id: int | None = None
    amount: float | None = None
    currency: str = "RUB"
    action: str = "new_subscription"


class InvoiceMarkPaidRequest(BaseModel):
    payment_id: int | None = None


class InvoiceOut(BaseModel):
    id: int
    user_id: int
    plan_id: int
    subscription_id: int | None = None
    amount: float
    currency: str
    status: str
    action: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @classmethod
    def from_orm(cls, obj):  # type: ignore[override]
        # amount is Numeric(10,2) → Decimal; coerce so Pydantic v2 doesn't trip.
        return cls(
            id=obj.id,
            user_id=obj.user_id,
            plan_id=obj.plan_id,
            subscription_id=obj.subscription_id,
            amount=float(obj.amount) if obj.amount is not None else 0.0,
            currency=obj.currency,
            status=obj.status.value if hasattr(obj.status, "value") else obj.status,
            action=obj.action.value if hasattr(obj.action, "value") else obj.action,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )


class InvoiceListItem(BaseModel):
    id: int
    user_id: int
    user_telegram_id: str | None
    plan_id: int | None = None
    plan_name: str | None = None
    subscription_id: int | None = None
    amount: float
    currency: str
    status: str
    action: str
    kind: str = "subscription"
    created_at: datetime


class InvoicePaidOut(InvoiceListItem):
    credentials: list[CredentialOut]
    provisioning_task_id: int | None = None
    device_id: int | None = None


class InvoiceCheckoutRequest(BaseModel):
    provider: str | None = Field(default=None, description="Payment provider name; defaults to server default")
    return_url: str | None = None


class InvoiceCheckoutOut(BaseModel):
    invoice_id: int
    provider: str
    external_id: str
    pay_url: str
    amount: float
    currency: str


class HealthProbeIn(BaseModel):
    source_region: str = Field(..., description="Probe origin label, e.g. 'ru-mts', 'kz', 'eu'")
    result: str = Field(..., description="ok|timeout|refused|tls_fail|unknown")
    latency_ms: int | None = None
    source_kind: str | None = None
    details: dict[str, Any] | None = None


class NodeTrafficSample(BaseModel):
    access_username: str = Field(..., description="Device.access_username the counter belongs to")
    uplink_bytes: int = Field(..., ge=0)
    downlink_bytes: int = Field(..., ge=0)


class NodeTrafficReport(BaseModel):
    collected_at: datetime = Field(..., description="Node-side wallclock when counters were read")
    window_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Length of the accounting window the samples cover, for diagnostics only",
    )
    samples: List[NodeTrafficSample]


class NodeTrafficSubscriptionResult(BaseModel):
    subscription_id: int
    used_mb_delta: int
    used_mb_total: int
    over_limit: bool
    revocation_task_ids: list[int] = Field(default_factory=list)


class NodeTrafficIngestOut(BaseModel):
    node_id: int
    accepted_samples: int
    unknown_usernames: list[str] = Field(default_factory=list)
    subscriptions: list[NodeTrafficSubscriptionResult] = Field(default_factory=list)


class ProbeTargetEndpoint(BaseModel):
    # One port to check on a node. A node with both ShadowTLS and VLESS
    # Reality will yield two endpoints. ``kind`` tells the agent which
    # check routine to run: plain TCP connect, or TLS handshake (with
    # ``sni`` as the expected SNI).
    protocol: str
    port: int
    kind: str = Field(..., description="tcp|tls")
    sni: str | None = None


class ProbeTarget(BaseModel):
    node_id: int
    name: str
    region: str
    host: str
    endpoints: list[ProbeTargetEndpoint]


class ProbeTargetList(BaseModel):
    generated_at: datetime
    targets: list[ProbeTarget]


class NodeHealthOut(BaseModel):
    node_id: int
    health_score: int
    blocked_regions: list[str] = []
    overall_success_rate: float
    per_region: dict[str, float]
    migrated_subscriptions: list[int] = Field(default_factory=list)

    @field_validator("blocked_regions", mode="before")
    @classmethod
    def _coerce_blocked_regions(cls, v: Any) -> list[str]:
        return v if v is not None else []


class CloudProviderCreate(BaseModel):
    name: str
    kind: str
    api_token: str | None = None
    default_image: str | None = None
    default_region: str | None = None
    default_plan: str | None = None
    ssh_key_ids: list[str] | None = None
    is_active: bool = True


class CloudProviderOut(BaseModel):
    id: int
    name: str
    kind: str
    default_image: str | None
    default_region: str | None
    default_plan: str | None
    ssh_key_ids: list[str] | None
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class PoolAutoscaleConfig(BaseModel):
    autoscale_enabled: bool | None = None
    autoscale_provider_id: int | None = None
    autoscale_region: str | None = None
    autoscale_plan: str | None = None
    autoscale_image: str | None = None
    autoscale_high_watermark: float | None = None
    autoscale_max_nodes: int | None = None


class PoolAutoscaleOut(BaseModel):
    pool_id: int
    pool_name: str
    autoscale_enabled: bool
    autoscale_provider_id: int | None
    autoscale_region: str | None
    autoscale_plan: str | None
    autoscale_image: str | None
    autoscale_high_watermark: float | None
    autoscale_max_nodes: int | None


class PoolDecisionOut(BaseModel):
    pool_id: int
    pool_name: str
    utilization: float
    total_capacity: int
    active_subs: int
    node_count: int
    scaled_up: bool
    new_node_id: int | None = None
    reason: str | None = None


class NodeSpawnRequest(BaseModel):
    provider_id: int
    name: str
    region: str
    plan: str
    image: str | None = None
    ssh_key_ids: list[str] | None = None
    pool_id: int | None = None
    user_data: str | None = None
    notes: str | None = None


class NodeReinstallRequest(BaseModel):
    # OS template/image id для провайдера (4vps: ostempl). None → default_image.
    image: str | None = None


class ProviderOfferingsOut(BaseModel):
    # Наполнение admin-формы заказа. Списки сырые-нормализованные (id+name+…)
    # из driver.list_datacenters/list_plans/list_images.
    datacenters: list[dict] = []
    plans: list[dict] = []
    images: list[dict] = []


class ProvisioningTaskOut(BaseModel):
    id: int
    target_type: str
    target_id: int
    action: str
    status: str
    payload: dict[str, Any] | None
    result: dict[str, Any] | None = None
    error_message: str | None
    created_at: UTCDateTime
    started_at: UTCDateTime | None
    finished_at: UTCDateTime | None
    # Phase 1: выставлен → оператор запросил отмену. Если status ещё running —
    # UI показывает «отменяется…» (раннер SIGTERM'нет на ближайшем poll'е).
    cancel_requested_at: UTCDateTime | None = None
    # Best-effort lookup: for device/subscription tasks we resolve the
    # owning user's telegram_id so the admin Tasks table can show who
    # the job belongs to without a second round-trip. None for node
    # tasks and for orphan rows whose FK chain got nulled.
    telegram_id: str | None = None
    # Группировка задач из одного batch-attach (POST /exits/batch-attach).
    # NULL для одиночных task'ов. UI рендерит badge «batch N/M» когда есть.
    batch_id: uuid.UUID | None = None

    class Config:
        from_attributes = True


class BatchSummary(BaseModel):
    """Сводка по batch_id для drawer-sidebar в UI.

    ``total`` — всего task'ов в батче, ``status_counts`` — гистограмма
    по ProvisioningTaskStatus. ``tasks`` — полный список child task'ов
    (обычно 5-15 шт., возвращаем целиком без пагинации). Drawer на
    polling'е считает прогресс по ``status_counts``, индивидуальные
    retry/logs пользуется ``tasks``.
    """
    batch_id: uuid.UUID
    total: int
    status_counts: dict[str, int]
    tasks: list[ProvisioningTaskOut]


class ApiTokenCreate(BaseModel):
    name: str
    scopes: list[str]


class ApiTokenOut(BaseModel):
    id: int
    name: str
    scopes: list[str]
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None = None

    class Config:
        from_attributes = True


class ApiTokenCreatedOut(ApiTokenOut):
    # Plaintext token, shown only on creation. Store it somewhere safe.
    token: str


class StatsOut(BaseModel):
    users_total: int
    subscriptions_active: int
    subscriptions_total: int
    invoices_pending: int
    nodes_total: int
    nodes_active: int
    devices_active: int
    provisioning_tasks_pending: int
    provisioning_tasks_failed: int


class UserOut(BaseModel):
    id: int
    telegram_id: str | None = None
    email: str | None = None
    created_at: datetime
    subscription_count: int = 0
    balance_kopecks: int = 0
    banned_at: datetime | None = None

    class Config:
        from_attributes = True


class BanRequest(BaseModel):
    # Same shape as DisableRequest but kept separate because ban is a
    # user-level action (blocks bot updates) while disable cascades to
    # subscription revocation — don't confuse the two in audit logs.
    reason: str | None = None


class AuditLogOut(BaseModel):
    id: int
    actor: str
    actor_type: str
    action: str
    target_type: str
    target_id: int | None
    created_at: datetime
    extra: dict | None = None

    class Config:
        from_attributes = True


# ── Health-ping admin dashboard ──


class HealthPingTotals(BaseModel):
    requests: int
    responses: int
    ok: int
    bad: int
    bad_prompted: int
    bad_self_reported: int
    opt_outs: int
    response_rate: float  # 0..1, clamped


class HealthPingPerNode(BaseModel):
    node_id: int | None  # null для rows, где node_id в extra отсутствует/невалиден
    node_name: str | None
    requests: int
    ok: int
    bad: int
    bad_ratio: float  # 0..1


class HealthPingTimeseriesPoint(BaseModel):
    bucket_ts: UTCDateTime
    ok: int
    bad: int


class HealthPingSummaryOut(BaseModel):
    from_ts: UTCDateTime
    to_ts: UTCDateTime
    hours: int
    bucket: str  # "hour" | "day"
    totals: HealthPingTotals
    per_node: list[HealthPingPerNode]
    timeseries: list[HealthPingTimeseriesPoint]


class HealthPingRecentBadItem(BaseModel):
    created_at: datetime
    telegram_id: str | None
    user_id: int | None
    node_id: int | None
    node_name: str | None
    subscription_id: int | None
    plan_name: str | None
    source: str  # "prompted" | "self_reported"


class HealthPingRecentBadOut(BaseModel):
    items: list[HealthPingRecentBadItem]


class NodeHealthPingStatsOut(BaseModel):
    node_id: int
    hours: int
    requests: int
    ok: int
    bad: int
    bad_ratio: float  # 0..1
    last_bad_at: datetime | None


class WGExitNodeCreate(BaseModel):
    name: str
    region: str
    host: str
    ssh_port: int = 22
    wg_port: int = 51820
    wg_address_v4: str = "10.77.0.1/24"
    # Optional — if omitted, POST /exits/{id}/keygen can generate one later.
    wg_public_key: str | None = None
    wg_private_key: str | None = None
    provider_id: int | None = None
    provider_external_id: str | None = None
    provider_region: str | None = None
    is_active: bool = True
    notes: str | None = None


class WGExitNodePatch(BaseModel):
    name: str | None = None
    region: str | None = None
    host: str | None = None
    ssh_port: int | None = None
    wg_port: int | None = None
    wg_address_v4: str | None = None
    wg_public_key: str | None = None
    wg_private_key: str | None = None
    provider_id: int | None = None
    provider_external_id: str | None = None
    provider_region: str | None = None
    status: str | None = None
    is_active: bool | None = None
    notes: str | None = None


class ExitLinkHealthMini(BaseModel):
    """Компактный срез health-данных одного relay→exit линка.

    Встраивается в ``WGExitNodeOut.links``, чтобы таблица Exits могла
    отрисовать ряд цветных кружочков без отдельных запросов на
    ``/exits/{id}/links`` по каждой ноде. Цвета считаются на клиенте
    той же ``linkHealth()`` функцией, что и в раскрытой панели, — так
    порог ``15m/3m`` живёт в одном месте.
    """
    relay_node_id: int
    relay_node_name: str
    wg_interface_name: str
    last_handshake_at: UTCDateTime | None = None
    last_observed_at: UTCDateTime | None = None


class WGExitNodeOut(BaseModel):
    id: int
    name: str
    region: str
    host: str
    ssh_port: int
    wg_port: int
    wg_address_v4: str
    wg_public_key: str | None
    has_private_key: bool
    provider_id: int | None
    provider_external_id: str | None
    provider_region: str | None
    status: str
    is_active: bool
    notes: str | None
    peers_count: int = 0
    links: list[ExitLinkHealthMini] = []
    # Сумма active subscriptions по всем relay-нодам, прикреплённым к
    # этому exit'у. «Сколько юзеров реально ходит через этот exit».
    # Дефолт 0 — одно-нодовые ответы (create/patch) не считают.
    active_subs_total: int = 0
    # Diagnostics overhaul (migration 0039) — exits get their own probe +
    # the same toggles/incident state as nodes.
    last_probe_at: UTCDateTime | None = None
    last_probe_status: str | None = None
    diagnostics_disabled_at: UTCDateTime | None = None
    alerts_muted_until: UTCDateTime | None = None
    diagnose_incident_open_at: UTCDateTime | None = None
    diagnose_follow_mode: str | None = None
    diagnose_acked_at: UTCDateTime | None = None
    last_diagnosed_at: UTCDateTime | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WGExitKeygenOut(BaseModel):
    id: int
    wg_public_key: str


class RelayExitLinkCreate(BaseModel):
    relay_node_id: int
    # Optional — if omitted, the server picks the next free /32 in the
    # exit's subnet. Must be host-form CIDR like "10.77.0.5/32".
    wg_client_address_v4: str | None = None


class BatchAttachRelayRequest(BaseModel):
    """Прицепить один relay сразу к N exit'ам одним POST'ом.

    Каждой паре (relay, exit) выделяется свой keypair, свой /32 в
    подсети exit'а и свой ``wgN`` interface на relay'е. WG-клиент-адрес
    нельзя задать руками — на batch'е это бессмыслено, пусть выделяет
    автоматом.
    """
    relay_node_id: int
    exit_ids: list[int]


class BatchAttachLinkOut(BaseModel):
    """Один link + порождённая task внутри batch-ответа.

    ``mode='attached'`` — link создан с нуля (INSERT + new keypair + /32).
    ``mode='reapplied'`` — link уже существовал, бэк не INSERT'ил, просто
    запустил relay_tunnel apply task на нём (привести wgN.conf к
    желаемому состоянию).
    """
    exit_id: int
    exit_name: str
    link_id: int
    task_id: int
    wg_interface_name: str
    wg_client_address_v4: str
    mode: str  # "attached" | "reapplied"


class BatchAttachRelayResponse(BaseModel):
    """Ответ batch-attach: общий batch_id + список созданных пар.

    Все валидируется ДО транзакции — частичных attach'ей не бывает.
    Если хоть один exit_id невалид/duplicate/inactive — endpoint
    возвращает 400/404/409 c деталями и ничего не пишет в БД. Сами
    же task'и независимы и retry'ятся per-task в UI через batch_id.
    """
    batch_id: uuid.UUID
    relay_node_id: int
    relay_node_name: str
    links: list[BatchAttachLinkOut]


class BatchDetachRelayRequest(BaseModel):
    """Отцепить несколько relay-нод от ОДНОГО exit'а одним POST'ом.

    Обратная операция к batch-attach (там один relay → N exit'ов, тут
    один exit → N relay'ев). relay_node_id, у которого нет линка к этому
    exit'у, попадает в ``not_found`` ответа — батч best-effort и не падает
    целиком из-за одной устаревшей строки в выборке UI.
    """
    relay_node_ids: list[int]


class BatchDetachLinkOut(BaseModel):
    """Один отцепленный relay + порождённая teardown-task внутри batch.

    ``task_id`` = ``None`` только если relay-строка уже исчезла (редко —
    FK cascade удалил link первым). ``credentials`` — summary миграции
    осиротевших creds: ``{"migrated": N, "distribution": {...}}`` либо
    ``{"cleared": N}`` (последний линк relay'я ушёл → exit_id обнулён).
    """
    relay_node_id: int
    relay_node_name: str
    link_id: int
    task_id: int | None
    credentials: dict[str, Any]


class BatchDetachRelayResponse(BaseModel):
    """Ответ batch-detach: общий batch_id + отцепленные + ненайденные.

    Все task'и идут под одним ``batch_id`` — UI рендерит прогресс тем же
    drawer'ом, что и batch-attach, retry отдельных через /tasks.
    ``not_found`` — relay_node_id'ы без линка к этому exit'у (не ошибка).
    """
    batch_id: uuid.UUID
    exit_id: int
    exit_name: str
    links: list[BatchDetachLinkOut]
    not_found: list[int] = []


class RelayExitLinkOut(BaseModel):
    id: int
    relay_node_id: int
    relay_node_name: str
    exit_id: int
    exit_name: str
    # Kernel interface the relay uses for this link (wg0, wg1, …).
    # G.5+ every link carries one; older rows were backfilled by the
    # same migration. Surfaced so the admin UI can label which wgN is
    # which exit in the multi-link case.
    wg_interface_name: str
    wg_client_public_key: str
    wg_client_address_v4: str
    created_at: datetime
    # Health telemetry — заполняется worker-тиком
    # run_relay_link_health_tick (см. services/relay_link_health.py).
    # NULL = тик ещё не прошёл / SSH не дошёл / peer не найден в wg.
    # Используем UTCDateTime — без хвостового ``Z`` браузер парсил бы
    # строку как local time и админский светофор всегда зажигал бы
    # красный "ssh N m" = TZ_offset пользователя.
    last_handshake_at: UTCDateTime | None = None
    last_rx_bytes: int | None = None
    last_tx_bytes: int | None = None
    last_observed_at: UTCDateTime | None = None
    # Количество active subscriptions, привязанных к relay-ноде этого
    # линка. Отвечает на вопрос «сколько юзеров реально ходит через этот
    # relay (и, косвенно, через этот exit)». Дефолт 0 — вычисляется в
    # ``list_exit_links`` и ``list_exits`` одним GROUP BY.
    active_subs: int = 0

    class Config:
        from_attributes = True


class NodeRelayLinkOut(BaseModel):
    """Per-link view from the *relay* side.

    Same data as ``RelayExitLinkOut`` reshaped for the Nodes admin
    screen (where the relay is the anchor) plus a live credentials
    counter so the operator can see how many active users are pinned
    to each exit via this link.

    ``last_auto_diagnose_*`` triplet exposes the most recent auto-trigger
    by `_auto_diagnose_stale_links` (worker_relay_link_health_tick).
    Frontend renders a small badge "автодиагностика N мин назад" with
    a click-through to the task's structured `checks` block.
    """
    link_id: int
    exit_id: int
    exit_name: str
    wg_interface_name: str
    wg_client_address_v4: str
    wg_client_public_key: str
    credentials_count: int
    created_at: datetime
    last_auto_diagnose_at: datetime | None = None
    last_auto_diagnose_task_id: int | None = None
    last_auto_diagnose_symptom: str | None = None
