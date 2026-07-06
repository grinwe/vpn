import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import QRCode from "qrcode";
import {
  activateSubscription,
  changePlan,
  createTopup,
  fetchMe,
  fetchPlans,
  MeResponse,
  WebAppPlan,
} from "../api";
import { navigate } from "../router";
import { getTg } from "../telegram";

type Period = "month" | "year";

// Превращает сырой текст ошибки от fetch ("503: {...}", "500: ...")
// в человекочитаемое сообщение. Юзеру не надо видеть JSON и коды.
function friendlyActivateError(raw: string): string {
  // 503 — нет свободных нод / нет триал-плана / провижининг недоступен
  if (/^503/.test(raw) || /no.*node/i.test(raw) || /no trial plan/i.test(raw)) {
    return "Сейчас нет свободных серверов. Мы уже знаем — попробуй чуть позже или напиши в поддержку через раздел «Помощь».";
  }
  // 502/504 — бэкенд/воркер недоступен
  if (/^(502|504)/.test(raw)) {
    return "Сервис временно недоступен. Попробуй ещё раз через минуту.";
  }
  // 500 — необработанная ошибка
  if (/^500/.test(raw)) {
    return "Что-то пошло не так на нашей стороне. Напиши в поддержку через раздел «Помощь» — разберёмся.";
  }
  // 401/403 — просрочен токен webapp
  if (/^(401|403)/.test(raw)) {
    return "Сессия истекла. Закрой и снова открой приложение.";
  }
  // 400 с читаемым detail — показываем только detail, без кода
  const m = /^\d+:\s*(.+)$/s.exec(raw);
  if (m) {
    const detail = m[1].trim();
    // Если detail — это JSON, лучше общее сообщение
    if (detail.startsWith("{")) {
      return "Не удалось выполнить операцию. Попробуй ещё раз или напиши в поддержку.";
    }
    return detail;
  }
  return "Не удалось выполнить операцию. Попробуй ещё раз или напиши в поддержку.";
}

export default function Plans({ onActivated, subLinkBase, me, changeSubscriptionId }: { onActivated: () => void; subLinkBase: string; me: MeResponse; changeSubscriptionId?: number }) {
  const [plans, setPlans] = useState<WebAppPlan[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [period, setPeriod] = useState<Period>("month");
  const [showHelp, setShowHelp] = useState(false);
  const [busyPlanId, setBusyPlanId] = useState<number | null>(null);
  const [topupHint, setTopupHint] = useState<{
    suggested: number;
    planId: number;
  } | null>(null);
  const [activated, setActivated] = useState<{
    tier: string;
    days: number;
    subToken: string | null;
  } | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  // Состояние платежа-пополнения из TopupHintSheet:
  //   "paying"    — летит createTopup / открыто окно Telegram (защита от
  //                 повторного тапа → дублей инвойсов);
  //   "crediting" — платёж прошёл, ждём зачисления на бэке перед refetch.
  const [topupState, setTopupState] = useState<"paying" | "crediting" | null>(null);

  // When changing an existing subscription, find the current plan ID
  // so we can highlight it and use changePlan API instead of activate.
  const changeSub = changeSubscriptionId
    ? me.subscriptions.find((s) => s.id === changeSubscriptionId)
    : undefined;
  const currentPlanId = changeSub?.plan_id ?? null;
  const isChangeMode = !!changeSub;

  useEffect(() => {
    fetchPlans()
      .then(setPlans)
      .catch((e) => setError(friendlyActivateError((e as Error).message)));
  }, []);

  if (error)
    return (
      <Centered>
        <div className="card border-red-500/40 text-red-200">
          <div className="font-semibold">Ошибка загрузки тарифов</div>
          <div className="text-tg-hint text-sm mt-1">{error}</div>
        </div>
      </Centered>
    );
  if (!plans) return <Centered>Загрузка…</Centered>;

  const visible = plans.filter((p) => p.period === period);

  async function activate(plan: WebAppPlan) {
    if (currentPlanId === plan.id) return; // already on this plan
    setBusyPlanId(plan.id);
    try {
      if (isChangeMode && changeSub) {
        const currentName = changeSub.plan_name;
        const newPrice = `${plan.price_rub.toFixed(0)} ₽/${plan.period === "year" ? "год" : "мес"}`;
        if (!confirm(`Сменить тариф «${currentName}» на «${plan.tier}»?\n\nНовая стоимость: ${newPrice}.\nБудет выполнен перерасчёт за остаток периода.`)) {
          setBusyPlanId(null);
          return;
        }
        const res = await changePlan(changeSub.id, plan.id);
        const tg = getTg();
        tg?.HapticFeedback?.notificationOccurred("success");
        const refund = res.refunded_kopecks > 0 ? ` Возврат ${(res.refunded_kopecks / 100).toFixed(0)} ₽.` : "";
        const charge = res.charged_kopecks > 0 ? ` Списано ${(res.charged_kopecks / 100).toFixed(0)} ₽.` : "";
        showToast(`Тариф изменён на «${res.new_plan_name}».${refund}${charge}`);
        setTimeout(() => {
          onActivated();
          navigate({ name: "home" });
        }, 1500);
      } else {
        const res = await activateSubscription(plan.id);
        const tg = getTg();
        tg?.HapticFeedback?.notificationOccurred("success");
        setActivated({ tier: plan.tier, days: res.plan_duration_days, subToken: res.sub_token });
      }
    } catch (e) {
      const msg = (e as Error).message;
      const match = /402.*suggested_topup_kopecks["']?\s*:\s*(\d+)/.exec(msg);
      if (match) {
        setTopupHint({ suggested: Number(match[1]), planId: plan.id });
      } else {
        showToast(friendlyActivateError(msg));
      }
    } finally {
      setBusyPlanId(null);
    }
  }

  // Поллит /me, пока баланс не превысит baseline (платёж зачислен) или пока
  // не выйдет таймаут. Колбэк openInvoice приходит раньше, чем вебхук Stars
  // успевает записать платёж, поэтому мгновенный refetch отдаёт старый баланс
  // и активация снова словит 402. ~6 попыток по 1.2 с ≈ 7 с — с запасом на
  // медленную обработку вебхука.
  async function waitForBalance(baselineKopecks: number): Promise<void> {
    for (let i = 0; i < 6; i++) {
      await new Promise((r) => setTimeout(r, 1200));
      try {
        const fresh = await fetchMe();
        if (fresh.balance.balance_kopecks > baselineKopecks) return;
      } catch {
        // Сетевой сбой при поллинге не критичен: onActivated ниже всё равно
        // перезапросит /me.
      }
    }
  }

  async function payTopup(amountKopecks: number) {
    const tg = getTg();
    if (!tg) {
      showToast("Открой эту страницу в Telegram");
      return;
    }
    // Защита от повторного тапа: пока платёж в работе, не создаём новый инвойс.
    if (topupState) return;
    setTopupState("paying");
    // Баланс до пополнения — точка отсчёта для ожидания зачисления.
    const baseline = me.balance.balance_kopecks;
    try {
      const res = await createTopup(amountKopecks, "telegram_stars");
      tg.openInvoice(res.pay_url, (status) => {
        if (status === "paid") {
          tg.HapticFeedback?.notificationOccurred("success");
          // Не полагаемся на мгновенный refetch: ждём фактического зачисления,
          // затем закрываем подсказку и обновляем /me.
          setTopupState("crediting");
          waitForBalance(baseline).finally(() => {
            setTopupState(null);
            setTopupHint(null);
            onActivated();
          });
        } else {
          // failed / cancelled — снимаем занятость, подсказка остаётся открытой.
          setTopupState(null);
          if (status === "failed") {
            tg.HapticFeedback?.notificationOccurred("error");
            showToast("Оплата не прошла. Попробуй ещё раз.");
          }
        }
      });
    } catch (e) {
      setTopupState(null);
      showToast(friendlyActivateError((e as Error).message));
    }
  }

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(null), 4000);
  }

  if (activated) {
    return (
      <ActivatedScreen
        tier={activated.tier}
        days={activated.days}
        subToken={activated.subToken}
        subLinkBase={subLinkBase}
        onHome={() => {
          onActivated();
          navigate({ name: "home" });
        }}
      />
    );
  }

  return (
    <div className="min-h-screen p-4 max-w-xl mx-auto">
      <header className="mb-4 flex items-center justify-between">
        <button onClick={() => navigate({ name: "home" })} className="text-tg-link">
          ← Назад
        </button>
        <h1 className="text-xl font-semibold">{isChangeMode ? "Сменить тариф" : "Тарифы"}</h1>
        <button onClick={() => setShowHelp(true)} className="text-tg-link text-sm">
          🤔 Помощь
        </button>
      </header>

      <div className="grid grid-cols-2 gap-2 mb-4">
        <PeriodButton label="Месяц" active={period === "month"} onClick={() => setPeriod("month")} />
        <PeriodButton
          label="Год · −20%"
          active={period === "year"}
          onClick={() => setPeriod("year")}
        />
      </div>

      <div className="space-y-3">
        {visible.length === 0 ? (
          <div className="card text-center text-tg-hint text-sm">
            {period === "year"
              ? "Годовых тарифов пока нет — попробуй месячный."
              : "Месячных тарифов пока нет — попробуй годовой."}
          </div>
        ) : (
          visible.map((p) => (
            <PlanCard
              key={p.id}
              plan={p}
              isCurrent={currentPlanId === p.id}
              isChangeMode={isChangeMode}
              busy={busyPlanId === p.id}
              disabled={busyPlanId !== null && busyPlanId !== p.id}
              onActivate={() => activate(p)}
            />
          ))
        )}
      </div>

      {showHelp && createPortal(<HelpSheet onClose={() => setShowHelp(false)} />, document.body)}
      {topupHint && createPortal(
        <TopupHintSheet
          suggested={topupHint.suggested}
          busy={topupState !== null}
          crediting={topupState === "crediting"}
          onPay={(kop) => payTopup(kop)}
          onClose={() => setTopupHint(null)}
        />,
        document.body,
      )}
      {toast && <Toast message={toast} onClose={() => setToast(null)} />}
    </div>
  );
}

// Russian plural for "устройство": 1 → устройство, 2-4 → устройства,
// 5-20 → устройств. 21 → устройство again. 11-14 are always "устройств".
function pluralDevices(n: number): string {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 14) return "устройств";
  const mod10 = n % 10;
  if (mod10 === 1) return "устройство";
  if (mod10 >= 2 && mod10 <= 4) return "устройства";
  return "устройств";
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
      className={`${active ? "btn-primary" : "btn-ghost"} text-sm`}
    >
      {label}
    </button>
  );
}

function PlanCard({
  plan,
  isCurrent,
  isChangeMode,
  busy,
  disabled,
  onActivate,
}: {
  plan: WebAppPlan;
  isCurrent: boolean;
  isChangeMode: boolean;
  busy: boolean;
  disabled: boolean;
  onActivate: () => void;
}) {
  const popular = plan.badge === "popular";
  const dailyRub = Math.round((plan.price_rub / plan.duration_days) * 10) / 10;

  const ringClass = isCurrent
    ? "ring-2 ring-green-500"
    : popular
      ? "ring-2 ring-[var(--accent-from)]"
      : "";

  const buttonLabel = busy
    ? isChangeMode ? "Меняем…" : "Активируем…"
    : isCurrent
      ? "Текущий тариф"
      : isChangeMode
        ? "Перейти"
        : "Активировать";

  return (
    <div className={`card relative ${ringClass}`}>
      {isCurrent && (
        <div className="chip inline-block mb-2 bg-green-500/15 text-green-300 border-green-500/30">
          ТЕКУЩИЙ
        </div>
      )}
      {!isCurrent && popular && (
        <div className="chip inline-block mb-2 bg-yellow-500/15 text-yellow-300 border-yellow-500/30">
          ⭐ ПОПУЛЯРНЫЙ
        </div>
      )}
      <div className="flex justify-between items-start">
        <div>
          <div className="text-lg font-semibold">{plan.tier}</div>
          <div className="text-tg-hint text-sm">
            {plan.max_devices} {pluralDevices(plan.max_devices)}
          </div>
        </div>
        <div className="text-right">
          <div className="text-xl font-bold">{plan.price_rub.toFixed(0)} ₽/{plan.period === "year" ? "год" : "мес"}</div>
          <div className="text-tg-hint text-xs">≈ {dailyRub} ₽/день</div>
        </div>
      </div>
      <button
        onClick={onActivate}
        disabled={busy || disabled || isCurrent}
        className={`w-full mt-3 ${isCurrent ? "btn-ghost opacity-60 cursor-default" : "btn-primary"}`}
      >
        {buttonLabel}
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
        className="bg-tg-bg rounded-t-3xl border-t border-white/10 p-6 max-w-xl w-full"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold mb-3">Как это работает</h2>
        <ul className="space-y-2 text-sm">
          <li>
            <b>Пополни баланс</b> — деньги хранятся в личном кабинете. Каждый день
            мы списываем стоимость активного тарифа.
          </li>
          <li>
            <b>Solo</b> — 1 устройство, ~5 ₽/день.
          </li>
          <li>
            <b>Family</b> ⭐ — 3 устройства, ~10 ₽/день. Самый выгодный.
          </li>
          <li>
            <b>Pro</b> — 5 устройств, ~17 ₽/день.
          </li>
          <li className="pt-2 text-tg-hint">
            Можно <b>заморозить</b> тариф на 14 дней — списания остановятся.
            Не нужен VPN — просто перестань пополнять, доступ закроется когда
            баланс закончится.
          </li>
        </ul>
        <button onClick={onClose} className="btn-primary w-full mt-4">
          Понятно
        </button>
      </div>
    </div>
  );
}

function TopupHintSheet({
  suggested,
  busy,
  crediting,
  onPay,
  onClose,
}: {
  suggested: number;
  busy: boolean;
  crediting: boolean;
  onPay: (kop: number) => void;
  onClose: () => void;
}) {
  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-end justify-center"
      onClick={busy ? undefined : onClose}
    >
      <div
        className="bg-tg-bg rounded-t-3xl border-t border-white/10 p-6 max-w-xl w-full"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold mb-2">Не хватает баланса</h2>
        <p className="text-tg-hint text-sm mb-4">
          Чтобы активировать этот тариф, нужно пополнить баланс. Рекомендуем{" "}
          {(suggested / 100).toFixed(0)} ₽ — этого хватит примерно на месяц.
        </p>
        <button onClick={() => onPay(suggested)} disabled={busy} className="btn-primary w-full">
          {crediting ? "Зачисляем…" : `Пополнить на ${(suggested / 100).toFixed(0)} ₽`}
        </button>
        <button
          onClick={onClose}
          disabled={busy}
          className="w-full mt-2 py-2 text-tg-hint text-sm"
        >
          Отмена
        </button>
      </div>
    </div>
  );
}

function Toast({ message, onClose }: { message: string; onClose: () => void }) {
  return (
    <div
      className="fixed top-4 left-4 right-4 z-50 bg-red-900/90 border border-red-500/40 rounded-xl px-4 py-3 text-sm text-red-100 shadow-lg"
      onClick={onClose}
    >
      {message}
    </div>
  );
}

function ActivatedScreen({
  tier,
  days,
  subToken,
  subLinkBase,
  onHome,
}: {
  tier: string;
  days: number;
  subToken: string | null;
  subLinkBase: string;
  onHome: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [showQR, setShowQR] = useState(false);
  const qrRef = useRef<HTMLCanvasElement | null>(null);

  const subUrl = subToken
    ? subLinkBase
      ? `${subLinkBase}/${subToken}`
      : `${window.location.origin}/api/sub/${subToken}`
    : null;

  useEffect(() => {
    if (!showQR || !subUrl || !qrRef.current) return;
    QRCode.toCanvas(qrRef.current, subUrl, {
      width: 260,
      margin: 2,
      errorCorrectionLevel: "M",
    }).catch((err) => console.warn("qr render failed", err));
  }, [showQR, subUrl]);

  return (
    <Centered>
      <div className="card text-center max-w-sm mx-auto">
        <div className="text-4xl mb-3">✅</div>
        <h2 className="text-xl font-semibold mb-2">Тариф активирован</h2>
        <p className="text-tg-hint text-sm mb-1">
          Тариф <b>«{tier}»</b> успешно подключён.
        </p>
        <p className="text-tg-hint text-sm mb-4">
          Следующее продление через {days} дней.
        </p>

        {subUrl && (
          <div className="mb-4">
            <p className="text-xs text-tg-hint mb-2">
              Скопируй ссылку и вставь в Hiddify или v2rayNG:
            </p>
            <div className="flex gap-2">
              <button
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(subUrl);
                    setCopied(true);
                    getTg()?.HapticFeedback?.impactOccurred("light");
                    setTimeout(() => setCopied(false), 2000);
                  } catch (err) {
                    console.warn("clipboard copy failed", err);
                  }
                }}
                className="flex-1 py-2 rounded-xl bg-tg-button text-tg-buttonText text-sm font-semibold"
              >
                {copied ? "Скопировано ✓" : "📋 Скопировать ссылку"}
              </button>
              <button
                onClick={() => setShowQR((v) => !v)}
                className="py-2 px-4 rounded-xl bg-tg-secondaryBg ring-1 ring-tg-hint text-tg-text text-sm"
              >
                QR
              </button>
            </div>
            {showQR && (
              <div className="mt-3 flex justify-center">
                <canvas ref={qrRef} />
              </div>
            )}
          </div>
        )}

        <button onClick={onHome} className="btn-primary w-full">
          Перейти в кабинет
        </button>
      </div>
    </Centered>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex items-center justify-center p-6 text-center">
      <div>{children}</div>
    </div>
  );
}
