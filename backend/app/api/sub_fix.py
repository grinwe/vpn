"""Страница «починить и продлить» на домене саб-ссылки — без Telegram.

Зачем: человек со сломанным VPN заперт в круге — кнопка «VPN не работает» и
оплата живут в Telegram-боте, а Telegram без VPN бывает недоступен. Домен
саб-ссылки доступен без VPN по определению (с него клиент и так тянет
конфиги), поэтому на нём же отдаём страницу, которая по sub_token умеет
ровно два действия: шаг лестницы ротации и счёт на продление картой.

Решения, которые здесь важны:

* **query-параметр, а не новый путь.** CF Worker матчит строго один сегмент
  пути (``/<token>``), но метод и query-строку пробрасывает как есть —
  ``?fix=1`` работает без правки воркера. Тело POST воркер не форвардит,
  поэтому форма без полей: всё в query.
* **починка — POST, не GET.** GET дёргают браузерные префетчеры, антивирусы
  и превью-фетчеры; каждый такой вызов сжигал бы шаг лестницы, а он конечен.
* **HTML только для браузера.** Дискриминатор — ``Accept: text/html``:
  человек, вставивший ссылку с ``?fix=1`` в VPN-клиент как подписку, всё
  равно получит нормальный base64-конфиг, а не HTML-мусор в профиле.
* **неизвестный токен = camo-лендинг**, тот же, что на корне домена.
  Никаких «подписка не найдена»: разница в статусе/размере/тексте сама
  становится оракулом для пробера.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import time
from string import Template

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import models
from ..services import self_repair
from ..time_utils import utcnow

logger = logging.getLogger("app.api.sub_fix")

# Окно nonce. Это CSRF-токен (защита от «чужая страница отправила форму от
# имени пользователя»), а НЕ аутентификация: секрет всё равно сам токен.
_NONCE_WINDOW_SEC = 900


def page_enabled() -> bool:
    return (os.getenv("SUB_FIX_PAGE") or "0").strip().lower() in (
        "1", "true", "on", "yes",
    )


def pay_enabled() -> bool:
    return (os.getenv("SUB_FIX_PAY") or "0").strip().lower() in (
        "1", "true", "on", "yes",
    )


def wants_html(request: Request) -> bool:
    """Просят ли HTML.

    VPN-клиенты (Happ, Hiddify, v2rayNG) ``text/html`` в Accept не шлют, а
    браузеры шлют всегда. Без этой проверки человек, случайно вставивший
    ссылку с ``?fix=1`` в клиент как подписку, получил бы HTML вместо
    конфигов и сломал себе профиль.
    """
    return "text/html" in (request.headers.get("accept") or "").lower()


def _throttle_sec() -> int:
    try:
        return int(os.getenv("SUB_FIX_THROTTLE_SEC") or "120")
    except ValueError:
        return 120


def _daily_max() -> int:
    try:
        return int(os.getenv("SUB_FIX_DAILY_MAX") or "5")
    except ValueError:
        return 5


def pay_provider() -> str:
    """Каким провайдером платит человек СО СТРАНИЦЫ.

    Пинить обязательно: без явного имени ``get_provider`` берёт провайдера из
    общей ротации, а её дефолт в проде — cryptobot. Страница существует ровно
    для того, у кого нет Telegram и, скорее всего, нет криптокошелька: он
    пришёл платить картой. Дефолт lava.top — единственный карточный провайдер
    без Telegram (Stars внутри Telegram платить нельзя по определению).
    """
    return (os.getenv("SUB_FIX_PROVIDER") or "lava_top").strip() or "lava_top"


def make_nonce(token: str, *, offset: int = 0) -> str:
    """CSRF-nonce, привязанный к токену и 15-минутному окну."""
    secret = (os.getenv("APP_SECRET_KEY") or "").encode() or b"dev-insecure"
    bucket = int(time.time() // _NONCE_WINDOW_SEC) - offset
    digest = hmac.new(secret, f"{token}:{bucket}".encode(), hashlib.sha256)
    return digest.hexdigest()[:16]


def nonce_valid(token: str, nonce: str | None) -> bool:
    """Текущее окно и предыдущее: страница, открытая 14 минут назад, обязана
    работать — иначе человек с плохой связью не успеет нажать кнопку."""
    if not nonce:
        return False
    return any(
        hmac.compare_digest(nonce, make_nonce(token, offset=off)) for off in (0, 1)
    )


# ── HTML ────────────────────────────────────────────────────────────────
#
# Страница самодостаточна: ни внешнего CSS, ни шрифтов, ни картинок, ни JS.
# Кнопка — обычная форма, работает без JavaScript. Причина простая: её
# открывают с телефона на дохлом мобильном интернете и часто в вебвью
# VPN-клиента, где половина внешних ресурсов не загрузится.

_CAMO = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Internal Tools Portal</title>
<style>
body{font-family:system-ui,sans-serif;max-width:720px;margin:4rem auto;padding:2rem;color:#333;line-height:1.6}
h1{color:#1a1a1a;border-bottom:1px solid #ddd;padding-bottom:.5rem}
.meta{color:#888;font-size:.9rem;margin-top:3rem}
</style>
</head>
<body>
<h1>Internal Tools Portal</h1>
<p>This domain hosts internal operational tooling. Access is restricted to authorized personnel.</p>
<p>If you believe you reached this page in error, please contact your system administrator.</p>
<div class="meta">&copy; Internal Tools &middot; maintained by operations</div>
</body>
</html>"""

_PAGE = Template("""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
$refresh
<title>Подключение</title>
<style>
:root{color-scheme:light dark}
body{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:1.5rem;
     line-height:1.5;background:#f5f6f8;color:#16181d}
main{max-width:30rem;margin:0 auto}
.card{background:#fff;border-radius:14px;padding:1.25rem;margin-bottom:1rem;
      box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1{font-size:1.25rem;margin:0 0 .75rem}
p{margin:.5rem 0}
.muted{color:#6b7280;font-size:.9rem}
.ok{color:#0f7b34}
.warn{color:#b45309}
.bad{color:#b91c1c}
button,.btn{display:block;box-sizing:border-box;width:100%;
       padding:.85rem 1rem;font-size:1rem;font-weight:600;text-align:center;
       text-decoration:none;border:0;border-radius:10px;
       background:#2563eb;color:#fff;cursor:pointer}
button.secondary,.btn.secondary{background:#e5e7eb;color:#16181d}
button:disabled{opacity:.45;cursor:not-allowed}
form{margin:0 0 .6rem}
.btn{margin-bottom:.6rem}
ol{padding-left:1.2rem}
a{color:#2563eb}
@media (prefers-color-scheme:dark){
  body{background:#0f1115;color:#e5e7eb}
  .card{background:#181b21;box-shadow:none}
  button.secondary,.btn.secondary{background:#2a2f3a;color:#e5e7eb}
  .muted{color:#9ca3af}
}
</style>
</head>
<body><main>
<div class="card">
<h1>$title</h1>
$body
</div>
$actions
<p class="muted">Эта страница работает без VPN и без Telegram.</p>
</main></body>
</html>""")


def _render(
    *,
    title: str,
    body: str,
    actions: str = "",
    refresh_sec: int | None = None,
    refresh_url: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    refresh = ""
    if refresh_sec:
        target = f";url={html.escape(refresh_url)}" if refresh_url else ""
        refresh = f'<meta http-equiv="refresh" content="{refresh_sec}{target}">'
    page = _PAGE.substitute(
        title=html.escape(title), body=body, actions=actions, refresh=refresh
    )
    return HTMLResponse(
        content=page,
        status_code=status_code,
        headers={
            "cache-control": "no-store, private",
            "pragma": "no-cache",
            # Иначе токен утечёт в Referer при переходе в Telegram/на оплату.
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "x-robots-tag": "noindex",
        },
    )


def camo_response(status_code: int = 200) -> HTMLResponse:
    """Скучный лендинг — ответ на неизвестный токен и на выключенную страницу.

    Ответ обязан совпадать с корнем домена ПОБАЙТОВО и по заголовкам, включая
    код 200: иначе сам код ответа становится оракулом («404 → токена нет,
    200 → есть»), а ради этого camo и существует. ``status_code`` оставлен
    параметром только для вызовов, где смысл именно в отказе.

    Кэш публичный и часовой — как на корне; для НАШЕГО человека это не
    страшно, потому что путь с валидным токеном сюда не попадает.
    """
    return HTMLResponse(
        content=_CAMO,
        status_code=status_code,
        headers={
            "content-type": "text/html; charset=utf-8",
            "cache-control": "public, max-age=3600",
            "strict-transport-security": "max-age=31536000",
        },
    )


def _button_form(token: str, label: str, *, extra: str = "", secondary=False) -> str:
    """Кнопка-форма. Все параметры в query: тело POST через CF Worker не едет."""
    cls = ' class="secondary"' if secondary else ""
    return (
        f'<form method="post" action="?fix=1&n={html.escape(make_nonce(token))}{extra}">'
        f"<button{cls}>{html.escape(label)}</button></form>"
    )


def _page_base_url() -> str:
    """Публичный адрес самой страницы — домен саб-ссылки."""
    return (os.getenv("SUB_LINK_BASE_URL") or "").strip().rstrip("/")


def _return_url(token: str, invoice_id: int) -> str | None:
    """URL возврата с оплаты: тот же токен + ``?paid=<id>``."""
    base = _page_base_url()
    if not base:
        return None
    return f"{base}/{token}?fix=1&paid={invoice_id}"


def _support_url() -> str | None:
    """Диалог с админом: ``t.me/<bot>?start=support`` открывает FSM поддержки —
    человек пишет одно сообщение, оно уходит админу, ответ приходит туда же."""
    bot = (os.getenv("BOT_USERNAME") or "").strip()
    return f"https://t.me/{bot}?start=support" if bot else None


def _help_button(*, enabled: bool) -> str:
    """Кнопка «Написать в поддержку» — активная только после починки.

    Порядок намеренный: сначала человек жмёт «Починить», получает новый набор
    серверов и пробует. Живой админ — дорогой ресурс, и звать его до того,
    как отработала автоматика, значит тратить его на случаи, которые
    чинятся кнопкой за секунду. Пока не починили — кнопка неактивна и прямо
    объясняет, что нажать сначала.
    """
    url = _support_url()
    if not url:
        return ""
    if not enabled:
        return (
            '<button class="secondary" disabled>Не помогло? Напишите нам</button>'
            '<p class="muted">Кнопка станет активной после починки — '
            "сначала нажмите её.</p>"
        )
    return (
        f'<a class="btn secondary" href="{html.escape(url)}">'
        "Не помогло? Напишите нам</a>"
    )


def _telegram_link() -> str:
    bot = (os.getenv("BOT_USERNAME") or "").strip()
    if not bot:
        return ""
    return (
        f'<p class="muted">Если Telegram доступен — '
        f'<a href="https://t.me/{html.escape(bot)}?start=account">открыть личный кабинет</a>.</p>'
    )


def _expiry_line(sub) -> str:
    if not sub.expires_at:
        return ""
    expires = sub.expires_at
    from datetime import timedelta, timezone

    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    msk = expires.astimezone(timezone(timedelta(hours=3)))
    left = expires - utcnow().replace(tzinfo=timezone.utc)
    if left.total_seconds() <= 0:
        return f'<p class="bad">Подписка закончилась {msk.strftime("%d.%m.%Y")}.</p>'
    days = left.days
    if days >= 1:
        return f'<p class="muted">Активна до {msk.strftime("%d.%m.%Y")} ({days} дн.).</p>'
    hours = max(1, int(left.total_seconds() // 3600))
    return f'<p class="warn">Заканчивается через {hours} ч.</p>'


# ── Экраны ──────────────────────────────────────────────────────────────


# Имена, которые ставит провижининг, а не человек. Показывать их нельзя:
# «Мы переключим primary» — это разговор с инженером, а не с пользователем.
_TECH_DEVICE_NAMES = {"primary", "device", "устройство", "default"}


def render_start(
    sub, token: str, *, device_name: str | None, repairable: bool = True
) -> HTMLResponse:
    """Главный экран: кнопка починки (и продления, если включено)."""
    name = (device_name or "").strip()
    if not name or name.lower() in _TECH_DEVICE_NAMES:
        name = "это устройство"
    who = html.escape(name)
    if repairable:
        body = (
            f"<p>Если VPN не подключается — нажмите кнопку ниже. "
            f"Мы переключим <b>{who}</b> на другой способ связи или другой сервер.</p>"
        )
    else:
        # Legacy-подписочный токен: устройства за ним нет, чинить нечего —
        # кнопка, которая всегда отвечает «не нашли устройство», хуже, чем
        # её отсутствие.
        body = (
            "<p>По этой ссылке автоматическая починка недоступна — "
            "обновите профиль в клиенте из личного кабинета.</p>"
        )
    body += _expiry_line(sub)
    actions = _button_form(token, "Починить подключение") if repairable else ""
    if pay_enabled():
        actions += _renew_button(sub, token)
    # Пока не чинили — «Напишите нам» неактивна (см. _help_button). Для
    # legacy-токена чинить нечего, поэтому там она сразу доступна.
    actions += _help_button(enabled=not repairable)
    return _render(title="Что-то не работает?", body=body, actions=actions)


def _renew_button(sub, token: str) -> str:
    """Кнопка продления С ЦЕНОЙ на ней.

    Сумму человек обязан увидеть ДО платёжного виджета: иначе он жмёт кнопку
    и попадает сразу на списание, не зная ни цены, ни срока.
    """
    from ..services.balance import total_renewal_cost_kopecks

    rub = total_renewal_cost_kopecks(sub) / 100 if sub.plan else 0
    days = sub.plan.duration_days if sub.plan else 0
    if rub <= 0:
        return ""
    label = f"Продлить на {days} дн. — {rub:g} ₽"
    return _button_form(token, label, extra="&pay=1", secondary=True)


def render_outcome(outcome, sub, token: str) -> HTMLResponse:
    """Экран результата починки — текст зависит от шага лестницы."""
    refresh_hint = (
        "<ol>"
        "<li>Откройте ваш VPN-клиент</li>"
        "<li>Нажмите 🔄 (обновить подписку) на профиле</li>"
        "<li>Подключитесь заново</li>"
        "</ol>"
        '<p class="muted">Без обновления клиент ещё какое-то время будет '
        "показывать старые серверы.</p>"
    )
    if outcome.action == "reshuffled":
        title, body = "Готово", (
            '<p class="ok">Переключили вас на другой способ связи.</p>' + refresh_hint
        )
    elif outcome.action == "migrated":
        node = html.escape(outcome.new_node_name or "новый сервер")
        title, body = "Готово", (
            f'<p class="ok">Перевели на другой сервер ({node}).</p>' + refresh_hint
        )
    elif outcome.action == "duplicated":
        title, body = "Готово", (
            '<p class="ok">Добавили запасной сервер — в клиенте появится ещё одна '
            "строка.</p>" + refresh_hint
        )
    elif outcome.action == "throttled":
        title, body = "Уже чиним", (
            '<p class="warn">Мы переключили вас пару минут назад.</p>'
            "<p>Откройте клиент и нажмите 🔄 — новые серверы уже там. "
            "Если не помогло, попробуйте ещё раз через несколько минут.</p>"
        )
    elif outcome.action == "daily_limit":
        title, body = "Слишком часто", (
            '<p class="warn">Сегодня мы уже несколько раз меняли вам серверы.</p>'
            "<p>Дальше нужна помощь человека — напишите в поддержку.</p>"
        )
    elif outcome.action == "no_target":
        title, body = "Сейчас не получилось", (
            '<p class="warn">Свободного сервера нет прямо сейчас.</p>'
            "<p>Попробуйте через 10 минут — они освобождаются постоянно.</p>"
        )
    else:  # no_subscription
        # Сюда попадает и «только что починили»: свежепровиженное устройство
        # какое-то время pending, и alias его ещё не видит. Текст обязан это
        # учитывать — иначе человек, у которого починка СРАБОТАЛА, читает
        # «ничего не нашли» и идёт жаловаться второй раз.
        title, body = "Проверьте клиент", (
            "<p>Похоже, серверы уже переключены.</p>" + refresh_hint
        )
    actions = ""
    if outcome.action in ("no_target", "throttled"):
        actions = _button_form(token, "Попробовать ещё раз")
    # Автоматика отработала (или упёрлась в потолок) — теперь живой человек
    # уместен, и кнопка активна.
    actions += _help_button(enabled=True)
    return _render(title=title, body=body, actions=actions)


def render_expired(sub, token: str) -> HTMLResponse:
    """Подписка кончилась — чинить нечего, показываем продление."""
    body = _expiry_line(sub) + (
        "<p>Пока подписка не продлена, VPN не подключается.</p>"
    )
    actions = _renew_button(sub, token) if pay_enabled() else ""
    if not actions:
        # Без включённой оплаты экран остался бы вообще без действий —
        # тупик ровно для того человека, ради которого страница написана.
        body += (
            "<p>Продлить можно в личном кабинете. Если Telegram недоступен, "
            "попробуйте открыть его через мобильный интернет другого "
            "оператора или Wi-Fi.</p>"
        )
    return _render(title="Подписка закончилась", body=body + _telegram_link(), actions=actions)


def render_rate_limited() -> HTMLResponse:
    """429 в человеческом виде (см. обработчик в main.py)."""
    return _render(
        title="Слишком часто",
        body="<p>Вы нажимали кнопку несколько раз подряд.</p>"
        "<p>Подождите минуту и попробуйте снова — предыдущее нажатие могло "
        "уже сработать: откройте клиент и нажмите 🔄.</p>" + _telegram_link(),
        status_code=429,
    )


def render_inactive(sub, token: str) -> HTMLResponse:
    """Пауза/блокировка — объясняем и уводим к человеку."""
    if sub.status == models.SubscriptionStatus.frozen:
        title = "Подписка на паузе"
        body = "<p>Вы поставили подписку на паузу. Снять её можно в личном кабинете.</p>"
    else:
        title = "Доступ приостановлен"
        body = "<p>Подписка заблокирована. Разобраться поможет поддержка.</p>"
    return _render(title=title, body=body + _telegram_link())


def render_pay_pending(invoice_id: int, token: str, *, paid: bool) -> HTMLResponse:
    """Ожидание подтверждения оплаты.

    Автообновление раз в 5 с: подтверждение занимает 30–60 с, потому что
    вебхуки провайдера в проде не долетают и работает тик сверки раз в
    минуту. Человеку об этом говорим честно — иначе он решит, что деньги
    пропали, и заплатит второй раз.
    """
    if paid:
        return _render(
            title="Оплата получена",
            body='<p class="ok">Подписка продлена. Откройте VPN-клиент и нажмите 🔄.</p>'
            + _telegram_link(),
        )
    body = (
        "<p>Ждём подтверждения от банка — обычно меньше минуты.</p>"
        '<p class="muted">Страница обновится сама. Платить второй раз не нужно.</p>'
    )
    # refresh ведёт на СВОЙ URL (?paid=<id>), иначе через 5 секунд человек
    # видел бы стартовый экран и решил, что оплата потерялась.
    return _render(
        title="Проверяем оплату",
        body=body,
        refresh_sec=5,
        refresh_url=f"?fix=1&paid={invoice_id}",
    )


# ── Действия ────────────────────────────────────────────────────────────


def do_repair(db: Session, found, token: str) -> HTMLResponse:
    """POST-починка: шаг лестницы через общее ядро."""
    from .client_control import COMPLAINT_DEDUP_SEC

    sub = found.sub
    device = found.serve_device or found.token_device
    if device is None:
        # Legacy-подписочный токен: устройства за ним нет, чинить нечего.
        return render_outcome(
            self_repair.RepairOutcome(action="no_subscription"), sub, token
        )
    user = db.get(models.User, sub.user_id)
    if user is None:
        return camo_response()

    outcome = self_repair.handle_broken_device(
        db,
        device.id,
        user=user,
        dedup_sec=COMPLAINT_DEDUP_SEC,
        source="sub_page",
        throttle_sec=_throttle_sec(),
        daily_max=_daily_max(),
    )
    logger.info(
        "sub-fix page: repair sub=%s device=%s -> %s",
        sub.id, device.id, outcome.action,
    )
    return render_outcome(outcome, sub, token)


def do_pay(db: Session, found, token: str) -> HTMLResponse | RedirectResponse:
    """POST-продление: счёт у провайдера и редирект на оплату.

    Всё денежное — в общем хелпере (services/payments/checkout.py): сумма со
    слотами, конвертация валют, реюз pay_url при повторном тапе.
    """
    from ..services.balance import total_renewal_cost_kopecks
    from ..services.payments import ProviderError
    from ..services.payments.checkout import checkout_pending_invoice

    sub = found.sub
    if sub.plan is None:
        return camo_response()

    # Дедуп: незакрытый счёт на продление этой подписки переиспользуем, иначе
    # каждый тап плодил бы счета, а человек путался бы, какой оплачивать.
    invoice = (
        db.query(models.Invoice)
        .filter(
            models.Invoice.subscription_id == sub.id,
            models.Invoice.action == models.InvoiceAction.renewal,
            models.Invoice.status == models.InvoiceStatus.pending,
        )
        .order_by(models.Invoice.id.desc())
        .first()
    )
    if invoice is None:
        invoice = models.Invoice(
            user_id=sub.user_id,
            plan_id=sub.plan_id,
            subscription_id=sub.id,
            amount=total_renewal_cost_kopecks(sub) / 100,
            currency="RUB",
            action=models.InvoiceAction.renewal,
        )
        db.add(invoice)
        db.commit()
        db.refresh(invoice)

    try:
        result = checkout_pending_invoice(
            db, invoice,
            provider_name=pay_provider(),
            # Куда провайдер вернёт человека после оплаты. Без этого экран
            # «проверяем оплату» был бы недостижим: страница ждёт
            # подтверждения (вебхуки lava не долетают, работает тик сверки),
            # а человек остался бы на чужой вкладке банка.
            return_url=_return_url(token, invoice.id),
        )
    except ProviderError:
        logger.exception("sub-fix page: checkout failed for sub %s", sub.id)
        return _render(
            title="Оплата недоступна",
            body="<p>Сейчас не получилось открыть оплату. Попробуйте через "
            "несколько минут.</p>" + _telegram_link(),
        )
    if not result.pay_url:
        return render_pay_pending(invoice.id, token, paid=False)
    # 303: после POST переход обязан стать GET, иначе «назад» в браузере
    # предложит переотправить форму (и выписать ещё один счёт).
    return RedirectResponse(url=result.pay_url, status_code=303)


def render_paid_state(db: Session, found, token: str, invoice_id: int) -> HTMLResponse:
    """``?paid=<id>`` — экран ожидания/подтверждения оплаты."""
    invoice = db.get(models.Invoice, invoice_id)
    # Чужой счёт не раскрываем: показываем нейтральное ожидание.
    if invoice is None or invoice.subscription_id != found.sub.id:
        return render_pay_pending(invoice_id, token, paid=False)
    return render_pay_pending(
        invoice.id, token, paid=invoice.status == models.InvoiceStatus.paid
    )
