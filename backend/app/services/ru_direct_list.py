"""РУ-список для клиентского правила ``ru-direct`` в Xray-JSON.

Источник истины — ansible-роль ``infra/ansible/roles/ru_direct_list``
(``defaults/main.yml``): именно её рендерят все четыре серверных шаблона.
Здесь — КОПИЯ, а не чтение YAML в рантайме: образ backend не содержит
``infra/`` (Dockerfile копирует только ``backend/``), а тащить в образ
ansible-роль ради одного файла значит связать сборку приложения с
инфраструктурой. Паритет копии с ролью держит
``backend/tests/test_split_routing_parity.py`` — правка одного без другого
роняет тест, а не расходится молча (ровно так RU-обход в проекте уже трижды
разъезжался между протоколами).

Зачем клиенту вообще нужен этот список, если split живёт на ноде: см.
модульный докстринг ``xray_client_config`` («Роутинг»). Коротко — на
direct-нодах без WG-туннеля split физически невозможен, а на них лежит лег
у большинства устройств.
"""

from __future__ import annotations

# Зоны верхнего уровня. На клиенте рендерятся так же, как на ноде:
# ``regexp:\.<zone>$`` — суффиксный матч всей зоны.
# xn--p1ai = .рф, xn--d1acj3b = .дети, xn--p1acf = .рус.
RU_DIRECT_ZONES: list[str] = [
    "ru",
    "su",
    "xn--p1ai",
    "moscow",
    "tatar",
    "xn--d1acj3b",
    "xn--p1acf",
]

# Конкретные домены в не-.ru зонах (``domain:`` — домен + все поддомены).
# Порядок и состав — как в роли; комментарии-группировки там же.
RU_DIRECT_DOMAINS: list[str] = [
    # VK / Mail.ru + их CDN
    "vk.com",
    "vk.me",
    "vk.cc",
    "userapi.com",
    "mycdn.me",
    # Яндекс вне .ru
    "yandex.com",
    "yandex.net",
    "yastatic.net",
    # Прочие РУ-сервисы в не-.ru зонах
    "habr.com",
    "2gis.com",
    "avito.st",
    # Гео-лицензированный стриминг
    "okko.tv",
    "premier.one",
    "more.tv",
    "my.games",
    "zvuk.com",
    # Хвост не-.ru зон РУ-брендов
    "ozon.travel",
    "rutube.com",
    "lenta.com",
    "tass.com",
    "sberbank.com",
    "tinkoff.com",
    "tbank.ru",
    "yandex.com.tr",
    "wildberries.by",
    "wildberries.kz",
    "wildberries.am",
]


def xray_client_domain_rules() -> list[str]:
    """Массив ``domain`` для клиентского правила — байт-в-байт как на ноде.

    Нода рендерит зоны через ``regex_replace('^(.*)$', 'regexp:\\\\.\\1$')``,
    то есть в JSON уезжает ``"regexp:\\.ru$"`` (строка ``regexp:\\.ru$``,
    где точка экранирована одним бэкслешем). Здесь то же самое: ``\\\\.``
    в питон-литерале = один бэкслеш + точка в строке. Тест паритета сверяет
    результат с рендером шаблона reality, а не с этим описанием.
    """
    return [f"regexp:\\.{zone}$" for zone in RU_DIRECT_ZONES] + [
        f"domain:{domain}" for domain in RU_DIRECT_DOMAINS
    ]
