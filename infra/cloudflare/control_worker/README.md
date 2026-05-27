# Control-channel CF Worker

Cloudflare Worker — proxy между custom-клиентом (Phase B) и нашим
backend'ом для `POST /api/client/report-failure`. Подробнее об
архитектуре — [`docs/operations/control_channel_roadmap.md`](../../../docs/operations/control_channel_roadmap.md).

## Что делает

1. Принимает `POST /report` от клиента (HTTPS).
2. Валидирует базовый shape (X-Client-ID есть, body ≤ 2KB).
3. Форвардит на backend по `X-Control-Channel-Secret`.
4. Возвращает ответ as-is клиенту.

Worker — **stateless**. Всю логику (rate-limit, target node selection,
migration) делает backend.

## Deploy с нуля

Требуется существующий CF аккаунт (используется тот же что для
`grn-ssync.pro`).

```bash
cd infra/cloudflare/control_worker
npm install -g wrangler  # один раз
wrangler login

# Деплой 3 Worker'ов для rotation (клиент перебирает random'ом):
for n in 1 2 3; do
    wrangler deploy --name "control-$n"
done

# Задать secrets для каждого Worker'а:
# APP_SECRET_KEY — тот же что в backend контейнере (vault_app_secret_key).
# Достать на mgmt:  docker compose exec -T worker env | grep APP_SECRET_KEY
APP_SECRET_KEY="<value-from-mgmt-env>"
BACKEND_URL="https://mgmt.grinwer.online"

for n in 1 2 3; do
    echo "$APP_SECRET_KEY" | wrangler secret put APP_SECRET_KEY --name "control-$n"
    echo "$BACKEND_URL" | wrangler secret put BACKEND_URL --name "control-$n"
done
```

URL'ы Worker'ов:
- `https://control-1.<account>.workers.dev`
- `https://control-2.<account>.workers.dev`
- `https://control-3.<account>.workers.dev`

После deploy'а — прописать список Worker URL'ов в подписочных данных
(`/api/sub/{token}` extra block для custom-клиента, Phase B). Backend
env'у дополнительных правок не требуется — APP_SECRET_KEY уже там.

## Rotation (при block'е RKN)

```bash
# Деплоим новый Worker:
wrangler deploy --name control-N+1
echo "$APP_SECRET_KEY" | wrangler secret put APP_SECRET_KEY --name "control-N+1"
echo "$BACKEND_URL" | wrangler secret put BACKEND_URL --name "control-N+1"

# Обновить подписочный generator на backend'е (env CONTROL_WORKER_URLS),
# add control-N+1 в список, remove самый старый.

# Через 60 дней (время чтобы все клиенты подхватили новый список через
# refresh подписки) удалить старый:
wrangler delete --name control-old
```

## Локальный dev

```bash
cd infra/cloudflare/control_worker
wrangler dev --local
# Будет на http://127.0.0.1:8787/report
# Не забыть set APP_SECRET_KEY + BACKEND_URL через --var.
```

## Smoke-тест после deploy'а

```bash
WORKER_URL="https://control-1.<account>.workers.dev"
CLIENT_ID="abcdefgh12345678"  # тестовый client_id_hmac известного Device

curl -sS -X POST "$WORKER_URL/report" \
    -H "Content-Type: application/json" \
    -H "X-Client-ID: $CLIENT_ID" \
    -d '{"kind":"user_reported","ts":1748400000,"current_node_id":12,"fail_count":1}' \
    | jq
```

Ожидаем `{"ok":true,"target_node_id":13,...,"action":"migrated"}`.

Если `{"action":"throttled"}` — недавно уже мигрировали, нормально.
Если `{"action":"no_target_available"}` — нет healthy target ноды,
проверь `/admin/nodes`.

## Что НЕ деплоим через ansible

CF Workers — это **не**-ansible инфра, deploy через wrangler. Это
осознанное архитектурное решение:

- ansible управляет нашими нодами и mgmt-стеком (см. [[deploy_via_ansible]]),
  но Worker'ы живут на инфре Cloudflare и управляются CF API.
- Wrangler CLI — единственный официальный путь.
- Сохранять Worker'ы в git как код (этот файл) — yes; деплоить через
  ansible — no.
