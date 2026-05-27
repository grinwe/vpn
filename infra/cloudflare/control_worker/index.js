// Cloudflare Worker — control-channel proxy для custom-клиента.
//
// Принимает POST /report от клиента, валидирует базовый shape, форвардит
// на наш backend по shared secret. Сам Worker URL — не публикуется
// открыто, передаётся клиенту в подписочных данных.
//
// Деплоим 3 штуки (control-1, control-2, control-3) для rotation —
// клиент перебирает random'ом, retry на следующий при error/timeout.
//
// Secrets (через `wrangler secret put`):
//   APP_SECRET_KEY — переиспользует существующий backend-секрет (тот же
//                    что Fernet'ит WG keys/cred'ы в БД). Один rotation
//                    point для всего control-channel'а.
//   BACKEND_URL    — https://mgmt.grinwer.online (не палится клиенту,
//                    живёт в Worker'е, не в подписочных данных)
//
// Не делаем здесь:
//   - Rate-limit per client_id — на backend'е через slowapi.
//     На Worker-уровне можно добавить через Durable Objects, если будет
//     нужно ограничить burst до того как он дойдёт до origin'а.
//   - Validation JSON shape — backend Pydantic схема проверит, дублировать
//     не имеет смысла, payload и так маленький.
//   - Логи с client_id — Worker логи в CF dashboard видны, не хотим
//     дополнительно identifier'ить клиентов на стороне CF.

const MAX_BODY_BYTES = 2048;

export default {
  async fetch(req, env) {
    if (req.method !== "POST") {
      return jsonResp(405, { error: "method not allowed" });
    }
    const url = new URL(req.url);
    if (url.pathname !== "/report") {
      return jsonResp(404, { error: "not found" });
    }

    const clientId = req.headers.get("X-Client-ID");
    if (!clientId || clientId.length < 8 || clientId.length > 32) {
      return jsonResp(400, { error: "missing or malformed X-Client-ID" });
    }

    // Bound body size — control-channel payload должен оставаться
    // маленьким (~150 байт). Cap на 2 KB защищает origin от gigabytes
    // через скомпрометированный client_id.
    const body = await req.text();
    if (body.length > MAX_BODY_BYTES) {
      return jsonResp(413, { error: "body too large" });
    }

    // Origin URL не палится клиенту — только Worker про него знает.
    // Если злоумышленник перехватит ответ от Worker'а — он не увидит
    // BACKEND_URL, только Worker forwards.
    const backendUrl = env.BACKEND_URL;
    const secret = env.APP_SECRET_KEY;
    if (!backendUrl || !secret) {
      // Deployment misconfig — не пропускаем POST к origin'у.
      return jsonResp(503, { error: "control channel misconfigured" });
    }

    let backendResp;
    try {
      backendResp = await fetch(`${backendUrl}/api/client/report-failure`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Control-Channel-Secret": secret,
          "X-Client-ID": clientId,
        },
        body,
        // CF Worker default timeout ~30s — нам хватит для backend'а,
        // даже если migrate_subscription_to_new_node делает ansible-run
        // (~10s warm-path / ~60s cold-path; на cold backend всё же
        // отвечает быстро, ансибл уходит в фон).
      });
    } catch (e) {
      return jsonResp(502, { error: "backend unreachable", detail: String(e) });
    }

    // Прокидываем статус + тело назад клиенту as-is. Не модифицируем,
    // чтобы клиент мог различить throttled / migrated / no_target_available
    // по action поле в response payload.
    const respBody = await backendResp.text();
    return new Response(respBody, {
      status: backendResp.status,
      headers: { "Content-Type": "application/json" },
    });
  },
};

function jsonResp(status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
