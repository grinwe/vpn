// Превращает сырой текст ошибки fetch ("402: {...}", "500: ...")
// в человекочитаемое сообщение. Юзеру нельзя показывать JSON / коды —
// всё сюда.
//
// Если вызывающая страница хочет специфический текст под конкретный
// экшен, передаёт его через ``fallback``. Базовые паттерны (сеть,
// 4xx/5xx, timeout) всё равно перехватываются этим хелпером — чтобы
// нигде не забыть про «Сервис временно недоступен».

export interface FriendlyOpts {
  /** Русское название операции для fallback'а — «Не удалось {fallback}». */
  fallback?: string;
}

const NETWORK_HINT =
  "Нет связи с сервером. Проверь интернет и попробуй ещё раз.";
const UNAVAILABLE_HINT =
  "Сервис временно недоступен. Попробуй ещё раз через минуту.";
const SERVER_HINT =
  "Что-то пошло не так на нашей стороне. Напиши в поддержку через раздел «Помощь» — разберёмся.";
const SESSION_HINT = "Сессия истекла. Закрой и снова открой приложение.";
const BUSY_SERVERS_HINT =
  "Сейчас нет свободных серверов. Мы уже знаем — попробуй чуть позже или напиши в поддержку через раздел «Помощь».";

export function friendlyError(raw: string, opts: FriendlyOpts = {}): string {
  const fallback = opts.fallback
    ? `Не удалось ${opts.fallback}. Попробуй ещё раз или напиши в поддержку.`
    : "Не удалось выполнить операцию. Попробуй ещё раз или напиши в поддержку.";

  // Недоступность сервисов: нет ноды / нет триал-плана / нет провайдера
  if (
    /^503/.test(raw) ||
    /no.*node/i.test(raw) ||
    /no trial plan/i.test(raw)
  ) {
    return BUSY_SERVERS_HINT;
  }
  // Upstream / gateway
  if (/^(502|504)/.test(raw)) return UNAVAILABLE_HINT;
  // Необработанная серверная ошибка
  if (/^500/.test(raw)) return SERVER_HINT;
  // Protected-resource ошибки
  if (/^(401|403)/.test(raw)) return SESSION_HINT;
  // Сетевая/CORS/fetch абортнулся — в webapp'е такие обычно летят как
  // "Failed to fetch" или "TypeError: NetworkError".
  if (/failed to fetch|network|timeout/i.test(raw)) return NETWORK_HINT;

  // 4xx/5xx с читаемым detail — показываем только текст, без JSON и кода
  const m = /^\d+:\s*(.+)$/s.exec(raw);
  if (m) {
    const detail = m[1].trim();
    // JSON detail — не показываем юзеру
    if (detail.startsWith("{") || detail.startsWith("[")) return fallback;
    // Известные внутренние сообщения бэкенда — пропускаем, если
    // выглядят человекочитаемо (кириллица или нормальный английский).
    if (detail.length > 0 && detail.length <= 200) return detail;
  }
  return fallback;
}

/**
 * Достаёт ``suggested_topup_kopecks`` из 402-тела, если оно там есть.
 * Используется /activate, /change_plan, /subscriptions/{id}/devices —
 * при нехватке баланса фронт предлагает пополнить ровно на нужную сумму.
 */
export function parseInsufficientBalance(raw: string): number | null {
  const m = /402.*suggested_topup_kopecks["']?\s*:\s*(\d+)/.exec(raw);
  return m ? Number(m[1]) : null;
}
