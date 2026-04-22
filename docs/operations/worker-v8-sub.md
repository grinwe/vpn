# Worker `v8-sub` — CDN-прокси sub-link на `grn-ssync.pro`

Короткий Cloudflare Worker, который проксирует `https://grn-ssync.pro/<sub_token>` в `https://grinwer.online/api/sub/<sub_token>`. Фронт нужен, чтобы RKN-блок основного домена не убивал уже установленных Hiddify/v2rayNG-клиентов (см. `infrastructure/deployment.md` → «Sub-link CDN proxy»).

**Код воркера в репо не коммитим** — он короткий, живёт в CF Dashboard, и правки всё равно деплоятся через Dashboard. Этот документ — snapshot-образец текущей рабочей версии + процедуры.

## Где это находится

- **CF account:** основной аккаунт grinwer (логин из 1Password → CF Dashboard → Workers & Pages).
- **Worker name:** `v8-sub`.
- **Route:** `grn-ssync.pro/*` → этот worker. Настроено в Zone → Workers Routes.
- **Zone-settings для `grn-ssync.pro`:**
  - `SSL/TLS mode = Full (strict)` — иначе CF ↔ backend будет plain HTTP.
  - `HTTP/2 = off`, `HTTP/3 = off`. **Не включать.** RKN DPI режет H2 stream после TLS-handshake — headers доходят, body теряется. HTTP/1.1 проскакивает. Тумблеры доступны только на Pro-плане.
  - `Always Use HTTPS = on`.

## Текущий рабочий код (snapshot)

Pre-anti-probing версия: любой путь, кроме `/<token>`, отдаёт 404. Это red-flag для активного пробера — пустой корень = «какой-то backend, надо сканить».

```javascript
const BACKEND = "https://grinwer.online";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;

    // Accept exactly one token segment.
    const m = path.match(/^\/([A-Za-z0-9_-]+)\/?$/);
    if (!m) {
      return new Response("Not Found", { status: 404 });
    }

    const token = m[1];
    const upstream = `${BACKEND}/api/sub/${token}`;

    const resp = await fetch(upstream, {
      method: request.method,
      headers: request.headers,
      redirect: "manual",
    });
    return new Response(resp.body, resp);
  },
};
```

## Анти-пробинг версия (paste-ready)

Отличия: на всё, что не `^/<token>$`, отдаём скучный HTML-landing с 200 + HSTS. Пробер видит «какой-то корпоративный портал» вместо 404-маркера.

```javascript
const BACKEND = "https://grinwer.online";
const TOKEN_RE = /^\/([A-Za-z0-9_-]{20,80})\/?$/;

const LANDING_HTML = `<!doctype html>
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
</html>`;

const LANDING_HEADERS = {
  "content-type": "text/html; charset=utf-8",
  "cache-control": "public, max-age=3600",
  "strict-transport-security": "max-age=31536000",
};

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const m = url.pathname.match(TOKEN_RE);

    if (!m) {
      return new Response(LANDING_HTML, { status: 200, headers: LANDING_HEADERS });
    }

    const upstream = `${BACKEND}/api/sub/${m[1]}`;
    const resp = await fetch(upstream, {
      method: request.method,
      headers: request.headers,
      redirect: "manual",
    });
    return new Response(resp.body, resp);
  },
};
```

`TOKEN_RE` намеренно шире, чем конкретный формат — `sub_token` сейчас это `secrets.token_urlsafe(32)` (43 символа), но исторические токены из старой Subscription-таблицы могут быть короче. Диапазон 20–80 покрывает оба случая без регресса.

## Как обновить

1. CF Dashboard → Workers & Pages → `v8-sub` → `Edit code`.
2. Заменить содержимое `worker.js` целиком на новый код.
3. `Save and deploy`. CF пропагирует за ≤30с по всем edge-локациям.
4. **Purge кэша landing'а:** Dashboard → zone `grn-ssync.pro` → Caching → Configuration → `Purge Everything`. Иначе часть пользователей видит старую 404-версию ещё до 1ч (из-за `cache-control: max-age=3600`).

## Проверка

```bash
# 1. Корень теперь landing, не 404
curl -sI https://grn-ssync.pro/
# → HTTP/1.1 200 OK
# → content-type: text/html; charset=utf-8

curl -s https://grn-ssync.pro/ | head -5
# → <!doctype html> ... Internal Tools Portal ...

# 2. Фейковый токен всё равно уходит на backend (и там ловит 404)
curl -sI https://grn-ssync.pro/notarealsubtoken123456
# → HTTP/1.1 404 Not Found   (от backend'а, не от worker'а)

# 3. Реальный токен работает
curl -sI "https://grn-ssync.pro/${REAL_SUB_TOKEN}"
# → HTTP/1.1 200 OK
# → content-type: text/plain; charset=utf-8  (base64-subscription от FastAPI)

# 4. HSTS заголовок на landing'е
curl -sI https://grn-ssync.pro/ | grep -i strict-transport
# → strict-transport-security: max-age=31536000
```

Если шаг 3 отдаёт 404 — проверь, что `SUB_LINK_BASE_URL` в `.env` совпадает с `BACKEND` в worker'е (`https://grinwer.online`) и что сам backend отдаёт по `/api/sub/<token>`:

```bash
ssh root@45.14.244.140 'curl -sI http://127.0.0.1:8000/api/sub/${TOKEN}'
# → 200 если токен живой
```

## Ротация landing'а

Если landing примелькался — редкая процедура, максимум раз в несколько месяцев:

1. Подобрать новый «boring»-шаблон (например, корпоративный портал другой индустрии — consulting, logistics, staffing). Никаких VPN-намёков, никаких разработческих маркеров.
2. Заменить `LANDING_HTML` в worker'е (и `LANDING_HEADERS.cache-control` оставить как есть).
3. Deploy + `Purge Everything`.
4. Зеркально — шаблон в `deploy_web_frontend/files/camo-index.html` и `install_vless_xhttp/...` (xhttp-ноды) должен совпадать, чтобы все 3 фронта смотрелись одинаково. Иначе fingerprint-корреляция через несколько hosts выдаёт связь.

## Отладка

- **CF worker logs:** Dashboard → worker → `Logs` → `Begin log stream`. Живая stream из edge. Полезно для неожиданных статусов от upstream'а.
- **Upstream error rate:** Dashboard → zone `grn-ssync.pro` → Analytics → HTTP. 5xx-всплеск = проблема на backend'е, не в воркере. Диагностика идёт туда (`runbook.md` → секция про backend).
- **CPU limits:** Free-план worker'ов — 10мс CPU, Pro — 50мс. Текущий код не делает ничего кроме `fetch` + возврат body, в лимит укладывается с запасом.
- **HTTP/2 снова включился:** если жалобы «сабскрипшн обновляется через раз на 4G» вернулись — первым делом Dashboard → zone → Network → проверить `HTTP/2 = off`. CF иногда реактивирует дефолты после миграции плана.

## Зависимости

- `infrastructure/deployment.md` → «Sub-link CDN proxy» — почему вообще существует этот фронт.
- `operations/env-reference.md` → `SUB_LINK_BASE_URL` — где значение прописывается на backend-стороне.
- `operations/runbook.md` → «Clients перестали обновлять подписку» — когда этот worker в подозреваемых.
