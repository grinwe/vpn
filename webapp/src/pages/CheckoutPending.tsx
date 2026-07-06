import { useEffect, useRef, useState } from "react";
import { fetchInvoiceStatus, InvoiceStatusResponse } from "../api";
import { friendlyError } from "../errors";
import { navigate } from "../router";

// 2-second poll. The slow leg is Ansible (multi-protocol provision on
// the node), and 30-60s is realistic. We cap polling at 3 minutes; if
// nothing happens by then we surface an error and let the user fall back
// to /status in the bot. Faster polling buys nothing, slower frustrates.
const POLL_INTERVAL_MS = 2000;
const MAX_POLL_MS = 180_000;
// Транзиентные сетевые ошибки (смена Wi-Fi→LTE, короткий 502) не должны
// быть терминальными: показываем «Ошибка» только после N фейлов подряд.
// Порог поднят (было 5): пользователь мог только что включить свежий VPN,
// что рвёт webview на десятки секунд — 5×2с=10с этого не переживали.
const MAX_CONSECUTIVE_ERRORS = 8;
// Бэкофф между ретраями при ошибках: задержка растёт линейно и упирается
// в потолок, чтобы платёжный поллинг переживал типичную смену сети (30-60с),
// не выжигая счётчик за 10 секунд.
const MAX_ERROR_BACKOFF_MS = 8000;

type Stage = "paid" | "provisioning" | "ready" | "stuck" | "error";

export default function CheckoutPending({ invoiceId }: { invoiceId: number }) {
  const [stage, setStage] = useState<Stage>("paid");
  const [error, setError] = useState<string | null>(null);
  const startedAt = useRef<number>(Date.now());

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    let consecutiveErrors = 0;
    // active=true, пока цикл поллинга жив (ждёт fetch или запланирован таймером).
    // Гасим его только на терминальной стадии; stopReason помнит, какой именно —
    // чтобы после 'ready' (успех) поллинг НЕ возобновлялся.
    let active = true;
    let stopReason: Stage | null = null;

    function stop(reason: Stage) {
      active = false;
      stopReason = reason;
    }

    async function tick() {
      if (cancelled) return;
      let data: InvoiceStatusResponse;
      try {
        data = await fetchInvoiceStatus(invoiceId);
      } catch (e) {
        if (cancelled) return;
        consecutiveErrors += 1;
        if (
          consecutiveErrors < MAX_CONSECUTIVE_ERRORS &&
          Date.now() - startedAt.current <= MAX_POLL_MS
        ) {
          // Одиночный сетевой чих — продолжаем поллинг, не меняя stage.
          // Линейный бэкофф с потолком: даём сети восстановиться.
          const delay = Math.min(
            POLL_INTERVAL_MS * consecutiveErrors,
            MAX_ERROR_BACKOFF_MS,
          );
          timer = window.setTimeout(tick, delay);
          return;
        }
        setError(
          friendlyError((e as Error).message, {
            fallback: "проверить статус оплаты",
          }),
        );
        setStage("error");
        stop("error");
        return;
      }
      if (cancelled) return;
      consecutiveErrors = 0;

      if (data.has_credentials && data.subscription_active) {
        setStage("ready");
        stop("ready");
        return;
      }
      if (data.status === "paid") setStage("provisioning");

      if (Date.now() - startedAt.current > MAX_POLL_MS) {
        setStage("stuck");
        stop("stuck");
        return;
      }
      timer = window.setTimeout(tick, POLL_INTERVAL_MS);
    }

    // Возобновление поллинга при возврате сети / фокуса приложения.
    // Короткий обрыв (пользователь как раз включил свежий VPN, webview
    // отвалился) не должен навсегда оставлять экран в ⚠️ «Ошибке» на реально
    // оплаченном инвойсе: как только связь/видимость вернулись — перезапускаем
    // проверку, размыкая терминальное состояние. После успеха ('ready') и при
    // ещё живом цикле ничего не делаем.
    function resume() {
      if (cancelled || active) return;
      if (stopReason === "ready") return;
      active = true;
      stopReason = null;
      consecutiveErrors = 0;
      startedAt.current = Date.now();
      setError(null);
      setStage("provisioning");
      tick();
    }
    function onVisibility() {
      if (document.visibilityState === "visible") resume();
    }

    window.addEventListener("online", resume);
    document.addEventListener("visibilitychange", onVisibility);

    tick();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      window.removeEventListener("online", resume);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [invoiceId]);

  return (
    <div className="min-h-screen p-6 max-w-xl mx-auto flex flex-col items-center justify-center text-center">
      <div className="text-5xl mb-4">
        {stage === "ready" ? "✅" : stage === "error" || stage === "stuck" ? "⚠️" : "⏳"}
      </div>
      <h1 className="text-2xl font-semibold mb-2">
        {stage === "ready"
          ? "Готово!"
          : stage === "error"
            ? "Ошибка"
            : stage === "stuck"
              ? "Долго отвечаем"
              : "Активируем подписку…"}
      </h1>
      <div className="card w-full mt-4">
        <Steps stage={stage} />
      </div>
      {stage === "ready" && (
        <button
          onClick={() => navigate({ name: "home" })}
          className="btn-primary w-full mt-6"
        >
          Открыть мой кабинет
        </button>
      )}
      {(stage === "stuck" || stage === "error") && (
        <>
          <p className="text-tg-hint text-sm mt-4">
            {stage === "error"
              ? `${error ?? "Не удалось проверить статус оплаты."} Оплата, скорее всего, уже прошла — напиши /status в боте, чтобы увидеть актуальный статус. Как только связь восстановится, проверка возобновится сама.`
              : "Это нештатно, обычно занимает меньше минуты. Напиши /status в боте — там увидишь актуальный статус."}
          </p>
          <button
            onClick={() => navigate({ name: "home" })}
            className="btn-ghost w-full mt-4"
          >
            Вернуться на главную
          </button>
        </>
      )}
    </div>
  );
}

function Steps({ stage }: { stage: Stage }) {
  const items: { label: string; done: boolean; current: boolean }[] = [
    {
      label: "Оплата получена",
      done: stage !== "paid",
      current: stage === "paid",
    },
    {
      label: "Готовим конфиг на сервере",
      done: stage === "ready",
      current: stage === "provisioning",
    },
    {
      label: "Подписка активна",
      done: stage === "ready",
      current: false,
    },
  ];
  return (
    <ul className="text-left space-y-2">
      {items.map((it) => (
        <li key={it.label} className="flex items-center gap-2 text-sm">
          <span>
            {it.done ? "✅" : it.current ? "⏳" : "○"}
          </span>
          <span className={it.done ? "" : it.current ? "text-tg-text" : "text-tg-hint"}>
            {it.label}
          </span>
        </li>
      ))}
    </ul>
  );
}
