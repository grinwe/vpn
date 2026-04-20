# Deployment (control-plane хост)

Документ описывает **как** и **куда** выкатывается control-plane (backend + worker + bot + admin SPA + webapp + nginx + Let's Encrypt). VPN-ноды — отдельная история, см. `infrastructure/nodes.md`.

## Топология одного хоста

На момент написания (`inventories/prod/hosts.yml`) все три логические группы резолвятся на **один и тот же физический сервер** `45.14.244.140`:

```
                       ┌────────────────────────────────────────┐
                       │   45.14.244.140  (Ubuntu, один VPS)    │
                       │                                         │
  user → CF edge ───────▶ nginx :443  (host)                     │
                       │     │                                   │
                       │     ├── /admin/ ──▶ 127.0.0.1:8080  admin SPA
                       │     ├── /app/   ──▶ 127.0.0.1:8082  webapp SPA
                       │     ├── /api/   ──▶ 127.0.0.1:8000  backend FastAPI
                       │     └── /sub/   ──▶ 127.0.0.1:8000  backend (sub link)
                       │                                         │
                       │   docker compose (stack in /opt/vpn):  │
                       │     db, redis, backend, worker, bot,    │
                       │     admin, webapp                       │
                       │                                         │
                       │   prometheus + grafana                  │
                       │   (отдельный compose, /opt/vpn-monitoring)
                       │                                         │
                       │   TG long-poll out ◀── bot container    │
                       │   ansible-playbook out ◀── worker (ssh) │
                       └────────────────────────────────────────┘
```

Формально в inventory это три группы (`db_host`, `monitoring`, `web`), каждая с одним `ansible_host: 45.14.244.140`. Разделение существует **исключительно под будущий split** — сейчас play'ы `deploy_app_stack`, `monitoring_stack`, `deploy_web_frontend` крутятся на одной машине и делят её ресурсы.

Группа `vpn_nodes` в `inventories/prod/hosts.yml` **закомментирована**. Реальные ноды живут только в БД (таблица `vpn_nodes`, туда пишет NodeSelector/autoscaler) и в генерируемом воркером временном inventory-файле — см. `infrastructure/ansible.md`.

## docker-compose стек

Всё приложение — `docker-compose.yml` в корне репозитория. Семь сервисов:

| service | image | порт | назначение |
|---|---|---|---|
| `db` | `postgres:16` | — | БД. Volume `db_data`. Healthcheck `pg_isready`. |
| `redis` | `redis:7-alpine` | — | RQ-брокер + rate-limit store. `requirepass`, volume `redis_data`. |
| `backend` | build `./backend` | `127.0.0.1:8000:8000` | FastAPI — все три роутера в одном процессе. |
| `worker` | build `backend/Dockerfile.worker` | — | RQ-воркер + self-rescheduling cron ticks (warm-pool / balance / autoscale / drain / renewal). |
| `bot` | build `./bot` | — | aiogram long-poll + notification poller. |
| `admin` | build `./admin` | `127.0.0.1:8080:80` | Админ-SPA (Vite build → nginx в контейнере). |
| `webapp` | build `./webapp` | `127.0.0.1:8082:80` | Telegram Mini App (Vite build → nginx в контейнере). |

Все публично-наружные порты завязаны на `127.0.0.1:` — наружу ничего не торчит. Реверс-прокси (хостовой nginx) — единственный источник входящего трафика.

### depends_on и порядок старта

```
db ─┬─▶ backend ─▶ bot
    └─▶ worker
redis ─┬─▶ backend
       └─▶ worker
```

`db` и `redis` имеют healthcheck'и, `backend` и `worker` ждут `condition: service_healthy`. `bot` ждёт только `backend` (без health — backend'у достаточно подняться, чтобы long-poll бота начал что-то возвращать при запросах).

### Миграции — owned by backend

```
# docker-compose.yml:103 (worker)
SKIP_MIGRATIONS: "1"
```

Backend запускает `alembic upgrade head` на старте. Worker — **нет**, и этот кусок зафиксирован в компоузе жёстко. Причина в inline-комменте (`docker-compose.yml:98-102`): если оба процесса одновременно идут в alembic, они дерутся за advisory lock; проигравший висит **до освобождения**, т.е. никогда не доходит до `Worker.work()`, и все provisioning-таски намертво застревают в `pending`.

### Зеркалирование env между backend и worker

Часть переменных **намеренно** дублируется в обоих сервисах:

```
WARM_POOL_ENABLED           — backend для "assign?", worker для warmer tick
SUB_LINK_BASE_URL           — backend для рендера Device.connection_uri,
                              worker для того же в warm-pool/миграции
MAX_FREEZE_DAYS_PER_PERIOD  — backend для WebApp endpoint'ов,
                              worker для hourly-charge tick
REFERRAL_BONUS_KOPECKS      — backend для /referral,
                              worker для ledger-операций
```

Всё read-on-call: при смене значения **нужно рестартовать оба контейнера**. `.env` → `docker compose up -d` не сам по себе перечитывает — handler `recreate app stack` (`deploy_app_stack/handlers/main.yml`) форсит `--force-recreate` когда env / ключ изменились.

### PROVISIONING_SSH_KEY — foot-gun в дефолте

```yaml
# docker-compose.yml:143-144
volumes:
  - ${PROVISIONING_SSH_KEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...provisioning@grinwer}:/run/secrets/provisioning_key:ro
```

Если `PROVISIONING_SSH_KEY` не задан в `.env`, docker попытается смонтировать **строку с публичным ключом** как хост-путь. На практике это значит: compose либо упадёт, либо создаст папку с таким именем. Ничего похожего на «упало ясным сообщением» не происходит — просто worker не сможет ходить на ноды. См. ⚠️ ниже.

Правильный путь — положить приватный ключ рядом (`secrets/provisioning_key`, mode 0600) и указать `PROVISIONING_SSH_KEY=./secrets/provisioning_key` в `.env`.

## Хостовой nginx + Cloudflare + Let's Encrypt

nginx живёт **на хосте**, не в docker. Один vhost — `grinwer.online`, рендерится ролью `deploy_web_frontend` из `nginx-site.conf.j2`.

### TLS-цепочка

```
browser ──TLS──▶ CF edge ──TLS (Full strict)──▶ origin nginx :443 ──HTTP──▶ 127.0.0.1 docker
```

- **CF Full (strict)** обязателен. `Flexible` режим отдаёт CF → origin по HTTP/:80, nginx там редиректит обратно на HTTPS → **бесконечный цикл**. Комментарий в шаблоне (`nginx-site.conf.j2:14-16`) явно это фиксирует.
- Сертификат на origin — обычный Let's Encrypt, полученный через DNS-01 у Cloudflare (plugin `certbot-dns-cloudflare`). `ssl_protocols TLSv1.2 TLSv1.3`, Mozilla Intermediate cipher list, OCSP stapling **выключен** (CF кэширует OCSP сам).
- HSTS 15768000 секунд ставится nginx'ом, **даже при том**, что CF сам ставит HSTS — belt-and-braces на случай, если кто-то однажды начнёт ходить на origin минуя CF.

### Cert bootstrap — порядок

В `deploy_web_frontend/tasks/main.yml` шаги идут намеренно так:

1. `apt install nginx certbot python3-certbot-dns-cloudflare`.
2. `ufw allow 80,443`.
3. Удалить `sites-enabled/default`.
4. `service nginx start` (nginx стартует с пустым конфигом — на :80 только дефолт, который сразу сняли).
5. **Снять сертификат** через `certbot certonly --dns-cloudflare` — **до** рендера TLS-vhost'а. Причина: TLS-vhost ссылается на `/etc/letsencrypt/live/grinwer.online/fullchain.pem`, которого ещё не существует, и nginx бы отказался ребутиться.
6. Отрендерить `nginx-site.conf.j2` → `sites-available/` + symlink в `sites-enabled/`.
7. `reload nginx` (через handler).

`certbot.timer` поднимается в `enabled`, обновления идут in-place без гонок с нашим templating'ом.

### Routing

```
/            → 302 /admin/
/admin/      → 127.0.0.1:8080/admin/   (admin SPA, base: "/admin/")
/app/        → 127.0.0.1:8082/app/     (webapp SPA, base: "/app/")
/api/        → 127.0.0.1:8000          (backend FastAPI, полный /api/* префикс)
/sub/        → 127.0.0.1:8000          (backend, вне /api/ ради коротких URL)
остальное    → 404
```

Почему `/sub/` — вне `/api/`: пользователь копирует subscription-URL в Hiddify/v2rayNG. Длинный `https://domain/api/sub/<token>` против короткого `https://domain/sub/<token>` — эстетика и меньше шанс, что человек его обрежет. Роут статически прописан в шаблоне nginx, backend про разницу ничего не знает (маршрут `/sub/<token>` у FastAPI работает на любом префиксе).

### Cloudflare real-IP

`deploy_web_frontend` на controller'е скачивает `https://www.cloudflare.com/ips-v4` и `ips-v6` (через `delegate_to: localhost`), рендерит их в `conf.d/cloudflare-real-ip.conf` как `set_real_ip_from` список + `real_ip_header CF-Connecting-IP`. Без этого все клиенты логировались бы как CF edge IP, и rate-limit'ы backend'а тоже считались бы по ним. Списки CF **не автообновляются** — задача вручную перезапустить роль, если CF расширит пулы.

## Ansible-flow для control-plane — `deploy_app_stack`

Роль `deploy_app_stack/tasks/main.yml`, шаги по порядку:

### 1. Pre-flight: assert секретов

```yaml
# deploy_app_stack/tasks/main.yml:6-17
- name: Assert required secrets are set
  assert:
    that:
      - deploy_app_stack_postgres_password | default('', true) | length >= 12
      - deploy_app_stack_admin_api_token   | default('', true) | length >= 20
      - deploy_app_stack_app_secret_key    | default('', true) | length >= 16
      - deploy_app_stack_bot_token         | default('', true) | length > 0
      - deploy_app_stack_admin_ids         | default('', true) | length > 0
      - deploy_app_stack_webapp_jwt_secret | default('', true) | length >= 32
```

Идея: лучше упасть **до** того, как контейнеры начнут разворачиваться с placeholder-паролем, чем получить рабочий стек с `POSTGRES_PASSWORD=changeme`. Минимальные длины — эвристика «заметно длиннее любого default'а».

### 2. Docker-probe

```yaml
# deploy_app_stack/tasks/main.yml:25-30
- name: Check if docker is already installed
  command: docker --version
  failed_when: false
```

На чистом хосте `apt install docker.io docker-compose-v2` отрабатывает. На хостах, где уже установлен `docker-ce + containerd.io` из docker.com (такие хосты приходят из соседнего проекта `vpn-setup` — CDN-стек ставит docker от Docker Inc.), попытка накатить `docker.io` **поломалась бы** из-за конфликта `containerd` (distro) vs `containerd.io` (docker.com). Probe делает инсталл условным: `when: _docker_probe.rc != 0`.

### 3. rsync репо в `/opt/vpn`

```yaml
rsync --delete --exclude=.git --exclude=__pycache__ --exclude=admin/node_modules
     --exclude=backend/.venv --exclude=.env --exclude=.env.* --exclude=secrets/
```

Список исключений консервативный: `.env` и `secrets/` **должны** быть на target'е отдельно управляемыми и не перезаписываться controller'ом. Флаг `delete: true` — следовательно старые файлы в `/opt/vpn`, которых больше нет в репо, удаляются. Результат записывается в `repo_sync.changed` — используется дальше как триггер билда.

### 4. Рендер `.env`

`.env` генерируется из `env.j2` с подстановкой всех `deploy_app_stack_*` переменных (секреты из vault + настройки из `group_vars/web/main.yml`), mode 0600. Этот файл — единственный источник переменных для `docker-compose.yml`, все `${FOO}` в compose разрешаются оттуда.

### 5. Provisioning SSH key (опционально)

```yaml
# deploy_app_stack/tasks/main.yml:109-117
- name: Install provisioning SSH key (if provided)
  copy:
    src: "{{ deploy_app_stack_provisioning_key_src }}"
    dest: "{{ deploy_app_stack_provisioning_key_dst }}"
    mode: "0600"
  when: deploy_app_stack_provisioning_key_src | length > 0
```

Если `deploy_app_stack_provisioning_key_src` пуст — задача пропускается, worker поднимается, но любой `ansible-playbook` из него будет падать на SSH-аутентификации. Явно допустимое состояние **первичного bootstrap'а**, когда ключа ещё нет.

### 6. Build + up

```yaml
# deploy_app_stack/tasks/main.yml:119-128
- name: Build docker images
  command: docker compose build --no-cache
  when:
    - deploy_app_stack_build | bool
    - repo_sync.changed
```

`--no-cache` + гейт `repo_sync.changed` = пересборка **только** когда реально что-то синкнулось. Повторный запуск роли против неизменённого дерева практически бесплатен.

```yaml
- name: Bring the stack up
  command: docker compose up -d {{ '--force-recreate' if deploy_app_stack_force_recreate else '' }}
```

`up -d` всегда запускается (не гейтится), но compose сам никого не перезапускает, если контейнер не изменился.

### 7. Health gate на `/healthz`

```yaml
# deploy_app_stack/tasks/main.yml:138-145
- name: Wait for backend to be healthy
  uri:
    url: "http://127.0.0.1:8000/healthz"
  retries: 30
  delay: 2
  until: backend_health.status == 200
```

60 секунд максимум. Если `backend` не поднялся — роль падает с понятным сообщением.

### 8. Crash-loop settle

```yaml
# deploy_app_stack/tasks/main.yml:153-168
- name: Settle compose (give crash loops time to become visible)
  pause:
    seconds: 10

- name: Collect compose service status
  command: docker compose ps --format '{{ "{{" }}.Name{{ "}}" }} {{ "{{" }}.State{{ "}}" }}'

- name: Fail if any compose service is restarting or exited
  assert:
    that:
      - "'restarting' not in compose_ps.stdout | lower"
      - "'exited' not in compose_ps.stdout | lower"
```

Смысл: у `worker` и `bot` **нет** HTTP-эндпоинта, на который можно было бы поставить health gate. Классический crash loop (aiogram API drift, отсутствующий env-var) проявляется как «контейнер поднимается → падает → docker его перезапускает». Без дополнительной паузы первый `docker compose ps` покажет «Up 1s» и роль отчитается зелёной. 10 секунд — эмпирический компромисс «достаточно, чтобы один цикл падения произошёл» / «не слишком долго для обычного деплоя».

### 9. Handler `recreate app stack`

```yaml
# deploy_app_stack/handlers/main.yml
- name: recreate app stack
  command: docker compose up -d --force-recreate
```

Нотифаится любым шагом, который поменял **не исходник**: render `.env`, install SSH key, rsync. Нужен затем, что compose по умолчанию не перезапустит контейнер при изменении bind-mount'а или env-файла — `--force-recreate` заставляет.

## Monitoring stack

Prometheus + Grafana живут в отдельном `docker-compose.yml` под `/opt/vpn-monitoring` (роль `monitoring_stack`). Порты Prometheus/Grafana bind'ятся на `127.0.0.1` — доступ через SSH-tunnel (`ssh -L 3000:127.0.0.1:3000 root@45.14.244.140`). Это сознательный выбор: ничего дополнительного наружу не торчит, auth делегирован SSH'у.

Prometheus scrape'ит `http://backend:8000/metrics` через docker network (сеть compose backend'а).

**Node coverage.** Prometheus job'ы рендерятся из inventory: `vpn-nodes` (группа `vpn_nodes`, RU-relay) и `wg-exit-nodes` (группа `wg_exit_nodes`, non-RU exit). Monitoring play в `site.yml` запускается на объединении этих групп (`vpn_nodes:wg_exit_nodes`) — роль `node_exporter` ставит экспортёр на каждой ноде, firewall-правило открывает порт 9100 **только** для IP из `node_exporter_allowed_ips` (см. `group_vars/all.yml`). Добавили ноду в inventory — перекатили `--tags monitoring`, в `prometheus.yml` появится новая target'а.

**Docker install идемпотентен.** Роль сначала проверяет `docker --version`; если Docker уже стоит (например, Docker CE из официального репо), `apt install docker.io` пропускается — иначе apt ломается на конфликте пакетов `docker.io` vs `docker-ce` + `containerd.io`.

**Grafana datasource uid.** Provisioned datasource шаблон явно задаёт `uid: prometheus` — дашборды в `docs/dashboards/` ссылаются на `{ type: prometheus, uid: "prometheus" }`, без явного uid они отваливались с «Datasource prometheus was not found».

## Sub-link CDN proxy

`Device.connection_uri` содержит «dynamic subscription URL» формата `<SUB_LINK_BASE_URL>/<sub_token>`. Если указать прямой `https://grinwer.online/api/sub/<token>` — RKN-блок основного домена уложит всех installed-клиентов. Поэтому фронт — отдельный «boring» домен на Cloudflare.

Текущий рабочий конфиг:

- **Домен:** `grn-ssync.pro` (CF-зона, Pro plan).
- **Worker:** `v8-sub` — делает `fetch(https://grinwer.online/api/sub/${token})` и стримит ответ обратно. Код есть в CF Dashboard Workers; в репо не коммитим (короткий, держим ближе к инфре).
- **CF Protocol settings:** `HTTP/2 = off`, `HTTP/3 = off`. **Критично**: RKN DPI на мобильном 4G режет H2 stream после TLS-handshake — headers доходят, тело 584 байта теряется. HTTP/1.1 проскакивает. На Free-плане CF тумблер HTTP/2 заблокирован — нужен Pro.
- **env:** `SUB_LINK_BASE_URL=https://grn-ssync.pro`. Зеркалится в backend и worker (см. `operations/env-reference.md`).
- **Миграция существующих Device'ов:** **не делаем**. Старые строки с `https://grinwer.online/...` остаются; установленные клиенты продолжают работать до тех пор, пока домен не заблочат окончательно. Новые Device'ы (provisioning после деплоя env) получают новый URL. По жалобам — правим `connection_uri` вручную по `id`.

## Volumes и persistence

```yaml
volumes:
  db_data:       # postgres, /var/lib/postgresql/data
  redis_data:    # redis, /data (AOF)
```

Оба — named volumes, живут под `/var/lib/docker/volumes/` на хосте. **Не** bind-mount'ы в `/opt/vpn`, т.е. `rsync --delete` в `deploy_app_stack` их не тронет. `docker compose down -v` — единственный способ случайно снести БД; обычный `down` их оставит.

Резервных копий postgres на уровне compose-стека **нет**. Backup-стратегия не зафиксирована в документации — любые pg_dump'ы запускаются руками или отдельным systemd-timer'ом за пределами репо.

## Кто дёргает `deploy_app_stack`

`site.yml` (см. `infrastructure/ansible.md`) содержит play для группы `web`:

```
- hosts: web
  roles:
    - deploy_web_frontend   # nginx + certbot + Cloudflare real-IP
    - deploy_app_stack      # docker compose up
```

Следовательно полный control-plane деплой на чистый хост:

```bash
cd infra/ansible
ansible-playbook -i inventories/prod/hosts.yml site.yml -l nl-web --ask-vault-pass
```

Первый прогон снимает сертификат (может занять ~30с на DNS propagation), последующие — только rsync + `up -d`.

## ⚠️ Неясные места

- **`PROVISIONING_SSH_KEY` дефолт — literal ed25519 public key в compose.** При пустом env-var compose пытается смонтировать строку-ключ как путь, поведение платформозависимое и в лучшем случае молчаливое. Никакой pre-flight проверки в `deploy_app_stack` на это нет — роль пройдёт, worker поднимется, provisioning упадёт по SSH при первом же таске.
- **`SKIP_MIGRATIONS=1` жёстко прибит в compose, а не в env.** Убрать его можно только редактированием `docker-compose.yml`. Это хорошо (защита от гонки), но рядом с другими env-vars, которые настраиваются через `.env`, выглядит несимметрично — читатель может не заметить разницы.
- **10-секундный settle после `up -d`.** Число выбрано эвристически (`deploy_app_stack/tasks/main.yml:155`), без замеров. Медленный crash-loop с интервалом >10с пройдёт гейт незамеченным.
- **Single-host для всех трёх логических ролей.** `db_host`, `monitoring`, `web` ссылаются на `45.14.244.140`. Компрометация / отказ этого хоста = полный outage control-plane. HA-стратегия в коде никак не зафиксирована, inventory явно комментирует «splitting is a trivial inventory change later».
- **Cloudflare edge → origin зависимость.** Без CF зоны `grinwer.online` домен не резолвится (origin IP — `45.14.244.140`, но публичного DNS A-record на него без CF нет по дизайну). Отзыв CF API-токена или zone миграция = ломается renewal сертификата через DNS-01, и рендер vhost'а начнёт падать при следующем прогоне роли.
- **Monitoring и web — один и тот же docker daemon.** Два compose-проекта под `/opt/vpn` и `/opt/vpn-monitoring` делят сеть, volume namespace, CPU, диск. Явная изоляция между ними — только через префиксы проектов compose. Для проверки «что ест диск» нужно залезать в оба.
- **Нет backup'ов Postgres/Redis в публичных ролях репо.** `db_data` и `redis_data` — named volumes, любая операция `docker volume rm` необратимо уничтожит БД. Отдельного `pg_dump` cron'а в compose нет.
