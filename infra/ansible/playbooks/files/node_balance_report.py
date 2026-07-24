# Диагностика балансировки нод: три распределения рядом (read-only, без записи).
#
#   primary  — метрика choose_node: девайсы (не revoked/disabled) АКТИВНЫХ
#              подписок по Subscription.node_id. Только это видит балансировщик.
#   assigned — метрика админ-колонки «Юзеры»: активные креды по
#              Credential.node_id (diverse-корректно) → девайсы и distinct-юзеры.
#   carrying — телеметрия: кого нода РЕАЛЬНО тащит по последнему traffic-сэмплу
#              (числитель/знаменатель carrying_fraction).
#
# Плюс ghost (девайсы НЕактивных подписок — невидимая для балансировщика масса)
# и вердикт «выбираема ли нода choose_node» с причинами исключения.
#
# Запуск: ansible-playbook playbooks/node_balance_report.yml (см. плейбук).
import os

from sqlalchemy import func

from app import models
from app.db import SessionLocal
from app.services.carrying import compute_carrying_fractions
from app.services.provisioning import MIN_HEALTHY_SCORE
from app.time_utils import utcnow

db = SessionLocal()
now = utcnow()

nodes = db.query(models.VPNNode).order_by(models.VPNNode.id).all()

LIVE_DEV = models.Device.status.notin_(
    [models.DeviceStatus.revoked, models.DeviceStatus.disabled]
)

# --- primary: как в choose_node (Subscription.node_id, активные подписки) ---
primary_rows = (
    db.query(models.Subscription.node_id, func.count(models.Device.id))
    .join(models.Device, models.Device.subscription_id == models.Subscription.id)
    .filter(models.Subscription.status == models.SubscriptionStatus.active, LIVE_DEV)
    .group_by(models.Subscription.node_id)
    .all()
)
primary = dict(primary_rows)

primary_subs = dict(
    db.query(models.Subscription.node_id, func.count(models.Subscription.id))
    .filter(models.Subscription.status == models.SubscriptionStatus.active)
    .group_by(models.Subscription.node_id)
    .all()
)

# --- ghost: девайсы НЕактивных подписок (балансировщик их не видит) ---
ghost = dict(
    db.query(models.Subscription.node_id, func.count(models.Device.id))
    .join(models.Device, models.Device.subscription_id == models.Subscription.id)
    .filter(models.Subscription.status != models.SubscriptionStatus.active, LIVE_DEV)
    .group_by(models.Subscription.node_id)
    .all()
)

# --- assigned: как админ-колонка «Юзеры» (Credential.node_id) ---
assigned_q = (
    db.query(
        models.Credential.node_id,
        func.count(func.distinct(models.Subscription.user_id)),
        func.count(func.distinct(models.Credential.device_id)),
    )
    .join(models.Device, models.Credential.device_id == models.Device.id)
    .join(models.Subscription, models.Device.subscription_id == models.Subscription.id)
    .filter(
        models.Credential.is_active.is_(True),
        models.Credential.device_id.isnot(None),
        models.Device.status == models.DeviceStatus.active,
        models.Subscription.status == models.SubscriptionStatus.active,
    )
    .group_by(models.Credential.node_id)
    .all()
)
assigned_users = {nid: u for nid, u, _d in assigned_q}
assigned_devs = {nid: d for nid, _u, d in assigned_q}

# --- carrying: телеметрия последнего сэмпла (may be stale) ---
carrying = {c["node_id"]: c for c in compute_carrying_fractions(db)}


def gates(n):
    """Почему нода НЕ участвует в choose_node (пусто = участвует)."""
    out = []
    if not n.is_active:
        out.append("inactive")
    if n.status != models.VPNNodeStatus.active:
        out.append(f"status={getattr(n.status, 'value', n.status)}")
    if n.health_score is not None and n.health_score < MIN_HEALTHY_SCORE:
        out.append(f"health={n.health_score}")
    if n.cooldown_until is not None and n.cooldown_until >= now:
        out.append("cooldown")
    if getattr(n, "auto_diagnose_disabled_at", None) is not None:
        out.append("diag_mute(auto)")
    if getattr(n, "diagnostics_disabled_at", None) is not None:
        out.append("diag_mute(hard)")
    if n.max_users is not None and primary.get(n.id, 0) >= n.max_users:
        out.append(f"full({primary.get(n.id, 0)}/{n.max_users})")
    return out


print(
    "DIVERSE_SUB_NODES=%s  CHOOSE_NODE_INCLUDE_REGISTERING=%s  MIN_HEALTHY_SCORE=%s"
    % (
        os.getenv("DIVERSE_SUB_NODES", "1"),
        os.getenv("CHOOSE_NODE_INCLUDE_REGISTERING", "0"),
        MIN_HEALTHY_SCORE,
    )
)
print()
hdr = "%-4s %-16s %-6s %-5s %7s %5s %5s %7s %7s %9s %5s  %s" % (
    "id", "name", "region", "pool", "primary", "subs", "ghost",
    "as.dev", "as.usr", "carry", "cap", "исключена: причины",
)
print(hdr)
print("-" * len(hdr))

eligible_primary = []
for n in sorted(nodes, key=lambda x: -primary.get(x.id, 0)):
    c = carrying.get(n.id) or {}
    frac = c.get("carrying_fraction")
    carry_s = "-"
    if c:
        carry_s = "%s/%s" % (c.get("carrying_devices", "?"), c.get("eligible_devices", "?"))
        if c.get("stale"):
            carry_s += "!"
    g = gates(n)
    if not g:
        eligible_primary.append(primary.get(n.id, 0))
    print(
        "%-4s %-16s %-6s %-5s %7d %5d %5d %7d %7d %9s %5s  %s"
        % (
            n.id,
            (n.name or "?")[:16],
            (n.region or "-")[:6],
            n.pool_id or "-",
            primary.get(n.id, 0),
            primary_subs.get(n.id, 0),
            ghost.get(n.id, 0),
            assigned_devs.get(n.id, 0),
            assigned_users.get(n.id, 0),
            carry_s + (" (%.2f)" % frac if frac is not None else ""),
            n.max_users if n.max_users is not None else "inf",
            ", ".join(g) if g else "",
        )
    )

print()
print(
    "итого: нод=%d, выбираемых choose_node=%d; primary по выбираемым: min=%s max=%s"
    % (
        len(nodes),
        len(eligible_primary),
        min(eligible_primary) if eligible_primary else "-",
        max(eligible_primary) if eligible_primary else "-",
    )
)
print(
    "легенда: primary=девайсы актив.подписок (метрика балансировщика); subs=актив.подписки;"
)
print(
    "  ghost=девайсы НЕактив.подписок (невидимы балансировщику); as.dev/as.usr=по кредам"
)
print(
    "  (метрика админ-колонки, diverse считается на каждой ноде); carry=тащит/из_скольких"
)
print("  по телеметрии (!=протухший сэмпл); cap=max_users (потолок в девайсах)")

# --- топ подписок по живым девайсам: ловит аномалии, раздувающие primary ---
top = (
    db.query(
        models.Subscription.id,
        models.Subscription.user_id,
        models.User.telegram_id,
        models.Plan.name,
        models.Subscription.node_id,
        func.count(models.Device.id).label("devs"),
    )
    .join(models.Device, models.Device.subscription_id == models.Subscription.id)
    .join(models.User, models.User.id == models.Subscription.user_id)
    .join(models.Plan, models.Plan.id == models.Subscription.plan_id)
    .filter(models.Subscription.status == models.SubscriptionStatus.active, LIVE_DEV)
    .group_by(
        models.Subscription.id,
        models.Subscription.user_id,
        models.User.telegram_id,
        models.Plan.name,
        models.Subscription.node_id,
    )
    .order_by(func.count(models.Device.id).desc())
    .limit(5)
    .all()
)
print()
print("топ-5 подписок по живым девайсам (sub_id, user_id, tg, plan, node_id, devs):")
for row in top:
    print("  %s" % (tuple(row),))

db.close()
