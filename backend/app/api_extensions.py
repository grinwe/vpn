"""Additional API endpoints for MVP features.

This module adds endpoints for:
  - Dynamic subscription links (GET /sub/{token})
  - Referral system (POST /api/referral/code, GET /api/referral/stats)
  - Notification delivery (GET /api/notifications/pending, POST /api/notifications/{id}/ack)
  - Auto-renew toggle (POST /api/subscriptions/{id}/auto_renew)
  - User registration with referral (POST /api/users/register)
  - Self-service config regeneration (POST /api/users/by_telegram/{id}/regenerate)
  - Rate-limited invoice creation (applied via decorator)
"""
from __future__ import annotations

import base64
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


from fastapi import APIRouter, Depends, HTTPException, Request

from .auth import optional_admin, require_admin  # noqa: F401 — re-exported for legacy imports
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .api import sub_fix
from .api._common import get_db  # единый источник FastAPI-зависимости сессии (без третьей копии)
from .config import get_settings
from .rate_limit import limiter
from .security import decrypt as _decrypt
from .services.admin_notify import notify_admins
from .time_utils import utcnow

logger = logging.getLogger(__name__)
settings = get_settings()

# Порог «здоровья» ноды для фильтрации кредов в саб-линке — зеркалит
# provisioning.MIN_HEALTHY_SCORE (читаем env напрямую, без импорта тяжёлого
# модуля в горячий read-путь).
MIN_HEALTHY_SCORE = int(os.getenv("MIN_HEALTHY_SCORE", "50"))

ext_router = APIRouter(prefix="/api")


# ── Dynamic subscription link ──

class SubLinkConfig(BaseModel):
    protocol: str
    uri: str


class SubLinkResponse(BaseModel):
    subscription_id: int
    status: str
    expires_at: str
    configs: list[SubLinkConfig]


def _autoconnect_enabled(sub: models.Subscription, device) -> bool:
    """Включать ли HAPP-autoconnect для этой сабы/девайса (Phase B-гейт).

    ``SUB_HAPP_AUTOCONNECT``: ""/"0"/"off" → никому; "all"/"on"/"true"/"yes" → всем;
    иначе — CSV ``user_id`` (обкатка на одном юзере, как diverse-backfill).
    NB: ``"1"`` — это ЮЗЕР 1, НЕ булево «вкл» (коллизия: user_id=1 совпал бы со
    старым булевым "1" → обкатка на user 1 молча включалась бы ВСЕМ). Для «всем»
    используй "all"/"on".
    ``SUB_HAPP_AUTOCONNECT_SINCE`` (опц. ISO-метка): доп.фильтр «только НОВЫЕ девайсы»
    — включаем лишь для девайсов с ``created_at >= метки`` (тест без путаницы со
    старыми/primary девайсами). Если метка задана, а девайса нет (legacy саб-токен)
    — не включаем.
    """
    ac = (os.getenv("SUB_HAPP_AUTOCONNECT") or "").strip().lower()
    if ac in ("all", "on", "true", "yes"):
        on = True
    elif ac and ac not in ("0", "off", "false"):
        ids = {x.strip() for x in ac.split(",") if x.strip()}
        on = str(getattr(sub, "user_id", "")) in ids
    else:
        on = False
    if not on:
        return False
    since_raw = (os.getenv("SUB_HAPP_AUTOCONNECT_SINCE") or "").strip()
    if since_raw:
        return bool(device) and _created_after(device, since_raw)
    return True


def _created_after(device, since_raw: str) -> bool:
    """device.created_at >= since_raw (ISO). Нормализуем обе в naive-UTC."""
    try:
        since = datetime.fromisoformat(since_raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    created = getattr(device, "created_at", None)
    if created is None:
        return False
    if since.tzinfo is not None:
        since = since.astimezone(timezone.utc).replace(tzinfo=None)
    if created.tzinfo is not None:
        created = created.astimezone(timezone.utc).replace(tzinfo=None)
    return created >= since


def _header_safe(value: str) -> str:
    """Значение HTTP-заголовка, которое гарантированно уедет клиенту.

    HTTP-заголовки — latin-1, и Starlette падает с UnicodeEncodeError на любой
    кириллице: не «заголовок потерялся», а ВЕСЬ ответ подписки превращается в
    500, то есть VPN перестаёт настраиваться у всех разом. Ровно это и
    случилось с блоком статуса, где текст по определению русский.

    Кодируем как ``base64:<...>`` — форма из документации Happ (announce).
    Санитайзер общий на все заголовки намеренно: следующий русский текст в
    заголовке появится обязательно, и он не должен ронять выдачу.
    """
    try:
        value.encode("latin-1")
    except (UnicodeEncodeError, AttributeError):
        return "base64:" + base64.b64encode(str(value).encode("utf-8")).decode()
    return value


def _status_banner_enabled() -> bool:
    """Рубильник блока статуса.

    Нужен, потому что отрисовка ``base64:``-значений — единственное, что мы не
    можем проверить со своей стороны: это делает чужой клиент. Если Happ
    покажет вместо текста абракадабру, выключение обязано стоить одну
    переменную окружения, а не откат релиза.
    """
    return (os.getenv("SUB_STATUS_BANNER") or "1").strip().lower() not in (
        "0", "false", "off", "no",
    )


def _happ_provider_id() -> str:
    """Provider ID из кабинета happ-proxy.com — ключ ко всему Advanced-слою Happ.

    Ресёрч по докам 2026-07-29: ``sub-info-*``, ``sub-expire`` и
    ``notification-subs-expire`` — «Advanced parameters», и клиент МОЛЧА
    игнорирует их, пока в ответе нет providerid. Прежние «проверено на живом
    устройстве — не работает» (заголовки ``base64:``, тело base64 и plain)
    прогонялись без providerid и потому ничего не доказали.
    ``subscription-autoconnect`` сюда НЕ входит (раздел «Managing app
    settings») — июньская обкатка lowestdelay без providerid валидна.

    ID привязан хешем к домену, который дёргает клиент (наш CDN-прокси, а не
    бэкенд). Побочка регистрации: клиент раз в сутки отстукивается на
    check.happ-proxy.com с HWID.

    Значение валидируется жёстко: оно едет и заголовком, и строкой тела.
    CR/LF в заголовке — это 500 на всей выдаче (инвариант «VPN настраивается
    у всех» дороже любого блока), а не-ASCII _header_safe завернул бы в
    ``base64:`` — и клиент получил бы в заголовке и теле ДВА РАЗНЫХ id.
    Кривой ID из vault безопаснее не слать вовсе.
    """
    pid = (os.getenv("HAPP_PROVIDER_ID") or "").strip()
    # isascii() обязателен: str.isalnum() юникодный и пропустил бы кириллицу.
    if pid and not (pid.isascii() and all(ch.isalnum() or ch in "-_" for ch in pid)):
        return ""
    return pid


def _announce_enabled(client: str = "happ") -> bool:
    """Слать ли ``announce`` этому клиенту.

    ``0`` — никогда (аварийный выключатель непроверенного рендера), ``1`` —
    всегда. Разница в режиме ``auto`` (дефолт):

    * **Happ** — announce едет, только пока providerid не задан: с ним статус
      рисует полноценный sub-info-блок, и второй блок с тем же текстом был бы
      дублем;
    * **v2rayTun** — едет всегда, потому что sub-info он не понимает вовсе, и
      announce у него ЕДИНСТВЕННЫЙ способ показать статус и дать вход на
      страницу. Гасить его из-за чужого providerid значит оставить клиента
      ни с чем.

    Аварийный ``SUB_STATUS_BANNER=0`` гасит и announce тоже — он выключает
    блок статуса целиком, поверх любых режимов здесь.
    """
    mode = (os.getenv("SUB_STATUS_ANNOUNCE") or "auto").strip().lower()
    if mode in ("0", "false", "off", "no", "never"):
        return False
    if mode in ("1", "true", "on", "yes", "always"):
        return True
    if client == "v2raytun":
        return True
    return not _happ_provider_id()


def _sub_status_banner(sub, token: str = "") -> dict[str, str]:
    """Блок статуса подписки над списком серверов (Happ: sub-info-*).

    Смысл: человек видит срок и кнопку продления прямо в VPN-клиенте и не идёт
    за этим в бота. Системный блок Happ (``sub-expire``) не годится — он считает
    только полные дни и показывается максимум за 3 дня до конца, а нам нужен
    статус всегда и с часами на финише.

    Цвет несёт смысл: синий — всё в порядке, красный — пора платить. Текст
    ограничен 200 символами (лимит Happ), кнопка — 25.
    """
    expires_at = sub.expires_at
    if not expires_at:
        return {}

    # Приводим к naive-UTC перед вычитанием: в БД срок лежит naive, но приезжает
    # он и из кода, где datetime собирают с tzinfo, а смешивать их нельзя —
    # TypeError тут означает 500 на всей выдаче подписки, а не кривой баннер.
    if expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
    left = expires_at - utcnow()
    total_hours = int(left.total_seconds() // 3600)
    if left.total_seconds() <= 0:
        text = "Подписка закончилась. Продлите, чтобы VPN снова заработал."
        color = "red"
    elif total_hours < 24:
        # Часы, а не «0 дней»: на финише это единственная понятная человеку
        # единица, и именно тут решение о продлении принимается.
        hours = max(1, total_hours)
        text = f"Подписка истекает через {_plural_hours(hours)}"
        color = "red"
    elif left.days <= 3:
        text = f"Подписка истекает через {_plural_days(left.days)}"
        color = "red"
    else:
        # Дата — по МСК, а не по UTC: срок хранится как 23:59:59 UTC, и
        # UTC-дата ВСЕГДА на день раньше московской. Клиент рядом рисует
        # «Истекает» в локальной зоне устройства — наши «до 14.08» против
        # его «15.08» выглядели багом. RU-сервис, фиксированный UTC+3.
        msk = expires_at.replace(tzinfo=timezone.utc).astimezone(
            timezone(timedelta(hours=3))
        )
        text = f"Подписка активна до {msk.strftime('%d.%m.%Y')}"
        color = "blue"

    banner = {
        "sub-info-text": text[:200],
        "sub-info-color": color,
    }
    # Кнопка блока статуса. В режиме ``all`` она ведёт на страницу починки и
    # называется «Не подключается?»: пока активно родное expire-предупреждение
    # Happ, блок sub-info не показывается вовсе — значит он виден только когда
    # до конца больше трёх дней, в «зелёном» состоянии. В этом окне человек
    # открывает клиент и что-то жмёт ровно по одной причине: сломалось.
    if _fix_entrypoints() == "all" and token:
        page = _fix_page_url(token)
        if page:
            banner["sub-info-button-text"] = "Не подключается?"
            banner["sub-info-button-link"] = page
            return banner
    renew_url = _renew_link()
    if renew_url:
        banner["sub-info-button-text"] = "Продлить"
        banner["sub-info-button-link"] = renew_url
    return banner


def _sub_body_directives(sub, token: str = "", client: str = "unknown") -> list[str]:
    """Директивы Happ строками-комментариями в ТЕЛЕ подписки.

    Тело — UTF-8, и документация Happ разрешает каждый параметр комментарием
    ``#имя: значение`` перед ссылками наравне с заголовками (заголовки в
    приоритете). Рабочий канал sub-info — ЗАГОЛОВКИ (base64:-обёртку клиент
    разворачивает, проверено 2026-07-29); из base64-тела директивы клиент не
    читает, так что здесь они лишь документированный fallback. Остальные
    клиенты строки с ``#`` игнорируют — это стандартная форма для панелей.

    ``providerid`` — особый формат БЕЗ двоеточия (``#providerid {id}``), так в
    доках. Без него sub-info-* клиент игнорирует (Advanced-слой): вывод «Happ
    не читает директивы из тела», записанный 2026-07-29, не доказан — тесты
    шли без providerid.
    """
    # v2rayTun и Hiddify эти директивы не читают (у первого свой синтаксис
    # announce, у второго нет блоков вовсе) — сыпать их в тело значит мусорить
    # в файле, который человек может открыть глазами.
    if client in ("v2raytun", "hiddify"):
        return []
    lines: list[str] = []
    provider_id = _happ_provider_id()
    if provider_id:
        # Не под рубильником баннера: providerid активирует ВЕСЬ Advanced-слой
        # (sub-expire, notification-subs-expire), не только блок статуса.
        lines.append(f"#providerid {provider_id}")
    if not _status_banner_enabled():
        return lines
    banner = _sub_status_banner(sub, token)
    if not banner:
        return lines
    lines += [f"#{key}: {value}" for key, value in banner.items()]
    return lines


def _sub_body(
    configs, sub, *, plain: bool = False, token: str = "",
    client: str = "unknown",
) -> str:
    """Тело подписки: директивы блока статуса + ссылки.

    ``plain`` — без base64, диагностика чтения директив из тела. Прежний
    вывод «Happ не разбирает директивы из закодированного тела» не доказан:
    проверка шла без providerid, а без него Advanced-директивы игнорируются
    при любом способе доставки.
    """
    lines = _sub_body_directives(sub, token, client) + [c.uri for c in configs]
    body = "\n".join(lines)
    return body if plain else base64.b64encode(body.encode()).decode()


def _renew_link() -> str | None:
    """Куда ведут кнопка «Продлить» и иконка Telegram — в личный кабинет.

    Не на голый ``/start``: человек, нажавший «продлить» в VPN-клиенте, уже
    знает, чего хочет, и приветственный экран бота — лишний шаг между ним и
    оплатой.

    Прямая ссылка на мини-апп (``t.me/<bot>/<short_name>``) открывает кабинет
    одним тапом, но короткое имя приложения задаётся в BotFather и его может
    не быть — тогда падаем на deep-link в бота, где ``account`` отвечает
    сообщением с кнопкой входа. WebApp-кнопку саму по себе из внешнего
    браузера открыть нельзя, только через Telegram.
    """
    bot = (os.getenv("BOT_USERNAME") or "").strip()
    if not bot:
        return None
    short_name = (os.getenv("TELEGRAM_MINIAPP_SHORT_NAME") or "").strip()
    if short_name:
        return f"https://t.me/{bot}/{short_name}"
    return f"https://t.me/{bot}?start=account"


def _fix_page_url(token: str) -> str | None:
    """Ссылка на страницу починки для кнопок в VPN-клиенте.

    Строится ТОЛЬКО от ``SUB_LINK_BASE_URL`` (домен саб-ссылки): именно он
    доступен без VPN — с него клиент и так тянет конфиги. Вести кнопку на
    основной домен бессмысленно, его как раз и может резать РКН.
    """
    base = (os.getenv("SUB_LINK_BASE_URL") or "").strip().rstrip("/")
    if not base or not token:
        return None
    return f"{base}/{token}?fix=1"


def _fix_entrypoints() -> str:
    """Какие слоты в клиенте ведут на страницу: off / support / all."""
    mode = (os.getenv("SUB_FIX_ENTRYPOINTS") or "off").strip().lower()
    return mode if mode in ("off", "support", "all") else "off"


# ── Кто именно пришёл за подпиской ──────────────────────────────────────
#
# Клиенты называют себя в User-Agent на каждом запросе (``Happ/2.4.1``,
# ``v2rayTun/5.24.76``, ``HiddifyNext/4.1.1 (windows) like ClashMeta``), и
# набор украшений у них РАЗНЫЙ: у Happ богатый Advanced-слой за Provider ID,
# у v2rayTun — одна строка announce с раскраской и кликом, у Hiddify нет
# ничего, кроме шкалы трафика и двух ссылок в меню профиля.
#
# Зачем ветвление, а не «слать всем всё»: имя ``announce`` у Happ и v2rayTun
# ОБЩЕЕ, но синтаксис разный — цветовые коды ``#RRGGBB``, которые v2rayTun
# красит, Happ покажет буквально («#ff5555Не подключается?»). Плюс каждый
# клиент получает только осмысленные для него заголовки, а не кашу.
_CLIENT_PATTERNS = (
    # HiddifyNext называет себя «like ClashMeta v2ray sing-box», поэтому
    # проверяется ПЕРВЫМ: иначе подстрока «v2ray» увела бы его в v2rayTun.
    ("hiddify", re.compile(r"hiddify", re.I)),
    ("v2raytun", re.compile(r"v2raytun", re.I)),
    ("happ", re.compile(r"\bhapp\b|happ/", re.I)),
    ("streisand", re.compile(r"streisand", re.I)),
    ("v2rayng", re.compile(r"v2rayng", re.I)),
)


def _client_kind(user_agent: str | None) -> str:
    """Какой клиент пришёл: happ / v2raytun / hiddify / … / unknown.

    ``unknown`` (в том числе пустой UA) получает нынешний общий набор — то
    есть добавление ветвления не может ничего сломать у клиента, которого мы
    не опознали.
    """
    ua = user_agent or ""
    for kind, pattern in _CLIENT_PATTERNS:
        if pattern.search(ua):
            return kind
    return "unknown"


def _v2raytun_announce(sub, token: str) -> tuple[str, str] | None:
    """Строка объявления для v2rayTun + ссылка, которую она открывает.

    У v2rayTun нет ни блока с фоном, ни кнопки с подписью: есть ``announce``
    (строка под шкалой трафика) и ``announce-url`` (делает ВЕСЬ текст
    тапабельным, справа появляется стрелка). Поэтому «кнопочность» пишется
    словами внутри самого текста.

    Цвет задаётся инлайн-кодом ``#RRGGBB`` перед словом и действует до
    следующего кода — красим только призыв, чтобы строка не превратилась в
    радугу. Регистрация клиенту не нужна, в отличие от Provider ID у Happ.
    """
    banner = _sub_status_banner(sub, token)
    status = banner.get("sub-info-text")
    if not status:
        return None
    entrypoints = _fix_entrypoints()
    page = _fix_page_url(token) if entrypoints != "off" else None
    if not page:
        # Без страницы объявление остаётся просто статусом — красить и
        # делать кликабельным нечего.
        return status, ""
    if entrypoints != "all":
        # Режим ``support`` — переходная ступень: ссылка на страницу уже
        # есть, но громкого призыва ещё нет. У Happ на этой ступени крупная
        # кнопка тоже не появляется (она включается только в ``all``), и
        # ломать лестницу раската ради одного клиента незачем.
        return status, page
    # Красный призыв + статус. Цвет статуса несёт тот же смысл, что
    # sub-info-color у Happ: красный — пора платить, серый — всё в порядке.
    # Иначе «Подписка закончилась» показывалась бы тем же тоном, что «активна
    # до …», и человек не отличил бы одно от другого.
    status_colour = "#e05252" if banner.get("sub-info-color") == "red" else "#9aa0a6"
    return (
        f"#e05252Не подключается? Нажмите здесь. {status_colour}{status}",
        page,
    )


def _plural_hours(n: int) -> str:
    """«1 час / 3 часа / 5 часов» — см. _plural_days."""
    n = int(n)
    if 11 <= n % 100 <= 14:
        return f"{n} часов"
    tail = n % 10
    if tail == 1:
        return f"{n} час"
    if tail in (2, 3, 4):
        return f"{n} часа"
    return f"{n} часов"


@dataclass(frozen=True)
class SubTokenLookup:
    """Что нашлось по sub_token: подписка и какое устройство обслуживать."""

    sub: models.Subscription
    # Устройство, которому принадлежит токен (None у legacy-подписочных).
    token_device: models.Device | None
    # Чьи креды реально отдаём: сам token_device либо живой сосед-алиас.
    serve_device: models.Device | None
    is_legacy: bool

    @property
    def aliased(self) -> bool:
        return (
            self.token_device is not None
            and self.serve_device is not None
            and self.serve_device.id != self.token_device.id
        )


def resolve_sub_token(db: Session, token: str) -> SubTokenLookup | None:
    """Найти подписку и обслуживающее устройство по sub_token.

    Вынесено из ``dynamic_sub_link`` ДОСЛОВНО (эпик «починка без Telegram»):
    странице на саб-домене нужен тот же lookup, а копия неизбежно разъехалась
    бы с оригиналом. Никаких побочных эффектов — ни аудита, ни отметки первой
    выдачи, ни проверки «подписка обслуживаема»: это остаётся на вызывающем,
    потому что странице продления нужна и ИСТЁКШАЯ подписка.

    ``None`` — токен неизвестен (вызывающий решает: 404 или camo-страница).
    """
    device = db.query(models.Device).filter_by(sub_token=token).first()
    if device:
        sub = device.subscription
        if sub is None:
            return None

        # ╔══════════════════════════════════════════════════════════════╗
        # ║  DO NOT TOUCH without reading docs/components/backend-api.md ║
        # ║  section "Sub-link invariant".                               ║
        # ║                                                              ║
        # ║  Seamless-migration alias. Every resync/migration path       ║
        # ║  (admin override, drain, auto-migrate-on-block, webapp       ║
        # ║  device-move, balance unfreeze) revokes the old Device row   ║
        # ║  and provisions a fresh one on the target node with a NEW    ║
        # ║  sub_token. The user's Hiddify/v2rayN profile is still       ║
        # ║  pointing at the OLD token — without this fallback the       ║
        # ║  saved subscription URL refreshes into an empty config and   ║
        # ║  the user has to manually reimport the new URL, which is     ║
        # ║  exactly the 404-loop we already shipped new URIs to fix.    ║
        # ║                                                              ║
        # ║  Load-bearing invariant (the three MUST hold together):      ║
        # ║    1. provisioning.py `_handle_task_outcome` keeps the       ║
        # ║       revoked Device row in the DB (option A).               ║
        # ║    2. Reprovisioning creates a NEW Device on the same Sub    ║
        # ║       (never mutates the old one's sub_token).               ║
        # ║    3. This block finds the live sibling on the same Sub.    ║
        # ║  Break any one and saved client URLs start 404-ing.          ║
        # ║                                                              ║
        # ║  Multi-device subs collapse to a single device after         ║
        # ║  migration (`reprovision_subscription` provisions one) —     ║
        # ║  known limitation, secondary clients alias onto the          ║
        # ║  survivor. Accepted: same user, same sub.                    ║
        # ╚══════════════════════════════════════════════════════════════╝
        source_device = device
        if device.status != models.DeviceStatus.active or not any(
            c.is_active for c in device.credentials
        ):
            # Prefer the most recently-updated active sibling so
            # chained migrations (A → B → C) always alias onto C,
            # not some stale B that was left around.
            live = next(
                (
                    d
                    for d in sorted(
                        sub.devices,
                        key=lambda x: x.updated_at or x.created_at,
                        reverse=True,
                    )
                    if d.id != device.id
                    and d.status == models.DeviceStatus.active
                    and any(c.is_active for c in d.credentials)
                ),
                None,
            )
            if live is not None:
                source_device = live
        return SubTokenLookup(
            sub=sub, token_device=device, serve_device=source_device, is_legacy=False
        )

    sub = db.query(models.Subscription).filter_by(sub_token=token).first()
    if sub is None:
        return None
    return SubTokenLookup(
        sub=sub, token_device=None, serve_device=None, is_legacy=True
    )


def _sub_response_headers(
    sub: models.Subscription, token: str, device=None,
    *, userinfo_expire_only: bool = False, user_agent: str | None = None,
) -> dict[str, str]:
    """Заголовки саб-ответа (читаются клиентом на каждом рефреше — existing юзеры
    подхватят без переимпорта).

    Phase B (HAPP «авто»): ``subscription-autoconnect`` + ``-type: lowestdelay`` →
    HAPP при (пере)коннекте сам берёт ноду с ЛУЧШИМ ПИНГОМ (дохлые с плохим/нет
    пинга — мимо). Бесшовного per-server failover у HAPP через плоскую сабу НЕТ
    (сверено по их докам) — это максимум, и он чисто server-side. За гейтом
    ``SUB_HAPP_AUTOCONNECT`` (+ опц. ``_SINCE`` для «только новых девайсов»).
    Autoconnect — раздел «Managing app settings», НЕ Advanced: работает без
    providerid (обкатка 2026-06-13 это и показала).
    ``fallback-url`` (если задан ``SUB_LINK_FALLBACK_BASE_URL``) — фейловер
    ИСТОЧНИКА сабы на запасной домен, когда основной саб-URL режет РКН.
    По докам Happ это тоже Advanced-параметр — без providerid не сработает.

    ``profile-update-interval`` (часы) — как часто Hiddify/v2rayNG/HAPP сами
    перечитывают сабу и через sibling-alias подхватывают новую ноду после
    failover/миграции. Захардкоженные 6ч означали окно устаревания конфига
    до полусуток; для анти-РКН профиля (РУ-ноды-расходники, частые баны)
    дефолт снижен до 2ч и вынесен в ``SUB_PROFILE_UPDATE_INTERVAL_H`` — прод
    может ужать до 1ч (компромисс свежесть-failover ↔ нагрузка read-пути;
    write-amplification уже срезается ``SUB_FETCH_AUDIT_SAMPLE``).

    ``cache-control: no-store, private`` (+ ``pragma``) — тело саб-ответа это
    ПЕРСОНАЛЬНЫЙ динамический конфиг, меняющийся при каждой миграции/ротации
    токена; запрещаем CF-Worker'у и любым промежуточным прокси его кэшировать,
    иначе seamless-alias-инвариант обнулится закэшированным старым конфигом.
    """
    client = _client_kind(user_agent)
    title = "V8-VPN"
    try:
        interval_h = int(os.getenv("SUB_PROFILE_UPDATE_INTERVAL_H") or "2")
    except ValueError:
        interval_h = 2
    interval_h = max(1, interval_h)
    headers: dict[str, str] = {
        "profile-update-interval": str(interval_h),
        "profile-title": title,
        "content-disposition": f'attachment; filename="{title}"',
        "cache-control": "no-store, private",
        "pragma": "no-cache",
        # Ответ теперь зависит от клиента — говорим это вслух любому кэшу на
        # пути. no-store уже запрещает кэширование, но Vary дешёвый и снимает
        # целый класс «а вдруг промежуточный прокси всё же сложит».
        "vary": "User-Agent",
    }
    if sub.expires_at:
        # ВСЕ ЧЕТЫРЕ ключа обязательны: Hiddify парсит заголовок только целиком
        # (upload/download/total/expire) и при неполном наборе игнорирует его —
        # а заодно теряет support-url и profile-web-page-url, которые цепляет
        # только к распарсенному userinfo. Мы месяцами отдавали один expire и
        # в Hiddify не показывали ничего.
        #
        # total=0 означает «безлимит» и в Hiddify, и в Happ. Это честно: тарифы
        # у нас по устройствам, а не по гигабайтам, а per-user трафик мы не
        # накапливаем — тик traffic_stats сбрасывает счётчики (xray `--reset`,
        # hysteria `?clear=1`) и хранит лишь сумму по ноде за интервал. Рисовать
        # шкалу из выдуманных цифр — врать человеку.
        expires = sub.expires_at
        if expires.tzinfo is None:
            # naive в БД — это UTC, но .timestamp() наивного datetime берёт
            # ЛОКАЛЬНУЮ зону процесса. В контейнере она UTC и всё совпадает —
            # однако дата, от которой клиент считает «через сколько платить»,
            # не должна зависеть от TZ окружения.
            expires = expires.replace(tzinfo=timezone.utc)
        if userinfo_expire_only:
            # Диагностика (?userinfo=expire): только срок, без трафик-ключей.
            # Гипотеза — без upload/download/total Happ не рисует шкалу
            # «0B/∞» (нулевые данные, шкала-пустышка), а дату «Истекает»
            # сохраняет. Поведение недокументировано, потому опт-ин: Hiddify
            # парсит заголовок только ЦЕЛИКОМ и при неполном наборе теряет
            # его вместе с support-url — дефолт менять нельзя, пока Happ
            # не подтвердит рендер на живом устройстве.
            headers["subscription-userinfo"] = f"expire={int(expires.timestamp())}"
        else:
            # download = реальные байты за текущий оплаченный период (наливает
            # тик traffic_stats, обнуляет продление). Счётчик информационный:
            # тарифы по устройствам, total=0 = безлимит — «12.5 GB/∞» вместо
            # прежнего вечного «0B». upload не разделяем: клиент показывает
            # сумму, а делить один счётчик на два поля — рисовать точность,
            # которой нет.
            used = int(getattr(sub, "traffic_used_bytes", 0) or 0)
            headers["subscription-userinfo"] = (
                f"upload=0; download={used}; total=0; "
                f"expire={int(expires.timestamp())}"
            )
    # Иконка справа в строке подписки (Happ) — «куда идти, если сломалось».
    # При включённых входах ведёт на страницу починки: она доступна без VPN,
    # а Telegram в тех же условиях может быть недоступен — то есть кнопка
    # «поддержка» вела бы ровно туда, куда человек не может попасть.
    # Побочка: не-t.me ссылка теряет Telegram-иконку, остаётся обычная.
    tg_link = _renew_link()
    entrypoints = _fix_entrypoints()
    page_url = _fix_page_url(token) if entrypoints != "off" else None
    # ``support`` — переходный режим (страница вместо Telegram в мелкой
    # иконке). В ``all`` иконка ВОЗВРАЩАЕТСЯ в Telegram намеренно: на
    # странице человек уже есть через большую кнопку блока статуса, а два
    # входа в одно место — дубль. К тому же t.me-ссылка рисуется телеграмной
    # иконкой, и это единственный оставшийся способ попасть в бота из клиента.
    # Кому иконка ведёт на страницу:
    #  * режим ``support`` — всем (переходный режим до больших кнопок);
    #  * Hiddify — ВСЕГДА, когда страница включена: у него нет ни блока
    #    статуса, ни объявлений, а ``profile-web-page-url`` на актуальных
    #    версиях не отображается (баги hiddify-app#2063, #1722). То есть
    #    support-url — его ЕДИНСТВЕННЫЙ достижимый вход, и уводить его в
    #    Telegram значит оставить второго по массовости клиента вообще без
    #    входа ровно тогда, когда Telegram недоступен;
    #  * Happ — в Telegram: у него вход есть большой кнопкой блока статуса, а
    #    t.me-ссылка рисуется телеграмной иконкой и остаётся единственным
    #    способом попасть в бота прямо из клиента.
    support_to_page = page_url and (
        entrypoints == "support" or (client == "hiddify" and entrypoints == "all")
    )
    support = page_url if support_to_page else tg_link
    if support:
        headers["support-url"] = support
    # ── Happ-специфика ──────────────────────────────────────────────────
    # Остальным эти заголовки бесполезны: v2rayTun и Hiddify их молча
    # игнорируют. ``unknown`` тоже получает их — так поведение для
    # неопознанного клиента остаётся ровно нынешним, и ветвление не может
    # ничего сломать там, где мы не уверены, кто пришёл.
    # streisand/v2rayng классифицируются (пригодится для статистики), но своих
    # веток у них нет — значит набор им остаётся ПРЕЖНИМ. Иначе правка была бы
    # чистой потерей: лишили бы их sub-info/providerid, не дав ничего взамен.
    happ_slots = client not in ("v2raytun", "hiddify")
    provider_id = _happ_provider_id() if happ_slots else ""
    if provider_id:
        # Ключ к Advanced-слою Happ: без него sub-expire, notification-subs-
        # expire и sub-info-* клиент молча игнорирует. Дубль едет в теле
        # (#providerid …) на случай прокси, режущего нестандартные заголовки.
        headers["providerid"] = provider_id
    if happ_slots and sub.expires_at:
        # Пуш-напоминания клиента за 3 дня до конца — бесплатный канал
        # возврата, работающий даже когда человек отключил уведомления нашего
        # бота. Advanced: оживает только с providerid. Без срока подписки
        # напоминать не о чем.
        headers["notification-subs-expire"] = "1"
    # Родное предупреждение Happ «подписка заканчивается через N д.» + кнопка
    # «Продлить» (текст кнопки фиксирован клиентом). Показывает его сам клиент
    # за ≤3 дня до конца и после истечения; Advanced — требует providerid.
    # Значения ASCII, поэтому заголовок безопасен.
    if happ_slots and _status_banner_enabled() and sub.expires_at:
        headers["sub-expire"] = "1"
        # В режиме ``all`` — на страницу, а не в Telegram. Именно у истёкшей
        # подписки родное предупреждение вытесняет блок sub-info, то есть эта
        # кнопка остаётся ЕДИНСТВЕННОЙ видимой — и вести ей в Telegram, до
        # которого без VPN не добраться, значит замкнуть тот самый круг,
        # ради разрыва которого написан эпик. На странице есть и продление,
        # и ссылка в Telegram для тех, у кого он жив.
        # …но только когда на странице реально МОЖНО заплатить. Пока
        # SUB_FIX_PAY выключен, кнопка «Продлить» на странице показала бы
        # текст «продлите в кабинете» — то есть лишний шаг на пути к оплате
        # вместо прямой ссылки в кабинет.
        expire_to_page = (
            entrypoints == "all" and page_url and sub_fix.pay_enabled()
        )
        expire_link = page_url if expire_to_page else tg_link
        if expire_link:
            headers["sub-expire-button-link"] = expire_link
    # ── announce: одно имя, два разных синтаксиса ───────────────────────
    #
    # v2rayTun красит текст инлайн-кодами ``#RRGGBB`` и делает его тапабельным
    # через ``announce-url``; Happ ни того, ни другого не умеет и показал бы
    # цветовые коды буквально («#e05252Не подключается?»). Поэтому строка
    # собирается ПОД КЛИЕНТА, а не одна на всех.
    # _announce_enabled() гейтит обе ветки: v2rayTun-рендер единственный, что
    # ещё не проверен на живом устройстве, и выключать его должно быть чем-то
    # точнее общего SUB_STATUS_BANNER (тот гасит блок статуса целиком).
    if _status_banner_enabled() and client == "v2raytun" and _announce_enabled(client):
        pair = _v2raytun_announce(sub, token)
        if pair:
            text, url = pair
            headers["announce"] = (
                "base64:" + base64.b64encode(text.encode("utf-8")).decode()
            )
            if url:
                headers["announce-url"] = url
    elif _status_banner_enabled() and happ_slots and _announce_enabled(client):
        # Happ-вариант: без цветовых кодов и без ссылки (их он не понимает).
        # Роль — запасной видимый статус, пока providerid не задан или не
        # провалидирован; в auto-режиме гаснет сам, когда живёт sub-info.
        announce_text = _sub_status_banner(sub, token).get("sub-info-text")
        if announce_text:
            headers["announce"] = (
                "base64:" + base64.b64encode(announce_text.encode("utf-8")).decode()
            )
    # Цветной блок статуса: sub-info-* ЗАГОЛОВКАМИ — рабочий канал, проверено
    # на живом устройстве 2026-07-29 (?subinfo=headers): Happ разворачивает
    # base64:-обёрнутую кириллицу в заголовках, а вот директивы из base64-ТЕЛА
    # не читает вовсе (тело остаётся документированным fallback'ом). Требует
    # providerid; без него клиент молча игнорирует — не вредно.
    if happ_slots and _status_banner_enabled():
        headers.update(_sub_status_banner(sub, token))
    # Бонусом к support-url: на версиях, где этот слот работает, у Hiddify
    # появляется ещё и пункт в меню профиля. Полагаться на него нельзя (в
    # 4.1.1 не отображается), поэтому основной вход — support-url выше.
    if client == "hiddify" and page_url and entrypoints == "all":
        # Только в ``all``: в режиме ``support`` на страницу уже ведёт
        # support-url, и второй пункт меню с тем же адресом — дубль.
        headers["profile-web-page-url"] = page_url
    if _autoconnect_enabled(sub, device):
        headers["subscription-autoconnect"] = "true"  # канон (HAPP принимает и "1")
        headers["subscription-autoconnect-type"] = "lowestdelay"
    fallback = (os.getenv("SUB_LINK_FALLBACK_BASE_URL") or "").strip().rstrip("/")
    if fallback and token:
        headers["fallback-url"] = f"{fallback}/{token}"
    # Последним рубежом, а не точечно у баннера: заголовок с кириллицей роняет
    # ВЕСЬ ответ подписки в 500 (latin-1), и цена промаха тут — «VPN не
    # настраивается ни у кого», а не «пропала одна подпись».
    return {k: _header_safe(v) for k, v in headers.items()}


def ensure_referral_code(db: Session, user) -> "models.ReferralCode":
    """Вернуть активный реферальный код пользователя, создав при необходимости.

    Один хелпер на всех, потому что кодов исторически было два формата: ручка
    для бота минтила ``token_urlsafe(8)``, ручка вебаппа — 6 символов в верхнем
    регистре, и один и тот же человек видел в боте и в мини-аппе РАЗНЫЕ ссылки.
    """
    existing = (
        db.query(models.ReferralCode)
        .filter_by(owner_id=user.id, is_active=True)
        .order_by(models.ReferralCode.id.desc())
        .first()
    )
    if existing is not None:
        return existing
    ref = models.ReferralCode(owner_id=user.id, code=secrets.token_urlsafe(8))
    db.add(ref)
    db.flush()
    return ref


def referral_share_url(code: str) -> str | None:
    """``t.me/<bot>?start=ref_<code>``. None, если BOT_USERNAME не задан."""
    bot = os.getenv("BOT_USERNAME")
    return f"https://t.me/{bot}?start=ref_{code}" if bot else None


def _plural_days(n: int) -> str:
    """«1 день / 3 дня / 30 дней» — русские числительные требуют трёх форм.

    Без этого шаблон печатал «дарим тебе 3 дней», и сообщение сразу читалось
    как машинное.
    """
    n = int(n)
    if 11 <= n % 100 <= 14:
        return f"{n} дней"
    tail = n % 10
    if tail == 1:
        return f"{n} день"
    if tail in (2, 3, 4):
        return f"{n} дня"
    return f"{n} дней"


def _mark_first_config_fetch(db: Session, sub) -> None:
    """Отметить первое скачивание конфига и позвать привести друга.

    Момент «вау»: человек только что забрал рабочий конфиг — единственная точка,
    где мы знаем, что VPN у него поехал. Предлагать реферальную ссылку в
    приветствии бессмысленно (человеку ещё нечего рекомендовать), а здесь —
    уместно, и рынок делает так же.

    Пишем в отдельную колонку, а не считаем по audit_logs: те сэмплируются и
    чистятся ретеншеном. Гейт `first_config_fetch_at IS NULL` заодно делает
    приглашение одноразовым — саб-ссылку клиент дёргает каждые пару часов.

    Read-путь горячий, поэтому всё внутри try: ни отметка, ни приглашение не
    стоят того, чтобы уронить выдачу конфига.
    """
    try:
        user = db.get(models.User, sub.user_id) if sub.user_id else None
        if user is None or user.first_config_fetch_at is not None:
            return
        user.first_config_fetch_at = utcnow()
        if user.telegram_id:
            from .services import balance as balance_svc

            ref = ensure_referral_code(db, user)
            db.add(
                models.AuditLog(
                    actor="system",
                    actor_type=models.AuditActor.system,
                    action="referral_invite",
                    target_type="user",
                    target_id=user.id,
                    extra={
                        "telegram_id": user.telegram_id,
                        "share_url": referral_share_url(ref.code),
                        "reward_days": (
                            ref.reward_days or balance_svc.REFERRAL_REWARD_DAYS
                        ),
                    },
                )
            )
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("first-config-fetch: не удалось отметить user=%s", sub.user_id)
        db.rollback()


def _should_log_sub_fetch() -> bool:
    """Сэмплирование записи ``subscription_fetch`` в самом горячем read-пути.

    Клиенты (Hiddify/v2rayNG) опрашивают ссылку каждые 6 ч (``profile-update-
    interval``) + ручные рефреши, и каждый успешный фетч пишет строку
    ``AuditLog`` + ``COMMIT`` — таблица ``audit_logs`` растёт неограниченно
    (ретеншен-джоба живёт в worker.py, вне этого модуля — см. аудит #247).
    ``SUB_FETCH_AUDIT_SAMPLE`` (int, по умолчанию 1 = писать каждый фетч) —
    прод-рубильник: при N>1 пишем примерно 1 фетч из N, срезая write-
    amplification в горячем эндпоинте, сохраняя сам сигнал активности. N<=1 или
    мусор → писать всегда (текущее поведение, дефолт-noop для тестов)."""
    try:
        n = int(os.getenv("SUB_FETCH_AUDIT_SAMPLE") or "1")
    except ValueError:
        n = 1
    if n <= 1:
        return True
    return secrets.randbelow(n) == 0


def _retry_after_sec() -> str:
    """Значение заголовка ``Retry-After`` для транзиентных 503 саб-линка (сек).

    Настраивается через ``SUB_RETRY_AFTER_SEC`` (дефолт 60). Без него «retry
    shortly» на практике вырождается в плановый ``profile-update-interval``
    (часы); заголовок даёт корректному клиенту машиночитаемый хинт перезапросить
    конфиг сразу после провижининга/разморозки."""
    try:
        n = int(os.getenv("SUB_RETRY_AFTER_SEC") or "60")
    except ValueError:
        n = 60
    return str(max(1, n))


def _node_serviceable(node, now) -> bool:
    """Пригодна ли нода к выдаче в саб-линке (зеркалит фильтр ``choose_node``):
    активна, не в cooldown, не заглушена диагностикой, ``health_score`` не ниже
    порога. Нездоровые ноды исключаем из набора, чтобы клиент не держал мёртвый
    эндпоинт в ротации client-side failover."""
    if not getattr(node, "is_active", True):
        return False
    cd = getattr(node, "cooldown_until", None)
    if cd is not None and cd > now:
        return False
    if getattr(node, "auto_diagnose_disabled_at", None) is not None:
        return False
    if getattr(node, "diagnostics_disabled_at", None) is not None:
        return False
    hs = getattr(node, "health_score", None)
    if hs is not None and hs < MIN_HEALTHY_SCORE:
        return False
    return True


def _healthy_node_ids(db: Session, creds) -> set[int]:
    """node_id активных кредов, чьи ноды пригодны к выдаче.

    Пустой результат = «фильтр не применять» (kill-switch ``SUB_FILTER_
    UNHEALTHY_NODES=0``, у кредов нет node_id, ИЛИ все ноды нездоровы — в
    последнем случае вызывающий деградирует к нефильтрованному набору: живой-
    но-неоптимальный конфиг лучше 503)."""
    if (os.getenv("SUB_FILTER_UNHEALTHY_NODES") or "1").strip().lower() in (
        "0", "off", "false",
    ):
        return set()
    node_ids = {c.node_id for c in creds if c.is_active and c.node_id is not None}
    if not node_ids:
        return set()
    now = utcnow()
    rows = db.query(models.VPNNode).filter(models.VPNNode.id.in_(node_ids)).all()
    return {n.id for n in rows if _node_serviceable(n, now)}


# Протоколы, умеющие RU split-tunnel на relay-ноде. С 2026-07-28 это ВСЕ
# четыре: у трёх vless-флаворов это routing-правила + sockopt.interface, у
# hysteria2 — секции ``outbounds`` (direct с ``bindDevice: wgN``) и
# ``acl.inline`` (РУ → local, остальное → tunnel). До той даты hy2 не биндил
# ничего и на relay-ноде выпускал весь трафик с российского IP.
#
# ``shadowtls+shadowsocks`` в список НЕ входит намеренно: роль отключена,
# split-tunnel там никто не делал, и живой кред этого протокола на relay-ноде
# — та же поломка, что была у hy2.
_SPLIT_TUNNEL_PROTOS = {
    "vless-reality",
    "vless-xhttp",
    "vless-ws-cdn",
    "hysteria2",
}


def _tunnel_blind_node_ids(db: Session, creds) -> set[int]:
    """node_id relay-нод — тех, у кого есть хотя бы один ``RelayExitLink``.

    На такой ноде «наружу» означает «через WG в зарубежный exit», и кред
    протокола без split-tunnel даёт юзеру рабочий коннект БЕЗ VPN.

    ⚠️ По умолчанию ВЫКЛЮЧЕН. Это аварийный рубильник, а не штатный механизм:
    штатно каждый протокол туннелируется сам (см. ``_SPLIT_TUNNEL_PROTOS``).
    Включать (``SUB_FILTER_TUNNEL_BLIND=1``), если ansible-раскатка
    split-tunnel где-то не прошла и надо срочно убрать слепые эндпоинты из
    выдачи, не дожидаясь починки ноды.

    Пустой результат = фильтр не применять (выключен рубильником, у кредов
    нет node_id, либо ни одна их нода не relay).
    """
    if (os.getenv("SUB_FILTER_TUNNEL_BLIND") or "0").strip().lower() not in (
        "1", "on", "true", "yes",
    ):
        return set()
    node_ids = {c.node_id for c in creds if c.is_active and c.node_id is not None}
    if not node_ids:
        return set()
    rows = (
        db.query(models.RelayExitLink.relay_node_id)
        .filter(models.RelayExitLink.relay_node_id.in_(node_ids))
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


# Эмодзи по протоколу для display-name эндпоинта в клиенте. Различает протокол
# РОЛЬ вместо техножаргона: человеку не нужны слова Reality/XHTTP, ему нужно
# понимать, что пробовать первым, если основное не пошло. Порядок ролей отражает
# реальную картину на флоте:
#   reality   — быстрый и незаметный, но в части регионов ТСПУ его режет;
#   hysteria2 — UDP/QUIC, вытаскивает как раз те регионы, где TCP-Reality лёг;
#   xhttp     — за nginx с LE-сертом, снаружи выглядит обычным сайтом;
#   ws-cdn    — ещё один такой же запасной. Он задумывался «через Cloudflare»,
#               но CF-проксирование для нас мертво (РКН режет CF-leg), и катаем
#               мы его напрямую — то есть особой стойкости в нём НЕТ.
# cred.proto — строка с дефисами.
_PROTO_LABEL = {
    "vless-reality": ("⚡", "Основной"),
    "hysteria2": ("🚀", "Быстрый"),
    "vless-xhttp": ("🛡️", "Запасной"),
    "vless-ws-cdn": ("☁️", "Резервный"),
    "shadowtls": ("🔒", "Резервный"),
}
_DEFAULT_PROTO_LABEL = ("🌐", "Сервер")

# Порядок в списке = порядок, в котором стоит пробовать руками.
_PROTO_ORDER = {
    "vless-reality": 0,
    "hysteria2": 1,
    "vless-xhttp": 2,
    "vless-ws-cdn": 3,
}

# Роль лега = протокол. Один источник правды для набора, подписи и ротации.
LEG_ROLE_BY_PROTO = {
    "vless-reality": "primary",
    "hysteria2": "fast",
    "vless-xhttp": "backup",
    "vless-ws-cdn": "reserve",
}


def sub_leg_scheme() -> str:
    """`legacy` — отдаём все активные креды (как было); `4x1` — только
    опубликованные (по одному протоколу с ноды).

    Флагом, а не миграцией: откат схемы должен стоить перезапуск контейнера, а
    не откат данных.
    """
    return (os.getenv("SUB_LEG_SCHEME") or "legacy").strip().lower()


def _strip_lonely_indices(configs: list[SubLinkConfig]) -> None:
    """Убрать номер у ролей, встречающихся в списке ровно один раз.

    При схеме 4×1 каждая роль живёт на своём сервере, поэтому «Основной 4»
    среди четырёх строк читается как «а где ещё три Основных?». Номер нужен
    только там, где роль дублируется — например после эскалации, когда человеку
    выдан второй «Быстрый» на другой ноде: там цифра снова несёт смысл. И в
    legacy-схеме, где ролей по три-четыре штуки, всё остаётся как было.
    """
    counts: dict[str, int] = {}
    for cfg in configs:
        role = _PROTO_LABEL.get(cfg.protocol, _DEFAULT_PROTO_LABEL)[1]
        counts[role] = counts.get(role, 0) + 1
    for cfg in configs:
        role = _PROTO_LABEL.get(cfg.protocol, _DEFAULT_PROTO_LABEL)[1]
        if counts[role] == 1:
            cfg.uri = _relabel_uri(cfg.uri, cfg.protocol, None)


def _relabel_uri(uri: str, proto: str, index: int | None) -> str:
    """Переписываем #fragment (display-name в клиенте) на «{эмодзи} {роль} N».

    Зачем: имя ноды НЕ должно палить страну (юзеры возмущаются «VPN в РФ» —
    СОРМ/приватность) и НЕ должно быть техножаргоном (Reality/XHTTP). Раньше все
    записи звались одинаково «V8 сервер N», и в списке из двенадцати штук
    человек не мог отличить рабочий вариант от запасного — при ручном выборе
    это означало тыкать наугад. Роль отвечает ровно на этот вопрос.

    ``index`` — номер сервера (не сквозной номер строки), поэтому «Основной 2» и
    «Быстрый 2» — это один и тот же сервер разными протоколами. ``None`` —
    номера нет вовсе: при схеме 4×1 каждая роль живёт на своём сервере, роль в
    списке одна, и цифра рядом с ней не значит ничего — «Основной 4» при
    четырёх строках выглядит так, будто три «Основных» куда-то потерялись.

    RAW UTF-8 (как исходный ``#reality-Russia``), НЕ percent-энкодим: часть
    клиентов кажет %XX буквально. Делается на ОТДАЧЕ сабы (не запекается в
    cred.config_text) → смена стиля не требует bulk-rebuild, только рефреш
    сабы у клиента.
    """
    emoji, role = _PROTO_LABEL.get(proto, _DEFAULT_PROTO_LABEL)
    base = uri.split("#", 1)[0]
    suffix = "" if index is None else f" {index}"
    return f"{base}#{emoji} {role}{suffix}"


def _decrypt_configs(
    creds, *, sub, device_id=None, node_filter=None, tunnel_blind_nodes=None
):
    """Собирает ``SubLinkConfig`` из АКТИВНЫХ кредов, расшифровывая config_text.

    ``node_filter`` — множество допустимых node_id (креды на прочих нодах
    пропускаем); None = без фильтра по нодам. Пустой результат ``_decrypt``
    логируется с полным контекстом (cred/proto/node/device/sub) — раньше
    per-device ветка молча выкидывала недешифруемый кред, и частичная
    деградация подписки (рассинхрон APP_SECRET_KEY, битый config_text) была
    невидима для диагностики.

    ``tunnel_blind_nodes`` — node_id relay-нод: на них выкидываем креды
    протоколов вне ``_SPLIT_TUNNEL_PROTOS``, потому что такой эндпоинт
    отдаёт юзеру коннект вообще без VPN (см. ``_tunnel_blind_node_ids``)."""
    out: list[SubLinkConfig] = []
    # Номер сервера, а не строки: «Основной 2» и «Быстрый 2» должны означать
    # один и тот же сервер разными протоколами — иначе номера в списке из
    # двенадцати строк не значат ничего.
    server_no: dict[int | None, int] = {}
    publish_only = sub_leg_scheme() == "4x1"
    for cred in creds:
        if not cred.is_active:
            continue
        # Схема 4×1: на ноде лежат все протоколы, но в подписку идёт один.
        # getattr — на случай кредов, прочитанных до миграции 0065.
        if publish_only and not getattr(cred, "leg_published", True):
            continue
        if (
            node_filter is not None
            and cred.node_id is not None
            and cred.node_id not in node_filter
        ):
            continue
        if (
            tunnel_blind_nodes
            and cred.node_id in tunnel_blind_nodes
            and cred.proto not in _SPLIT_TUNNEL_PROTOS
        ):
            logger.warning(
                "sub-link: dropping tunnel-blind credential %s (proto=%s) on relay "
                "node %s — this endpoint egresses with the relay's own IP, no VPN "
                "(sub=%s, device=%s)",
                cred.id, cred.proto, cred.node_id, sub.id, device_id,
            )
            continue
        decrypted = _decrypt(cred.config_text)
        if decrypted:
            index = server_no.setdefault(cred.node_id, len(server_no) + 1)
            out.append(SubLinkConfig(
                protocol=cred.proto,
                uri=_relabel_uri(decrypted, cred.proto, index),
                node_id=cred.node_id,
            ))
        else:
            logger.warning(
                "sub-link: decrypt returned empty for credential %s "
                "(proto=%s, node=%s, device=%s, sub=%s)",
                cred.id, cred.proto, cred.node_id, device_id, sub.id,
            )
    # Сверху — то, что стоит пробовать первым. Автовыбор (HAPP lowestdelay) на
    # порядок не смотрит, но при ручном выборе человек тыкает в первую строку, и
    # это должен быть основной протокол, а не случайный резервный.
    out.sort(key=lambda c: _PROTO_ORDER.get(c.protocol, len(_PROTO_ORDER)))
    _strip_lonely_indices(out)
    return out


def _raise_if_sub_not_serviceable(sub: models.Subscription) -> None:
    """Гейт статуса/срока подписки для саб-линка.

    ``frozen`` — ВРЕМЕННАЯ user-initiated пауза (sub_token сохраняется, при
    разморозке alias-блок переклеит старый URL на живого сиблинга) → отдаём
    503+Retry-After, а НЕ 403: многие клиенты на 403 чистят сохранённый
    профиль и перестают опрашивать ссылку, и после пополнения/разморозки
    бесшовного авто-восстановления не происходит. ``blocked``/``expired`` —
    терминальные состояния, 403 корректен (и покрыт тестами)."""
    if sub.status == models.SubscriptionStatus.frozen:
        raise HTTPException(
            status_code=503,
            detail="Subscription temporarily paused — retry shortly",
            headers={"Retry-After": _retry_after_sec()},
        )
    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(status_code=403, detail="Subscription is not active")
    if sub.expires_at and sub.expires_at < utcnow():
        raise HTTPException(status_code=403, detail="Subscription expired")


def _device_label(found) -> str | None:
    device = found.serve_device or found.token_device
    return (device.name if device else None) or None


def _sub_fix_key(request: Request) -> str:
    """Ключ лимита — сам токен из пути (см. докстринг ``sub_fix_action``)."""
    return f"subfix:{request.path_params.get('token', '')}"


@ext_router.post("/sub/{token}")
@limiter.limit("2/minute;6/hour", key_func=_sub_fix_key)
def sub_fix_action(
    request: Request,  # noqa: ARG001 — нужен slowapi key_func
    token: str,
    db: Session = Depends(get_db),
    fix: str | None = None,
    n: str | None = None,
    pay: str | None = None,
):
    """Действие со страницы починки: шаг лестницы или счёт на продление.

    POST, а не GET, намеренно: GET дёргают префетчеры браузера, антивирусы и
    превью-фетчеры, и каждый такой вызов сжигал бы конечный шаг лестницы.
    Тело пустое — CF Worker его не форвардит, поэтому всё в query.

    Лимит ключуется по ТОКЕНУ, а не по IP: у мобильных операторов CGNAT, и
    per-IP лимит выкосил бы половину абонентов МТС одним нетерпеливым
    соседом. GET-выдачу конфигов этот лимит не трогает — он на своём роуте.
    """
    if not sub_fix.page_enabled() or fix != "1":
        return sub_fix.camo_response()
    found = resolve_sub_token(db, token)
    if found is None:
        return sub_fix.camo_response()
    # CSRF: без валидного nonce действие не выполняем и в БД не пишем.
    if not sub_fix.nonce_valid(token, n):
        return sub_fix.render_start(
            found.sub, token,
            device_name=_device_label(found),
            repairable=not found.is_legacy,
        )
    if pay == "1":
        if not sub_fix.pay_enabled():
            return sub_fix.render_start(
                found.sub, token, device_name=_device_label(found)
            )
        return sub_fix.do_pay(db, found, token)
    return sub_fix.do_repair(db, found, token)


@ext_router.get("/sub/{token}")
def dynamic_sub_link(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
    fmt: str | None = None,
    userinfo: str | None = None,
    fix: str | None = None,
    paid: str | None = None,
):
    """Dynamic subscription link — per-device or legacy per-subscription.

    Lookup order:
    1. Device.sub_token → returns only that device's credentials (secure).
    2. Subscription.sub_token → backward compat for clients installed
       before per-device tokens. Returns all active credentials.

    Clients (Hiddify, v2rayNG) poll this URL and auto-update when the
    server changes due to migration. The token is stable across migrations.

    ``?fmt=plain`` отдаёт тело незакодированным — диагностика чтения директив
    из тела. Исходная загадка «блок не появился ни заголовками, ни телом»
    разгадана иначе: ``sub-info-*``/``sub-expire`` — Advanced-параметры Happ и
    без providerid игнорируются при любом способе доставки. Параметр оставлен:
    он позволит проверить plain- против base64-тела уже С providerid. Обычные
    клиенты параметр не шлют — поведение для них не меняется ни на байт.

    ``?userinfo=expire`` — той же природы диагностика: userinfo без
    трафик-ключей. Гипотеза — Happ перестанет рисовать шкалу-пустышку
    «0B/∞» (данных о трафике мы не собираем), сохранив дату «Истекает».
    Подтвердится на устройстве → кандидат на прод-дефолт для UA Happ
    (Hiddify неполный набор выбрасывает целиком, ему нельзя).
    """
    # ── Страница починки (?fix=1) ──────────────────────────────────────
    # Ветвление ДО _raise_if_sub_not_serviceable, _mark_first_config_fetch и
    # аудита выдачи: открытие страницы ничего не пишет в БД и не отравляет
    # онбординг-воронку («первая выдача конфига»), а истёкшей подписке нужен
    # экран продления, а не 403.
    if fix == "1" and sub_fix.page_enabled() and sub_fix.wants_html(request):
        found = resolve_sub_token(db, token)
        if found is None:
            return sub_fix.camo_response()
        sub = found.sub
        if paid is not None:
            # str, а не int: типизированный параметр заставлял бы FastAPI
            # отвечать 422-JSON на ЛЮБОЙ нечисловой ?paid= — на горячем
            # роуте выдачи конфигов, независимо от флагов страницы.
            try:
                invoice_id = int(paid)
            except (TypeError, ValueError):
                return sub_fix.camo_response()
            return sub_fix.render_paid_state(db, found, token, invoice_id)
        if sub.status == models.SubscriptionStatus.expired or (
            sub.expires_at and sub.expires_at < utcnow()
        ):
            return sub_fix.render_expired(sub, token)
        if sub.status != models.SubscriptionStatus.active:
            return sub_fix.render_inactive(sub, token)
        return sub_fix.render_start(
            sub, token,
            device_name=_device_label(found),
            repairable=not found.is_legacy,
        )

    # ── Per-device lookup (preferred) ──────────────────────────────────
    # Сам lookup (включая seamless-alias на живого соседа) — в
    # resolve_sub_token: им же пользуется страница починки на саб-домене.
    found = resolve_sub_token(db, token)
    if found is not None and not found.is_legacy:
        device = found.token_device
        sub = found.sub
        _raise_if_sub_not_serviceable(sub)
        source_device = found.serve_device

        # Исключаем креды нод в cooldown/декоммишене/с низким health_score —
        # иначе клиент держит заведомо мёртвый эндпоинт в ротации client-side
        # failover (лишние таймауты на каждом реконнекте).
        healthy = _healthy_node_ids(db, source_device.credentials)
        blind = _tunnel_blind_node_ids(db, source_device.credentials)
        configs = _decrypt_configs(
            source_device.credentials,
            sub=sub,
            device_id=source_device.id,
            node_filter=healthy or None,
            tunnel_blind_nodes=blind,
        )
        # Fallback: фильтр по здоровью выкинул все креды (единственная нода в
        # cooldown) → лучше отдать живой-но-неоптимальный набор, чем 503.
        # tunnel-blind фильтр здесь СОХРАНЯЕМ: эндпоинт без VPN — это не
        # «неоптимально», это не тот продукт, за который платят.
        if not configs and healthy:
            configs = _decrypt_configs(
                source_device.credentials,
                sub=sub,
                device_id=source_device.id,
                tunnel_blind_nodes=blind,
            )
        # Последняя ступень: у юзера НЕТ ни одного туннелирующего эндпоинта.
        # Отдаём как есть — 503 навсегда оставил бы его вообще без связи, —
        # но кричим в лог: это состояние чинится снятием hy2-конфигов с
        # relay-нод или доведением hy2 до настоящего split-tunnel.
        if not configs and blind:
            logger.error(
                "sub-link: sub=%s device=%s has ONLY tunnel-blind endpoints on relay "
                "nodes — serving them anyway, but this user gets no VPN",
                sub.id, source_device.id,
            )
            configs = _decrypt_configs(
                source_device.credentials, sub=sub, device_id=source_device.id
            )

        # Safety net: if neither the direct device nor its alias
        # yielded a single working config, return 503 instead of an
        # empty 200. Empty-200 = "subscription with zero servers" and
        # most clients will *overwrite* the local cached profile with
        # nothing, stranding the user. 503 tells the client to retry
        # and keeps the last-known-good profile in place. Retry-After даёт
        # клиенту хинт перезапросить через ~минуту (провижининг обычно
        # укладывается в секунды-минуты), а не ждать planовый refresh.
        if not configs:
            logger.warning(
                "sub-link: no active configs for token=%s sub=%s device=%s (source=%s)",
                token, sub.id, device.id, source_device.id,
            )
            raise HTTPException(
                status_code=503,
                detail="No active endpoints — provisioning in progress, retry shortly",
                headers={"Retry-After": _retry_after_sec()},
            )

        _mark_first_config_fetch(db, sub)

        # Read-путь ничего, кроме audit-строки, не пишет — при сэмплировании
        # (SUB_FETCH_AUDIT_SAMPLE>1) просто пропускаем и INSERT, и COMMIT.
        if _should_log_sub_fetch():
            db.add(
                models.AuditLog(
                    actor=str(sub.user_id),
                    actor_type=models.AuditActor.user,
                    action="subscription_fetch",
                    target_type="device",
                    target_id=device.id,
                    extra={
                        "protocols": [c.protocol for c in configs],
                        "device_token": True,
                        "aliased_to_device_id": (
                            source_device.id if source_device.id != device.id else None
                        ),
                    },
                )
            )
            db.commit()

        encoded = _sub_body(
            configs, sub, plain=(fmt == "plain"), token=token,
            client=_client_kind(request.headers.get("user-agent")),
        )
        return PlainTextResponse(
            content=encoded,
            media_type="text/plain",
            # device = владелец токена (для гейта «только новые девайсы» по created_at)
            headers=_sub_response_headers(
                sub, token, device,
                userinfo_expire_only=(userinfo == "expire"),
                user_agent=request.headers.get("user-agent"),
            ),
        )

    # ── Legacy per-subscription fallback ───────────────────────────────
    if found is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    sub = found.sub

    _raise_if_sub_not_serviceable(sub)

    # Как и в per-device ветке: отсекаем креды нездоровых нод и логируем пустой
    # decrypt (общий хелпер).
    healthy = _healthy_node_ids(db, sub.credentials)
    blind = _tunnel_blind_node_ids(db, sub.credentials)
    configs = _decrypt_configs(
        sub.credentials,
        sub=sub,
        node_filter=healthy or None,
        tunnel_blind_nodes=blind,
    )
    if not configs and healthy:
        configs = _decrypt_configs(sub.credentials, sub=sub, tunnel_blind_nodes=blind)
    if not configs and blind:
        logger.error(
            "sub-link: legacy sub=%s has ONLY tunnel-blind endpoints on relay nodes "
            "— serving them anyway, but this user gets no VPN",
            sub.id,
        )
        configs = _decrypt_configs(sub.credentials, sub=sub)

    # Same safety as the per-device branch — see the invariant box
    # above. Empty-200 would wipe the user's cached profile.
    if not configs:
        logger.warning(
            "sub-link: legacy sub-token has no active configs sub=%s token=%s",
            sub.id, token,
        )
        raise HTTPException(
            status_code=503,
            detail="No active endpoints — provisioning in progress, retry shortly",
            headers={"Retry-After": _retry_after_sec()},
        )

    encoded = _sub_body(
            configs, sub, plain=(fmt == "plain"), token=token,
            client=_client_kind(request.headers.get("user-agent")),
        )

    _mark_first_config_fetch(db, sub)

    # См. per-device ветку: сэмплируем горячую audit-запись (аудит #247).
    if _should_log_sub_fetch():
        db.add(
            models.AuditLog(
                actor=str(sub.user_id),
                actor_type=models.AuditActor.user,
                action="subscription_fetch",
                target_type="subscription",
                target_id=sub.id,
                extra={"protocols": [c.protocol for c in configs], "legacy_token": True},
            )
        )
        db.commit()

    return PlainTextResponse(
        content=encoded,
        media_type="text/plain",
        headers=_sub_response_headers(
            sub, token, userinfo_expire_only=(userinfo == "expire"),
            user_agent=request.headers.get("user-agent"),
        ),
    )


# ── Auto-renew toggle ──

class AutoRenewRequest(BaseModel):
    auto_renew: bool


@ext_router.post("/subscriptions/{subscription_id}/auto_renew")
def toggle_auto_renew(
    subscription_id: int,
    body: AutoRenewRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    sub.auto_renew = body.auto_renew
    db.add(sub)
    db.commit()
    return {"ok": True, "auto_renew": sub.auto_renew}


# ── Referral system ──

class ReferralCodeRequest(BaseModel):
    telegram_id: str


class ReferralCodeOut(BaseModel):
    code: str
    uses: int
    bonus_days: int
    reward_days: int


@ext_router.post("/referral/code", response_model=ReferralCodeOut)
def get_or_create_referral(
    body: ReferralCodeRequest,
    db: Session = Depends(get_db),
    admin_token: str | None = Depends(optional_admin),
):
    """Get existing referral code or create a new one for the user."""
    user = db.query(models.User).filter_by(telegram_id=body.telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    ref = ensure_referral_code(db, user)
    db.commit()
    db.refresh(ref)
    return ReferralCodeOut(
        code=ref.code, uses=ref.uses,
        bonus_days=ref.bonus_days, reward_days=ref.reward_days,
    )


# ── User registration with referral ──

_SOURCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Внутренние start-ключи бота/вебаппа — НЕ рекламные метки (иначе, напр.,
# t.me/bot?start=support из вебаппа попал бы в рекламную воронку). ``ref_*``
# отсекается отдельно (это реферал). Пополнять при новых служебных deep-link'ах.
# ``renew`` — кнопка «Продлить» из блока статуса в VPN-клиенте (см.
# _renew_link). Без него метка утекла бы в рекламную воронку и отравила
# статистику каналов десятками фальшивых «переходов по рекламе».
_RESERVED_SOURCES = frozenset({"support", "renew"})


def _clean_source(raw: str | None) -> str | None:
    """Рекламная метка из deep-link: только Telegram-допустимые символы старт-
    параметра (``[A-Za-z0-9_-]``, ≤64), не служебный ключ. Мусор/пусто/служебное
    → None (не пишем)."""
    if not raw:
        return None
    raw = raw.strip()
    low = raw.lower()  # служебные ключи отсекаем регистронезависимо
    if low in _RESERVED_SOURCES or low.startswith("ref_"):
        return None
    return raw if _SOURCE_RE.match(raw) else None


class UserRegisterRequest(BaseModel):
    telegram_id: str
    referral_code: str | None = None
    source: str | None = None  # рекламная метка (first-touch)


@ext_router.post("/users/register")
@limiter.limit("10/minute")
def register_user(
    request: Request,
    body: UserRegisterRequest,
    db: Session = Depends(get_db),
    admin_token: str | None = Depends(optional_admin),
):
    """Register or get user, optionally applying a referral code."""
    user = db.query(models.User).filter_by(telegram_id=body.telegram_id).first()
    created = False
    if not user:
        user = models.User(telegram_id=body.telegram_id)
        db.add(user)
        db.flush()
        created = True

    # Trial-flow rework: /register no longer credits bonuses. It only
    # attaches the referral when the field is still NULL — this means a
    # user who accidentally /start'ed before ever getting a ref link can
    # still be attributed the next time they click one. The actual
    # referee-side bonus is handed out on /trial/activate, and the
    # referrer-side bonus lands on the first confirmed kind=topup (see
    # _mark_invoice_paid_core in api.py).
    if body.referral_code and not user.referred_by_id:
        ref = (
            db.query(models.ReferralCode)
            .filter_by(code=body.referral_code, is_active=True)
            .first()
        )
        if ref and ref.owner_id != user.id:
            if ref.max_uses is None or ref.uses < ref.max_uses:
                user.referred_by_id = ref.owner_id
                ref.uses += 1
                db.add(ref)
                db.flush()

    # Рекламная метка (first-touch): ставим один раз, если ещё не задана —
    # как и referral, чтобы органик-старт без метки не блокировал атрибуцию
    # при последующем заходе по рекламной ссылке.
    if not user.source:
        cleaned = _clean_source(body.source)
        if cleaned:
            # Управляемая AdLink выключена → ссылку «погасили», новые заходы не
            # атрибутируем. Неизвестная метка (ad-hoc, без AdLink) — атрибутируем.
            link = db.query(models.AdLink).filter_by(tag=cleaned).first()
            if link is None or link.is_active:
                user.source = cleaned
                db.flush()

    db.commit()
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "created": created,
        # Always false under the trial-flow model — retained for
        # backward compat with older bot builds that still read it.
        "referral_bonus_credited": False,
        # Bot reads this to decide whether to include the "first month
        # on us" line in the welcome copy. True iff the user hasn't
        # activated their trial yet — works retroactively for users
        # who registered before this column existed.
        "trial_available": user.trial_activated_at is None,
        # Онбординг-роадмап E3.3: бот прячет кнопку «🆘 VPN не работает» у
        # тех, кому нечего чинить. Раньше она висела у всех с первого экрана
        # и вела в тупик «У тебя нет активной подписки. Оформить — /buy»
        # (команды /buy не существует). Считаем по живым девайсам, а не по
        # подписке: чинить можно только выданное устройство.
        "has_devices": (
            db.query(models.Device.id)
            .filter(
                models.Device.user_id == user.id,
                models.Device.status == models.DeviceStatus.active,
            )
            .first()
            is not None
        ),
    }


# ── Ad-source funnel (admin) ──

class AdSourceRow(BaseModel):
    source: str
    started: int  # юзеров пришло с метки
    trial: int  # из них активировали триал (trial_activated_at)
    paid: int  # из них сделали ≥1 реальную оплату (topup)
    revenue_kopecks: int  # суммарная выручка с этой метки (topup'ы)


class AdSourcesResponse(BaseModel):
    sources: list[AdSourceRow]
    total_started: int
    total_paid: int
    total_revenue_kopecks: int


def _compute_source_funnel(db: Session) -> dict[str, dict[str, int]]:
    """``{source: {started, trial, paid, revenue_kopecks}}``. ДВА запроса с
    мержем — join транзакций к users раздул бы ``started`` кратно числу транзакций
    юзера (join-fanout). Переиспользуется и воронкой, и листингом AdLink."""
    base = (
        db.query(
            models.User.source.label("source"),
            func.count(models.User.id).label("started"),
            func.count(models.User.trial_activated_at).label("trial"),
        )
        .filter(models.User.source.isnot(None))
        .group_by(models.User.source)
        .all()
    )
    paid = (
        db.query(
            models.User.source.label("source"),
            func.count(func.distinct(models.BalanceTransaction.user_id)).label("paid"),
            func.coalesce(func.sum(models.BalanceTransaction.amount_kopecks), 0).label("revenue"),
        )
        .join(
            models.BalanceTransaction,
            models.BalanceTransaction.user_id == models.User.id,
        )
        .filter(
            models.User.source.isnot(None),
            models.BalanceTransaction.kind == models.BalanceTxKind.topup,
        )
        .group_by(models.User.source)
        .all()
    )
    paid_map = {r.source: (int(r.paid), int(r.revenue or 0)) for r in paid}
    out: dict[str, dict[str, int]] = {}
    for r in base:
        p, rev = paid_map.get(r.source, (0, 0))
        out[r.source] = {
            "started": int(r.started), "trial": int(r.trial),
            "paid": p, "revenue_kopecks": rev,
        }
    return out


@ext_router.get("/admin/ad-sources", response_model=AdSourcesResponse)
def ad_sources_funnel(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Воронка по ВСЕМ рекламным меткам (включая ad-hoc без управляемой AdLink)."""
    funnel = _compute_source_funnel(db)
    rows = [
        AdSourceRow(
            source=src, started=f["started"], trial=f["trial"],
            paid=f["paid"], revenue_kopecks=f["revenue_kopecks"],
        )
        for src, f in funnel.items()
    ]
    rows.sort(key=lambda x: x.started, reverse=True)
    return AdSourcesResponse(
        sources=rows,
        total_started=sum(x.started for x in rows),
        total_paid=sum(x.paid for x in rows),
        total_revenue_kopecks=sum(x.revenue_kopecks for x in rows),
    )


class FunnelStep(BaseModel):
    key: str
    label: str
    # None, когда шаг НЕизмерим (нет данных телеметрии) — это принципиально
    # иное состояние, чем 0, и UI обязан показать его словами, а не полосой.
    count: int | None
    denominator: int
    pct: float | None
    measurable: bool


class OnboardingFunnelResponse(BaseModel):
    days: int | None
    total: int
    # Сколько юзеров когорты пришли ПОСЛЕ включения телеметрии (2026-07-25).
    # 0 — про шаг «открыли кабинет» не известно ничего.
    telemetry_cohort: int
    steps: list[FunnelStep]
    losses: list[FunnelStep]
    trial_failures: int


@ext_router.get("/admin/onboarding-funnel", response_model=OnboardingFunnelResponse)
def onboarding_funnel(
    days: int = 7,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Воронка нового юзера: бот → кабинет → триал → ссылка → оплата.

    Считает тот же сервис, что и CLI (`scripts/onboarding_funnel.py`), чтобы
    цифры в админке и в консоли не разъезжались. ``days=0`` — за всё время.
    """
    from .services import onboarding_funnel as funnel_svc

    return funnel_svc.compute(db, days or None)


# ── Ad links (управляемые рекламные deep-link'и, admin) ──

class AdLinkCreate(BaseModel):
    name: str
    tag: str | None = None  # пусто → сгенерим ad_<random>
    notes: str | None = None
    # Во сколько обошлось размещение (копейки). Без него воронка не отвечает
    # на «окупилось ли»; NULL = бесплатно (обмен, свой канал).
    cost_kopecks: int | None = None


class AdLinkUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    notes: str | None = None
    cost_kopecks: int | None = None


class AdLinkOut(BaseModel):
    id: int
    name: str
    tag: str
    is_active: bool
    notes: str | None
    created_at: datetime
    share_url: str | None
    # воронка по этой метке
    started: int
    trial: int
    paid: int
    revenue_kopecks: int
    cost_kopecks: int | None
    # Цена платящего клиента по этой метке. None, если затрат нет или ещё никто
    # не заплатил — делить не на что.
    cac_kopecks: int | None
    # Во сколько раз выручка перекрыла затраты. <1 — канал убыточен.
    roi: float | None


def _ad_link_share_url(tag: str) -> str | None:
    bot = os.getenv("BOT_USERNAME")
    return f"https://t.me/{bot}?start={tag}" if bot else None


def _ad_link_out(link: models.AdLink, funnel: dict[str, dict[str, int]]) -> AdLinkOut:
    f = funnel.get(link.tag) or {}
    paid = f.get("paid", 0)
    revenue = f.get("revenue_kopecks", 0)
    cost = link.cost_kopecks
    return AdLinkOut(
        id=link.id, name=link.name, tag=link.tag, is_active=link.is_active,
        notes=link.notes, created_at=link.created_at,
        share_url=_ad_link_share_url(link.tag),
        started=f.get("started", 0), trial=f.get("trial", 0),
        paid=paid, revenue_kopecks=revenue,
        cost_kopecks=cost,
        cac_kopecks=int(round(cost / paid)) if cost and paid else None,
        roi=round(revenue / cost, 2) if cost else None,
    )


@ext_router.get("/admin/ad-links", response_model=list[AdLinkOut])
def list_ad_links(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Список управляемых рекламных ссылок + живая воронка по каждой."""
    funnel = _compute_source_funnel(db)
    links = db.query(models.AdLink).order_by(models.AdLink.created_at.desc()).all()
    return [_ad_link_out(link, funnel) for link in links]


@ext_router.post("/admin/ad-links", response_model=AdLinkOut)
def create_ad_link(
    body: AdLinkCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Завести рекламную ссылку. tag валидируется как source-метка; пустой → ad_<random>."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name обязателен")
    raw_tag = (body.tag or "").strip() or f"ad_{secrets.token_urlsafe(6)}"
    tag = _clean_source(raw_tag)
    if not tag:
        raise HTTPException(
            status_code=400,
            detail="tag недопустим: только [A-Za-z0-9_-], ≤64, не служебный (ref_/support)",
        )
    if db.query(models.AdLink).filter_by(tag=tag).first():
        raise HTTPException(status_code=409, detail=f"ссылка с меткой '{tag}' уже существует")
    link = models.AdLink(
        name=name, tag=tag, notes=(body.notes or None),
        cost_kopecks=body.cost_kopecks,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return _ad_link_out(link, _compute_source_funnel(db))


@ext_router.patch("/admin/ad-links/{link_id}", response_model=AdLinkOut)
def update_ad_link(
    link_id: int,
    body: AdLinkUpdate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Правка ярлыка/заметок и вкл/выкл (tag неизменяем — иначе осиротит стату)."""
    link = db.get(models.AdLink, link_id)
    if not link:
        raise HTTPException(status_code=404, detail="ad link not found")
    if body.name is not None:
        nm = body.name.strip()
        if not nm:
            raise HTTPException(status_code=400, detail="name не может быть пустым")
        link.name = nm
    if body.is_active is not None:
        link.is_active = body.is_active
    if body.notes is not None:
        link.notes = body.notes or None
    if body.cost_kopecks is not None:
        # 0 трактуем как «бесплатное размещение» и храним NULL: иначе CAC делил
        # бы на ноль, а ROI показывал бесконечность.
        link.cost_kopecks = body.cost_kopecks or None
    db.commit()
    db.refresh(link)
    return _ad_link_out(link, _compute_source_funnel(db))


@ext_router.delete("/admin/ad-links/{link_id}", status_code=204)
def delete_ad_link(
    link_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Удалить управляемую ссылку. Историческая атрибуция в User.source НЕ
    трогается (метка останется в /ad-sources как ad-hoc, но без ярлыка)."""
    link = db.get(models.AdLink, link_id)
    if not link:
        raise HTTPException(status_code=404, detail="ad link not found")
    db.delete(link)
    db.commit()


# ── Free trial activation ──

class TrialActivateRequest(BaseModel):
    telegram_id: str


class TrialActivateResponse(BaseModel):
    trial_amount_kopecks: int
    referral_bonus_kopecks: int
    balance_kopecks: int
    trial_expires_at: str  # ISO-8601, serialized from datetime


@ext_router.post("/trial/activate", response_model=TrialActivateResponse)
@limiter.limit("10/minute")
def activate_trial(
    request: Request,
    body: TrialActivateRequest,
    db: Session = Depends(get_db),
    admin_token: str | None = Depends(optional_admin),
):
    """Grant the one-time trial bonus to a user identified by telegram_id.

    Gated on ``User.trial_activated_at IS NULL``. Returns 409 on a
    repeat call so the client knows to hide the banner, 404 if the
    user row doesn't exist yet (the bot should /register first), and
    503 if no visible 30-day plan is configured (nothing to size the
    trial against). Bonus amounts read from the Plan table at call
    time — no hardcoded rubles.
    """
    from .services import trial as trial_svc

    user = db.query(models.User).filter_by(telegram_id=body.telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        result = trial_svc.activate_trial(db, user.id)
    except trial_svc.TrialAlreadyActivated:
        raise HTTPException(status_code=409, detail="Trial already activated")
    except trial_svc.NoTrialPlan:
        raise HTTPException(status_code=503, detail="No trial plan configured")
    db.commit()
    return TrialActivateResponse(
        trial_amount_kopecks=result.trial_amount_kopecks,
        referral_bonus_kopecks=result.referral_bonus_kopecks,
        balance_kopecks=result.balance_kopecks,
        trial_expires_at=result.trial_expires_at.isoformat(),
    )


# ── Self-service: regenerate config ──

@ext_router.post("/users/by_telegram/{telegram_id}/regenerate")
def regenerate_user_config(
    telegram_id: str,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Move the user's active subscription to a freshly chosen node,
    preserving every device and its sub-link (device-preserving self-service
    migration)."""
    from .services.provisioning import ProvisioningOrchestrator

    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    active_sub = (
        db.query(models.Subscription)
        .filter(
            models.Subscription.user_id == user.id,
            models.Subscription.status == models.SubscriptionStatus.active,
        )
        .order_by(models.Subscription.created_at.desc())
        .first()
    )
    if not active_sub:
        raise HTTPException(status_code=404, detail="No active subscription")

    orchestrator = ProvisioningOrchestrator(db)

    # Device-preserving self-service node move. The old body revoked EVERY
    # device, provisioned a brand-new single-device subscription and blocked
    # the old one — collapsing an N-device sub to one "primary", rerolling the
    # sub-link, and dropping device rows. That breaks the sub-link invariant
    # (revoked rows must survive as aliases) and was the source of the
    # "all devices deleted, one primary" report. migrate_subscription_to_new_node
    # relocates the SAME subscription to a freshly chosen node, mirroring every
    # live device N:N and preserving sub_token + connection_uri — installed
    # links keep working and no device is lost.
    try:
        target, _device, task = orchestrator.migrate_subscription_to_new_node(
            active_sub
        )
    except RuntimeError as exc:
        # No eligible target node (single-node pool, all excluded, no healthy
        # node in the plan's pools, …). Surface as 409 so the bot shows
        # "couldn't regenerate, try later" instead of a 500.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "ok": True,
        "subscription_id": active_sub.id,
        "node_id": target.id,
        "task_id": task.id,
    }


# ── Notification delivery system ──

class NotificationOut(BaseModel):
    id: int
    telegram_id: str
    text: str
    type: str = "info"
    # Optional context the bot needs to build inline keyboards. Currently
    # used by Phase C health-ping prompts so the callback data can carry
    # the subscription ID — leave NULL for notification types that don't
    # need it (renewal_reminder, expiry_reminder, ...).
    subscription_id: int | None = None
    # Diagnose-incident push (admin_alert_node_diagnosis): carries the
    # target so the bot can build the ack / mute / follow keyboard
    # (callback ``diag:<action>:<kind>:<id>``).
    target_kind: str | None = None
    target_id: int | None = None


# Классы уведомлений, которые поллер бота забирает и доставляет. Список закрытый:
# admin-алерт с kind'ом, которого тут нет, молча осядет в audit_logs и до админа
# не доедет. Вынесен на уровень модуля, чтобы это можно было проверить тестом.
ADMIN_NOTIFICATION_ACTIONS = [
    "renewal_reminder", "expiry_reminder",
    "renewal_reminder_1d", "expiry_reminder_1d",
    "config_ready", "migration_notice", "sublink_rotated",
    "low_balance_warning", "trial_expiry_warning", "health_ping_request",
    # Приглашение позвать друга — шлём ОДИН раз, сразу после первого скачивания
    # конфига (см. _mark_first_config_fetch).
    "referral_invite",
    # Admin push-уведомления (см. services/admin_notify.py).
    # Текст полностью рендерится на backend-е и кладётся в
    # extra["text"] — бот отдаёт as-is, без собственного
    # форматирования по kind.
    "admin_alert_user_report",
    "admin_alert_infra_ssh",
    "admin_alert_infra_dlq",
    # Speaking node/exit diagnosis push (diagnostics overhaul) — text is
    # rendered in services/admin_notify.notify_node_diagnosis; the bot
    # attaches the ack/mute/follow keyboard from target_kind/target_id.
    "admin_alert_node_diagnosis",
    # Дрейф версий xray: вышел новый релиз / ноды отстали от пина в роли
    # (services/xray_releases.py). Без строки в этом списке пуш молча
    # оседал бы в audit_logs и до админа не доезжал.
    "admin_alert_xray_version_drift",
    "admin_alert_leg_gap",
]


@ext_router.get("/notifications/pending", response_model=list[NotificationOut])



def get_pending_notifications(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    limit: int = 20,
):
    """Return pending bot notifications (config-ready, migration, renewals).

    The bot polls this endpoint and delivers messages to users. Notifications
    are generated by the worker (provisioning callbacks, renewal checks,
    health-triggered migrations) and stored in AuditLog with special actions.
    """
    # Sharing-enforcer notifications (sharing_warning/kick/block) are
    # intentionally absent here — the v2 enforcer is currently off
    # (see SHARING_ENFORCEMENT_ENABLED in traffic_stats.py). Dropping
    # them from the poller list also suppresses delivery of any
    # backlog rows that were written before the gate landed, so
    # nobody gets a stale "we caught you sharing" ping.
    notif_actions = ADMIN_NOTIFICATION_ACTIONS
    # Admin broadcast — массовая рассылка (см. /admin/broadcasts). Держим её в
    # ОТДЕЛЬНОМ, низкоприоритетном классе: диспетчер наполняет её батчами по
    # BROADCAST_BATCH_SIZE=50/тик, а поллер сливает 20/тик — при общей очереди с
    # DESC-сортировкой массовая рассылка топила срочные транзакционные пуши
    # (config_ready, «истекает завтра», migration_notice, health_ping) в хвост
    # на десятки минут. Разделяем на priority + bulk и доставляем FIFO (asc):
    # сперва все срочные (до limit), остаток добиваем broadcast'ом.
    bulk_actions = ["admin_broadcast"]

    def _fetch(actions: list[str], lim: int) -> list[models.AuditLog]:
        if lim <= 0:
            return []
        return (
            db.query(models.AuditLog)
            .filter(
                models.AuditLog.action.in_(actions),
                models.AuditLog.actor_type == models.AuditActor.system,
            )
            .order_by(models.AuditLog.created_at.asc())  # FIFO — честный порядок
            .limit(lim)
            .all()
        )

    priority_logs = _fetch(notif_actions, limit)
    # Добиваем свободные слоты массовой рассылкой (не даём ей вытеснить срочные).
    bulk_logs = _fetch(bulk_actions, limit - len(priority_logs))
    logs = priority_logs + bulk_logs

    results = []
    for log in logs:
        extra = log.extra or {}
        telegram_id = extra.get("telegram_id")
        if not telegram_id:
            continue

        if log.action == "renewal_reminder":
            text = (
                "⏰ Подписка истекает через 3 дня!\n"
                f"Дата: {extra.get('expires_at', '?')[:10]}\n"
                "Пополни баланс, чтобы автопродление сработало."
            )
        elif log.action == "renewal_reminder_1d":
            text = (
                "🚨 Подписка истекает завтра!\n"
                f"Дата: {extra.get('expires_at', '?')[:10]}\n"
                "Пополни баланс сейчас — иначе подписка отключится."
            )
        elif log.action == "expiry_reminder":
            text = (
                "⚠️ Твоя подписка истекает через 3 дня.\n"
                f"Дата: {extra.get('expires_at', '?')[:10]}\n"
                "Используй /renew или /plans для продления."
            )
        elif log.action == "expiry_reminder_1d":
            text = (
                "🚨 Подписка истекает завтра!\n"
                f"Дата: {extra.get('expires_at', '?')[:10]}\n"
                "Продли сейчас через /renew, иначе VPN отключится."
            )
        elif log.action == "config_ready":
            sub_uri = extra.get("sub_uri")
            text = (
                "✅ Конфиг VPN готов!\n"
                + (f"Ссылка: {sub_uri}\n" if sub_uri else "")
            )
        elif log.action == "referral_invite":
            # Шлётся один раз — после того, как человек впервые скачал конфиг.
            #
            # Ссылку зовём отправить лично, а не постить публично: публикация
            # ссылки на VPN-бота — состав по ч.18 ст.14.3 КоАП (в январе 2026
            # за такое оштрафовали владельца Telegram-канала), и отвечает
            # разместивший, то есть наш же пользователь. Прямо про закон в
            # тексте не пишем — не пугаем, просто не предлагаем публичность.
            ref_url = extra.get("share_url")
            reward_days = extra.get("reward_days")
            reward_line = (
                f"Если приведёшь друга — получишь {_plural_days(reward_days)} "
                "подписки, когда он оплатит.\n\n"
                if reward_days
                else ""
            )
            text = (
                "Готово, VPN работает 🎉\n\n"
                + reward_line
                + (f"Ссылка для друзей:\n{ref_url}" if ref_url else "")
            )
        elif log.action == "migration_notice":
            # Намеренно без URL. Подписочная ссылка, лежащая в профиле
            # Hiddify/V2rayNG, продолжает резолвиться после миграции
            # через sibling-alias в /sub/{token} (см. api_extensions.py
            # «Seamless-migration alias»). Юзеру достаточно дёрнуть
            # refresh в клиенте. Раньше в сообщении был /api/sub/…,
            # но это путало — люди копировали его и импортировали
            # заново вместо того, чтобы нажать 🔄.
            text = (
                "🔄 Твой VPN-сервер переехал.\n"
                "Подписка обновится в клиенте автоматически — "
                "просто нажми 🔄 рядом с профилем в Hiddify / V2rayNG / Streisand.\n"
                "Ничего переустанавливать и копировать не нужно."
            )
        elif log.action == "sublink_rotated":
            # Перегенерация sub-link (admin bulk-regenerate, обычно хвосты
            # аварии). В отличие от migration_notice здесь sub_token
            # СМЕНИЛСЯ — авто-refresh в клиенте подтянет конфиг по старой
            # ссылке через sibling-alias, но в ЛК уже лежит новая ссылка,
            # и правильнее переподключиться по ней. Старый конфиг пока
            # продолжает работать, так что без паники и без обрыва.
            text = (
                "🔁 Мы обновили твой VPN-конфиг.\n"
                "Чтобы всё продолжило работать без перебоев — открой "
                "личный кабинет и возьми оттуда новую ссылку.\n"
                "Старый конфиг ещё работает, но лучше обновиться сейчас."
            )
        elif log.action == "low_balance_warning":
            days = extra.get("days_remaining", "?")
            balance_rub = extra.get("balance_rub", "?")
            text = (
                f"⚠️ Низкий баланс: {balance_rub} ₽ — хватит на {days} дн.\n"
                "Пополни через /balance, иначе подписка отключится."
            )
        elif log.action == "trial_expiry_warning":
            text = (
                "⏳ Твой пробный месяц кончается через 3 дня.\n"
                "Пополни баланс, чтобы подписка не отключилась — "
                "реферальные 50 ₽ (если есть) остаются при тебе."
            )
        elif log.action == "health_ping_request":
            text = (
                "🛟 Помогите нам улучшить сервис!\n\n"
                "Подскажите, как сейчас работает VPN на вашем "
                "устройстве? Это займёт одну секунду и поможет нам "
                "быстрее находить и устранять проблемы.\n\n"
                "Спасибо, что вы с нами! 💛"
            )
        elif log.action.startswith("admin_alert_") or log.action == "admin_broadcast":
            # Текст готов на backend-е в hook-site (submit_health_ping_response,
            # run_relay_link_health_tick, dlq_exception_handler, а для
            # broadcast — в POST /api/broadcasts). Если extra.text пуст —
            # AuditLog-строка битая, тихо пропускаем.
            text = extra.get("text") or ""
            if not text:
                continue
        # elif log.action == "sharing_warning":
        #     text = (
        #         "Привет! 👋 Мы заметили, что к твоему аккаунту "
        #         "подключаются с нескольких устройств одновременно. "
        #         "Напоминаем, что передача конфигурации другим людям "
        #         "запрещена правилами сервиса — это влияет на качество "
        #         "и скорость для всех пользователей. Если это ошибка — "
        #         "просто проигнорируй это сообщение."
        #     )
        # elif log.action == "sharing_kick":
        #     text = (
        #         "Привет! Нам очень жаль, но мы снова зафиксировали "
        #         "одновременное подключение к твоему аккаунту с нескольких "
        #         "устройств. Соединение было временно разорвано. Передача "
        #         "конфигурации снижает качество сервиса для всех, поэтому "
        #         "мы вынуждены реагировать. Пожалуйста, убедись, что "
        #         "конфигурацию используешь только ты."
        #     )
        # elif log.action == "sharing_block":
        #     text = (
        #         "Привет 😔 Нам очень жаль, но мы вынуждены временно "
        #         "заблокировать доступ к VPN — мы зафиксировали "
        #         "систематическое использование аккаунта с нескольких "
        #         "устройств. Это негативно влияет на качество услуги "
        #         "для других пользователей, поэтому мы не можем это "
        #         "игнорировать. Напиши в поддержку — мы разберёмся "
        #         "и поможем восстановить доступ."
        #     )
        else:
            continue

        sub_id_extra: int | None = None
        # health_ping_request always carries subscription_id in extra.
        # Other notification types might too (e.g. config_ready) but we
        # only forward it for the ones the bot needs it for, to keep
        # the keyboard-routing logic on the bot side simple.
        if log.action == "health_ping_request":
            raw = extra.get("subscription_id")
            if isinstance(raw, int):
                sub_id_extra = raw

        # Diagnose-incident push: forward target so the bot can build the
        # ack / mute / follow inline keyboard.
        target_kind_extra: str | None = None
        target_id_extra: int | None = None
        if log.action == "admin_alert_node_diagnosis":
            tk = extra.get("target_kind")
            ti = extra.get("target_id")
            if isinstance(tk, str) and isinstance(ti, int):
                target_kind_extra = tk
                target_id_extra = ti

        results.append(NotificationOut(
            id=log.id,
            telegram_id=telegram_id,
            text=text,
            type=log.action,
            subscription_id=sub_id_extra,
            target_kind=target_kind_extra,
            target_id=target_id_extra,
        ))

    return results


# ── Phase C: bot health-ping responses + opt-out ──

class HealthPingResponseRequest(BaseModel):
    telegram_id: str
    subscription_id: int | None = None
    answer: str  # "ok" | "bad"
    # "prompted" = answer to scheduled health-ping bot message,
    # "self_reported" = user pressed "VPN doesn't work" button themselves.
    # Legacy clients don't send this → default to "prompted".
    source: str | None = None


@ext_router.post("/users/health-ping-response")
def submit_health_ping_response(
    body: HealthPingResponseRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Record a user's answer to the health-ping prompt.

    Writes an AuditLog row that the (eventual) detector reads as a
    time-series of per-node user reports. We don't update health_score
    here — the detector lives in a follow-up — but the row carries
    enough context (subscription_id, node lookup at write time) for
    that future detector to backfill from.
    """
    if body.answer not in ("ok", "bad"):
        raise HTTPException(status_code=400, detail="answer must be 'ok' or 'bad'")

    source = body.source if body.source in ("prompted", "self_reported") else "prompted"

    user = db.query(models.User).filter_by(telegram_id=body.telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    node_id: int | None = None
    sub: models.Subscription | None = None
    if body.subscription_id is not None:
        sub = db.get(models.Subscription, body.subscription_id)
        # Cross-check ownership so a leaked sub_id from one user can't
        # be used to forge a report from another.
        if sub and sub.user_id == user.id:
            node_id = sub.node_id

    db.add(
        models.AuditLog(
            actor=str(user.id),
            actor_type=models.AuditActor.user,
            action="health_ping_response",
            target_type="subscription",
            target_id=body.subscription_id,
            extra={
                "telegram_id": body.telegram_id,
                "answer": body.answer,
                "node_id": node_id,
                "source": source,
            },
        )
    )

    # Плохой ответ (или self-reported «VPN не работает») → алерт
    # админам через бота. Дедуп по (node_id, user_id) за окно
    # ADMIN_ALERT_DEDUP_WINDOW_SEC, чтобы один юзер, жмущий кнопку
    # 50 раз за минуту, не захламил пуши. Не коммитим внутри хелпера —
    # db.commit() ниже положит и user-report-row, и admin-alert-rows
    # одной транзакцией (или обе откатятся).
    if body.answer == "bad":
        node_label = f"#{node_id}" if node_id else "?"
        sub_label = f"#{body.subscription_id}" if body.subscription_id else "?"
        admin_text = (
            f"🚨 Юзер tg={body.telegram_id} (id={user.id}) жалуется: "
            f"VPN не работает.\n"
            f"Нода: {node_label}, подписка: {sub_label}, источник: {source}"
        )
        try:
            notify_admins(
                db,
                kind="user_report",
                text=admin_text,
                dedup_key={"node_id": node_id, "user_id": user.id},
                extra={
                    "source": source,
                    "subscription_id": body.subscription_id,
                },
            )
        except Exception:  # noqa: BLE001
            # Алерт админу — best effort, не ломаем user-facing flow,
            # если notify_admins упал (DB-race, missing ADMIN_TELEGRAM_IDS
            # уже покрыт внутри хелпера, но мало ли).
            logger.exception(
                "notify_admins не отработал для health-ping-response"
            )

        # «Не работает» от юзера → сразу делаем ему то же, что админская
        # кнопка «обновить подписку»: переселяем на свободную ноду +
        # БАНИМ проблемную для него (NodeUserBan), плюс краудсорс-эскалация
        # «плохости» ноды. Только если sub валидна, принадлежит юзеру и
        # active. 5-мин throttle внутри _do_failover не даёт спамить
        # миграциями. Best-effort — не ломаем user-facing ответ.
        if (
            sub is not None
            and sub.user_id == user.id
            and sub.status == models.SubscriptionStatus.active
        ):
            from .api.client_control import _do_failover

            try:
                _do_failover(db, sub, kind="user_reported", actor=f"user:{user.id}")
            except Exception:  # noqa: BLE001
                # Roll back so a mid-migration failure can't be flushed by the
                # trailing db.commit() as a half-migrated sub (inner commits
                # already persisted the audit/migration rows we care about).
                if db.is_active:
                    db.rollback()
                logger.exception(
                    "health-ping-response: failover for sub %s failed",
                    body.subscription_id,
                )

    db.commit()
    return {"ok": True}


class HealthPingOptOutRequest(BaseModel):
    telegram_id: str


@ext_router.post("/users/health-ping-opt-out")
def opt_out_health_ping(
    body: HealthPingOptOutRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Set ``User.health_ping_opt_out`` so the worker stops queueing
    pings for this user. Idempotent — calling twice is fine.
    """
    user = db.query(models.User).filter_by(telegram_id=body.telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.health_ping_opt_out = True
    db.add(user)
    db.add(
        models.AuditLog(
            actor=str(user.id),
            actor_type=models.AuditActor.user,
            action="health_ping_opt_out",
            target_type="user",
            target_id=user.id,
            extra={"telegram_id": body.telegram_id},
        )
    )
    db.commit()
    return {"ok": True, "opted_out": True}


# ── Per-user notification preferences ──

class NotificationPrefsRequest(BaseModel):
    telegram_id: str
    notify_renewals: bool | None = None
    notify_migrations: bool | None = None
    health_ping_opt_out: bool | None = None


class NotificationPrefsOut(BaseModel):
    notify_renewals: bool
    notify_migrations: bool
    health_ping_opt_out: bool


@ext_router.get("/users/notification-prefs")
def get_notification_prefs(
    telegram_id: str,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Return current notification preferences for a user."""
    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return NotificationPrefsOut(
        notify_renewals=user.notify_renewals,
        notify_migrations=user.notify_migrations,
        health_ping_opt_out=user.health_ping_opt_out,
    )


@ext_router.post("/users/notification-prefs")
def update_notification_prefs(
    body: NotificationPrefsRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Update notification preferences. Only provided fields are changed."""
    user = db.query(models.User).filter_by(telegram_id=body.telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    changed: dict[str, bool] = {}
    if body.notify_renewals is not None:
        user.notify_renewals = body.notify_renewals
        changed["notify_renewals"] = body.notify_renewals
    if body.notify_migrations is not None:
        user.notify_migrations = body.notify_migrations
        changed["notify_migrations"] = body.notify_migrations
    if body.health_ping_opt_out is not None:
        user.health_ping_opt_out = body.health_ping_opt_out
        changed["health_ping_opt_out"] = body.health_ping_opt_out

    if changed:
        db.add(user)
        db.add(
            models.AuditLog(
                actor=str(user.id),
                actor_type=models.AuditActor.user,
                action="notification_prefs_updated",
                target_type="user",
                target_id=user.id,
                extra={"telegram_id": body.telegram_id, **changed},
            )
        )
        db.commit()

    return NotificationPrefsOut(
        notify_renewals=user.notify_renewals,
        notify_migrations=user.notify_migrations,
        health_ping_opt_out=user.health_ping_opt_out,
    )


class NotificationAck(BaseModel):
    """Что бот знает об отправленном сообщении.

    ``message_id`` нужен, чтобы доставленное можно было ОТОЗВАТЬ: Bot API умеет
    удалять свои сообщения 48 часов, но только по id. Пока мы его не сохраняли,
    ошибочная рассылка была необратима — ровно это и случилось 2026-07-27 с
    приглашением позвать друга.
    """

    message_id: int | None = None
    chat_id: int | None = None


@ext_router.post("/notifications/{notif_id}/ack")
def ack_notification(
    notif_id: int,
    body: NotificationAck | None = None,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Acknowledge a notification so it won't be returned again.

    We simply update the action name to mark it delivered.
    """
    log = db.get(models.AuditLog, notif_id)
    if not log:
        raise HTTPException(status_code=404, detail="Notification not found")
    log.action = f"{log.action}:delivered"
    if body and body.message_id:
        # JSONB in-place не детектится SQLAlchemy → новый dict.
        log.extra = {
            **(log.extra or {}),
            "message_id": body.message_id,
            "chat_id": body.chat_id,
        }
    db.add(log)
    db.commit()
    return {"ok": True}
