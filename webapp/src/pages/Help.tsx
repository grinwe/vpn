import { useState } from "react";
import { navigate } from "../router";
import {
  fetchRepairState,
  reportBrokenAll,
  reportBrokenDevice,
  reportOk,
  reportStillBroken,
  setReportOperator,
  RepairAction,
  RepairDevice,
  RepairResponse,
  RepairScope,
  RepairState,
  VPN_OPERATORS,
} from "../api";

interface Props {
  botUsername?: string;
}

// Исход шага починки в том виде, в каком его показываем: action + секунды
// ожидания (throttled) + чьё устройство (migrated) + масштаб (одно
// устройство / вся подписка). Один тип и для ответа POST, и для пре-чека
// repair-state.
interface RepairView {
  action: RepairAction;
  retryAfterSec: number | null;
  deviceName: string | null;
  scope: RepairScope;
}

// Машина состояний кнопки «VPN сейчас не работает». Тот же флоу, что у бота
// и у страницы на саб-домене (docs/operations/
// vpn_broken_channels_parity_2026_09_12.md), одно ядро на бэке:
//   idle → (тап) pending → GET repair-state →
//     · wait_reason → outcome (throttled / daily_limit)
//     · 0 устройств → outcome (no_subscription; not_ready, если все pending)
//     · 1 устройство → POST report-broken-device → outcome по action
//     · >1 → pick_device → POST по устройству или «все мои» → outcome
//   успешный исход (migrated / reshuffled / duplicated) → pick_operator →
//     feedback (✅ report-ok / ❌ report-still-broken) → done_ok / done_fail.
// После любого неуспеха кнопка снова активна сразу — повторы троттлит
// сервер, клиентских кулдаунов нет. Только транспортная ошибка держит
// текст 5 с.
type Step =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "error" }
  | { kind: "pick_device"; devices: RepairDevice[] }
  | { kind: "outcome"; view: RepairView }
  | { kind: "pick_operator"; reportId: number; view: RepairView }
  | {
      kind: "feedback";
      reportId: number;
      view: RepairView;
      busy: boolean;
      failed: boolean;
    }
  | { kind: "done_ok" }
  | { kind: "done_fail" };

const ERROR_TEXT = "Не получилось обработать — попробуй ещё раз через минуту.";

const SUCCESS_ACTIONS: ReadonlyArray<RepairAction> = [
  "migrated",
  "reshuffled",
  "duplicated",
];
// Исходы, где текст зовёт в поддержку — рядом даём ссылку.
const SUPPORT_ACTIONS: ReadonlyArray<RepairAction> = ["daily_limit", "no_target"];

// Имена, которые ставит провижининг, а не человек (primary, device-6…) —
// зеркало _TECH_NAME_RE страницы: «поменяли сервер для «device-6»» — это
// разговор с инженером, а не с пользователем.
const TECH_NAME_RE = /^(primary|device|устройство|default|user)[\s_-]*\d*$/i;

function whoseServer(view: RepairView): string {
  if (view.scope === "subscription") return "для всех устройств";
  const name = (view.deviceName ?? "").trim();
  if (!name || TECH_NAME_RE.test(name)) return "для этого устройства";
  return `для «${name}»`;
}

// M = max(1, ceil(retry_after_sec / 60)) — как у бота.
function waitMinutes(retryAfterSec: number | null): number {
  return Math.max(1, Math.ceil((retryAfterSec ?? 0) / 60));
}

// Тексты исходов — единая таблица (те же формулировки, что у бота).
function outcomeText(view: RepairView): string {
  switch (view.action) {
    case "migrated":
      return (
        `🔄 Поменяли сервер ${whoseServer(view)}. Подписка обновится в ` +
        "приложении сама — нажми 🔄 рядом с профилем и попробуй подключиться " +
        "через пару минут."
      );
    case "reshuffled":
      return (
        "🔀 Переключили тебя на другой способ подключения — чаще всего не " +
        "работает именно он, а не сам сервер. Нажми 🔄 рядом с профилем в " +
        "приложении и попробуй подключиться."
      );
    case "duplicated":
      return (
        "➕ Добавили тебе запасной сервер по тому способу связи, который у " +
        "тебя работает. Нажми 🔄 рядом с профилем — в списке появится ещё " +
        "один вариант."
      );
    case "throttled":
      return (
        "👍 Мы уже переключали тебя пару минут назад. Нажми 🔄 рядом с " +
        "профилем и попробуй подключиться. Если через " +
        `${waitMinutes(view.retryAfterSec)} мин. всё ещё не работает — нажми ` +
        "кнопку ещё раз."
      );
    case "daily_limit":
      return (
        "Сегодня мы уже несколько раз меняли тебе серверы — дальше нужна " +
        "помощь человека. Напиши в поддержку."
      );
    case "no_target":
      return (
        "Не смогли автоматически подобрать другой сервер. Попробуй через " +
        "10 минут — серверы освобождаются постоянно. Если срочно — напиши в " +
        "поддержку."
      );
    case "not_ready":
      return (
        (view.scope === "subscription"
          ? "⏳ Устройства ещё настраиваются — подожди пару минут и нажми 🔄 "
          : "⏳ Устройство ещё настраивается — подожди пару минут и нажми 🔄 ") +
        "рядом с профилем. Если через 10 минут не заработает — нажми кнопку " +
        "ещё раз."
      );
    case "no_subscription":
      return (
        "Чинить нечего: подписка не активна или это устройство уже " +
        "отключено. Проверь подписку в кабинете."
      );
    default:
      // Неизвестный action (бэк новее закэшированного бандла) — честный
      // нейтральный текст вместо ложного «готово».
      return ERROR_TEXT;
  }
}

const FAQ: { title: string; body: string }[] = [
  {
    title: "Кабинет не открывается",
    body:
      "Попробуй по порядку:\n\n" +
      "• Если ты на Wi-Fi — выключи его и зайди через мобильный интернет (или наоборот)\n" +
      "• Попробуй открыть кабинет с включённым VPN — иногда провайдер режет наш домен\n" +
      "• Длинное нажатие на кнопку «Открыть личный кабинет» → «Открыть в браузере»\n" +
      "• Обнови страницу (меню браузера → ⟳)\n" +
      "• Полностью закрой Telegram и открой заново",
  },
  {
    title: "VPN не подключается / медленный",
    body:
      "Чек-лист:\n\n" +
      "• Нажми «Сообщить, что VPN сейчас не работает» ниже — переключим тебя автоматически\n" +
      "• Обнови подписку в VPN-клиенте — нажми 🔄 рядом с профилем, чтобы подтянуть актуальный конфиг\n" +
      "• Проверь баланс в личном кабинете — при 0 ₽ доступ блокируется\n" +
      "• Обнови клиент (Hiddify / v2rayNG / Streisand) до последней версии\n" +
      "• Выключи другие VPN-профили — два VPN одновременно не работают\n" +
      "• Перезагрузи телефон\n" +
      "• Удали старый профиль и импортируй конфиг заново из кабинета\n" +
      "• Проверь, что подписка активна (раздел «Мои подписки»)",
  },
  {
    // С 15.04.2026 Яндекс, банки, маркетплейсы и госсервисы начали закрываться
    // при включённом VPN — массовая боль, и у нас на неё есть ответ. Текст
    // осторожный по одной причине: RU-обход живёт на relay-нодах (там `direct`
    // уходит в WG), а на standalone-зарубежной ноде правило — безвредный no-op
    // (docs/infrastructure/nodes.md § RU-обход). По протоколам оговорка больше
    // не нужна: с 2026-07-28 все четыре (reality/xhttp/ws-cdn/hysteria2) ходят
    // одинаково.
    title: "Российские сайты и банки при включённом VPN",
    body:
      "Многие сервисы (Яндекс, банки, маркетплейсы, госуслуги) с весны 2026 " +
      "закрываются, если видят VPN. У нас для этого есть раздельный маршрут: " +
      "на российских серверах трафик к российским сайтам идёт напрямую, " +
      "мимо VPN — выключать его каждый раз не нужно.\n\n" +
      "Работает не на всех серверах. Если что-то российское не открывается — " +
      "не отключай VPN целиком, а напиши в поддержку: подберём подходящий " +
      "сервер.",
  },
  {
    title: "Как добавить устройство",
    body:
      "В карточке подписки нажми «+ Добавить устройство». " +
      "Каждое дополнительное устройство сверх тарифа добавляет ~100 ₽/мес к стоимости. " +
      "Новый конфиг появится в списке — скопируй его или отсканируй QR.",
  },
];

export default function Help({ botUsername }: Props) {
  const [open, setOpen] = useState<number | null>(null);
  const [step, setStep] = useState<Step>({ kind: "idle" });

  const supportUrl = botUsername
    ? `https://t.me/${botUsername}?start=support`
    : null;

  // Транспортная ошибка / не-200: текст на 5 с, потом кнопка снова idle.
  // Функциональный setState: если юзер за эти 5 с уже тапнул снова, его
  // pending не затираем. Таймер не чистим при размонтировании намеренно —
  // React 18 на setState после unmount не ругается, а лишний ref/effect
  // только добавил бы места для ошибки в порядке хуков.
  const showError = () => {
    setStep({ kind: "error" });
    setTimeout(() => {
      setStep((s) => (s.kind === "error" ? { kind: "idle" } : s));
    }, 5000);
  };

  const applyOutcome = (res: RepairResponse) => {
    const view: RepairView = {
      action: res.action,
      retryAfterSec: res.retry_after_sec,
      deviceName: res.device_name,
      scope: res.scope,
    };
    if (SUCCESS_ACTIONS.includes(res.action) && res.report_id != null) {
      setStep({ kind: "pick_operator", reportId: res.report_id, view });
      return;
    }
    // Неуспех (или успех без report_id — спрашивать оператора не о чем):
    // показываем текст, кнопка активна сразу — повторы троттлит сервер.
    setStep({ kind: "outcome", view });
  };

  const runRepair = async (call: () => Promise<RepairResponse>) => {
    setStep({ kind: "pending" });
    try {
      applyOutcome(await call());
    } catch (err) {
      console.error("self-repair failed", err);
      showError();
    }
  };

  const handleReportBroken = async () => {
    if (step.kind === "pending") return;
    setStep({ kind: "pending" });
    let state: RepairState;
    try {
      state = await fetchRepairState();
    } catch (err) {
      console.error("fetchRepairState failed", err);
      showError();
      return;
    }
    // Пре-чек: ждать по единой политике повторов — независимо от числа
    // устройств (жалобу бэк уже записал).
    if (state.wait_reason) {
      setStep({
        kind: "outcome",
        view: {
          action: state.wait_reason,
          retryAfterSec: state.retry_after_sec,
          deviceName: null,
          scope: "device",
        },
      });
      return;
    }
    if (state.devices.length === 0) {
      // Подписка есть, живых устройств нет → все ещё pending (собираются);
      // подписки нет → чинить нечего.
      setStep({
        kind: "outcome",
        view: {
          action: state.subscription_id != null ? "not_ready" : "no_subscription",
          retryAfterSec: null,
          deviceName: null,
          scope: "subscription",
        },
      });
      return;
    }
    if (state.devices.length === 1) {
      const only = state.devices[0];
      await runRepair(() => reportBrokenDevice(only.id));
      return;
    }
    // >1 устройства → спросим, какое именно барахлит, и перетряхнём ТОЛЬКО
    // его; либо «все мои устройства» → перенос всей подписки.
    setStep({ kind: "pick_device", devices: state.devices });
  };

  // null = «Пропустить». Сразу идём дальше — оператор best-effort
  // (advisory-сигнал для матрицы), не блокируем юзера, если запрос не дойдёт.
  const handlePickOperator = async (operator: string | null) => {
    if (step.kind !== "pick_operator") return;
    const { reportId, view } = step;
    setStep({ kind: "feedback", reportId, view, busy: false, failed: false });
    if (operator == null) return;
    try {
      await setReportOperator(reportId, operator);
    } catch (err) {
      console.error("setReportOperator failed", err);
    }
  };

  const handleFeedback = async (ok: boolean) => {
    if (step.kind !== "feedback" || step.busy) return;
    const { reportId } = step;
    setStep({ ...step, busy: true, failed: false });
    try {
      if (ok) await reportOk(reportId);
      else await reportStillBroken(reportId);
      setStep({ kind: ok ? "done_ok" : "done_fail" });
    } catch (err) {
      // «Не помогло» — самый весомый сигнал для матрицы, терять его молча
      // нельзя: кнопки остаются на месте, рядом текст ошибки.
      console.error("repair feedback failed", err);
      setStep({ ...step, busy: false, failed: true });
    }
  };

  // Текст над основной кнопкой (исход, ошибка, финал) + ссылки к нему.
  let statusText: string | null = null;
  let showSupport = false;
  let showPlans = false;
  if (step.kind === "outcome") {
    statusText = outcomeText(step.view);
    showSupport = SUPPORT_ACTIONS.includes(step.view.action);
    showPlans = step.view.action === "no_subscription";
  } else if (step.kind === "error") {
    statusText = ERROR_TEXT;
  } else if (step.kind === "done_ok") {
    statusText = "Отлично, рад что заработало! 🎉";
  } else if (step.kind === "done_fail") {
    statusText =
      "Жаль, что не помогло 😕 Передаём в поддержку — опиши проблему и " +
      "приложи модель устройства.";
    showSupport = true;
  }

  return (
    <div className="p-4 max-w-xl mx-auto space-y-4">
      <button
        onClick={() => navigate({ name: "home" })}
        className="text-tg-link text-sm"
      >
        ← Назад
      </button>

      <h1 className="text-lg font-semibold">Помощь</h1>

      <div className="space-y-2">
        {FAQ.map((item, i) => (
          <div key={i} className="card">
            <button
              className="w-full text-left flex items-center justify-between gap-2"
              onClick={() => setOpen(open === i ? null : i)}
            >
              <span className="font-medium text-sm">{item.title}</span>
              <span className="text-tg-hint text-xs shrink-0">
                {open === i ? "▲" : "▼"}
              </span>
            </button>
            {open === i && (
              <div className="mt-2 text-tg-hint text-sm whitespace-pre-line leading-relaxed">
                {item.body}
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="pt-2 border-t border-white/10">
        <p className="text-tg-hint text-xs mb-3">
          Не нашёл ответ? Напиши нам — отвечаем живыми людьми.
        </p>
        {supportUrl ? (
          <a
            href={supportUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="btn-primary w-full py-3 block text-center"
          >
            💬 Написать в поддержку
          </a>
        ) : (
          <p className="text-tg-hint text-xs">
            Напиши /help в боте, чтобы связаться с поддержкой.
          </p>
        )}
      </div>

      {/* ── Self-repair «VPN не работает» ──
          Нейтральный секондари-экшн внизу помощи. Отдельно от «Написать
          в поддержку» потому что это другой канал: не диалог с живым
          оператором, а автоматическая починка — то же ядро, что у кнопки
          бота и у страницы на саб-домене. Намеренно без красного — на
          Home.tsx эта кнопка читалась как «сервис сломан», что путало. */}
      <div className="pt-2">
        {step.kind === "pick_device" ? (
          <div className="space-y-2">
            <p className="text-sm">
              У тебя несколько устройств. Какое не работает?
            </p>
            <p className="text-tg-hint text-xs">
              Перенесём только его — остальные не тронем.
            </p>
            <div className="grid grid-cols-1 gap-2">
              {step.devices.map((d) => (
                <button
                  key={d.id}
                  onClick={() => runRepair(() => reportBrokenDevice(d.id))}
                  className="py-2 px-3 rounded-lg text-sm text-left border border-white/10 hover:bg-white/5 transition-colors"
                >
                  📱 {d.name}
                </button>
              ))}
              <button
                onClick={() => runRepair(reportBrokenAll)}
                className="py-2 px-3 rounded-lg text-sm text-left border border-white/10 hover:bg-white/5 transition-colors"
              >
                🔁 Все мои устройства
              </button>
              <button
                onClick={() => setStep({ kind: "idle" })}
                className="py-2 rounded-lg text-xs text-tg-hint hover:bg-white/5 transition-colors"
              >
                Отмена
              </button>
            </div>
          </div>
        ) : step.kind === "pick_operator" ? (
          <div className="space-y-2">
            <p className="text-sm">{outcomeText(step.view)}</p>
            <p className="text-tg-hint text-xs">
              Чтобы мы быстрее ловили блокировки — подскажи, какой у тебя
              интернет?
            </p>
            <div className="grid grid-cols-2 gap-2">
              {VPN_OPERATORS.map((op) => (
                <button
                  key={op.value}
                  onClick={() => handlePickOperator(op.value)}
                  className="py-2 rounded-lg text-sm border border-white/10 hover:bg-white/5 transition-colors"
                >
                  {op.label}
                </button>
              ))}
            </div>
            <button
              onClick={() => handlePickOperator(null)}
              className="w-full py-2 rounded-lg text-xs text-tg-hint hover:bg-white/5 transition-colors"
            >
              Пропустить
            </button>
          </div>
        ) : step.kind === "feedback" ? (
          <div className="space-y-2">
            <p className="text-sm">{outcomeText(step.view)}</p>
            <p className="text-tg-hint text-xs">Получилось подключиться?</p>
            {step.failed && <p className="text-tg-hint text-xs">{ERROR_TEXT}</p>}
            <div className="grid grid-cols-2 gap-2">
              <button
                onClick={() => handleFeedback(true)}
                disabled={step.busy}
                className="py-2 rounded-lg text-sm border border-white/10 hover:bg-white/5 disabled:opacity-60 transition-colors"
              >
                ✅ Всё работает
              </button>
              <button
                onClick={() => handleFeedback(false)}
                disabled={step.busy}
                className="py-2 rounded-lg text-sm border border-white/10 hover:bg-white/5 disabled:opacity-60 transition-colors"
              >
                ❌ Всё равно не работает
              </button>
            </div>
          </div>
        ) : (
          <>
            {statusText && <p className="text-sm mb-2">{statusText}</p>}
            {showSupport && supportUrl && (
              <a
                href={supportUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-tg-link text-sm block mb-2"
              >
                💬 Написать в поддержку
              </a>
            )}
            {showPlans && (
              <button
                onClick={() => navigate({ name: "plans" })}
                className="text-tg-link text-sm block mb-2"
              >
                💎 Выбрать тариф
              </button>
            )}
            <button
              onClick={handleReportBroken}
              disabled={step.kind === "pending"}
              className="w-full py-2 rounded-lg text-sm text-tg-hint border border-white/10 hover:bg-white/5 disabled:opacity-60 transition-colors"
            >
              {step.kind === "pending"
                ? "Отправляем..."
                : "Сообщить, что VPN сейчас не работает"}
            </button>
            <p className="text-tg-hint text-xs mt-2">
              Кнопка переключит тебя на другой способ связи или другой сервер.
              Это не замена поддержке — для диалога используй кнопку выше.
            </p>
          </>
        )}
      </div>

      {/* Версия сборки. Нужна поддержке: юзер присылает её вместе с жалобой, и
          сразу видно, старый ли у него бандл из кэша Telegram-вебвью. Vite
          запекает значение на сборке (build-arg VITE_APP_VERSION из файла
          VERSION). */}
      <p className="text-tg-hint text-[11px] text-center mt-6">
        версия {import.meta.env.VITE_APP_VERSION ?? "0.0.0-dev"}
      </p>
    </div>
  );
}
