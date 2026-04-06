import { useQuery } from "@tanstack/react-query";
import { api, VPNNodeOut } from "../api";

function statusColor(status: string) {
  switch (status) {
    case "active":
      return "text-emerald-400";
    case "registering":
      return "text-yellow-400";
    case "disabled":
      return "text-slate-500";
    case "error":
      return "text-red-400";
    default:
      return "text-slate-300";
  }
}

export default function Nodes() {
  const { data, isLoading, error } = useQuery<VPNNodeOut[]>({
    queryKey: ["nodes"],
    queryFn: () => api.get("/nodes"),
    refetchInterval: 20_000,
  });

  if (isLoading) return <div>Загрузка…</div>;
  if (error) return <div className="text-red-400">Ошибка: {String(error)}</div>;

  return (
    <div>
      <h1 className="text-2xl font-semibold mb-4">Nodes</h1>
      <table className="w-full text-sm">
        <thead className="text-left text-slate-400 border-b border-slate-700">
          <tr>
            <th className="py-2">ID</th>
            <th>Имя</th>
            <th>Регион</th>
            <th>Host</th>
            <th>Pool</th>
            <th>Статус</th>
            <th>Активна</th>
            <th>Обновлена</th>
          </tr>
        </thead>
        <tbody>
          {data?.map((n) => (
            <tr key={n.id} className="border-b border-slate-800">
              <td className="py-2">{n.id}</td>
              <td className="font-mono">{n.name}</td>
              <td>{n.region}</td>
              <td className="font-mono text-slate-400">{n.host}</td>
              <td>{n.pool_id ?? "—"}</td>
              <td className={statusColor(n.status)}>{n.status}</td>
              <td>{n.is_active ? "✓" : "✕"}</td>
              <td>{new Date(n.updated_at).toLocaleString()}</td>
            </tr>
          ))}
          {data && data.length === 0 && (
            <tr>
              <td colSpan={8} className="py-4 text-slate-500 text-center">
                Нод нет
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <p className="text-xs text-slate-500 mt-4">
        Здоровье нод по регионам и probe-метрики — смотри Grafana dashboard
        "Fleet". Создание/удаление нод через этот UI пока не доступно — это
        делает бэкенд через <span className="font-mono">POST /api/nodes/spawn</span>.
      </p>
    </div>
  );
}
