import { useEffect, useRef, useState } from "react";
import { fetchInvoiceStatus, InvoiceStatusResponse } from "../api";
import { navigate } from "../router";

// 2-second poll. The slow leg is Ansible (multi-protocol provision on
// the node), and 30-60s is realistic. We cap polling at 3 minutes; if
// nothing happens by then we surface an error and let the user fall back
// to /status in the bot. Faster polling buys nothing, slower frustrates.
const POLL_INTERVAL_MS = 2000;
const MAX_POLL_MS = 180_000;

type Stage = "paid" | "provisioning" | "ready" | "stuck" | "error";

export default function CheckoutPending({ invoiceId }: { invoiceId: number }) {
  const [stage, setStage] = useState<Stage>("paid");
  const [error, setError] = useState<string | null>(null);
  const startedAt = useRef<number>(Date.now());

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;

    async function tick() {
      if (cancelled) return;
      let data: InvoiceStatusResponse;
      try {
        data = await fetchInvoiceStatus(invoiceId);
      } catch (e) {
        setError((e as Error).message);
        setStage("error");
        return;
      }
      if (cancelled) return;

      if (data.has_credentials && data.subscription_active) {
        setStage("ready");
        return;
      }
      if (data.status === "paid") setStage("provisioning");

      if (Date.now() - startedAt.current > MAX_POLL_MS) {
        setStage("stuck");
        return;
      }
      timer = window.setTimeout(tick, POLL_INTERVAL_MS);
    }
    tick();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
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
              ? error
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
