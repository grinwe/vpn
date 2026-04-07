import { useEffect, useState } from "react";
import { createCheckout, fetchPlans, WebAppPlan } from "../api";
import { navigate } from "../router";
import { getTg } from "../telegram";

type Period = "month" | "year";

export default function Plans() {
  const [plans, setPlans] = useState<WebAppPlan[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [period, setPeriod] = useState<Period>("month");
  const [showHelp, setShowHelp] = useState(false);
  const [busyPlanId, setBusyPlanId] = useState<number | null>(null);

  useEffect(() => {
    fetchPlans()
      .then(setPlans)
      .catch((e) => setError((e as Error).message));
  }, []);

  if (error)
    return (
      <Centered>
        <div className="text-red-400">Ошибка загрузки тарифов</div>
        <div className="text-tg-hint text-sm mt-1">{error}</div>
      </Centered>
    );
  if (!plans) return <Centered>Загрузка…</Centered>;

  const visible = plans.filter((p) => p.period === period);

  async function buy(plan: WebAppPlan) {
    const tg = getTg();
    if (!tg) {
      alert("Открой эту страницу в Telegram");
      return;
    }
    setBusyPlanId(plan.id);
    try {
      const checkout = await createCheckout(plan.id, "telegram_stars");
      tg.openInvoice(checkout.pay_url, (status) => {
        setBusyPlanId(null);
        if (status === "paid") {
          tg.HapticFeedback?.notificationOccurred("success");
          navigate({ name: "checkout", invoiceId: checkout.invoice_id });
        } else if (status === "failed") {
          tg.HapticFeedback?.notificationOccurred("error");
          alert("Оплата не прошла. Попробуй ещё раз.");
        }
        // "cancelled" / "pending" — пользователь сам решит, ничего не делаем.
      });
    } catch (e) {
      setBusyPlanId(null);
      alert(`Не удалось создать счёт: ${(e as Error).message}`);
    }
  }

  return (
    <div className="min-h-screen p-4 max-w-xl mx-auto">
      <header className="mb-4 flex items-center justify-between">
        <button onClick={() => navigate({ name: "home" })} className="text-tg-link">
          ← Назад
        </button>
        <h1 className="text-xl font-semibold">Тарифы</h1>
        <button onClick={() => setShowHelp(true)} className="text-tg-link text-sm">
          🤔 Помощь
        </button>
      </header>

      <div className="grid grid-cols-2 gap-2 mb-4 bg-tg-secondaryBg rounded-xl p-1">
        <PeriodButton label="Месяц" active={period === "month"} onClick={() => setPeriod("month")} />
        <PeriodButton
          label="Год · −20%"
          active={period === "year"}
          onClick={() => setPeriod("year")}
        />
      </div>

      <div className="space-y-3">
        {visible.map((p) => (
          <PlanCard
            key={p.id}
            plan={p}
            busy={busyPlanId === p.id}
            disabled={busyPlanId !== null && busyPlanId !== p.id}
            onBuy={() => buy(p)}
          />
        ))}
      </div>

      {showHelp && <HelpSheet onClose={() => setShowHelp(false)} />}
    </div>
  );
}

function PeriodButton({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`py-2 rounded-lg text-sm font-medium transition-colors ${
        active ? "bg-tg-button text-tg-buttonText" : "text-tg-hint"
      }`}
    >
      {label}
    </button>
  );
}

function PlanCard({
  plan,
  busy,
  disabled,
  onBuy,
}: {
  plan: WebAppPlan;
  busy: boolean;
  disabled: boolean;
  onBuy: () => void;
}) {
  const popular = plan.badge === "popular";
  return (
    <div
      className={`relative bg-tg-secondaryBg rounded-2xl p-4 ${
        popular ? "ring-2 ring-yellow-500" : ""
      }`}
    >
      {popular && (
        <div className="absolute -top-2 right-3 bg-yellow-500 text-black text-xs font-bold px-2 py-0.5 rounded">
          ⭐ ПОПУЛЯРНЫЙ
        </div>
      )}
      <div className="flex justify-between items-start">
        <div>
          <div className="text-lg font-semibold">{plan.tier}</div>
          <div className="text-tg-hint text-sm">
            {plan.max_devices} {plan.max_devices === 1 ? "устройство" : "устройств"} ·{" "}
            {plan.period === "year" ? "365 дней" : "30 дней"}
          </div>
        </div>
        <div className="text-right">
          <div className="text-xl font-bold">⭐ {plan.price_stars}</div>
          <div className="text-tg-hint text-xs">≈ {plan.price_rub.toFixed(0)} ₽</div>
        </div>
      </div>
      <button
        onClick={onBuy}
        disabled={busy || disabled}
        className="w-full mt-3 py-2 rounded-xl bg-tg-button text-tg-buttonText font-semibold disabled:opacity-50"
      >
        {busy ? "Открываем оплату…" : "Купить"}
      </button>
    </div>
  );
}

function HelpSheet({ onClose }: { onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-end justify-center"
      onClick={onClose}
    >
      <div
        className="bg-tg-bg rounded-t-3xl p-6 max-w-xl w-full"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold mb-3">Какой тариф выбрать?</h2>
        <ul className="space-y-2 text-sm">
          <li>
            <b>Solo</b> — 1 устройство. Если VPN нужен только тебе на телефоне или
            ноуте.
          </li>
          <li>
            <b>Family</b> ⭐ — 3 устройства. Самый выгодный, если хочешь раздать жене
            и паре друзей. Большинство берут именно его.
          </li>
          <li>
            <b>Pro</b> — 5 устройств. Для большой семьи или если у тебя зоопарк
            техники.
          </li>
          <li className="pt-2 text-tg-hint">
            Год экономит ~20% — бери, если уже знаешь, что VPN нужен надолго.
          </li>
        </ul>
        <button
          onClick={onClose}
          className="w-full mt-4 py-2 rounded-xl bg-tg-button text-tg-buttonText font-semibold"
        >
          Понятно
        </button>
      </div>
    </div>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex items-center justify-center p-6 text-center">
      <div>{children}</div>
    </div>
  );
}
