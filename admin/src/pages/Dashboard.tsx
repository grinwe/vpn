import { useQuery } from "@tanstack/react-query";
import { api, StatsOut } from "../api";

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
