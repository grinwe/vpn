import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import QRCode from "qrcode";
import {
  activateTrial,
  activateSubscription,
  fetchPlans,
  authWithInitData,
  fetchReferral,
  setToken,
  MeResponse,
  ReferralInfo,
  Subscription,
  SubscriptionExtra,
  DeviceSummary,
  addDevice,
  renameDevice,
  removeDevice,
  createTopup,
  fetchMe,
  pollBalanceIncrease,
  cancelSubscription,
  freezeSubscription,
  unfreezeSubscription,
  toggleAutoRenew,
} from "../api";
import { navigate } from "../router";
import { getTg, openExternalUrl } from "../telegram";

// ── Устойчивая загрузка реф-блока ────────────────────────────────────
// Раньше реферал тянулся одним fetchReferral().catch(() => undefined) на
// маунте: один сетевой промах на плохой сети (метро/лифт — основная среда
// Mini App) — и весь реферальный блок (промокод, ссылка, заработок) не
// рендерился до полной перезагрузки приложения. Тянем его тем же устойчивым
// способом, что и /me в App.tsx: ретраи на транзиентных сбоях + разовая
// прозрачная переавторизация по initData на 401/403.
const REFERRAL_RETRIES = 2;
const REFERRAL_RETRY_DELAY_MS = 2000;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Ошибки из api.ts прилетают как Error("401: ...") / Error("Failed to fetch").
function isReferralAuthError(e: unknown): boolean {
  return /^(401|403)/.test((e as Error)?.message ?? "");
}

// Разовая переавторизация через Telegram initData (живёт весь сеанс Mini App).
async function reauthReferral(): Promise<boolean> {
  const tg = getTg();
  if (!tg || !tg.initData) return false;
  try {
    const auth = await authWithInitData(tg.initData);
    setToken(auth.token);
    return true;
  } catch {
    return false;
  }
}

async function fetchReferralResilient(): Promise<ReferralInfo> {
  let lastErr: unknown;
  for (let attempt = 0; attempt <= REFERRAL_RETRIES; attempt++) {
    try {
      return await fetchReferral();
    } catch (e) {
      lastErr = e;
      // Токен протух — переавторизуемся один раз и сразу повторяем без паузы.
      if (isReferralAuthError(e) && (await reauthReferral())) {
        try {
          return await fetchReferral();
        } catch (e2) {
          lastErr = e2;
        }
      }
      if (attempt < REFERRAL_RETRIES) await sleep(REFERRAL_RETRY_DELAY_MS);
    }
  }
  throw lastErr;
}

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
  // E2.2/E2.3: результат активации подарка должен быть ВИДЕН. Раньше баннер
  // просто исчезал, баланс снова показывал 0 ₽, и юзер не понимал, выдали ему
  // VPN или нет; а провал уходил в console.warn — «тапнул, ничего не
  // произошло, ушёл».
  const [trialDone, setTrialDone] = useState<{ subToken: string | null } | null>(null);
  const [trialError, setTrialError] = useState<string | null>(null);

  // Trial is retroactive: any user whose trial_activated_at is still
  // NULL sees the banner, including "old" users who registered before
  // the trial system existed. Activation is one-shot on the backend
  // (409 on repeat), so we just trust the flag from /me.
  const trialAvailable = me.balance.trial_available;
  const trialAmountRub = me.balance.trial_amount_kopecks / 100;

  const handleActivateTrial = async () => {
    if (trialActivating) return;
    setTrialActivating(true);
    setTrialError(null);
    try {
      await activateTrial(); // зачисляет бонус на баланс
      // Сразу тратим бонус на подписку и провижн — «Забрать месяц» = рабочий
      // VPN, а не деньги на балансе (иначе большинство застревает на бонусе:
      // триал даёт только баланс, и второй шаг «активировать план» неочевиден).
      // Бонус размерен ровно под самый дешёвый 30-дневный план (_trial_plan),
      // а мы активируем ровно его (самый дешёвый месячный) — баланса хватает.
      //
      // НО только когда у юзера НЕТ живой подписки: /subscriptions/activate —
      // это смена тарифа в single-sub модели, он отзывает все active/frozen
      // подписки (+ревок девайсов, +проратный возврат). Для юзера с годовой
      // подпиской, который просто не забирал триал, тихая авто-активация
      // означала бы потерю подписки без единого подтверждения (аудит
      // 2026-07-25). Флаг считает бэкенд; undefined (старый бэк) = «нельзя».
      if (!me.balance.trial_autoactivate_allowed) {
        onRefresh();
        return;
      }
      try {
        const plans = await fetchPlans();
        const cheapestMonthly = plans
          .filter((p) => p.period === "month")
          .sort((a, b) => a.price_rub - b.price_rub)[0];
        if (cheapestMonthly) {
          const res = await activateSubscription(cheapestMonthly.id);
          getTg()?.HapticFeedback?.notificationOccurred("success");
          // Ссылка приходит прямо в ответе — показываем экран «Готово» без
          // лишнего запроса и без ожидания рефреша /me.
          setTrialDone({ subToken: res.sub_token ?? null });
        }
      } catch (actErr) {
        // Бонус на балансе, но подписка не активировалась (нет свободных нод,
        // таймаут провижининга). Молчать здесь нельзя: для юзера это выглядит
        // как «нажал и ничего не случилось».
        console.warn("trial auto-activate failed", actErr);
        setTrialError(
          "Бонус зачислен, но VPN не выдался — попробуй ещё раз. " +
            "Если не выйдет, напиши в поддержку из раздела «Помощь».",
        );
      }
      // Refresh /me: баннер исчезнет, появятся активная подписка + баланс.
      onRefresh();
    } catch (err) {
      // 409 = подарок уже забран (другая вкладка/устройство) — это не ошибка,
      // просто обновляем состояние. Остальное — показываем юзеру.
      const status = (err as { status?: number } | null)?.status;
      if (status !== 409) {
        setTrialError(
          "Не получилось забрать подарок. Проверь связь и попробуй ещё раз.",
        );
      }
      console.warn("trial activate failed", err);
      onRefresh();
    } finally {
      setTrialActivating(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetchReferralResilient()
        .then((r) => {
          if (!cancelled) setReferral(r);
        })
        .catch(() => undefined);
    };
    load();
    // Пере-запрашиваем реф-блок при возврате в приложение / восстановлении
    // связи — тем же событием, что и /me в App.tsx. Если первый заход
    // пришёлся на секундный обрыв, блок подтянется, когда юзер вернётся в
    // кабинет, а не исчезнет на весь сеанс. Дебаунс: при частой смене сети
    // (toggling VPN, Wi-Fi↔LTE) события сыплются пачками — схлопываем.
    let timer: ReturnType<typeof setTimeout> | undefined;
    const trigger = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(load, 500);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") trigger();
    };
    window.addEventListener("online", trigger);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      window.removeEventListener("online", trigger);
      document.removeEventListener("visibilitychange", onVisibility);
    };
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

      {/* ── Free trial banner (E2.1: главный элемент первого экрана) ──
          Раньше самым крупным блоком была карточка баланса «0 ₽» с кнопкой
          «Пополнить» — просьба занести денег до того, как показана польза. */}
      {trialAvailable && trialAmountRub > 0 && (
        <section className="card-hero mb-4">
          <div className="text-tg-hint text-xs uppercase tracking-wide">Подарок</div>
          <div className="text-2xl font-bold mt-1">🎁 Забери бесплатный месяц</div>
          <div className="text-sm text-tg-hint mt-2">
            Карта не нужна. Один тап — и получишь ссылку с инструкцией,
            как подключиться.
          </div>
          <button
            className="btn-primary w-full mt-4 py-3 text-base"
            disabled={trialActivating}
            onClick={handleActivateTrial}
          >
            {trialActivating ? "Включаем VPN…" : "Активировать бесплатно"}
          </button>
          {trialError && (
            <div className="mt-3 text-sm bg-red-900/40 ring-1 ring-red-500 rounded-lg p-2 text-red-100">
              {trialError}
              <button
                className="underline ml-1"
                onClick={handleActivateTrial}
                disabled={trialActivating}
              >
                Попробовать снова
              </button>
            </div>
          )}
        </section>
      )}

      {trialDone && (
        <TrialSuccess
          subToken={trialDone.subToken}
          subLinkBase={me.sub_link_base_url}
          onClose={() => {
            setTrialDone(null);
            onRefresh();
          }}
        />
      )}

      {/* ── Balance card ── */}
      {/* Пока подписок нет, баланс — не главное: показываем его обычной
          карточкой, чтобы не спорить за внимание с подарком (E2.1). */}
      <section className="mb-4">
        <div
          className={`${me.subscriptions.length === 0 ? "card" : "card-hero"} ${
            lowBalance ? "card-danger" : ""
          }`}
        >
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
          <div className="card text-sm">
            <div className="text-tg-hint">
              {trialAvailable
                ? "Подписки пока нет — забери бесплатный месяц выше."
                : "Подписки пока нет."}
            </div>
            {!trialAvailable && (
              <button
                className="btn-primary w-full mt-3"
                onClick={() => navigate({ name: "plans" })}
              >
                Выбрать тариф
              </button>
            )}
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
      {/* E2.7: не просим приводить друзей у того, кто сам ещё не пользовался
          сервисом — на первом экране это расфокусирует и выглядит как шум. */}
      {referral && referral.code && me.subscriptions.length > 0 && (
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
      {topupOpen && createPortal(<TopupModal onClose={() => setTopupOpen(false)} onRefresh={onRefresh} baselineKopecks={me.balance.balance_kopecks} />, document.body)}
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
    // Игнорируем клики, пока запрос в полёте: автопродление — денежная
    // настройка, а быстрый двойной тап отправил бы два POST с одинаковым
    // newValue (пропсы обновятся только после onRefresh) и тумблер бы скакал.
    if (busy) return;
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
          className={`mt-3 flex items-center justify-between cursor-pointer ${
            busy ? "opacity-50 pointer-events-none" : ""
          }`}
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


/** Ссылки на клиенты под платформу юзера (E2.4).
 *
 * Раньше инструкция говорила «установите v2rayNG из Google Play» без единой
 * ссылки: человек должен был выйти из Telegram, найти приложение по названию и
 * вернуться — самый длинный разрыв между «получил ссылку» и «работает VPN».
 */
const CLIENT_LINKS: { label: string; url: string }[] = [
  { label: "App Store", url: "https://apps.apple.com/app/happ-proxy-utility/id6504287215" },
  { label: "Google Play", url: "https://play.google.com/store/apps/details?id=com.happproxy" },
];

/** Экран «Готово» после активации подарка (E2.2 + E2.4 + E3.1).
 *
 * У платного пути такой экран есть (Plans.ActivatedScreen), у бесплатного не
 * было: баннер исчезал, баланс снова показывал 0 ₽, и юзер не понимал, что VPN
 * уже выдан и что ссылку нужно вставить в отдельное приложение.
 */
function TrialSuccess({
  subToken,
  subLinkBase,
  onClose,
}: {
  subToken: string | null;
  subLinkBase: string;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const subUrl = subToken
    ? subLinkBase
      ? `${subLinkBase}/${subToken}`
      : `${window.location.origin}/api/sub/${subToken}`
    : null;

  return createPortal(
    <div className="fixed inset-0 z-50 bg-black/70 flex items-end sm:items-center justify-center p-4">
      <div className="card w-full max-w-md">
        <div className="text-center">
          <div className="text-4xl mb-2">✅</div>
          <h2 className="text-xl font-semibold">Готово, VPN активен</h2>
        </div>

        {subUrl && (
          <div className="mt-4">
            <div className="text-xs uppercase tracking-wide text-tg-hint mb-1">
              Твоя ссылка
            </div>
            <div className="text-xs break-all bg-tg-secondaryBg rounded-lg p-2 ring-1 ring-tg-hint/30">
              {subUrl}
            </div>
            <button
              className="btn-primary w-full mt-2"
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
            >
              {copied ? "Скопировано ✓" : "📋 Скопировать ссылку"}
            </button>
          </div>
        )}

        <div className="mt-4 text-sm">
          <div className="font-semibold mb-2">Как подключиться</div>
          <ol className="space-y-2 text-tg-hint">
            <li>
              1. Установи HAPP:{" "}
              {CLIENT_LINKS.map((c, i) => (
                <span key={c.url}>
                  {i > 0 && " · "}
                  <button
                    className="underline text-tg-text"
                    onClick={() => openExternalUrl(getTg(), c.url)}
                  >
                    {c.label}
                  </button>
                </span>
              ))}
            </li>
            <li>2. Вставь ссылку в приложение</li>
            <li>3. Нажми «Подключиться»</li>
          </ol>
        </div>

        {/* E3.1 — продаём «Помощь» ровно там, где она нужна: в момент первого
            подключения. В приветствии это читалось бы как «у нас часто не
            работает», здесь — как забота. Кнопка реально переносит устройство
            на другой сервер. */}
        <div className="mt-4 text-xs text-tg-hint">
          Не подключается? Нажми <b>«Помощь»</b> — перенесём тебя на другой сервер.
        </div>

        <button className="btn-ghost w-full mt-4" onClick={onClose}>
          Понятно
        </button>
      </div>
    </div>,
    document.body,
  );
}

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
  baselineKopecks,
}: {
  onClose: () => void;
  onRefresh: () => void;
  // Баланс на момент открытия модалки — точка отсчёта для поллинга карты
  // (передаётся из Home, чтобы не зависеть от отдельного fetchMe).
  baselineKopecks: number;
}) {
  const [busy, setBusy] = useState(false);
  const [customRub, setCustomRub] = useState<string>("");
  // Выбранная сумма в копейках: null → шаг ввода суммы; число → шаг «чем
  // платить». Сначала сумма, затем способ оплаты.
  const [amount, setAmount] = useState<number | null>(null);
  // Карта отдаёт внешнюю страницу без callback → после её открытия ждём
  // зачисление поллингом баланса.
  const [waiting, setWaiting] = useState(false);
  const [waitTimedOut, setWaitTimedOut] = useState(false);
  // Успех карточной оплаты: lava не редиректит обратно в Mini App, поэтому
  // показываем явное «баланс пополнен», а не молча закрываем модалку.
  const [success, setSuccess] = useState(false);
  // Модалку можно закрыть во время ожидания — гвардим setState после unmount
  // (poll живёт ~90с, юзер мог уже закрыть).
  const mountedRef = useRef(true);
  useEffect(() => () => { mountedRef.current = false; }, []);
  // Токен поколения поллинга: закрытие/повторная проверка инкрементят его,
  // отменяя отвязанный поллинг, чтобы он не дёргал UI постфактум.
  const pollGenRef = useRef(0);
  // Baseline, реально использованный для текущего платежа (чтобы «Проверить
  // ещё раз» сравнивал с той же точкой отсчёта, а не с уже зачисленным).
  const baselineUsedRef = useRef(0);

  async function payStars(amountKopecks: number) {
    const tg = getTg();
    if (!tg) {
      alert("Открой эту страницу в Telegram");
      return;
    }
    setBusy(true);
    try {
      const res = await createTopup(amountKopecks, "telegram_stars");
      tg.openInvoice(res.pay_url, (status) => {
        if (mountedRef.current) setBusy(false);
        if (status === "paid") {
          tg.HapticFeedback?.notificationOccurred("success");
          // Даём бэкенду тик, чтобы пометить инвойс оплаченным, затем
          // явно перезапрашиваем /me (App сам не перезагрузит: route
          // не меняется) и закрываем модалку — баланс на экране обновится.
          // onClose гвардим mountedRef: callback старого (закрытого) инстанса
          // не должен закрыть заново открытую модалку.
          setTimeout(() => {
            onRefresh();
            if (mountedRef.current) onClose();
          }, 500);
        } else if (status === "failed") {
          tg.HapticFeedback?.notificationOccurred("error");
          if (mountedRef.current) alert("Оплата не прошла. Попробуй ещё раз.");
        }
      });
    } catch (e) {
      if (mountedRef.current) setBusy(false);
      alert(`Не удалось создать счёт: ${(e as Error).message}`);
    }
  }

  // Поллинг зачисления после открытия внешней карточной страницы.
  async function pollCredit() {
    const myGen = ++pollGenRef.current;
    setWaitTimedOut(false);
    setWaiting(true);
    // ~90 c: карта дольше Stars (редирект, ввод карты, 3DS).
    const credited = await pollBalanceIncrease(baselineUsedRef.current, {
      attempts: 30,
      delayMs: 3000,
      shouldStop: () => pollGenRef.current !== myGen || !mountedRef.current,
    });
    // Поллинг мог быть отменён (закрытие/новый платёж) — не трогаем UI.
    if (pollGenRef.current !== myGen) return;
    if (credited) {
      getTg()?.HapticFeedback?.notificationOccurred("success");
      onRefresh(); // App-level: баланс на экране обновится под модалкой
      // Не закрываем молча — lava не вернёт юзера в приложение, поэтому
      // показываем явный экран успеха (он же обновит фон балансом).
      if (mountedRef.current) {
        setWaiting(false);
        setSuccess(true);
      }
      return;
    }
    if (mountedRef.current) {
      setWaiting(false);
      setWaitTimedOut(true);
    }
  }

  async function payCard(amountKopecks: number) {
    const tg = getTg();
    setBusy(true);
    try {
      // Свежий baseline с фолбэком на проп при сбое /me — без недостижимого
      // сентинела (иначе поллинг никогда не подтвердил бы) и без устаревшего
      // значения (иначе быстрый повторный топап дал бы ложное «зачислено»).
      let baseline = baselineKopecks;
      try {
        baseline = (await fetchMe()).balance.balance_kopecks;
      } catch {
        /* /me не ответил — используем проп-baseline (реальное число) */
      }
      baselineUsedRef.current = baseline;
      const res = await createTopup(amountKopecks, "lava_top");
      // Модалку могли закрыть во время await — не открываем внешнюю страницу
      // и не стартуем поллинг постфактум.
      if (!mountedRef.current) return;
      openExternalUrl(tg, res.pay_url);
      setBusy(false);
      await pollCredit();
    } catch (e) {
      if (mountedRef.current) {
        setBusy(false);
        setWaiting(false);
      }
      alert(`Не удалось создать счёт: ${(e as Error).message}`);
    }
  }

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-end justify-center z-50 animate-fadeIn"
      onClick={onClose}
    >
      <div
        className="bg-tg-bg rounded-t-3xl border-t border-white/10 p-6 max-w-xl w-full max-h-[80vh] overflow-y-auto animate-slideUp"
        onClick={(e) => e.stopPropagation()}
      >
        {success ? (
          <div className="text-center py-4">
            <h2 className="text-lg font-semibold mb-2">✅ Баланс пополнен</h2>
            <p className="text-tg-hint text-sm mb-4">
              {amount != null ? `Зачислено ${(amount / 100).toFixed(0)} ₽. ` : ""}
              Спасибо!
            </p>
            <button onClick={onClose} className="btn-primary w-full">
              Готово
            </button>
          </div>
        ) : waiting ? (
          <div className="text-center py-4">
            <h2 className="text-lg font-semibold mb-2">Ждём подтверждение оплаты…</h2>
            <p className="text-tg-hint text-sm mb-4">
              Оплатите на открывшейся странице. Баланс обновится автоматически
              после подтверждения.
            </p>
            <button onClick={onClose} className="w-full py-2 text-tg-hint text-sm">
              Закрыть
            </button>
          </div>
        ) : waitTimedOut ? (
          <div className="py-2">
            <h2 className="text-lg font-semibold mb-2">Оплата пока не подтвердилась</h2>
            <p className="text-tg-hint text-sm mb-4">
              Если вы оплатили — баланс появится в течение минуты. Можно
              проверить ещё раз.
            </p>
            <button onClick={pollCredit} className="btn-primary w-full">
              Проверить ещё раз
            </button>
            <button onClick={onClose} className="w-full mt-2 py-2 text-tg-hint text-sm">
              Закрыть
            </button>
          </div>
        ) : amount === null ? (
          <>
            <h2 className="text-lg font-semibold mb-3">Пополнение баланса</h2>
            <div className="grid grid-cols-2 gap-2 mb-4">
              {TOPUP_PRESETS.map((kop) => (
                <button
                  key={kop}
                  disabled={busy}
                  onClick={() => setAmount(kop)}
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
                disabled={!customRub || Number(customRub) < 100}
                // Округляем до целых копеек: ввод «100.1»/«100.505» иначе
                // ушёл бы на бэкенд как float и упал бы на int-валидации (422).
                onClick={() => setAmount(Math.round(Number(customRub) * 100))}
                className="btn-primary"
              >
                Далее
              </button>
            </div>
            {customRub && Number(customRub) > 0 && Number(customRub) < 100 && (
              <div className="text-red-400 text-xs mt-1">Минимальная сумма пополнения — 100 ₽</div>
            )}
            <button
              onClick={onClose}
              className="w-full mt-4 py-2 text-tg-hint text-sm"
            >
              Отмена
            </button>
          </>
        ) : (
          <>
            <h2 className="text-lg font-semibold mb-1">Выберите способ оплаты</h2>
            <p className="text-tg-hint text-sm mb-4">
              Пополнение на {(amount / 100).toFixed(0)} ₽
            </p>
            <button
              disabled={busy}
              onClick={() => payStars(amount)}
              className="btn-ghost w-full mb-2"
            >
              ⭐ Telegram Stars
            </button>
            <button
              disabled={busy}
              onClick={() => payCard(amount)}
              className="btn-primary w-full"
            >
              💳 Карта РФ / СБП
            </button>
            <button
              onClick={() => setAmount(null)}
              disabled={busy}
              className="w-full mt-4 py-2 text-tg-hint text-sm"
            >
              ← Назад
            </button>
          </>
        )}
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
