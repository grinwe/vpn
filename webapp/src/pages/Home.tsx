import { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import {
  activateTrial,
  fetchReferral,
  MeResponse,
  ReferralInfo,
  Subscription,
  SubscriptionExtra,
  addDevice,
  createTopup,
  freezeSubscription,
  unfreezeSubscription,
  cancelSubscription,
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
  const [showVideos, setShowVideos] = useState(false);
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
        <div className="text-tg-hint text-sm">Личный кабинет</div>
        <div className="text-2xl font-semibold">
          {me.user.telegram_id ? `@${me.user.telegram_id}` : "Гость"}
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

      <button
        className="btn-primary w-full py-3"
        onClick={() => navigate({ name: "plans" })}
      >
        {me.subscriptions.length === 0 ? "Выбрать подписку" : "Добавить подписку"}
      </button>

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

      {/* ── Video instructions ── */}
      <section className="card mt-6">
        <button
          onClick={() => setShowVideos((v) => !v)}
          className="w-full flex items-center justify-between text-left"
        >
          <span className="text-sm font-semibold">📺 Как настроить</span>
          <span className="text-tg-hint text-xs">{showVideos ? "▲" : "▼"}</span>
        </button>
        {showVideos && (
          <ul className="mt-3 space-y-2 text-sm">
            <li>
              <a className="text-tg-link" href="https://hiddify.com/" target="_blank" rel="noreferrer">
                iOS — Hiddify Next
              </a>
            </li>
            <li>
              <a className="text-tg-link" href="https://hiddify.com/" target="_blank" rel="noreferrer">
                Android — Hiddify Next / v2rayNG
              </a>
            </li>
            <li>
              <a className="text-tg-link" href="https://hiddify.com/" target="_blank" rel="noreferrer">
                Windows — Hiddify Desktop
              </a>
            </li>
            <li>
              <a className="text-tg-link" href="https://hiddify.com/" target="_blank" rel="noreferrer">
                macOS — Hiddify Desktop
              </a>
            </li>
          </ul>
        )}
      </section>

      {topupOpen && <TopupModal onClose={() => setTopupOpen(false)} />}
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
  // Stage 8 — when SUB_LINK_BASE_URL is configured we surface that
  // boring-domain URL (e.g. https://c1.cloudfn.app/sub/<token>) so
  // already-installed clients keep working even if the primary domain
  // gets blocked. Empty base falls back to the same-origin relative
  // path on `grinwer.online`.
  const subUrl = sub.sub_token
    ? subLinkBase
      ? `${subLinkBase}/${sub.sub_token}`
      : `${window.location.origin}/api/sub/${sub.sub_token}`
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

  async function handleCancel() {
    if (
      !confirm(
        "Отписаться? Доступ будет отключён, остаток предоплаты вернётся на баланс.",
      )
    )
      return;
    setBusy(true);
    try {
      const res = await cancelSubscription(sub.id);
      const refundRub = (res.refunded_kopecks / 100).toFixed(0);
      alert(
        res.refunded_kopecks > 0
          ? `Подписка отменена. ${refundRub} ₽ возвращено на баланс.`
          : "Подписка отменена.",
      );
      onAction();
    } catch (e) {
      alert(`Не удалось отписаться: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="flex justify-between items-start mb-1">
        <div className="font-semibold">{sub.plan_name}</div>
        <StatusBadge status={sub.status} />
      </div>
      <div className="text-tg-hint text-sm">{sub.region}</div>
      {extra && extra.daily_cost_kopecks !== null && (
        <div className="text-tg-hint text-xs mt-1">
          {(extra.daily_cost_kopecks / 100).toFixed(0)} ₽/день
          {extra.next_charge_at && !isFrozen && (
            <> · списание {new Date(extra.next_charge_at).toLocaleDateString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</>
          )}
        </div>
      )}
      {isFrozen && extra?.frozen_until && (
        <div className="text-tg-hint text-xs mt-1">
          Авто-разморозка {new Date(extra.frozen_until).toLocaleDateString("ru-RU")}
        </div>
      )}

      {/* Devices list + Add button */}
      {extra && !isFrozen && (
        <div className="mt-3">
          <div className="text-tg-hint text-xs uppercase tracking-wide mb-1">
            Устройства {extra.device_count}/{extra.bundled_devices}
            {extra.device_count > extra.bundled_devices && (
              <> · +{(extra.extra_device_daily_kopecks / 100 * 30).toFixed(0)} ₽/мес за каждое сверх</>
            )}
          </div>
          <ul className="space-y-1">
            {extra.devices.map((d, i) => (
              <li
                key={d.id}
                className="flex items-center justify-between bg-tg-bg rounded px-2 py-1 text-xs"
              >
                <span>Устройство {i + 1}</span>
                <span className="text-tg-hint">{d.status}</span>
              </li>
            ))}
          </ul>
          <button
            onClick={async () => {
              const extraCost =
                extra.device_count + 1 > extra.bundled_devices
                  ? ` (+${(extra.extra_device_daily_kopecks / 100 * 30).toFixed(0)} ₽/мес)`
                  : "";
              if (!confirm(`Добавить ещё одно устройство?${extraCost}`)) return;
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

      {subUrl && !isFrozen && (
        <>
          <div className="mt-3 flex gap-2">
            <button
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(subUrl);
                  getTg()?.HapticFeedback?.notificationOccurred("success");
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1500);
                } catch {
                  // Fallback: старые WebView без Clipboard API
                  const ta = document.createElement("textarea");
                  ta.value = subUrl;
                  document.body.appendChild(ta);
                  ta.select();
                  try { document.execCommand("copy"); } catch { /* empty */ }
                  document.body.removeChild(ta);
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1500);
                }
              }}
              className="flex-1 py-2 rounded-xl bg-tg-secondaryBg ring-1 ring-tg-link text-tg-link text-sm font-semibold"
            >
              {copied ? "Скопировано ✓" : "Скопировать"}
            </button>
            <button
              onClick={() => {
                setShowQR((v) => !v);
                getTg()?.HapticFeedback?.notificationOccurred("success");
              }}
              className="flex-1 py-2 rounded-xl bg-tg-secondaryBg ring-1 ring-tg-link text-tg-link text-sm font-semibold"
            >
              {showQR ? "Скрыть QR" : "Показать QR"}
            </button>
          </div>
          {showQR && (
            <div className="mt-3 flex justify-center bg-white rounded-xl p-3">
              <canvas ref={qrCanvasRef} />
            </div>
          )}
        </>
      )}

      {/* Freeze / unfreeze / cancel controls */}
      {extra && (
        <div className="mt-3 flex gap-2">
          {isFrozen ? (
            <button
              onClick={handleUnfreeze}
              disabled={busy}
              className="flex-1 py-2 rounded-xl bg-tg-button text-tg-buttonText text-sm font-semibold disabled:opacity-50"
            >
              {busy ? "…" : "Разморозить"}
            </button>
          ) : (
            extra.can_freeze && (
              <button
                onClick={handleFreeze}
                disabled={busy}
                className="flex-1 py-2 rounded-xl bg-tg-secondaryBg ring-1 ring-tg-hint text-tg-text text-sm disabled:opacity-50"
              >
                {busy ? "…" : "Заморозить на 7 дн."}
              </button>
            )
          )}
          <button
            onClick={handleCancel}
            disabled={busy}
            className="py-2 px-4 rounded-xl text-red-400 ring-1 ring-red-400/30 text-sm disabled:opacity-50"
          >
            {busy ? "…" : "Отписаться"}
          </button>
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const color =
    status === "active"
      ? "bg-green-500/15 text-green-300 border-green-500/30"
      : status === "frozen"
        ? "bg-blue-500/15 text-blue-300 border-blue-500/30"
        : status === "expired"
          ? "bg-yellow-500/15 text-yellow-300 border-yellow-500/30"
          : "bg-red-500/15 text-red-300 border-red-500/30";
  const label =
    status === "active"
      ? "активна"
      : status === "frozen"
        ? "заморожена"
        : status === "expired"
          ? "истекла"
          : status;
  return <span className={`chip ${color}`}>{label}</span>;
}

const TOPUP_PRESETS = [10000, 30000, 60000, 150000]; // kopecks: 100/300/600/1500 ₽

function TopupModal({ onClose }: { onClose: () => void }) {
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
          // Give the backend a tick to mark the invoice paid, then
          // close so /me reloads via Home's refresh.
          setTimeout(onClose, 500);
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
    <div className="fixed inset-0 bg-black/60 flex items-end justify-center" onClick={onClose}>
      <div
        className="bg-tg-bg rounded-t-3xl border-t border-white/10 p-6 max-w-xl w-full"
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
