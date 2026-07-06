import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import QRCode from "qrcode";
import {
  activateTrial,
  fetchReferral,
  MeResponse,
  ReferralInfo,
  Subscription,
  SubscriptionExtra,
  DeviceSummary,
  addDevice,
  renameDevice,
  removeDevice,
  createTopup,
  cancelSubscription,
  freezeSubscription,
  unfreezeSubscription,
  toggleAutoRenew,
} from "../api";
import { navigate } from "../router";
import { getTg } from "../telegram";

export default function Home({
  me,
  onRefresh,
}: {
  me: MeResponse;
  onRefresh: () => void;
}) {
  const [topupOpen, setTopupOpen] = useState(false);
  const [referral, setReferral] = useState<ReferralInfo | null>(null);
  const [showSetup, setShowSetup] = useState(false);
  const [trialActivating, setTrialActivating] = useState(false);

  // Trial is retroactive: any user whose trial_activated_at is still
  // NULL sees the banner, including "old" users who registered before
  // the trial system existed. Activation is one-shot on the backend
  // (409 on repeat), so we just trust the flag from /me.
  const trialAvailable = me.balance.trial_available;
  const trialAmountRub = me.balance.trial_amount_kopecks / 100;

  const handleActivateTrial = async () => {
    if (trialActivating) return;
    setTrialActivating(true);
    try {
      await activateTrial();
      // Refresh /me so the banner disappears and the new balance
      // (including the +50₽ referral bonus if any) shows up.
      onRefresh();
    } catch (err) {
      // 409 = already activated by another tab/device in the
      // meantime. Either way, just refresh — /me will tell the
      // truth and the banner will hide itself.
      console.warn("trial activate failed", err);
      onRefresh();
    } finally {
      setTrialActivating(false);
    }
  };

  useEffect(() => {
    fetchReferral().then(setReferral).catch(() => undefined);
  }, []);

  const balanceRub = me.balance.balance_rub;
  const minDays = me.balance.min_days_remaining;
  const lowBalance = minDays !== null && minDays <= 3;

  const extraBySub: Record<number, SubscriptionExtra> = {};
  for (const e of me.subscription_extras) {
    extraBySub[e.subscription_id] = e;
  }

  return (
    <div className="min-h-screen p-4 max-w-xl mx-auto">
      <header className="mb-6">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-tg-hint text-sm">Личный кабинет</div>
            <div className="text-2xl font-semibold">
              {me.user.telegram_id ? `@${me.user.telegram_id}` : "Гость"}
            </div>
          </div>
          <button
            onClick={() => setShowSetup(true)}
            className="btn-ghost px-3 py-2 text-sm"
          >
            ⚙️ Настройки
          </button>
        </div>
      </header>

      {/* ── Low-runway warning banner ── */}
      {lowBalance && (
        <div className="mb-3 bg-red-900/50 ring-1 ring-red-500 rounded-xl p-3 text-sm text-red-100">
          ⚠️ Подписка скоро кончится. Пополни баланс и продли, чтобы доступ не отключился.
        </div>
      )}

      {/* ── Free trial banner ── */}
      {trialAvailable && trialAmountRub > 0 && (
        <section className="card mb-4 border border-[var(--accent-from)]/40">
          <div className="text-tg-hint text-xs uppercase tracking-wide">Подарок</div>
          <div className="text-lg font-semibold mt-1">🎁 Забери пробный месяц</div>
          <div className="text-sm text-tg-hint mt-1">
            Кладём <b>{trialAmountRub.toFixed(0)} ₽</b> тебе на баланс — хватит на
            месяц подписки Solo. Без карты, без автосписаний.
          </div>
          <button
            className="btn-primary w-full mt-3"
            disabled={trialActivating}
            onClick={handleActivateTrial}
          >
            {trialActivating ? "Активируем…" : "Активировать месяц"}
          </button>
        </section>
      )}

      {/* ── Balance card ── */}
      <section className="mb-4">
        <div className={`card-hero ${lowBalance ? "card-danger" : ""}`}>
          <div className="text-tg-hint text-xs uppercase tracking-wide">Баланс</div>
          <div className="text-4xl font-bold mt-1 tracking-tight">
            {balanceRub.toFixed(0)} <span className="text-2xl text-tg-hint">₽</span>
          </div>
          {minDays !== null && (
            <div className={`text-sm mt-1 ${lowBalance ? "text-red-300" : "text-tg-hint"}`}>
              {minDays > 0
                ? `хватит на ${minDays} ${pluralDays(minDays)}`
                : "баланс заканчивается сегодня"}
            </div>
          )}
          <div className="grid grid-cols-2 gap-2 mt-4">
            <button className="btn-primary" onClick={() => setTopupOpen(true)}>
              Пополнить
            </button>
            <button className="btn-ghost" onClick={() => navigate({ name: "history" })}>
              История
            </button>
          </div>
        </div>
      </section>

      <section className="space-y-3 mb-6">
        <h2 className="text-sm uppercase tracking-wide text-tg-hint">Мои подписки</h2>
        {me.subscriptions.length === 0 ? (
          <div className="card text-tg-hint text-sm">
            У вас пока нет подписок. Активируйте тариф ниже.
          </div>
        ) : (
          me.subscriptions.map((s) => (
            <SubscriptionCard
              key={s.id}
              sub={s}
              extra={extraBySub[s.id]}
              subLinkBase={me.sub_link_base_url}
              onAction={onRefresh}
            />
          ))
        )}
      </section>

      <div className="flex gap-2">
        <button
          className="btn-primary flex-1 py-3"
          onClick={() => {
            const activeSub = me.subscriptions.find((s) => s.status === "active");
            navigate({ name: "plans", subscriptionId: activeSub?.id });
          }}
        >
          {me.subscriptions.length === 0 ? "Выбрать подписку" : "Сменить подписку"}
        </button>
        <button
          className="btn-ghost py-3 px-4"
          onClick={() => navigate({ name: "help" })}
        >
          Помощь
        </button>
      </div>


      {/* ── Referral block ── */}
      {referral && referral.code && (
        <section className="card mt-6">
          <div className="text-tg-hint text-xs uppercase tracking-wide">
            Пригласи друга
          </div>
          <div className="text-sm mt-1">
            Получи <b>{(referral.bonus_kopecks / 100).toFixed(0)} ₽</b> на баланс,
            когда друг пополнит счёт впервые. Бонус капает автоматически.
          </div>
          {referral.share_url ? (
            <>
              <div className="text-tg-hint text-xs uppercase tracking-wide mt-4 mb-1">
                Твоя ссылка
              </div>
              <a
                href={referral.share_url}
                className="block break-all text-sm text-tg-link underline"
                onClick={(e) => {
                  // Inside Telegram, let openTelegramLink handle the
                  // navigation so the share sheet opens in-app instead
                  // of bouncing through the system browser.
                  const tg = getTg();
                  if (tg?.openTelegramLink) {
                    e.preventDefault();
                    tg.openTelegramLink(referral.share_url!);
                  }
                }}
              >
                {referral.share_url}
              </a>
              <button
                className="btn-primary w-full mt-3"
                onClick={() => {
                  getTg()?.openTelegramLink?.(
                    `https://t.me/share/url?url=${encodeURIComponent(referral.share_url!)}&text=${encodeURIComponent("Дарю тебе доступ в обход блокировок 🔓")}`,
                  );
                }}
              >
                🔗 Поделиться
              </button>
            </>
          ) : (
            <div className="mt-3 text-tg-hint text-xs">
              Промокод: <span className="font-mono">{referral.code}</span>
            </div>
          )}
          <div className="text-tg-hint text-xs mt-2">
            Приглашено: {referral.invited_count} · заработано:{" "}
            {(referral.earned_kopecks / 100).toFixed(0)} ₽
          </div>
        </section>
      )}

      {showSetup && createPortal(<SetupSheet onClose={() => setShowSetup(false)} />, document.body)}
      {topupOpen && createPortal(<TopupModal onClose={() => setTopupOpen(false)} onRefresh={onRefresh} />, document.body)}
    </div>
  );
}

function pluralDays(n: number): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return "день";
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "дня";
  return "дней";
}

function SubscriptionCard({
  sub,
  extra,
  subLinkBase,
  onAction,
}: {
  sub: Subscription;
  extra: SubscriptionExtra | undefined;
  subLinkBase: string;
  onAction: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [showQR, setShowQR] = useState(false);
  const qrCanvasRef = useRef<HTMLCanvasElement | null>(null);
  // Per-device sub link — use the first active device's token so the QR
  // exposes only that device's credentials.  Falls back to the legacy
  // subscription-level token for old subscriptions without device tokens.
  const primaryDevice = extra?.devices?.find((d) => d.sub_token);
  const linkToken = primaryDevice?.sub_token ?? sub.sub_token;
  const subUrl = linkToken
    ? subLinkBase
      ? `${subLinkBase}/${linkToken}`
      : `${window.location.origin}/api/sub/${linkToken}`
    : null;

  const isFrozen = sub.status === "frozen";

  useEffect(() => {
    if (!showQR || !subUrl || !qrCanvasRef.current) return;
    QRCode.toCanvas(qrCanvasRef.current, subUrl, {
      width: 260,
      margin: 2,
      errorCorrectionLevel: "M",
    }).catch((err) => console.warn("qr render failed", err));
  }, [showQR, subUrl]);

  async function handleFreeze() {
    if (!confirm(`Заморозить подписку на 7 дней? Деньги списываться не будут, доступ восстановится автоматически.`))
      return;
    setBusy(true);
    try {
      await freezeSubscription(sub.id);
      onAction();
    } catch (e) {
      alert(`Не удалось заморозить: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleUnfreeze() {
    setBusy(true);
    try {
      await unfreezeSubscription(sub.id);
      onAction();
    } catch (e) {
      alert(`Не удалось разморозить: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleToggleAutoRenew() {
    const newValue = !extra?.auto_renew;
    if (!newValue && !confirm("Отключить автопродление? Подписка будет активна до конца оплаченного периода."))
      return;
    setBusy(true);
    try {
      await toggleAutoRenew(sub.id, newValue);
      onAction();
    } catch (e) {
      alert(`Не удалось изменить автопродление: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  const priceKopecks = extra
    ? extra.total_per_period_kopecks ?? extra.total_monthly_kopecks
    : null;
  const priceRub = priceKopecks !== null ? (priceKopecks / 100).toFixed(0) : null;
  const periodLabel = extra?.period === "year" ? "год" : "мес";
  const expiresDate = extra?.expires_at
    ? new Date(extra.expires_at).toLocaleDateString("ru-RU", { day: "numeric", month: "long" })
    : null;

  return (
    <div className="card">
      <div className="flex justify-between items-start mb-1">
        <div className="font-semibold">{sub.plan_name}</div>
        <StatusBadge status={sub.status} autoRenew={extra?.auto_renew} />
      </div>
      <div className="text-tg-hint text-sm">{sub.region}</div>
      {extra && priceRub && (
        <div className="text-tg-hint text-xs mt-1">
          {priceRub} ₽/{periodLabel}
          {expiresDate && !isFrozen && (
            <> · {extra.auto_renew ? "до" : "истекает"} {expiresDate}</>
          )}
        </div>
      )}
      {isFrozen && extra?.frozen_until && (
        <div className="text-tg-hint text-xs mt-1">
          Авто-разморозка {new Date(extra.frozen_until).toLocaleDateString("ru-RU")}
        </div>
      )}

      {/* Config link + copy + QR */}
      {subUrl && !isFrozen && (
        <div className="mt-3">
          <div className="flex gap-2">
            <button
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(subUrl);
                } catch {
                  const ta = document.createElement("textarea");
                  ta.value = subUrl;
                  document.body.appendChild(ta);
                  ta.select();
                  try { document.execCommand("copy"); } catch { /* empty */ }
                  document.body.removeChild(ta);
                }
                getTg()?.HapticFeedback?.notificationOccurred("success");
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              }}
              className="flex-1 py-2 rounded-xl bg-tg-secondaryBg ring-1 ring-tg-link text-tg-link text-xs font-semibold"
            >
              {copied ? "✓ Скопировано" : "📋 Копировать ссылку"}
            </button>
            <button
              onClick={() => {
                setShowQR((v) => !v);
                getTg()?.HapticFeedback?.notificationOccurred("success");
              }}
              className="flex-1 py-2 rounded-xl bg-tg-secondaryBg ring-1 ring-tg-link text-tg-link text-xs font-semibold"
            >
              {showQR ? "Скрыть QR" : "Показать QR"}
            </button>
          </div>
          {showQR && (
            <div className="mt-3 flex justify-center bg-white rounded-xl p-3">
              <canvas ref={qrCanvasRef} />
            </div>
          )}
        </div>
      )}

      {/* Devices list + Add button */}
      {extra && !isFrozen && (
        <div className="mt-3">
          <div className="text-tg-hint text-xs uppercase tracking-wide mb-1">
            Устройства {extra.device_count}/{extra.bundled_devices}
          </div>
          <ul className="space-y-1">
            {extra.devices.map((d) => (
              <DeviceRow
                key={d.id}
                device={d}
                subLinkBase={subLinkBase}
                canRemove={extra.device_count > 1}
                isPaidSlot={extra.device_count > extra.bundled_devices}
                extraDeviceMonthly={extra.extra_device_monthly_kopecks}
                onAction={onAction}
              />
            ))}
          </ul>
          <button
            onClick={async () => {
              const fee = extra.next_extra_fee_kopecks;
              const monthly = extra.extra_device_monthly_kopecks;
              const msg = fee > 0
                ? `Добавить ещё одно устройство?\n\nСпишется ${(fee / 100).toFixed(0)} ₽ за остаток периода, далее +${(monthly / 100).toFixed(0)} ₽/мес.`
                : "Добавить ещё одно устройство?";
              if (!confirm(msg)) return;
              setBusy(true);
              try {
                await addDevice(sub.id);
                getTg()?.HapticFeedback?.notificationOccurred("success");
                onAction();
              } catch (e) {
                const msg = (e as Error).message;
                const m = /402.*suggested_topup_kopecks["']?\s*:\s*(\d+)/.exec(msg);
                if (m) {
                  alert(
                    `Не хватает баланса. Пополни хотя бы на ${(Number(m[1]) / 100).toFixed(0)} ₽ и попробуй снова.`,
                  );
                } else {
                  alert(`Не удалось добавить устройство: ${msg}`);
                }
              } finally {
                setBusy(false);
              }
            }}
            disabled={busy}
            className="mt-2 w-full py-2 rounded-xl bg-tg-secondaryBg ring-1 ring-tg-link text-tg-link text-sm font-semibold disabled:opacity-50"
          >
            + Добавить устройство
          </button>
        </div>
      )}

      {/* Auto-renew toggle */}
      {extra && !isFrozen && (
        <div
          className="mt-3 flex items-center justify-between cursor-pointer"
          onClick={handleToggleAutoRenew}
        >
          <span className="text-sm">Автопродление</span>
          <div
            className={`w-10 h-6 rounded-full relative transition-colors ${
              extra.auto_renew ? "bg-tg-button" : "bg-slate-600"
            }`}
          >
            <div
              className={`absolute top-1 w-4 h-4 rounded-full bg-white transition-transform ${
                extra.auto_renew ? "translate-x-5" : "translate-x-1"
              }`}
            />
          </div>
        </div>
      )}

      {/* Freeze / unfreeze */}
      {extra && (isFrozen || (!isFrozen && extra.can_freeze)) && (
        <div className="mt-3">
          {isFrozen ? (
            <button
              onClick={handleUnfreeze}
              disabled={busy}
              className="w-full py-2 rounded-xl bg-tg-button text-tg-buttonText text-sm font-semibold disabled:opacity-50"
            >
              {busy ? "…" : "Разморозить"}
            </button>
          ) : (
            <button
              onClick={handleFreeze}
              disabled={busy}
              className="w-full py-2 rounded-xl bg-tg-secondaryBg ring-1 ring-tg-hint text-tg-text text-sm disabled:opacity-50"
            >
              {busy ? "…" : "Заморозить на 7 дн."}
            </button>
          )}
        </div>
      )}

      {/* Cancel / restore subscription */}
      {extra && !isFrozen && extra.auto_renew && (
        <button
          onClick={async () => {
            const date = sub.expires_at
              ? new Date(sub.expires_at).toLocaleDateString("ru-RU")
              : "окончания срока";
            if (
              !confirm(
                `Подписка продолжит работать до ${date}, но не будет продлена. Отменить?`,
              )
            )
              return;
            setBusy(true);
            try {
              await cancelSubscription(sub.id);
              getTg()?.HapticFeedback?.notificationOccurred("success");
              onAction();
            } catch (e) {
              alert(`Ошибка: ${(e as Error).message}`);
            } finally {
              setBusy(false);
            }
          }}
          disabled={busy}
          className="mt-2 w-full py-2 rounded-xl text-red-400 text-xs disabled:opacity-50"
        >
          Отменить подписку
        </button>
      )}
      {extra && !isFrozen && !extra.auto_renew && sub.status === "active" && (
        <button
          onClick={async () => {
            setBusy(true);
            try {
              await toggleAutoRenew(sub.id, true);
              getTg()?.HapticFeedback?.notificationOccurred("success");
              onAction();
            } catch (e) {
              alert(`Ошибка: ${(e as Error).message}`);
            } finally {
              setBusy(false);
            }
          }}
          disabled={busy}
          className="mt-2 w-full py-2 rounded-xl bg-tg-button text-tg-buttonText text-sm font-semibold disabled:opacity-50"
        >
          Восстановить подписку
        </button>
      )}
    </div>
  );
}

function StatusBadge({ status, autoRenew }: { status: string; autoRenew?: boolean }) {
  const isCancelled = status === "active" && autoRenew === false;
  const color = isCancelled
    ? "bg-yellow-500/15 text-yellow-300 border-yellow-500/30"
    : status === "active"
      ? "bg-green-500/15 text-green-300 border-green-500/30"
      : status === "frozen"
        ? "bg-blue-500/15 text-blue-300 border-blue-500/30"
        : status === "expired"
          ? "bg-yellow-500/15 text-yellow-300 border-yellow-500/30"
          : "bg-red-500/15 text-red-300 border-red-500/30";
  const label = isCancelled
    ? "отменена"
    : status === "active"
      ? "активна"
      : status === "frozen"
        ? "заморожена"
        : status === "expired"
          ? "истекла"
          : status;
  return <span className={`chip ${color}`}>{label}</span>;
}

const SETUP_PLATFORMS = [
  {
    label: "Android (v2rayNG)",
    text: "1. Установите v2rayNG из Google Play или GitHub\n2. Скопируйте ссылку конфига из раздела «Устройства»\n3. Откройте v2rayNG → нажмите + → Импорт из буфера\n4. Нажмите кнопку ▶️ для подключения\n\nАльтернатива: Hiddify (Google Play) — автоимпорт по ссылке.",
  },
  {
    label: "iOS (Hiddify / Streisand)",
    text: "1. Установите Hiddify или Streisand из App Store\n2. Скопируйте ссылку конфига из раздела «Устройства»\n3. Откройте приложение → + → Добавить из буфера\n4. Нажмите Подключить",
  },
  {
    label: "Windows (Hiddify / Nekoray)",
    text: "1. Скачайте Hiddify с hiddify.com или Nekoray с GitHub\n2. Скопируйте ссылку конфига из раздела «Устройства»\n3. В программе: Добавить профиль из буфера\n4. Активируйте системный прокси и подключитесь",
  },
  {
    label: "macOS (Hiddify)",
    text: "1. Скачайте Hiddify с hiddify.com\n2. Скопируйте ссылку конфига из раздела «Устройства»\n3. Добавьте профиль из буфера обмена\n4. Подключитесь",
  },
];

function SetupSheet({ onClose }: { onClose: () => void }) {
  const [open, setOpen] = useState<number | null>(null);

  return (
    <div className="fixed inset-0 bg-black/60 flex items-end justify-center z-50 animate-fadeIn" onClick={onClose}>
      <div
        className="bg-tg-bg rounded-t-3xl border-t border-white/10 p-6 max-w-xl w-full max-h-[80vh] overflow-y-auto animate-slideUp"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold mb-3">Как настроить VPN</h2>
        <div className="space-y-2">
          {SETUP_PLATFORMS.map((p, i) => (
            <div key={i}>
              <button
                onClick={() => setOpen(open === i ? null : i)}
                className="w-full text-left px-3 py-2.5 rounded-xl bg-tg-secondaryBg text-sm font-semibold flex items-center justify-between"
              >
                {p.label}
                <span className="text-tg-hint text-xs">{open === i ? "▲" : "▼"}</span>
              </button>
              {open === i && (
                <div className="px-3 py-2 text-sm text-tg-hint whitespace-pre-line">
                  {p.text}
                </div>
              )}
            </div>
          ))}
        </div>
        <button onClick={onClose} className="btn-primary w-full mt-4">
          Закрыть
        </button>
      </div>
    </div>
  );
}

const TOPUP_PRESETS = [10000, 30000, 60000, 150000]; // kopecks: 100/300/600/1500 ₽

function TopupModal({
  onClose,
  onRefresh,
}: {
  onClose: () => void;
  onRefresh: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [customRub, setCustomRub] = useState<string>("");

  async function pay(amountKopecks: number) {
    const tg = getTg();
    if (!tg) {
      alert("Открой эту страницу в Telegram");
      return;
    }
    setBusy(true);
    try {
      const res = await createTopup(amountKopecks, "telegram_stars");
      tg.openInvoice(res.pay_url, (status) => {
        setBusy(false);
        if (status === "paid") {
          tg.HapticFeedback?.notificationOccurred("success");
          // Даём бэкенду тик, чтобы пометить инвойс оплаченным, затем
          // явно перезапрашиваем /me (App сам не перезагрузит: route
          // не меняется) и закрываем модалку — баланс на экране обновится.
          setTimeout(() => {
            onRefresh();
            onClose();
          }, 500);
        } else if (status === "failed") {
          tg.HapticFeedback?.notificationOccurred("error");
          alert("Оплата не прошла. Попробуй ещё раз.");
        }
      });
    } catch (e) {
      setBusy(false);
      alert(`Не удалось создать счёт: ${(e as Error).message}`);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 flex items-end justify-center z-50 animate-fadeIn" onClick={onClose}>
      <div
        className="bg-tg-bg rounded-t-3xl border-t border-white/10 p-6 max-w-xl w-full max-h-[80vh] overflow-y-auto animate-slideUp"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold mb-3">Пополнение баланса</h2>
        <div className="grid grid-cols-2 gap-2 mb-4">
          {TOPUP_PRESETS.map((kop) => (
            <button
              key={kop}
              disabled={busy}
              onClick={() => pay(kop)}
              className="btn-ghost"
            >
              {(kop / 100).toFixed(0)} ₽
            </button>
          ))}
        </div>
        <div className="flex gap-2">
          <input
            type="number"
            min={100}
            placeholder="Своя сумма, ₽"
            value={customRub}
            onChange={(e) => setCustomRub(e.target.value)}
            className="flex-1 rounded-xl border border-white/10 bg-white/[0.04] px-4 py-3 text-tg-text placeholder:text-tg-hint outline-none focus:border-[var(--accent-from)] transition-colors"
          />
          <button
            disabled={busy || !customRub || Number(customRub) < 100}
            onClick={() => pay(Number(customRub) * 100)}
            className="btn-primary"
          >
            Оплатить
          </button>
        </div>
        {customRub && Number(customRub) > 0 && Number(customRub) < 100 && (
          <div className="text-red-400 text-xs mt-1">Минимальная сумма пополнения — 100 ₽</div>
        )}
        <button
          onClick={onClose}
          disabled={busy}
          className="w-full mt-4 py-2 text-tg-hint text-sm"
        >
          Отмена
        </button>
      </div>
    </div>
  );
}

function DeviceRow({
  device,
  subLinkBase,
  canRemove,
  isPaidSlot,
  extraDeviceMonthly,
  onAction,
}: {
  device: DeviceSummary;
  subLinkBase: string;
  canRemove: boolean;
  isPaidSlot: boolean;
  extraDeviceMonthly: number;
  onAction: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(device.name);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const deviceUrl = device.sub_token
    ? subLinkBase
      ? `${subLinkBase}/${device.sub_token}`
      : `${window.location.origin}/api/sub/${device.sub_token}`
    : null;

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  async function saveName() {
    const trimmed = name.trim();
    if (!trimmed || trimmed === device.name) {
      setName(device.name);
      setEditing(false);
      return;
    }
    setBusy(true);
    try {
      await renameDevice(device.id, trimmed);
      setEditing(false);
      onAction();
    } catch (e) {
      alert(`Ошибка: ${(e as Error).message}`);
      setName(device.name);
      setEditing(false);
    } finally {
      setBusy(false);
    }
  }

  async function handleRemove() {
    const costNote = isPaidSlot
      ? `\n\nЕжемесячная стоимость уменьшится на ${(extraDeviceMonthly / 100).toFixed(0)} ₽.`
      : "";
    if (!confirm(`Удалить устройство «${device.name}»?\n\nДоступ будет отозван.${costNote}`)) return;
    setBusy(true);
    try {
      await removeDevice(device.id);
      getTg()?.HapticFeedback?.notificationOccurred("success");
      onAction();
    } catch (e) {
      alert(`Ошибка: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function copyDeviceLink() {
    if (!deviceUrl) return;
    try {
      await navigator.clipboard.writeText(deviceUrl);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = deviceUrl;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch { /* empty */ }
      document.body.removeChild(ta);
    }
    getTg()?.HapticFeedback?.impactOccurred("light");
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <li className="bg-tg-bg rounded px-2 py-1.5 text-xs">
      <div className="flex items-center gap-2">
        {editing ? (
          <input
            ref={inputRef}
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={saveName}
            onKeyDown={(e) => {
              if (e.key === "Enter") saveName();
              if (e.key === "Escape") {
                setName(device.name);
                setEditing(false);
              }
            }}
            maxLength={64}
            disabled={busy}
            className="flex-1 bg-transparent border-b border-tg-link outline-none text-tg-text"
          />
        ) : (
          <span
            className="flex-1 truncate cursor-pointer"
            onClick={() => setEditing(true)}
            title="Нажми, чтобы переименовать"
          >
            {device.name}
          </span>
        )}
        <span className="text-tg-hint shrink-0">{device.status}</span>
        {deviceUrl && (
          <button
            onClick={copyDeviceLink}
            className="text-tg-link shrink-0"
            title="Скопировать ссылку устройства"
          >
            {copied ? "✓" : "🔗"}
          </button>
        )}
        {canRemove && (
          <button
            onClick={handleRemove}
            disabled={busy}
            className="text-red-400 hover:text-red-300 shrink-0 disabled:opacity-50"
            title="Удалить устройство"
          >
            ✕
          </button>
        )}
      </div>
    </li>
  );
}
