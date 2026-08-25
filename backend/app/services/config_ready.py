"""Пуш «конфиг готов» — единственный продюсер строки ``config_ready`` в очереди бота.

Зачем отдельный модуль. До инцидента 2026-08-25 (юзер 1000054: бот пообещал
«сейчас пришлю ссылку», ссылка не пришла, повторный тап дал «подарок уже
использован» и тупик) канал ``config_ready`` существовал только на стороне
потребителя: поллер бота умел его доставить, рендер в
``api_extensions.get_pending_notifications`` умел его собрать, а бэкенд НИКОГДА
его не писал — ``_notify_bot_config_ready`` клал ``_notify`` в
``ProvisioningTask.result``, который никто не читает. Отложенной доставки не
было ни для триала, ни для оплаты картой (lava-вебхук), хотя бот при
выставлении счёта обещает «конфиг придёт автоматически».

Теперь пуш — обычная строка ``AuditLog(action='config_ready')`` (тот же
механизм, что ``sublink_rotated`` / ``trial_expiry_warning``): поллер заберёт
её через ``/api/notifications/pending`` и после отправки переименует в
``config_ready:delivered``. Бот при этом жив или нет — не важно, строка ждёт.

Два источника, оба сходятся сюда:

* ``warm`` — warm-pool хит в ``provision_subscription``: девайс сразу active,
  Ansible не запускается, ``_handle_task_outcome`` не проходит. Коммитит
  вызывающий (``activate_trial_full`` / ``webapp_activate`` /
  ``_create_subscription_for_user`` делают ``db.commit`` после
  ``provision_subscription``), поэтому ``commit=False``.
* ``cold`` — успешный ``apply`` в ``_handle_task_outcome``, и только если
  таска несёт ``payload.notify_config_ready`` (его ставит ТОЛЬКО cold-ветка
  ``provision_subscription``). Failover / migrate / swap / unfreeze /
  add-device идут через ``reprovision_subscription`` и флага не получают —
  структурный гейт, а не эвристика.

Гейт внутри — пояс с подтяжками поверх структурного: пуш только на ПЕРВЫЙ
рабочий девайс СВЕЖЕЙ подписки, один раз. Любой сбой здесь — лог и
``False``: провижн уже случился, ронять его из-за уведомления нельзя.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow
from . import sub_links

logger = logging.getLogger(__name__)

ACTION = "config_ready"
# Подписка старше — это ретрай старой таски или разгребание бэклога воркера,
# а не «человек только что нажал кнопку». Пуш через сутки после активации
# выглядит как спам и путает (у юзера уже другая ссылка/другой тариф).
FRESH_WINDOW = timedelta(hours=24)

# Только числовой chat_id. В БД лежат и служебные ``telegram_id`` вроде
# 'legacy-user' / 'unknown' — бот на таком упадёт в sendMessage и строка
# зависнет в очереди навсегда, блокируя окно limit=20 для остальных.
_TG_ID_RE = re.compile(r"[0-9]+")


def _naive_utc(value: datetime) -> datetime:
    """Колонки у нас naive-UTC, но на всякий случай приводим и aware."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _absolute_sub_uri(token: str | None) -> str | None:
    """Ссылка в пуш — только абсолютная http(s).

    ``sub_links.sub_url_for`` без ``SUB_LINK_BASE_URL`` вернёт относительный
    ``/api/sub/<token>`` — в Telegram он не кликается и выглядит как мусор.
    Лучше без ссылки (текст отсылает в личный кабинет), чем битая.
    """
    if not token:
        return None
    url = sub_links.sub_url_for(token)
    if url.startswith(("https://", "http://")):
        return url
    return None


def _passes_gate(db: Session, device: models.Device) -> models.Subscription | None:
    """Все условия И; возвращает подписку, если пуш уместен, иначе None."""
    sub = device.subscription
    if sub is None:
        return None
    # (а) подписка живая. activate_trial_full на 402 (бонуса не хватило)
    # откатывает подписку в expired, но cold-таска к тому моменту уже в
    # очереди и позже успешно отработает; blocked — аналогично. «Конфиг
    # готов» по мёртвой подписке — прямая ложь юзеру.
    if sub.status != models.SubscriptionStatus.active:
        return None
    user = sub.user
    tg = user.telegram_id if user is not None else None
    if not (isinstance(tg, str) and _TG_ID_RE.fullmatch(tg)):
        return None

    # (б) девайс реально рабочий: active и хоть один живой кред. На cold-пути
    # креды активирует _handle_task_outcome ДО вызова, на warm — они живые
    # с момента assign; pending/failed сюда не доходят.
    if device.status != models.DeviceStatus.active:
        return None
    if not any(c.is_active for c in device.credentials or []):
        return None

    # (в) «первый рабочий девайс подписки». revoked/disabled строки никогда
    # не удаляются (саб-линк инвариант), поэтому любой предшественник —
    # failover, миграция, второй девайс — виден и блокирует пуш: у такого
    # юзера ссылка уже есть, «конфиг готов» его только запутает.
    predecessor = (
        db.query(models.Device.id)
        .filter(
            models.Device.subscription_id == sub.id,
            models.Device.id != device.id,
            models.Device.status.notin_([
                models.DeviceStatus.pending, models.DeviceStatus.failed
            ]),
        )
        .first()
    )
    if predecessor is not None:
        return None

    # (г) свежесть подписки. created_at выставляется python-дефолтом на
    # flush; None тут — аномалия (объект не сброшен в БД), пуш не шлём.
    created = sub.created_at
    if created is None:
        logger.warning(
            "config_ready: subscription %s has no created_at — skipping push", sub.id
        )
        return None
    if utcnow() - _naive_utc(created) > FRESH_WINDOW:
        return None

    # (д) дедуп: ретрай apply-таски / повторный warm-вызов не должны дать
    # второй пуш. ':delivered' — то, во что ACK поллера переименовывает
    # доставленную строку; ретеншен воркера её со временем удалит, но к тому
    # моменту подписка давно старше FRESH_WINDOW.
    already = (
        db.query(models.AuditLog.id)
        .filter(
            models.AuditLog.target_type == "subscription",
            models.AuditLog.target_id == sub.id,
            models.AuditLog.action.in_([ACTION, f"{ACTION}:delivered"]),
        )
        .first()
    )
    if already is not None:
        return None
    return sub


def notify_config_ready(
    db: Session,
    device: models.Device,
    *,
    source: str,
    commit: bool,
) -> bool:
    """Поставить в очередь бота пуш «конфиг готов» для владельца ``device``.

    ``source`` — 'warm' | 'cold', уходит в ``extra`` для разбора инцидентов.
    ``commit=False`` — строка остаётся в сессии, коммитит вызывающий.
    Возвращает True, только если строка реально добавлена. Никогда не
    бросает: сбой уведомления не должен ронять провижн.
    """
    try:
        sub = _passes_gate(db, device)
        if sub is None:
            return False
        extra: dict = {
            "telegram_id": str(sub.user.telegram_id),
            "subscription_id": sub.id,
            "device_id": device.id,
            "source": source,
        }
        sub_uri = _absolute_sub_uri(sub.sub_token)
        if sub_uri:
            extra["sub_uri"] = sub_uri
        # SAVEPOINT: выход из контекста делает flush (SessionLocal у нас
        # autoflush=False — без него повторный вызов в той же транзакции не
        # увидел бы строку в дедуп-запросе), а исключение внутри откатывает
        # ТОЛЬКО savepoint. Внешняя транзакция вызывающего (warm-путь: там
        # ещё не закоммичены подписка и девайс) остаётся рабочей, а не
        # PendingRollbackError на следующем же запросе.
        with db.begin_nested():
            db.add(
                models.AuditLog(
                    actor="provisioning",
                    actor_type=models.AuditActor.system,
                    action=ACTION,
                    target_type="subscription",
                    target_id=sub.id,
                    extra=extra,
                )
            )
        if commit:
            db.commit()
        logger.info(
            "config_ready queued: sub=%s device=%s source=%s link=%s",
            sub.id, device.id, source, "yes" if sub_uri else "no",
        )
        return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "config_ready: failed to queue push for device %s (source=%s)",
            getattr(device, "id", None), source,
        )
        return False
