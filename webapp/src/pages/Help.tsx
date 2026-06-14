import { useState } from "react";
import { navigate } from "../router";
import {
  reportVpnBroken,
  reportBrokenDevice,
  setReportOperator,
  VPN_OPERATORS,
  DeviceSummary,
} from "../api";

interface Props {
  botUsername?: string;
  // Устройства активной подписки. Если их >1 — спрашиваем «какое не работает?»
  // и перетряхиваем ноды ТОЛЬКО выбранного, не трогая соседние.
  devices?: DeviceSummary[];
}

// Состояния кнопки «у меня прямо сейчас не работает VPN». Используется
// как priority-сигнал для админов поверх плановых health-ping'ов бота.
// Клиентский 5-мин cooldown после успешной отправки — сервер повторы
// тоже принимает, но нет смысла плодить AuditLog одним тапом.
// pick_operator — нас переселили на свободную ноду, спрашиваем оператора
// (operator-routing P1, operator_routing_roadmap.md); operator_done — спасибо.
type SelfReportState =
  | "idle"
  | "pending"
  | "sent"
  | "error"
  | "pick_device"
  | "pick_operator"
  | "operator_done";

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
      "• Обнови подписку в VPN-клиенте — нажми 🔄 рядом с профилем, чтобы подтянуть актуальный конфиг\n" +
      "• Проверь баланс в личном кабинете — при 0 ₽ доступ блокируется\n" +
      "• Обнови клиент (Hiddify / v2rayNG / Streisand) до последней версии\n" +
      "• Выключи другие VPN-профили — два VPN одновременно не работают\n" +
      "• Перезагрузи телефон\n" +
      "• Удали старый профиль и импортируй конфиг заново из кабинета\n" +
      "• Проверь, что подписка активна (раздел «Мои подписки»)",
  },
  {
    title: "Как добавить устройство",
    body:
      "В карточке подписки нажми «+ Добавить устройство». " +
      "Каждое дополнительное устройство сверх тарифа добавляет ~100 ₽/мес к стоимости. " +
      "Новый конфиг появится в списке — скопируй его или отсканируй QR.",
  },
];

export default function Help({ botUsername, devices = [] }: Props) {
  const [open, setOpen] = useState<number | null>(null);
  const [selfReportState, setSelfReportState] = useState<SelfReportState>("idle");
  const [reportId, setReportId] = useState<number | null>(null);

  const supportUrl = botUsername
    ? `https://t.me/${botUsername}?start=support`
    : null;

  const applyReportResult = (res: {
    migrated?: boolean;
    report_id?: number | null;
  }) => {
    if (res.migrated && res.report_id) {
      // Переселили → спрашиваем оператора (один тап).
      setReportId(res.report_id);
      setSelfReportState("pick_operator");
    } else {
      setSelfReportState("sent");
      setTimeout(() => setSelfReportState("idle"), 5 * 60 * 1000);
    }
  };

  const handleReportBroken = async () => {
    if (
      selfReportState === "pending" ||
      selfReportState === "sent" ||
      selfReportState === "pick_device" ||
      selfReportState === "pick_operator"
    )
      return;
    // >1 устройства → сперва спросим, какое именно барахлит, и перетряхнём ноды
    // ТОЛЬКО его (рабочие не трогаем). Одно устройство → сразу отчёт по сабе.
    if (devices.length > 1) {
      setSelfReportState("pick_device");
      return;
    }
    setSelfReportState("pending");
    try {
      applyReportResult(await reportVpnBroken());
    } catch (err) {
      console.error("reportVpnBroken failed", err);
      setSelfReportState("error");
      setTimeout(() => setSelfReportState("idle"), 5000);
    }
  };

  const handlePickDevice = async (deviceId: number) => {
    setSelfReportState("pending");
    try {
      applyReportResult(await reportBrokenDevice(deviceId));
    } catch (err) {
      console.error("reportBrokenDevice failed", err);
      setSelfReportState("error");
      setTimeout(() => setSelfReportState("idle"), 5000);
    }
  };

  const handlePickOperator = async (operator: string) => {
    const rid = reportId;
    // Сразу переводим в «спасибо» — выбор оператора best-effort, не блокируем
    // юзера, если запрос не дойдёт (карьер — advisory-сигнал для матрицы).
    setSelfReportState("operator_done");
    setReportId(null);
    setTimeout(() => setSelfReportState("idle"), 5 * 60 * 1000);
    if (rid == null) return;
    try {
      await setReportOperator(rid, operator);
    } catch (err) {
      console.error("setReportOperator failed", err);
    }
  };

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

      {/* ── Self-report «VPN не работает» ──
          Нейтральный секондари-экшн внизу помощи. Отдельно от «Написать
          в поддержку» потому что это другой канал: не диалог с живым
          оператором, а priority-сигнал для админов (поверх плановых
          health-ping'ов бота) — «юзер не дождался следующего пинга,
          ткнул сам». Намеренно без красного — на Home.tsx эта кнопка
          читалась как «сервис сломан», что путало. */}
      <div className="pt-2">
        {selfReportState === "pick_device" ? (
          <div className="space-y-2">
            <p className="text-sm">Какое устройство сейчас не работает?</p>
            <p className="text-tg-hint text-xs">
              Перетряхнём серверы только для него — остальные устройства не
              тронем.
            </p>
            <div className="grid grid-cols-1 gap-2">
              {devices.map((d) => (
                <button
                  key={d.id}
                  onClick={() => handlePickDevice(d.id)}
                  className="py-2 px-3 rounded-lg text-sm text-left border border-white/10 hover:bg-white/5 transition-colors"
                >
                  📱 {d.name}
                </button>
              ))}
              <button
                onClick={() => setSelfReportState("idle")}
                className="py-2 rounded-lg text-xs text-tg-hint hover:bg-white/5 transition-colors"
              >
                Отмена
              </button>
            </div>
          </div>
        ) : selfReportState === "pick_operator" ? (
          <div className="space-y-2">
            <p className="text-sm">
              🔄 Поменяли тебе сервер. Попробуй подключиться через пару минут.
            </p>
            <p className="text-tg-hint text-xs">
              Через какой интернет сейчас выходишь? Поможет нам понять, где
              блокируют (один тап, по желанию).
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
          </div>
        ) : (
          <>
            <button
              onClick={handleReportBroken}
              disabled={selfReportState === "pending"}
              className="w-full py-2 rounded-lg text-sm text-tg-hint border border-white/10 hover:bg-white/5 disabled:opacity-60 transition-colors"
            >
              {selfReportState === "pending" && "Отправляем..."}
              {selfReportState === "operator_done" &&
                "✓ Спасибо! Проверяй подключение"}
              {selfReportState === "sent" &&
                "✓ Жалоба отправлена — админы смотрят"}
              {selfReportState === "error" &&
                "Не удалось отправить, попробуй позже"}
              {selfReportState === "idle" &&
                "Сообщить, что VPN сейчас не работает"}
            </button>
            <p className="text-tg-hint text-xs mt-2">
              Кнопка переселит тебя на свободный сервер и пошлёт маячок админам.
              Это не замена поддержке — для диалога используй кнопку выше.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
