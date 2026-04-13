import { useState } from "react";
import { navigate } from "../router";

interface Props {
  botUsername?: string;
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

export default function Help({ botUsername }: Props) {
  const [open, setOpen] = useState<number | null>(null);

  const supportUrl = botUsername
    ? `https://t.me/${botUsername}?start=support`
    : null;

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
    </div>
  );
}
