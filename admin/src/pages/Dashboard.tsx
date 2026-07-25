import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, StatsOut } from "../api";

type FunnelStep = { key: string; label: string; count: number; pct: number };
type FunnelOut = {
  days: number | null;
  total: number;
  steps: FunnelStep[];
  losses: FunnelStep[];
  trial_failures: number;
  telemetry_partial: boolean;
};

/** Полоска шага воронки: ширина = доля от пришедших в бота. */
function FunnelBar({ step, tone }: { step: FunnelStep; tone: "step" | "loss" }) {
  const bar = tone === "loss" ? "bg-red-800" : "bg-emerald-800";
  return (
    <div className="mb-2">
      <div className="flex justify-between text-sm">
        <span className="text-slate-300">{step.label}</span>
        <span className="text-slate-400">
          {step.count} <span className="text-slate-500">({step.pct}%)</span>
        </span>
      </div>
      <div className="h-2 bg-slate-900 rounded mt-1 overflow-hidden">
        <div className={`h-full ${bar}`} style={{ width: `${Math.min(step.pct, 100)}%` }} />
      </div>
    </div>
  );
}

function OnboardingFunnel() {
  const [days, setDays] = useState(7);
  const { data, isLoading, error } = useQuery<FunnelOut>({
    queryKey: ["onboarding-funnel", days],
    queryFn: () => api.get(`/admin/onboarding-funnel?days=${days}`),
  });

  return (
    <section className="mb-6">
      <div className="flex items-center gap-3 mb-2">
        <h2 className="text-xs uppercase text-slate-400">Онбординг новых юзеров</h2>
        <div className="flex gap-1">
          {[7, 30, 0].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`text-xs px-2 py-0.5 rounded ${
                days === d ? "bg-slate-700 text-white" : "bg-slate-800 text-slate-400"
              }`}
            >
              {d === 0 ? "всё время" : `${d} дн.`}
            </button>
          ))}
        </div>
      </div>
      <div className="bg-slate-800 rounded-lg p-4 border border-slate-700">
        {isLoading ? (
          <div className="text-slate-400 text-sm">Загрузка…</div>
        ) : error || !data ? (
          <div className="text-red-400 text-sm">Ошибка: {String(error)}</div>
        ) : data.total === 0 ? (
          <div className="text-slate-400 text-sm">В окне нет новых юзеров.</div>
        ) : (
          <>
            <div className="text-sm text-slate-400 mb-3">
              Пришло в бота: <span className="text-white font-semibold">{data.total}</span>
            </div>
            {data.steps.slice(1).map((s) => (
              <FunnelBar key={s.key} step={s} tone="step" />
            ))}
            <div className="text-xs uppercase text-slate-500 mt-4 mb-2">Где теряем</div>
            {data.losses.map((s) => (
              <FunnelBar key={s.key} step={s} tone="loss" />
            ))}
            {data.trial_failures > 0 && (
              <div className="text-sm text-yellow-400 mt-3">
                ⚠️ У {data.trial_failures} юзеров активация триала отказала — смотри
                AuditLog «trial_activate_rejected».
              </div>
            )}
            {data.telemetry_partial && (
              <div className="text-xs text-slate-500 mt-3">
                Часть когорты старше телеметрии (2026-07-25): «Открыли кабинет»
                занижено — события тогда ещё не писались.
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}

function Card({
  title,
  value,
  tone = "default",
}: {
  title: string;
  value: string | number;
  tone?: "default" | "warn" | "bad" | "good";
}) {
  const toneCls =
    tone === "warn"
      ? "border-yellow-600"
      : tone === "bad"
      ? "border-red-600"
      : tone === "good"
      ? "border-emerald-600"
      : "border-slate-700";
  return (
    <div className={`bg-slate-800 rounded-lg p-4 border ${toneCls}`}>
      <div className="text-xs uppercase text-slate-400">{title}</div>
      <div className="text-2xl font-semibold mt-1">{value}</div>
    </div>
  );
}

export default function Dashboard() {
  const { data, isLoading, error, refetch, isFetching } = useQuery<StatsOut>({
    queryKey: ["stats"],
    queryFn: () => api.get("/stats"),
    refetchInterval: 15_000, // cheap counters; 15s keeps the page "live"
  });

  if (isLoading) return <div>Загрузка…</div>;
  if (error || !data)
    return <div className="text-red-400">Ошибка: {String(error)}</div>;

  const nodesDown = data.nodes_total - data.nodes_active;

  return (
    <div>
      <div className="flex items-center mb-4 gap-3">
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <button
          onClick={() => refetch()}
          className="text-sm px-2 py-1 rounded bg-slate-800 hover:bg-slate-700"
        >
          {isFetching ? "…" : "↻"}
        </button>
      </div>

      <OnboardingFunnel />

      <section className="mb-6">
        <h2 className="text-xs uppercase text-slate-400 mb-2">Пользователи</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Card title="Всего" value={data.users_total} />
          <Card title="Активные подписки" value={data.subscriptions_active} tone="good" />
          <Card title="Всего подписок" value={data.subscriptions_total} />
          <Card title="Устройств активно" value={data.devices_active} />
        </div>
      </section>

      <section className="mb-6">
        <h2 className="text-xs uppercase text-slate-400 mb-2">
          Активность за 24ч{" "}
          <span className="normal-case text-slate-500">
            (по реальному трафику нод)
          </span>
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Card
            title="Юзеры (24ч)"
            value={data.users_active_24h}
            tone="good"
          />
          <Card title="Устройства (24ч)" value={data.devices_active_24h} />
          <Card
            title="Сироты активны (24ч)"
            value={data.orphans_active_24h}
            tone={data.orphans_active_24h > 0 ? "warn" : "default"}
          />
        </div>
      </section>

      <section className="mb-6">
        <h2 className="text-xs uppercase text-slate-400 mb-2">Ноды</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Card title="Всего" value={data.nodes_total} />
          <Card
            title="Активные"
            value={data.nodes_active}
            tone={data.nodes_active === data.nodes_total ? "good" : "warn"}
          />
          <Card
            title="Down"
            value={nodesDown}
            tone={nodesDown > 0 ? "bad" : "default"}
          />
        </div>
      </section>

      <section>
        <h2 className="text-xs uppercase text-slate-400 mb-2">Очередь / платежи</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Card
            title="Инвойсы pending"
            value={data.invoices_pending}
            tone={data.invoices_pending > 0 ? "warn" : "default"}
          />
          <Card
            title="Задачи pending"
            value={data.provisioning_tasks_pending}
          />
          <Card
            title="Задачи failed"
            value={data.provisioning_tasks_failed}
            tone={data.provisioning_tasks_failed > 0 ? "bad" : "default"}
          />
        </div>
      </section>

      <p className="text-xs text-slate-500 mt-6">
        Time-series и per-node метрики — в Grafana (SSH-туннель на :3000).
      </p>
    </div>
  );
}
