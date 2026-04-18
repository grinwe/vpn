import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";

interface AuditLogOut {
  id: number;
  actor: string;
  actor_type: string;
  action: string;
  target_type: string;
  target_id: number | null;
  created_at: string;
  extra: Record<string, unknown> | null;
}

interface AuditLogListResponse {
  items: AuditLogOut[];
  total: number;
  has_more: boolean;
}

export default function AuditLogs() {
  const [action, setAction] = useState("");
  const [targetType, setTargetType] = useState("");
  const [actor, setActor] = useState("");
  const [offset, setOffset] = useState(0);
  const limit = 50;

  const params = new URLSearchParams();
  if (action) params.set("action", action);
  if (targetType) params.set("target_type", targetType);
  if (actor) params.set("actor", actor);
  params.set("limit", String(limit));
  params.set("offset", String(offset));

  const { data, isLoading, error } = useQuery<AuditLogListResponse>({
    queryKey: ["audit-logs", action, targetType, actor, offset],
    queryFn: () => api.get(`/audit-logs?${params.toString()}`),
  });

  return (
    <div>
      <h1 className="text-xl font-bold mb-4">Audit Log</h1>

      {/* Filters */}
      <div className="flex gap-2 mb-4 text-sm">
        <input
          placeholder="Action (e.g. config_created)"
          value={action}
          onChange={(e) => { setAction(e.target.value); setOffset(0); }}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 w-48"
        />
        <input
          placeholder="Target type (e.g. vpn_node)"
          value={targetType}
          onChange={(e) => { setTargetType(e.target.value); setOffset(0); }}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 w-48"
        />
        <input
          placeholder="Actor"
          value={actor}
          onChange={(e) => { setActor(e.target.value); setOffset(0); }}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 w-40"
        />
        {(action || targetType || actor) && (
          <button
            onClick={() => { setAction(""); setTargetType(""); setActor(""); setOffset(0); }}
            className="text-xs text-tg-hint hover:text-white"
          >
            Сбросить
          </button>
        )}
      </div>

      {isLoading && <div className="text-slate-400">Загрузка…</div>}
      {error && <div className="text-red-400">{(error as Error).message}</div>}

      {data && (
        <>
          <div className="text-xs text-slate-400 mb-2">
            Всего: {data.total} | Показано: {offset + 1}–{offset + data.items.length}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-slate-400 border-b border-slate-700">
                <tr>
                  <th className="text-left py-1 px-2">ID</th>
                  <th className="text-left py-1 px-2">Дата</th>
                  <th className="text-left py-1 px-2">Actor</th>
                  <th className="text-left py-1 px-2">Action</th>
                  <th className="text-left py-1 px-2">Target</th>
                  <th className="text-left py-1 px-2">Extra</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((log) => (
                  <tr key={log.id} className="border-b border-slate-800 hover:bg-slate-800/50">
                    <td className="py-1 px-2 text-slate-500">{log.id}</td>
                    <td className="py-1 px-2 text-slate-400 whitespace-nowrap">
                      {new Date(log.created_at).toLocaleString()}
                    </td>
                    <td className="py-1 px-2">
                      <span className="text-slate-300">{log.actor}</span>
                      <span className="text-slate-500 ml-1">({log.actor_type})</span>
                    </td>
                    <td className="py-1 px-2 font-mono text-blue-300">{log.action}</td>
                    <td className="py-1 px-2">
                      <span className="text-slate-400">{log.target_type}</span>
                      {log.target_id != null && (
                        <span className="text-slate-500 ml-1">#{log.target_id}</span>
                      )}
                    </td>
                    <td className="py-1 px-2 text-slate-500 max-w-xs truncate font-mono">
                      {log.extra ? JSON.stringify(log.extra) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          <div className="flex gap-2 mt-3">
            <button
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - limit))}
              className="text-xs px-3 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-30"
            >
              ← Назад
            </button>
            <button
              disabled={!data.has_more}
              onClick={() => setOffset(offset + limit)}
              className="text-xs px-3 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-30"
            >
              Вперёд →
            </button>
          </div>
        </>
      )}
    </div>
  );
}
