import enum

from .time_utils import utcnow
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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
    # Stage 5 — downscale knobs. low_watermark is the symmetric counterpart
    # to high_watermark: when ``active/capacity < low_watermark`` AND we
    # still have more than ``min_nodes`` eligible nodes, the drain tick
    # picks the youngest auto-spawned node and starts moving its subs off.
    # ``min_nodes`` is the floor — never shrink below it, even at 0%
    # utilization, so the pool always has at least one warm node ready.
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
    status = Column(Enum(VPNNodeStatus), default=VPNNodeStatus.registering)
    is_active = Column(Boolean, default=True)
    pool_id = Column(Integer, ForeignKey("server_pools.id"))
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    notes = Column(Text)

    max_users = Column(Integer, nullable=True)
    max_bandwidth_mbps = Column(Integer, nullable=True)
    health_score = Column(Integer, nullable=True)
    last_health_check_at = Column(DateTime, nullable=True)
    blocked_regions = Column(JSONB, nullable=True)
    cooldown_until = Column(DateTime, nullable=True)
    # Relay config: when set, this node is a jump node that tunnels
    # traffic through a WireGuard tunnel to a foreign exit node.
    # Keys: wg_private_key, wg_address_v4, wg_address_v6,
    #        wg_endpoint, wg_exit_public_key
    relay_config = Column(JSONB, nullable=True)

    provider_id = Column(Integer, ForeignKey("cloud_providers.id"), nullable=True)
    provider_external_id = Column(String, nullable=True)
    provider_region = Column(String, nullable=True)
    provider_plan = Column(String, nullable=True)
    monthly_cost = Column(Numeric(10, 2), nullable=True)

    pool = relationship("ServerPool", back_populates="nodes")
    configs = relationship("VPNConfig", back_populates="node", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="node")
    provider = relationship("CloudProvider", back_populates="nodes")
    probes = relationship("HealthProbe", back_populates="node", cascade="all, delete-orphan")
    traffic_samples = relationship(
        "NodeTrafficSample", back_populates="node", cascade="all, delete-orphan"
    )


class VPNConfig(Base):
    __tablename__ = "vpn_configs"

    id = Column(Integer, primary_key=True)
    node_id = Column(Integer, ForeignKey("vpn_nodes.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    protocol = Column(Enum(VPNConfigProtocol), nullable=False)
    port = Column(Integer, nullable=False)
    sni = Column(String, nullable=True)
    public_key = Column(String, nullable=True)
    fallback = Column(String, nullable=True)
    settings = Column(JSONB, nullable=True)
    is_enabled = Column(Boolean, default=True)
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
    max_devices = Column(Integer, default=1)
    price = Column(Numeric(10, 2), default=0)
    traffic_limit_mb = Column(Integer, nullable=True)
    is_visible = Column(Boolean, default=True)
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

    invoices = relationship("Invoice", back_populates="user")
    devices = relationship("Device", back_populates="user")
    referral_codes = relationship(
        "ReferralCode", back_populates="owner", foreign_keys="ReferralCode.owner_id"
    )
    balance_transactions = relationship(
        "BalanceTransaction", back_populates="user", cascade="all, delete-orphan"
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    node_id = Column(Integer, ForeignKey("vpn_nodes.id"), nullable=False)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    expires_at = Column(DateTime, nullable=False)
    status = Column(Enum(SubscriptionStatus), default=SubscriptionStatus.active)
    notes = Column(Text)
    traffic_limit_mb = Column(Integer, nullable=True)
    traffic_used_mb = Column(Integer, default=0)
    auto_renew = Column(Boolean, default=False)
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

    user = relationship("User")
    plan = relationship("Plan")
    node = relationship("VPNNode", back_populates="subscriptions")
    credentials = relationship("Credential", back_populates="subscription")
    devices = relationship("Device", back_populates="subscription")
    payments = relationship("Payment", back_populates="subscription")


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False)
    config_id = Column(Integer, ForeignKey("vpn_configs.id"), nullable=False)
    name = Column(String, nullable=False)
    status = Column(Enum(DeviceStatus), default=DeviceStatus.pending)
    access_username = Column(String, nullable=True)
    connection_uri = Column(Text, nullable=True)
    # Per-device dynamic sub-link token — each device gets its own URL
    # so sharing a link exposes only one device's credentials.
    sub_token = Column(String, unique=True, index=True, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    last_seen_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="devices")
    subscription = relationship("Subscription", back_populates="devices")
    config = relationship("VPNConfig", back_populates="devices")
    credentials = relationship("Credential", back_populates="device")


class Credential(Base):
    __tablename__ = "credentials"

    id = Column(Integer, primary_key=True)
    # NULL for warm credentials waiting in the pool — bound on assignment.
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=True)
    config_id = Column(Integer, ForeignKey("vpn_configs.id"), nullable=True)
    # Denormalized for the warm-pool partial index. We could derive it
    # from config_id but the index can't traverse a join, and warm-pool
    # SELECT must be sub-millisecond.
    node_id = Column(Integer, ForeignKey("vpn_nodes.id"), nullable=True, index=True)
    proto = Column(String, nullable=False)
    config_text = Column(Text, nullable=False)
    # Username shared across all credentials in the same warm bundle.
    # When pool_state=warm, the warmer groups credentials by
    # (node_id, access_username) to atomically assign all protocols of
    # one identity in a single transaction.
    access_username = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow)
    is_active = Column(Boolean, default=True)
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
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True, index=True)
    amount = Column(Numeric(10, 2), default=0)
    currency = Column(String, default="USD")
    status = Column(Enum(PaymentStatus), default=PaymentStatus.pending)
    provider = Column(String, default="manual")
    external_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    subscription = relationship("Subscription", back_populates="payments")
    invoice = relationship("Invoice", foreign_keys=[invoice_id])


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # Nullable since stage 4 — topup invoices (kind='topup') don't bind
    # to a plan. Subscription invoices still set this on creation.
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)
    amount = Column(Numeric(10, 2), default=0)
    currency = Column(String, default="USD")
    status = Column(Enum(InvoiceStatus), default=InvoiceStatus.pending)
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
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)

    nodes = relationship("VPNNode", back_populates="provider")


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

    id = Column(Integer, primary_key=True)
    node_id = Column(
        Integer,
        ForeignKey("vpn_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    observed_at = Column(DateTime, default=utcnow, nullable=False)
    interval_seconds = Column(Integer, nullable=False, default=0, server_default="0")
    # BigInteger because xray byte counters routinely cross 2^31 (2.1 GB)
    # per interval on a busy node — Integer would silently overflow.
    uplink_bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    downlink_bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    active_users = Column(Integer, nullable=False, default=0, server_default="0")
    # Per-protocol breakdown:
    #   {"vless-reality": {"uplink": 123, "downlink": 456, "users": 7}, ...}
    # plus a "_errors" key listing protocols whose collection failed.
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


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    actor = Column(String, nullable=False)
    actor_type = Column(Enum(AuditActor), default=AuditActor.system)
    action = Column(String, nullable=False)
    target_type = Column(String, nullable=False)
    target_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    extra = Column("metadata", JSONB, nullable=True)


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
    bonus_days = Column(Integer, default=3)
    reward_days = Column(Integer, default=3)
    uses = Column(Integer, default=0)
    max_uses = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)

    owner = relationship("User", back_populates="referral_codes", foreign_keys=[owner_id])


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
