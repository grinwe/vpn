"""Выбор домена саб-ссылки: раскладываем юзеров по двум фронтам.

Зачем. Саб-ссылка — единственный канал, по которому клиент забирает конфиги,
и он обязан работать БЕЗ VPN. Один домен = одна точка отказа: `grn-ssync.pro`
(CF Worker) у части людей перестал открываться, и починить это удалённо
нельзя — адрес уже вбит в клиент. Второй фронт `grwr.ink` идёт мимо CF, на
origin напрямую (см. `nginx-sub-mirror.conf.j2`), и живёт там, где первый
лёг. Раскладывая людей по двум доменам, мы уполовиниваем радиус поражения:
падение одного фронта уносит часть, а не всех.

Ключевое свойство: **оба домена отдают ЛЮБОЙ токен**. Выбор здесь влияет
только на то, какую ссылку мы ПОКАЗЫВАЕМ и записываем новым девайсам; уже
розданные ссылки продолжают работать на своём домене. Поэтому менять долю
безопасно в любую сторону и в любой момент.

Выбор детерминирован по токену, а не случаен: бот, кабинет, страница
починки и провижининг считают его независимо друг от друга и обязаны
сойтись в одном ответе. Хэш токена даёт это без единой строчки в БД.
"""

from __future__ import annotations

import hashlib
import os


def _clean(name: str) -> str:
    return (os.getenv(name) or "").strip().rstrip("/")


def alt_share() -> int:
    """Доля юзеров на запасном домене, 0..100.

    0 (дефолт) — фича выключена, поведение ровно как до неё: все на
    основном домене. Именно поэтому включение делается одной переменной
    окружения и не требует правок кода.
    """
    raw = (os.getenv("SUB_LINK_ALT_SHARE") or "0").strip()
    try:
        share = int(raw)
    except ValueError:
        return 0
    return max(0, min(100, share))


def use_alt(token: str) -> bool:
    """Попадает ли этот токен в долю запасного домена."""
    alt = _clean("SUB_LINK_BASE_URL_ALT")
    share = alt_share()
    if not alt or not token or share <= 0:
        return False
    if share >= 100:
        return True
    # sha256, а не hash(): встроенный hash() солится за процесс (PYTHONHASHSEED),
    # то есть бэкенд, воркер и бот разошлись бы в ответах, а после рестарта
    # разошлись бы сами с собой — и юзеру каждый раз показывался бы другой
    # домен.
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100 < share


def sub_base_for(token: str) -> str:
    """Базовый URL саб-ссылки для конкретного токена.

    Возвращает пустую строку, если основной домен не задан — вызывающий код
    в этом случае строит относительный `/api/sub/<token>` как и раньше.
    """
    if use_alt(token):
        return _clean("SUB_LINK_BASE_URL_ALT")
    return _clean("SUB_LINK_BASE_URL")


def sub_url_for(token: str) -> str:
    """Готовая ссылка `<base>/<token>` или относительный путь без базы."""
    base = sub_base_for(token)
    return f"{base}/{token}" if base else f"/api/sub/{token}"



# Ссылка, которую бот показывает юзеру («конфиг готов» и /config).
#
# Раньше бот отдавал токен ПОДПИСКИ, а он при нескольких устройствах отдаёт
# креды всех устройств сразу: телефон, куда импортировали ссылку из бота,
# занимал логины device-2/device-3, которые юзер тем временем раздавал родным
# из кабинета (user 1000076, 27.09.2026: 12 конфигов в одном Hiddify).
#
# Какое устройство «главное», запоминается в Subscription.link_token при
# создании подписки (токен первого устройства). Угадывать по строкам Device
# нельзя: failover/миграция переносят токен на НОВУЮ строку, и «самое раннее
# устройство» становилось device-2 — бот показал бы ссылку родственника.
_RETIRED = ("revoked", "disabled")


def _status(device) -> str:
    return getattr(device.status, "value", device.status)


def link_token_for(sub) -> str | None:
    """Токен для показа юзеру в боте.

    * ``link_token`` пуст (подписка до 0070) → legacy ``sub_token``: он уже в
      клиентах, другая ссылка дала бы профиль-дубль.
    * Устройство с этим токеном не выведено (или его строка не загружена) →
      токен. Замены после разморозки/enable/refresh перенимают link_token
      сами (provisioning._adopt_link_token), сюда доходят уже с ним.
    * Держатель выведен (revoked/disabled), а перенос не случился — юзер сам
      удалил устройство, либо подписку чинили до этого релиза: одноимённое
      живое устройство → самое раннее активное → самое раннее ещё pending.
      Не отдаём токен выведенного: саб-эндпоинт алиасил бы его на «последнее
      обновлённое» соседнее, и оно менялось бы от тика к тику.
    * Живых устройств нет вовсе → legacy ``sub_token``.
    """
    token = getattr(sub, "link_token", None)
    if not token:
        return sub.sub_token
    devices = list(getattr(sub, "devices", None) or [])
    holder = next((d for d in devices if d.sub_token == token), None)
    if holder is None or _status(holder) not in _RETIRED:
        return token
    # pending_swap_from — замена в незавершённом failover-свапе: её токен
    # временный и исчезнет при свапе (ссылка стала бы 404).
    alive = [
        d for d in devices
        if d.sub_token
        and _status(d) not in _RETIRED + ("failed",)
        and getattr(d, "pending_swap_from", None) is None
    ]
    if not alive:
        return sub.sub_token

    def _key(d):
        return (d.created_at is None, d.created_at, d.id)

    same_name = [d for d in alive if (d.name or "primary") == (holder.name or "primary")]
    active = [d for d in alive if _status(d) == "active"]
    return min(same_name or active or alive, key=_key).sub_token
