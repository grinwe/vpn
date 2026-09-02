import enum

from .time_utils import utcnow
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import relationship
from .db import Base


class SubscriptionStatus(str, enum.Enum):
    active = "active"
    blocked = "blocked"
    expired = "expired"
    # Stage 4: user-initiated pause. No charges, devices physically
    # revoked from the node so the slot is freed for others. One freeze
    # of FREEZE_DAYS per calendar year (tracked via has_frozen_this_year).
    frozen = "frozen"


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"
    failed = "failed"
    refunded = "refunded"


class InvoiceStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"
    failed = "failed"


class InvoiceAction(str, enum.Enum):
    new_subscription = "new_subscription"
    renewal = "renewal"


class VPNNodeStatus(str, enum.Enum):
    registering = "registering"
    active = "active"
    disabled = "disabled"
    error = "error"
    # Stage 5 — downscale: node is being decommissioned. Excluded from
    # ``choose_node`` (no new subs land here) and from autoscale's
    # eligibility/utilization math, but its existing subscriptions keep
    # working until the drain tick migrates them off. Once the live-sub
    # count hits zero AND the grace window has elapsed, the drain tick
    # calls ``destroy_node`` which flips the row to ``disabled``.
    draining = "draining"


class WGExitNodeStatus(str, enum.Enum):
    # Relay-era exit node lifecycle. Separate from VPNNodeStatus because
    # an exit has no VLESS configs / warm pool / choose_node participation
    # — it's just a WG server that relays hand-traffic to.
    registering = "registering"
    active = "active"
    error = "error"
    disabled = "disabled"


class VPNConfigProtocol(str, enum.Enum):
    shadowtls_ss = "shadowtls+shadowsocks"
    vless_reality = "vless-reality"
    vless_ws_cdn = "vless-ws-cdn"
    hysteria2 = "hysteria2"
    vless_xhttp = "vless-xhttp"


class DeviceStatus(str, enum.Enum):
    pending = "pending"
    failed = "failed"
    active = "active"
    disabled = "disabled"
    revoked = "revoked"


class ProvisioningTaskStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"  # Phase 1: оператор отменил (до старта или SIGTERM)


class AuditActor(str, enum.Enum):
    user = "user"
    admin = "admin"
    system = "system"


class ProbeResult(str, enum.Enum):
    ok = "ok"
    timeout = "timeout"
    refused = "refused"
    tls_fail = "tls_fail"
    unknown = "unknown"


class CloudProviderKind(str, enum.Enum):
    hetzner = "hetzner"
    vultr = "vultr"
    digitalocean = "digitalocean"
    aeza = "aeza"
    # 4vps.su (он же 4vds) — RU-хостер. Python-имя fourvps (нельзя начинать с
    # цифры), wire-значение "4vps" (get_driver/frontend/CloudProviderOut). Кредсы:
    # api_token_enc хранит "panel_id:apikey".
    # NB: SQLAlchemy Enum хранит в PG ИМЯ члена ("fourvps"), а не value — так же
    # как vless_reality/vless_ws_cdn хранятся именами, не "vless-reality". Поэтому
    # PG-enum cloudproviderkind должен содержать 'fourvps' (см. миграция 0046),
    # а не '4vps'.
    fourvps = "4vps"
    # VDSina — RU/EU-хостер. Имя == value == "vdsina" (нет рассинхрона как у 4vps),
    # PG-enum label "vdsina" (миграция 0048). Custom REST API, инжектит ssh-ключ →
    # как hetzner, без парольного bootstrap. ``vdsina`` = .com-инсталляция
    # (userapi.vdsina.com); ``vdsina_ru`` ниже = .ru (другой токен/баланс/домен).
    vdsina = "vdsina"
    # Отдельная .ru-инсталляция VDSina — тот же драйвер, base → userapi.vdsina.ru
    # (см. get_driver). Токен .ru-аккаунта на .com даёт 401 и наоборот. Миграция 0051.
    vdsina_ru = "vdsina_ru"
    # ISPsystem BILLmanager — ОДИН generic-драйвер на пачку RU-хостеров (DataCheap/
    # UFO/AdminVPS), конкретный хост+креды в api_token_enc как JSON
    # {base_url,username,password}. Имя==value=="billmgr" (см. миграция 0049).
    # No-key + root-password (как 4vps).
    billmgr = "billmgr"
    manual = "manual"


class CredentialPoolState(str, enum.Enum):
    """Lifecycle state for the warm-credential pool (stage 2.5).

    - ``warm``: provisioned on the node, no subscription bound, ready for
      atomic assignment on purchase.
    - ``assigned``: bound to a Subscription, in active use.
    - ``revoked``: unassigned, scheduled for physical removal from the
      node by the warm-pool worker. Two-stage revoke means the API
      doesn't block on Ansible.
    """
    warm = "warm"
    assigned = "assigned"
    revoked = "revoked"


class BalanceTxKind(str, enum.Enum):
    """Direction of a BalanceTransaction (stage 4).

    Positive for top-ups (from a payment), negative for spend (daily
    billing tick or one-shot adjustments).
    """
    topup = "topup"
    spend = "spend"
    refund = "refund"
    bonus = "bonus"
    adjust = "adjust"


plan_serverpool = Table(
    "plan_serverpool",
    Base.metadata,
    Column("plan_id", Integer, ForeignKey("plans.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "server_pool_id",
        Integer,
        ForeignKey("server_pools.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class ServerPool(Base):
    __tablename__ = "server_pools"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(Text)

    autoscale_enabled = Column(Boolean, default=False)
    autoscale_provider_id = Column(Integer, ForeignKey("cloud_providers.id"), nullable=True)
    autoscale_region = Column(String, nullable=True)
    autoscale_plan = Column(String, nullable=True)
    autoscale_image = Column(String, nullable=True)
    autoscale_high_watermark = Column(Numeric(4, 3), nullable=True)
    autoscale_max_nodes = Column(Integer, nullable=True)
    # Stage 5 — downscale knobs. ЗАРЕЗЕРВИРОВАНО, ПОКА НЕ ИСПОЛЬЗУЕТСЯ (audit
    # #137): колонки заведены миграцией 0010 под будущий авто-downscale, но
    # ни autoscale-тик, ни provisioning их не читают, и ни одна схема
    # (PoolAutoscaleConfig/Out) их не отдаёт. Даунскейл сегодня — только
    # ручной перевод ноды в status=draining. Задумка (когда фичу доведут):
    # low_watermark — симметричный порог к high_watermark (при
    # ``active/capacity < low_watermark`` и числе eligible-нод выше
    # ``min_nodes`` drain-тик снимает subs с самой молодой авто-ноды);
    # min_nodes — нижняя граница, ниже которой пул не сжимается. НЕ выставляй
    # эти значения прямым UPDATE в расчёте на автодаунскейл — эффекта не будет.
    autoscale_low_watermark = Column(Numeric(4, 3), nullable=True)
    autoscale_min_nodes = Column(Integer, nullable=True)
    # Stage 6 — multi-cloud fallback chain. When the primary
    # autoscale_provider raises NodeSpawnError (Hetzner abuse-locked,
    # quota hit, region out of stock, ...), the autoscaler walks this
    # list in order before going into per-pool spawn backoff. Each entry
    # is a CloudProvider.id; missing/inactive rows are skipped silently.
    autoscale_fallback_provider_ids = Column(JSONB, nullable=True)

    nodes = relationship("VPNNode", back_populates="pool")
    plans = relationship("Plan", secondary=plan_serverpool, back_populates="server_pools")


class VPNNode(Base):
    __tablename__ = "vpn_nodes"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    region = Column(String, nullable=False)
    host = Column(String, nullable=False)
    ssh_port = Column(Integer, default=22)
    # NOT NULL + server_default (audit #131): статус/флаг нельзя оставлять
    # NULL — NULL-строка молча выпадает из filter(is_active == True) и
    # filter(status == active), «нода исчезает» из выборок без ошибки.
    status = Column(
        Enum(VPNNodeStatus),
        nullable=False,
        server_default=VPNNodeStatus.registering.value,
        default=VPNNodeStatus.registering,
    )
    is_active = Column(
        Boolean, nullable=False, server_default="true", default=True
    )
    pool_id = Column(Integer, ForeignKey("server_pools.id"))
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    notes = Column(Text)

    max_users = Column(Integer, nullable=True)
    max_bandwidth_mbps = Column(Integer, nullable=True)
    health_score = Column(Integer, nullable=True)
    last_health_check_at = Column(DateTime, nullable=True)
    # Phase 2+3 reconciler (RECONCILER_ENABLED, миграция 0043): правка бампает
    # desired_generation + ставит reconcile_due_at (debounce); reconcile-тик
    # сходит ноды, где due наступил и desired > reconciled, ОДНИМ bootstrap'ом;
    # на успехе reconciled = desired@start. desired бампнулся во время прогона
    # → supersession (тик прогонит снова). См. reconciler_epic.md.
    desired_generation = Column(
        Integer, nullable=False, server_default="0", default=0
    )
    reconciled_generation = Column(
        Integer, nullable=False, server_default="0", default=0
    )
    reconcile_due_at = Column(DateTime, nullable=True)
    blocked_regions = Column(JSONB, nullable=True)
    cooldown_until = Column(DateTime, nullable=True)
    # Phase D traffic-drop detector: set when active_users drops from
    # ≥MIN to 0 between two traffic_stats ticks. Cleared on next tick
    # after confirmation (→ error) or false-alarm (→ None).
    suspect_since = Column(DateTime, nullable=True)
    # Relay config: when set, this node is a jump node that tunnels
    # traffic through a WireGuard tunnel to a foreign exit node.
    # Keys: wg_private_key, wg_address_v4, wg_endpoint, wg_exit_public_key
    relay_config = Column(JSONB, nullable=True)

    provider_id = Column(Integer, ForeignKey("cloud_providers.id"), nullable=True)
    provider_external_id = Column(String, nullable=True)
    provider_region = Column(String, nullable=True)
    provider_plan = Column(String, nullable=True)
    monthly_cost = Column(Numeric(10, 2), nullable=True)
    # Рут-пароль, выданный хостером при заказе (Fernet). Только для провайдеров
    # без инъекции SSH-ключа (4vps): нужен для first-connect SSH перед
    # установкой нашего ключа. NULL у key-based провайдеров. См. node_spawner.
    provider_root_password_enc = Column(Text, nullable=True)

    # Mute-флаг для smart-диагностики и Telegram-алертов на эту ноду.
    # NULL = всё работает (default). Timestamp = оператор выключил
    # auto-trigger И заглушил admin_notify (notify_admins фильтрует
    # failed_relay_names против muted nodes перед отправкой).
    # Управление: POST /nodes/{id}/auto-diagnose/{disable|enable}.
    # Покрывает оба уровня — relay→exit linkи у этой ноды + сам ноду.
    auto_diagnose_disabled_at = Column(DateTime, nullable=True)

    # ── Diagnostics overhaul (migration 0039) ──────────────────────────
    # ДВА независимых тумблера (оператор попросил разделить):
    #   diagnostics_disabled_at — hard-стоп ВСЕХ диаг-тасок (авто+ручные),
    #     гейт в worker-триггерах, ручных эндпоинтах И оркестраторе;
    #   alerts_muted_until      — молчание admin-Telegram до TTL
    #     («замутить N часов» из пуша). NULL или прошлое = не muted.
    # ``auto_diagnose_disabled_at`` выше — legacy combined-флаг, выводится из
    # обихода; новый код читает две колонки ниже.
    diagnostics_disabled_at = Column(DateTime, nullable=True)
    alerts_muted_until = Column(DateTime, nullable=True)
    # Per-outage инцидент-стейт: упавшую ноду диагностируем ОДИН раз, а не
    # каждый тик. incident_open_at NULL = нет открытого инцидента (чистится
    # на recovery). follow_mode: NULL/'once' = one-and-done; 'exponential' =
    # оператор включил backoff 30m→2h→6h из пуша. acked_at = «вижу, работаю».
    diagnose_incident_open_at = Column(DateTime, nullable=True)
    last_diagnosed_at = Column(DateTime, nullable=True)
    diagnose_backoff_until = Column(DateTime, nullable=True)
    diagnose_follow_mode = Column(String, nullable=True)
    diagnose_acked_at = Column(DateTime, nullable=True)
    # Reachability-tick telemetry (ping/ssh from controller).
    last_probe_at = Column(DateTime, nullable=True)
    last_probe_status = Column(String, nullable=True)  # ok | unreachable
    # Начало текущей серии непрошедших probe'ов (reachability-tick). Алерт о
    # недоступности шлётся только если серия длится >= NODE_ALERT_CONFIRM_MIN
    # (анти-спам: единичные пропущенные пинги не будят админа). Чистится на
    # recovery. Своя колонка, НЕ suspect_since (та — у traffic-drop детектора).
    unreachable_since = Column(DateTime, nullable=True)
    # Версии софта на ноде — снимает tick-node-versions по SSH (см.
    # services/node_versions.py). До 2026-07-26 фактическая версия xray нигде не
    # сохранялась: её знал только bash-скрипт установки, и «на каких нодах уже
    # новое ядро» приходилось выяснять руками.
    xray_version = Column(String, nullable=True)
    # Версия НАШЕГО кода, которой прошита нода: site.yml пишет её в
    # /etc/vpn-node-release.json после успешного прогона всех ролей.
    release_version = Column(String, nullable=True)
    # Версия бинаря hysteria (демон hysteria-server — отдельный продукт, xray его
    # не обслуживает). До 2026-07-26 не пинилась и не собиралась вовсе: роль
    # ставила latest один раз и больше не трогала.
    hysteria_version = Column(String, nullable=True)
    versions_checked_at = Column(DateTime, nullable=True)

    pool = relationship("ServerPool", back_populates="nodes")
    configs = relationship("VPNConfig", back_populates="node", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="node")
    provider = relationship("CloudProvider", back_populates="nodes")
    probes = relationship("HealthProbe", back_populates="node", cascade="all, delete-orphan")
    traffic_samples = relationship(
        "NodeTrafficSample", back_populates="node", cascade="all, delete-orphan"
    )

    @property
    def has_relay_config(self) -> bool:
        """True iff this node is configured as a relay (tunnels to an exit).

        Surfaced via ``VPNNodeOut`` so the admin UI can tell whether a
        node already has an exit link and thus isn't attachable.
        """
        return self.relay_config is not None


class VPNConfig(Base):
    __tablename__ = "vpn_configs"

    id = Column(Integer, primary_key=True)
    node_id = Column(
        Integer,
        ForeignKey("vpn_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,  # FK-индекс (audit #129) — удаление ноды/выборки по node_id
    )
    name = Column(String, nullable=False)
    protocol = Column(Enum(VPNConfigProtocol), nullable=False)
    port = Column(Integer, nullable=False)
    sni = Column(String, nullable=True)
    public_key = Column(String, nullable=True)
    fallback = Column(String, nullable=True)
    settings = Column(JSONB, nullable=True)
    # NOT NULL + server_default (audit #131).
    is_enabled = Column(
        Boolean, nullable=False, server_default="true", default=True
    )
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    node = relationship("VPNNode", back_populates="configs")
    credentials = relationship("Credential", back_populates="config")
    devices = relationship("Device", back_populates="config")


class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    duration_days = Column(Integer, nullable=False)
    # NOT NULL + server_default (audit #131) — NULL в этих колонках рвал
    # ORM-выборки/PlanOut (защитный None-гард в schemas.py стоял именно из-за
    # NULL is_visible).
    max_devices = Column(
        Integer, nullable=False, server_default="1", default=1
    )
    price = Column(
        Numeric(10, 2), nullable=False, server_default="0", default=0
    )
    traffic_limit_mb = Column(Integer, nullable=True)
    is_visible = Column(
        Boolean, nullable=False, server_default="true", default=True
    )
    # Pay-as-you-go price per device per day, in kopecks (stage 4).
    # When NULL, daily_billing() falls back to ``price * 100 / duration_days``.
    # Set this explicitly when you want to decouple period pricing from
    # daily charge (e.g. an annual plan with a discounted daily rate).
    daily_rate_kopecks = Column(Integer, nullable=True)
    server_pools = relationship("ServerPool", secondary=plan_serverpool, back_populates="plans")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    referred_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Когда пользователь ВПЕРВЫЕ скачал конфиг по саб-ссылке — признак «человек
    # дошёл до рабочего VPN» (текущую активность с 2026-09 несёт
    # Device.last_seen_at, который штампует тик traffic_stats). Материализуем в
    # колонку, потому что вычислять на лету из audit_logs нельзя — их чистит
    # ретеншен (90 дней).
    first_config_fetch_at = Column(DateTime, nullable=True)
    # Рекламный источник (first-touch): метка из deep-link старт-параметра
    # ``t.me/bot?start=<tag>`` (не ``ref_``-префикс — те идут в referred_by_id).
    # Ставится ОДИН раз при первом /start с меткой. Воронка started→trial→paid
    # по этой колонке. См. docs/operations/ad_source_attribution.md.
    source = Column(String(64), nullable=True, index=True)
    # Pay-as-you-go balance (stage 4). Stored in kopecks (1₽ = 100 kop)
    # to dodge floating-point rounding on every daily-billing tick.
    # The legacy invoice/subscription model still works while
    # ``BILLING_MODEL=invoice``; this column is dormant until the
    # operator flips the flag to ``balance``.
    balance_kopecks = Column(Integer, nullable=False, server_default="0", default=0)
    # Free-trial bonus (stage "trial"). NULL ⇒ user hasn't claimed their
    # one-time trial yet — UI shows the banner, POST /api/trial/activate
    # gates on this being NULL. Set to now() on successful activation.
    trial_activated_at = Column(DateTime, nullable=True)
    # Set alongside trial_activated_at to activated_at + TRIAL_DURATION_DAYS.
    # Worker tick reads this to send the 3-day warning and, at expiry,
    # clawback the unspent trial bonus iff the user never made a real topup.
    # Cleared (set to NULL) after clawback so the tick doesn't revisit.
    trial_expires_at = Column(DateTime, nullable=True)
    # Per-user notification preferences. Each controls a group of
    # notification types that the worker/health-monitor emits.
    notify_renewals = Column(
        Boolean, nullable=False, server_default="true", default=True
    )
    notify_migrations = Column(
        Boolean, nullable=False, server_default="true", default=True
    )
    # Phase C — bot health-ping consent. The worker tick that queues
    # "помогите нам улучшить сервис" prompts skips users where this is
    # True. Flipped to True from a `hping:optout` callback handler.
    health_ping_opt_out = Column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # Per-user 24h debounce on the health-ping prompt. Updated each time
    # the worker queues a new ping for the user (write happens before
    # the bot delivers it, so a Telegram retry can't double-send).
    health_ping_last_at = Column(DateTime, nullable=True)
    # User-level ban. When set, the bot drops all incoming updates from
    # this user silently (no ACK — we don't want to give DDoS bots
    # feedback). Orthogonal to Subscription.status=blocked: banning a
    # user does NOT touch their subs, and blocking a sub doesn't set
    # this. Cleared to NULL on unban.
    banned_at = Column(DateTime, nullable=True)

    invoices = relationship("Invoice", back_populates="user")
    subscriptions = relationship("Subscription", back_populates="user")
    devices = relationship("Device", back_populates="user")
    referral_codes = relationship(
        "ReferralCode", back_populates="owner", foreign_keys="ReferralCode.owner_id"
    )
    balance_transactions = relationship(
        "BalanceTransaction", back_populates="user", cascade="all, delete-orphan"
    )


class NodeUserBan(Base):
    """Per-node бан юзера — список нод, на которые авто-выбор НЕ должен
    селить этого юзера.

    Ортогонально глобальному ``User.banned_at`` (тот — бан на уровне
    бота, дропает апдейты). Используется при «обновлении подписки»: при
    миграции на свободный сервер старая нода заносится сюда, и
    последующий auto-pick (``choose_node`` через ``exclude_node_ids``)
    её пропускает. Бан per-USER, а не per-subscription — у юзера может
    быть несколько подписок, бан ноды распространяется на все.
    """

    __tablename__ = "node_user_bans"
    __table_args__ = (
        UniqueConstraint("user_id", "node_id", name="uq_node_user_ban"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id = Column(
        Integer,
        ForeignKey("vpn_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reason = Column(Text, nullable=True)
    # Кто поставил бан: admin-actor / "auto" / telegram_id. Свободная
    # строка, как actor в AuditLog.
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    user = relationship("User")
    node = relationship("VPNNode")


class OperatorNodeReport(Base):
    """Краудсорс-сигнал «нода блочит оператора» из юзерского флоу
    «VPN не работает» (Phase 1 — operator-aware routing).

    Юзер тапает «не работает» → бэк авто-мигрирует (migrate-auto + бан
    старой ноды) и создаёт этот репорт (``outcome="pending"``). Сам тап —
    сильный **fail** для ``(failed_node, operator)``. Затем бот спрашивает
    оператора одним тапом → ``operator`` проставляется. Воркер через
    T_RECONNECT (15 мин) смотрит ``NodeTrafficSample.details["users"]``
    целевой ноды на ``target_access_username`` → ``outcome`` ok/
    inconclusive. Матрица node×operator агрегируется из этих репортов с
    recency-decay (24ч) и порогом K=5 разных device. **Advisory** —
    ``choose_node`` пока НЕ трогаем (см. operations roadmap).
    """

    __tablename__ = "operator_node_reports"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscription_id = Column(
        Integer,
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    device_id = Column(
        Integer,
        ForeignKey("devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Оператор/сеть из инлайн-выбора: mts/beeline/megafon/tele2/home_wifi/
    # other/unknown. NULL пока юзер не ответил (тап-репорт создаётся ДО
    # выбора оператора).
    operator = Column(String, nullable=True, index=True)
    # Нода, которую юзер пометил «не работает» — сильный fail-сигнал.
    failed_node_id = Column(
        Integer,
        ForeignKey("vpn_nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Нода, на которую авто-мигрировали — исход меряем по ней.
    target_node_id = Column(
        Integer,
        ForeignKey("vpn_nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # access_username нового девайса на target-ноде. Watcher ищет именно
    # его в NodeTrafficSample.details["users"] (после migrate имя другое —
    # суффикс против TTL-коллизий).
    target_access_username = Column(String, nullable=True)
    # pending → ok / fail / inconclusive (проставляет watcher; «всё равно
    # не работает» из бота → fail сразу).
    outcome = Column(
        String, nullable=False, server_default="pending", default="pending"
    )
    reported_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    resolved_at = Column(DateTime, nullable=True)

    user = relationship("User")
    subscription = relationship("Subscription")
    device = relationship("Device")
    failed_node = relationship("VPNNode", foreign_keys=[failed_node_id])
    target_node = relationship("VPNNode", foreign_keys=[target_node_id])


class Subscription(Base):
    __tablename__ = "subscriptions"
    # Композитный индекс под биллинг-/экспирацион-тики, которые фильтруют
    # по (status, expires_at)/next_charge_at (audit #129). Одиночные FK-
    # индексы — через index=True на колонках ниже.
    __table_args__ = (
        Index(
            "ix_subscriptions_status_expires_at", "status", "expires_at"
        ),
        # Partial-индекс под биллинг-тик due-charge (миграция 0009). Имя и
        # WHERE обязаны точно совпадать с миграцией, иначе alembic-drift.
        Index(
            "ix_subscriptions_due_charge",
            "next_charge_at",
            postgresql_where=text(
                "status = 'active' AND next_charge_at IS NOT NULL"
            ),
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    plan_id = Column(
        Integer, ForeignKey("plans.id"), nullable=False, index=True
    )
    # Nullable + SET NULL so deleting a VPNNode detaches historical
    # (terminated/expired) subs instead of hitting an IntegrityError.
    # Active/frozen subs are guarded at the /nodes/{id} DELETE endpoint.
    node_id = Column(
        Integer,
        ForeignKey("vpn_nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,  # FK-индекс под choose_node/подсчёт подписок на ноде (audit #129)
    )
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    expires_at = Column(DateTime, nullable=False)
    # NOT NULL + server_default (audit #131) — NULL-статус выпадал из фильтров.
    status = Column(
        Enum(SubscriptionStatus),
        nullable=False,
        server_default=SubscriptionStatus.active.value,
        default=SubscriptionStatus.active,
    )
    notes = Column(Text)
    traffic_limit_mb = Column(Integer, nullable=True)
    traffic_used_mb = Column(
        Integer, nullable=False, server_default="0", default=0
    )
    # Информационный счётчик для юзера (шкала в VPN-клиенте): сколько байт
    # ушло через VPN за ТЕКУЩИЙ оплаченный период. Наполняет тик
    # traffic_stats (per-user разбивка с нод), обнуляет каждое продление.
    # НИКАКОЙ блокировки на нём нет — в отличие от traffic_used_mb, чей
    # блокирующий ингест удалён (api/traffic.py, 2026-07-29). Байты, а не
    # мегабайты: дельты за 5-минутный тик бывают < 1 МБ, и округление
    # съедало бы трафик лёгких юзеров подчистую.
    traffic_used_bytes = Column(
        BigInteger, nullable=False, server_default="0", default=0
    )
    auto_renew = Column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # Stable token for the dynamic subscription link — survives migrations.
    sub_token = Column(String, unique=True, index=True, nullable=True)

    # ── Stage 4: balance billing anchor + freeze ────────────────────
    # Committed prepayment for this subscription's billing window. On
    # activate the full plan price is debited from User.balance_kopecks
    # and credited here; charge_subscription spends from this bucket,
    # not from the user's wallet. Manual revoke refunds the remainder
    # back to balance as kind=refund; expired subs forfeit whatever is
    # left (which should be ~0 by construction).
    prepaid_kopecks = Column(
        Integer, nullable=False, server_default="0", default=0
    )
    # Anchor for the next per-day spend tick. NULL = legacy invoice
    # subscription, the charge cron skips it. Set on activate, advanced
    # by +24h on every successful charge_subscription().
    next_charge_at = Column(DateTime, nullable=True)
    # When the user paused this sub. Cleared on unfreeze.
    frozen_at = Column(DateTime, nullable=True)
    # Hard upper bound on the current freeze window — auto-unfreeze cron
    # picks subs where frozen_until <= now.
    frozen_until = Column(DateTime, nullable=True)
    # Days consumed against this year's freeze budget. Reset when
    # frozen_year flips to a new calendar year.
    frozen_days_used = Column(
        Integer, nullable=False, server_default="0", default=0
    )
    frozen_year = Column(Integer, nullable=True)
    # V2: simple "1 freeze per year" flag. True = already used this year.
    has_frozen_this_year = Column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # Paid-for device slots *above* the plan's bundled ``max_devices``.
    # Bumped by ``webapp_add_device`` when the user buys an extra slot,
    # never decremented — removing the physical device leaves the slot
    # on the sub so the next renewal still bills for it. Reset to 0 by
    # ``balance.change_plan`` since the new plan has its own bundle.
    # Admin add-device does NOT touch this counter (operator override).
    extra_device_slots = Column(
        Integer, nullable=False, server_default="0", default=0
    )

    user = relationship("User", back_populates="subscriptions")
    plan = relationship("Plan")
    node = relationship("VPNNode", back_populates="subscriptions")
    credentials = relationship("Credential", back_populates="subscription")
    devices = relationship("Device", back_populates="subscription")
    payments = relationship("Payment", back_populates="subscription")


class Device(Base):
    __tablename__ = "devices"
    # Partial-индекс под скан жнеца свапов (миграция 0068). Имя и WHERE
    # обязаны точно совпадать с миграцией, иначе alembic-drift; дубль в
    # модели нужен, чтобы create_all на свежей БД тоже его создал.
    __table_args__ = (
        Index(
            "ix_devices_pending_swap_from",
            "pending_swap_from",
            postgresql_where=text("pending_swap_from IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # ON DELETE RESTRICT — часть sub-link инварианта (audit #130): Device-строки
    # НИКОГДА не удаляются (revoked-девайсы держат sub_token, /api/sub/{token}
    # алиасит на живого соседа; см. provisioning.py «DO NOT revert to
    # db.delete(device)»). CASCADE молча снёс бы revoked-девайсы вместе с их
    # sub_token'ами при удалении Subscription — RESTRICT заставляет БД охранять
    # инвариант. index=True — FK-индекс под выдачу sub-link (audit #129).
    subscription_id = Column(
        Integer,
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Nullable + ON DELETE SET NULL so deleting a node (which CASCADEs
    # into its vpn_configs) doesn't trip the FK on historical disabled
    # Device rows. Those rows survive on purpose — their sub_token keeps
    # /api/sub/{token} resolving to a live sibling. See migration 0030.
    config_id = Column(
        Integer,
        ForeignKey("vpn_configs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,  # FK-индекс (audit #129)
    )
    name = Column(String, nullable=False)
    # NOT NULL + server_default (audit #131).
    status = Column(
        Enum(DeviceStatus),
        nullable=False,
        server_default=DeviceStatus.pending.value,
        default=DeviceStatus.pending,
    )
    access_username = Column(String, nullable=True)
    connection_uri = Column(Text, nullable=True)
    # Per-device dynamic sub-link token — each device gets its own URL
    # so sharing a link exposes only one device's credentials.
    sub_token = Column(String, unique=True, index=True, nullable=True)
    # HMAC-SHA256(APP_SECRET_KEY, sub_token)[:12] base64-urlsafe без padding.
    # Идентификатор юзера в control-channel'е (Phase A roadmap'а): клиент
    # шлёт этот hash на /api/client/report-failure, backend O(1) lookup'ит
    # Device по индексу. sub_token не передаётся в plain. Подробнее —
    # docs/operations/control_channel_roadmap.md §4.3.
    client_id_hmac = Column(String(24), unique=True, index=True, nullable=True)
    # Журнал намерения failover-свапа (миграция 0068): id старого девайса,
    # который эта замена должна сменить. Ставится при создании замены,
    # снимается атомарно со свапом sub_token. Ненулевой маркер старше
    # порога = прерванный failover — его доделывает (или компенсирует)
    # жнец-тик run_device_swap_reaper_tick. NULL у всех обычных девайсов.
    pending_swap_from = Column(
        Integer,
        ForeignKey("devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    # Последний интервал, в котором креды девайса гнали байты через ноду.
    # Штампует тик traffic_stats (_touch_devices_last_seen) с точностью до
    # TRAFFIC_STATS_INTERVAL; NULL = активности не видели ни разу (либо
    # последняя была раньше окна бэкфилла миграции 0069 — 30 дней).
    # Кормит «активен за 24ч» в админке (список юзеров + воронка).
    last_seen_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="devices")
    subscription = relationship("Subscription", back_populates="devices")
    config = relationship("VPNConfig", back_populates="devices")
    credentials = relationship("Credential", back_populates="device")


class Credential(Base):
    __tablename__ = "credentials"
    # Partial-индекс под warm-pool SELECT (миграция 0008). Имя и WHERE
    # обязаны точно совпадать с миграцией, иначе alembic-drift.
    __table_args__ = (
        Index(
            "ix_credentials_warm_node",
            "node_id",
            postgresql_where=text("pool_state = 'warm'"),
        ),
    )

    id = Column(Integer, primary_key=True)
    # NULL for warm credentials waiting in the pool — bound on assignment.
    # index=True — FK-индексы под выдачу sub-link (credentials по sub/device)
    # и удаление ноды/конфига (audit #129).
    subscription_id = Column(
        Integer, ForeignKey("subscriptions.id"), nullable=True, index=True
    )
    device_id = Column(
        Integer, ForeignKey("devices.id"), nullable=True, index=True
    )
    config_id = Column(
        Integer, ForeignKey("vpn_configs.id"), nullable=True, index=True
    )
    # Denormalized for the warm-pool partial index. We could derive it
    # from config_id but the index can't traverse a join, and warm-pool
    # SELECT must be sub-millisecond.
    node_id = Column(Integer, ForeignKey("vpn_nodes.id"), nullable=True, index=True)
    # G.3+: pin this credential (and therefore its xray user UUID) to
    # one exit. Populated by provisioning in G.4 when the relay has
    # multiple RelayExitLinks; NULL means "use relay's default outbound"
    # (legacy 1:1 relays + warm-pool bundles before assignment).
    exit_id = Column(
        Integer,
        ForeignKey("wg_exit_nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    proto = Column(String, nullable=False)
    config_text = Column(Text, nullable=False)
    # Публикуется ли этот кред в подписке. Тёплый бандл назначается ЦЕЛИКОМ (на
    # ноде под одним именем физически лежат все её протоколы), а в саб-линк при
    # схеме 4×1 отдаём ровно один протокол с ноды — остальные висят
    # неопубликованными и ждут своей очереди при ротации. Отдельная колонка, а
    # НЕ is_active: is_active означает «учётка жива на ноде», её массово
    # переставляет провижининг, и схема публикации разваливалась бы молча.
    leg_published = Column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Роль лега в наборе: primary (reality) | fast (hy2) | backup (xhttp) |
    # reserve (ws-cdn) | dup (дубль, выданный по эскалации). NULL — легаси-кред
    # до перехода на схему.
    leg_role = Column(String, nullable=True)
    # Username shared across all credentials in the same warm bundle.
    # When pool_state=warm, the warmer groups credentials by
    # (node_id, access_username) to atomically assign all protocols of
    # one identity in a single transaction.
    access_username = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow)
    # NOT NULL + server_default (audit #131).
    is_active = Column(
        Boolean, nullable=False, server_default="true", default=True
    )
    revoked_at = Column(DateTime, nullable=True)
    # Stage 2.5 warm pool. ``warm`` = ready, ``assigned`` = bound to a sub,
    # ``revoked`` = pending physical removal. Defaults to ``assigned`` so
    # legacy rows (created before the column existed) keep their semantics.
    pool_state = Column(
        Enum(CredentialPoolState),
        nullable=False,
        server_default=CredentialPoolState.assigned.value,
        default=CredentialPoolState.assigned,
    )
    warmed_at = Column(DateTime, nullable=True)
    assigned_at = Column(DateTime, nullable=True)

    subscription = relationship("Subscription", back_populates="credentials")
    device = relationship("Device", back_populates="credentials")
    config = relationship("VPNConfig", back_populates="credentials")


class Payment(Base):
    __tablename__ = "payments"
    # #52 — composite unique so the same provider can't record the same
    # external payment twice. NULLs are excluded by Postgres (multiple
    # rows with external_id=NULL are allowed — manual payments, etc.).
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "external_id",
            name="uq_payments_provider_external_id",
        ),
    )

    id = Column(Integer, primary_key=True)
    subscription_id = Column(
        Integer, ForeignKey("subscriptions.id"), nullable=True, index=True
    )  # FK-индекс (audit #129)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True, index=True)
    # NOT NULL + server_default (audit #131).
    amount = Column(
        Numeric(10, 2), nullable=False, server_default="0", default=0
    )
    currency = Column(String, default="USD")
    status = Column(
        Enum(PaymentStatus),
        nullable=False,
        server_default=PaymentStatus.pending.value,
        default=PaymentStatus.pending,
    )
    provider = Column(String, default="manual")
    external_id = Column(String, nullable=True)
    # Ссылка на оплату у провайдера. Персистится ради идемпотентности:
    # «повторный тап → тот же pay_url» без второго похода к провайдеру —
    # раньше URL жил только в ответе чекаута, и каждый повтор плодил новый
    # счёт у провайдера (а lava зовёт это новым инвойсом в кабинете).
    pay_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    subscription = relationship("Subscription", back_populates="payments")
    invoice = relationship("Invoice", foreign_keys=[invoice_id])


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )  # FK-индекс (audit #129)
    # Nullable since stage 4 — topup invoices (kind='topup') don't bind
    # to a plan. Subscription invoices still set this on creation.
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=True)
    subscription_id = Column(
        Integer, ForeignKey("subscriptions.id"), nullable=True, index=True
    )  # FK-индекс (audit #129)
    amount = Column(Numeric(10, 2), default=0)
    currency = Column(String, default="USD")
    # NOT NULL + server_default (audit #131).
    status = Column(
        Enum(InvoiceStatus),
        nullable=False,
        server_default=InvoiceStatus.pending.value,
        default=InvoiceStatus.pending,
    )
    action = Column(Enum(InvoiceAction), default=InvoiceAction.new_subscription)
    # Stage 4 discriminator. ``subscription`` = legacy plan-purchase
    # invoice; on paid the orchestrator provisions/renews a subscription.
    # ``topup`` = balance topup; on paid we credit user.balance_kopecks
    # via services.balance.topup() and never touch provisioning.
    kind = Column(String, nullable=False, server_default="subscription", default="subscription")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    user = relationship("User", back_populates="invoices")
    plan = relationship("Plan")
    subscription = relationship("Subscription")


class ProvisioningTask(Base):
    __tablename__ = "provisioning_tasks"
    # Partial UNIQUE — инвариант ≤1 активный node-bootstrap на ноду
    # (миграция 0041). Имя и WHERE обязаны точно совпадать с миграцией.
    __table_args__ = (
        Index(
            "uq_active_node_bootstrap",
            "target_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'running') "
                "AND target_type = 'node' "
                "AND action = 'bootstrap'"
            ),
        ),
    )

    id = Column(Integer, primary_key=True)
    target_type = Column(String, nullable=False)
    target_id = Column(Integer, nullable=False)
    action = Column(String, nullable=False)
    status = Column(Enum(ProvisioningTaskStatus), default=ProvisioningTaskStatus.pending)
    payload = Column(JSONB, nullable=True)
    result = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    # NULL для одиночных task'ов, общий UUID для всех children одного
    # batch-attach (POST /admin/exits/batch-attach). UI группирует по
    # этому полю и считает прогресс N/M; orchestrator его не читает.
    batch_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    # Phase-0 coalescing (см. docs/operations/provisioning_reconciler_epic.md):
    # инвариант — ≤1 активный (pending|running) node-bootstrap на ноду
    # (partial unique index uq_active_node_bootstrap, миграция 0041). Если
    # во время активного прогона прилетают правки, мы не плодим вторую
    # таску, а ставим этот флаг на активной — worker на финише создаёт
    # ровно ОДИН свежий bootstrap.
    rerun_requested = Column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # Phase 1 (cancel): оператор попросил отмену. Worker проверяет ПЕРЕД
    # стартом (skip → status=cancelled) и поллит во время ansible-прогона
    # (→ SIGTERM процессу ansible-playbook). Миграция 0042.
    cancel_requested_at = Column(DateTime, nullable=True)


class CloudProvider(Base):
    __tablename__ = "cloud_providers"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    kind = Column(Enum(CloudProviderKind), nullable=False)
    api_token_enc = Column(Text, nullable=True)
    default_image = Column(String, nullable=True)
    ssh_key_ids = Column(JSONB, nullable=True)
    default_region = Column(String, nullable=True)
    default_plan = Column(String, nullable=True)
    # NOT NULL + server_default (audit #131).
    is_active = Column(
        Boolean, nullable=False, server_default="true", default=True
    )
    created_at = Column(DateTime, default=utcnow)

    nodes = relationship("VPNNode", back_populates="provider")


class WGExitNode(Base):
    """Foreign exit node in the relay architecture.

    A WireGuard server that RU relay nodes (``VPNNode`` rows with
    ``relay_config`` set) tunnel to. One exit holds many relays (N:M via
    ``relay_exit_links``). Not listed in ``vpn_nodes`` on purpose —
    exits have no VLESS configs, no warm credential pool, and are never
    passed to ``choose_node`` for subscription placement.
    """
    __tablename__ = "wg_exit_nodes"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    region = Column(String, nullable=False)
    host = Column(String, nullable=False)
    ssh_port = Column(Integer, default=22, nullable=False)

    wg_port = Column(Integer, default=51820, nullable=False)
    wg_address_v4 = Column(String, default="10.77.0.1/24", nullable=False)
    wg_public_key = Column(String, nullable=True)
    # Encrypted via ``security.encrypt`` — same Fernet scheme as cloud tokens.
    wg_private_key_enc = Column(Text, nullable=True)

    provider_id = Column(Integer, ForeignKey("cloud_providers.id"), nullable=True)
    provider_external_id = Column(String, nullable=True)
    provider_region = Column(String, nullable=True)
    # Рут-пароль от облачного провайдера без инъекции SSH-ключа (4vps) — для
    # bootstrap по паролю (worker кладёт provisioning-ключ перед bootstrap_exit).
    # Fernet. None если exit заведён вручную / провайдер инжектит ключ. Зеркалит
    # VPNNode.provider_root_password_enc.
    provider_root_password_enc = Column(Text, nullable=True)

    status = Column(Enum(WGExitNodeStatus), default=WGExitNodeStatus.registering, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    # ── Diagnostics overhaul (migration 0039) ──────────────────────────
    # Exits раньше не имели health-телеметрии вообще; теперь у них свой
    # reachability-пробинг (ping/port/ssh с контроллера). Те же два тумблера
    # и инцидент-стейт, что у VPNNode.
    last_probe_at = Column(DateTime, nullable=True)
    last_probe_status = Column(String, nullable=True)  # ok | unreachable | degraded
    diagnostics_disabled_at = Column(DateTime, nullable=True)
    alerts_muted_until = Column(DateTime, nullable=True)
    diagnose_incident_open_at = Column(DateTime, nullable=True)
    last_diagnosed_at = Column(DateTime, nullable=True)
    diagnose_backoff_until = Column(DateTime, nullable=True)
    diagnose_follow_mode = Column(String, nullable=True)
    diagnose_acked_at = Column(DateTime, nullable=True)
    # Начало серии непрошедших probe'ов — confirm-окно перед алертом (см.
    # VPNNode.unreachable_since). Reachability-tick гоняет nodes + exits общим
    # кодом, поэтому колонка нужна на обеих таблицах.
    unreachable_since = Column(DateTime, nullable=True)

    provider = relationship("CloudProvider")


class RelayExitLink(Base):
    """N:N link — a relay may tunnel to multiple exits (post-G.3).

    Separate from ``VPNNode.relay_config`` (which the worker reads as a
    flat dict) to keep the WG client keypair in a properly normalized
    table. Uniqueness is composite ``(relay_node_id, wg_interface_name)``
    so each tunnel gets its own kernel interface (``wg0``, ``wg1``, …).
    Legacy rows rolled forward on migration 0029 carry ``wg0``; the
    attach endpoint still enforces 1:1 at the app layer until G.4 lands
    the allocator.

    ``wg_client_private_key_enc`` is Fernet-encrypted (via
    ``security.encrypt``); the worker decrypts when building extra vars.
    """
    __tablename__ = "relay_exit_links"
    __table_args__ = (
        UniqueConstraint(
            "relay_node_id",
            "wg_interface_name",
            name="uq_relay_exit_links_relay_iface",
        ),
        # Защита от дублей (relay, exit) — до 0031 read-then-insert
        # guard в attach_relay мог пропустить параллельный запрос.
        UniqueConstraint(
            "relay_node_id",
            "exit_id",
            name="uq_relay_exit_links_relay_exit",
        ),
    )

    id = Column(Integer, primary_key=True)
    relay_node_id = Column(
        Integer,
        ForeignKey("vpn_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    exit_id = Column(
        Integer,
        ForeignKey("wg_exit_nodes.id", ondelete="RESTRICT"),
        nullable=False,
        # FK-индекс ix_relay_exit_links_exit_id (миграция 0027); имя даёт
        # дефолтная конвенция SQLAlchemy ix_<table>_<col>.
        index=True,
    )
    # Kernel interface name on the relay — each link gets its own
    # wg-quick@wgN unit so the relay_jump_node role can loop cleanly.
    # Defaults to "wg0" for legacy 1:1 rows; G.4 allocator hands out
    # wg1/wg2/... when a second exit is attached to the same relay.
    wg_interface_name = Column(
        String(16), nullable=False, server_default="wg0"
    )
    wg_client_private_key_enc = Column(Text, nullable=False)
    wg_client_public_key = Column(String, nullable=False)
    # Client address inside the tunnel subnet, e.g. "10.77.0.5/32".
    wg_client_address_v4 = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    # Health telemetry — заполняется тиком run_relay_link_health_tick,
    # который раз в 5 минут SSH'ит на relay и читает wg show all dump.
    # last_handshake_at = latest-handshake из dump (None если handshake
    # ни разу не было с момента старта интерфейса). last_observed_at —
    # время последнего успешного тика (NULL если SSH ни разу не вышел),
    # помогает отличить «данных ещё нет» от «relay недоступен давно».
    last_handshake_at = Column(DateTime, nullable=True)
    last_rx_bytes = Column(BigInteger, nullable=True)
    last_tx_bytes = Column(BigInteger, nullable=True)
    last_observed_at = Column(DateTime, nullable=True)

    relay_node = relationship("VPNNode")
    exit_node = relationship("WGExitNode")


class HealthProbe(Base):
    __tablename__ = "health_probes"

    id = Column(Integer, primary_key=True)
    node_id = Column(Integer, ForeignKey("vpn_nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    source_region = Column(String, nullable=False, index=True)
    source_kind = Column(String, nullable=True)
    result = Column(Enum(ProbeResult), nullable=False)
    latency_ms = Column(Integer, nullable=True)
    observed_at = Column(DateTime, default=utcnow, index=True)
    details = Column(JSONB, nullable=True)

    node = relationship("VPNNode", back_populates="probes")


class NodeTrafficSample(Base):
    """One periodic snapshot of a node's xray stats counters (Phase B).

    Written by ``services.traffic_stats.collect_node_stats`` once per
    worker tick (TRAFFIC_STATS_INTERVAL). uplink/downlink are *cumulative
    bytes since the previous reset* — the collector calls
    ``xray api statsquery --reset`` so each row is a delta over
    ``interval_seconds``, not an absolute counter that overflows.

    The detector that uses these rows lives in a follow-up; for the MVP
    we just collect the time-series so the next iteration has data to
    baseline against.
    """
    __tablename__ = "node_traffic_samples"
    # Композитный индекс из миграции 0019 (ix_node_traffic_samples_node_observed)
    # обслуживает запросы «последний сэмпл ноды» (order_by(observed_at.desc())).
    # Одиночный index=True на node_id убран: композит покрывает node_id как
    # префикс, а лишний одиночный индекс раньше вызывал дрейф модель↔схема
    # (миграция 0019 его не создавала). См. audit #134.
    __table_args__ = (
        Index(
            "ix_node_traffic_samples_node_observed", "node_id", "observed_at"
        ),
    )

    id = Column(Integer, primary_key=True)
    node_id = Column(
        Integer,
        ForeignKey("vpn_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    observed_at = Column(DateTime, default=utcnow, nullable=False)
    interval_seconds = Column(Integer, nullable=False, default=0, server_default="0")
    # BigInteger because xray byte counters routinely cross 2^31 (2.1 GB)
    # per interval on a busy node — Integer would silently overflow.
    uplink_bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    downlink_bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    active_users = Column(Integer, nullable=False, default=0, server_default="0")
    # Per-protocol breakdown:
    #   {"vless-reality": {
    #       "uplink": 123, "downlink": 456,
    #       "users": ["user-1-2", "user-4-7"],    # sorted access_username list
    #       "user_count": 2                        # == len(users)
    #   }, ...}
    # plus a "_errors" key listing protocols whose collection failed.
    # Legacy rows (pre-2026-04) have ``users`` as a bare int count —
    # readers must tolerate both formats (see api/nodes.py::list_node_users).
    details = Column(JSONB, nullable=True)

    node = relationship("VPNNode", back_populates="traffic_samples")


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    scopes = Column(ARRAY(String), nullable=False, default=list)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)


class SoftwareRelease(Base):
    """Кэш последнего upstream-релиза стороннего софта (пока — Xray-core).

    Тик ``tick-xray-upstream`` раз в несколько часов спрашивает GitHub и кладёт
    ответ сюда. Кэш нужен, чтобы админка показывала «последняя версия / у нас
    пин / на нодах» не дёргая GitHub на каждый рендер (и не упираясь в его
    rate-limit), а также чтобы отличить «релиза не было» от «мы не смогли
    сходить»: ``checked_at`` обновляется только при успешном ответе.
    """

    __tablename__ = "software_releases"

    id = Column(Integer, primary_key=True)
    # 'xray-core'; строкой, а не enum — добавление hysteria2/sing-box сюда не
    # должно требовать миграции типа.
    name = Column(String, nullable=False, unique=True, index=True)
    latest_version = Column(String, nullable=True)
    # Наш целевой пин на момент проверки. Пишет воркер: ansible-дерево есть
    # только в его образе (COPY infra), а API-контейнер роль прочитать не может
    # — без этой колонки сводка версий отдавала pinned=null.
    pinned_version = Column(String, nullable=True)
    published_at = Column(DateTime, nullable=True)
    html_url = Column(String, nullable=True)
    checked_at = Column(DateTime, nullable=True)
    # Текст последней ошибки похода к upstream — чтобы «не проверялось N часов»
    # можно было объяснить, не лазая в логи воркера.
    last_error = Column(String, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    # #127 — audit_logs это не только журнал, но и hot-path: поллер
    # уведомлений бота (GET /api/notifications/pending) фильтрует по
    # action + actor_type и сортирует по created_at DESC, worker-тики и
    # админ-дашборды фильтруют по action/created_at. Без индексов каждый
    # такой запрос — seq scan неограниченно растущей таблицы (broadcast
    # пишет строку на каждого получателя). DESC в индексе не нужен:
    # btree Postgres читается и в обратную сторону.
    __table_args__ = (
        Index("ix_audit_logs_action_created_at", "action", "created_at"),
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    actor = Column(String, nullable=False)
    actor_type = Column(Enum(AuditActor), default=AuditActor.system)
    action = Column(String, nullable=False)
    target_type = Column(String, nullable=False)
    target_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    extra = Column("metadata", JSONB, nullable=True)


class OpsPlan(Base):
    """Сохранённый dry-run ops-план (AI_AGENT_ROADMAP Phase 2 → фундамент Phase 3).

    Планировщик (``services/agent/ops.py::plan_ops``) пишет сюда КАЖДЫЙ
    построенный план целиком — шаги, params, оценку стоимости/влияния, хэш. Это:

    1. Полноценный аудит-след. Раньше в ``audit_logs`` лежал только счётчик шагов;
       по нему нельзя восстановить, ЧТО предлагалось (provider_id/node_id/count).
       Теперь план персистится дословно.
    2. Фундамент Phase 3 (исполнение «по одному подтверждению»). Confirm→execute
       ОБЯЗАН ссылаться на сохранённый план по ``id`` + ``content_hash``, чтобы
       исполнялось РОВНО то, что оператор видел и подтвердил — без params от
       клиента и без переплана LLM на момент confirm (TOCTOU). ``expires_at`` —
       якорь TTL: протухший план переисполнять нельзя.

    Пока НИЧЕГО не исполняется — это только запись. ``status`` — свободная строка
    (``proposed`` сейчас; ``expired|executed|cancelled`` — для Phase 3), чтобы не
    плодить миграции enum-типа.
    """

    __tablename__ = "ops_plans"

    id = Column(Integer, primary_key=True)
    actor = Column(String, nullable=False, index=True)  # X-Admin-Actor (TG-id)
    command = Column(Text, nullable=False)  # полная NL-команда (без обрезки [:500])
    model = Column(String, nullable=True)  # модель LLM
    plan = Column(JSONB, nullable=False)  # весь план целиком (шаги/params/оценка)
    content_hash = Column(String(64), nullable=False)  # sha256 каноничного плана
    feasible = Column(Boolean, nullable=True)
    needs_confirmation = Column(Boolean, nullable=True)
    status = Column(String, nullable=False, default="proposed", server_default="proposed")
    created_at = Column(DateTime, default=utcnow, index=True)
    expires_at = Column(DateTime, nullable=True)  # TTL-якорь для Phase 3 confirm
    # Результат исполнения (per-step статусы/созданные ноды/стоимость) — для
    # отчёта оператору; пишется исполнителем. None пока план не исполнялся.
    execution = Column(JSONB, nullable=True)


class ReferralCode(Base):
    """Referral invite code owned by a user.

    When a new user registers via a referral link containing this code,
    the invitee gets ``bonus_days`` added to their first subscription and
    the owner gets ``reward_days`` added to their active subscription.
    """
    __tablename__ = "referral_codes"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code = Column(String(32), unique=True, nullable=False, index=True)
    # Размеры подарков в днях подписки. До 2026-07-27 поля не читались вообще
    # (награда была фиксированной суммой), поэтому у старых кодов тут лежит
    # исторический дефолт 3 — миграция 0064 подтягивает их к текущим значениям.
    bonus_days = Column(Integer, default=3)
    reward_days = Column(Integer, default=10)
    uses = Column(Integer, default=0)
    max_uses = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)

    owner = relationship("User", back_populates="referral_codes", foreign_keys=[owner_id])


class AdLink(Base):
    """Управляемая рекламная ссылка (deep-link метка) для админки.

    Админ заводит именованную ссылку под рекламщика/размещение: ``name`` (ярлык) +
    ``tag`` (метка в ``t.me/bot?start=<tag>`` → пишется в ``User.source``).
    Статистика-воронка (started→trial→paid) считается по ``User.source == tag``.
    ``is_active=False`` = новые заходы по ссылке БОЛЬШЕ не атрибутируются
    (см. ``api_extensions.register_user``); историческая стата сохраняется.

    Это аналитический слой над source-меткой, БЕЗ бонусов — в отличие от
    [[ReferralCode]] (реферал человека с наградами).
    """

    __tablename__ = "ad_links"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)  # человекочитаемый ярлык
    tag = Column(String(64), unique=True, nullable=False, index=True)  # метка в start-param
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    # Во сколько обошлось размещение. Без этого поля воронка по метке отвечала
    # на «сколько пришло», но не на главный вопрос закупки — «окупилось ли»:
    # CAC = cost_kopecks / paid, ROI = revenue_kopecks / cost_kopecks.
    # Заполняет админ руками; NULL = бесплатное размещение (обмен, свой канал).
    cost_kopecks = Column(Integer, nullable=True)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class BalanceTransaction(Base):
    """Append-only ledger of balance changes (stage 4).

    Every top-up, daily-billing tick, refund, bonus and admin adjustment
    appends a row here. The current balance on ``User.balance_kopecks``
    must always equal ``SUM(amount_kopecks)`` over this user's rows —
    we trust the column for reads but reconcile from the ledger nightly
    in case anything got skewed.

    Amounts are signed: positive = balance went up, negative = went down.
    """

    __tablename__ = "balance_transactions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    amount_kopecks = Column(Integer, nullable=False)  # signed
    kind = Column(Enum(BalanceTxKind), nullable=False)
    # Free-form reference: invoice id, device id, "daily-billing-2025-12-01", etc.
    reference = Column(String, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    user = relationship("User", back_populates="balance_transactions")


class BroadcastStatus(str, enum.Enum):
    queued = "queued"
    sending = "sending"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"


class Broadcast(Base):
    """Админская рассылка сообщений юзерам через бота.

    Логика dispatch-а — в `run_broadcast_dispatch_tick`: тик берёт
    следующий `queued/sending` broadcast, режет юзеров по target_filter
    батчами по BROADCAST_BATCH_SIZE, пишет `AuditLog(admin_broadcast)`
    по одной строке на юзера. Bot-поллер подхватывает и шлёт в телегу
    с задержкой 0.05s (≤20 msg/sec, под Telegram API).

    target_filter shapes:
      * {"type": "all"}      — все юзеры с telegram_id
      * {"type": "active"}   — юзеры с активной подпиской
      * {"type": "ids", "ids": [1,2,3]}  — конкретные User.id

    Курсор `last_user_id_cursor` сохраняется между тиками — tick
    стартует с `User.id > cursor ORDER BY id ASC LIMIT batch`. На первом
    проходе считается и запоминается `total_recipients` для прогресс-бара.
    """

    __tablename__ = "broadcasts"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    created_by = Column(String(64), nullable=False)
    text = Column(Text, nullable=False)
    target_filter = Column(JSONB, nullable=False)
    status = Column(
        Enum(BroadcastStatus, name="broadcast_status"),
        default=BroadcastStatus.queued,
        nullable=False,
        index=True,
    )
    total_recipients = Column(Integer, nullable=True)
    sent_count = Column(Integer, default=0, nullable=False)
    failed_count = Column(Integer, default=0, nullable=False)
    last_user_id_cursor = Column(Integer, default=0, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    cancelled_reason = Column(String(255), nullable=True)
