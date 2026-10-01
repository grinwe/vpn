import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiTokenOut, ApiTokenCreatedOut } from "../api";

// traffic:write удалён 2026-07-29 вместе с блокирующим ингестом трафика:
// учёт наливает тик traffic_stats напрямую. Держать в sync с ALL_SCOPES
// (backend/app/auth.py) — бэкенд отвергает неизвестные скоупы с 400.
const AVAILABLE_SCOPES = ["probe:read", "probe:write"] as const;

export default function ApiTokens() {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>([]);
  const [justCreated, setJustCreated] = useState<ApiTokenCreatedOut | null>(null);

  const { data, isLoading } = useQuery<ApiTokenOut[]>({
    queryKey: ["api-tokens"],
    queryFn: () => api.get("/api-tokens"),
  });

  const createToken = useMutation({
    mutationFn: (payload: { name: string; scopes: string[] }) =>
      api.post<ApiTokenCreatedOut>("/api-tokens", payload),
    onSuccess: (created) => {
      setJustCreated(created);
      setName("");
      setScopes([]);
      qc.invalidateQueries({ queryKey: ["api-tokens"] });
    },
  });

  const revokeToken = useMutation({
    mutationFn: (id: number) => api.del(`/api-tokens/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["api-tokens"] }),
  });

  const toggleScope = (s: string) => {
    setScopes((prev) =>
      prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]
    );
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-2">
        <h1 className="text-2xl font-semibold mb-4">API tokens</h1>

        {isLoading ? (
          <div>Загрузка…</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-slate-400 border-b border-slate-700">
              <tr>
                <th className="py-2">ID</th>
                <th>Имя</th>
                <th>Scopes</th>
                <th>Активен</th>
                <th>Последнее использование</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data?.map((t) => (
                <tr key={t.id} className="border-b border-slate-800">
                  <td className="py-2">{t.id}</td>
                  <td className="font-mono">{t.name}</td>
                  <td>
                    <div className="flex flex-wrap gap-1">
                      {t.scopes.map((s) => (
                        <span
                          key={s}
                          className="text-xs px-2 py-0.5 rounded bg-slate-700"
                        >
                          {s}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td>{t.is_active ? "✓" : "✕"}</td>
                  <td className="text-slate-400">
                    {t.last_used_at
                      ? new Date(t.last_used_at).toLocaleString()
                      : "—"}
                  </td>
                  <td>
                    {t.is_active && (
                      <button
                        onClick={() => {
                          if (confirm(`Отозвать токен "${t.name}"?`))
                            revokeToken.mutate(t.id);
                        }}
                        className="text-xs px-2 py-1 rounded bg-red-700 hover:bg-red-600"
                      >
                        revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {data && data.length === 0 && (
                <tr>
                  <td colSpan={6} className="py-4 text-slate-500 text-center">
                    Токенов нет
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      <aside className="bg-slate-800 rounded-lg p-4 border border-slate-700 h-fit space-y-4">
        <h2 className="font-semibold">Создать новый</h2>

        <label className="block text-sm">
          Имя
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="probe-eu-1"
            className="mt-1 w-full px-3 py-2 rounded bg-slate-900 border border-slate-700"
          />
        </label>

        <div className="text-sm">
          Scopes
          <div className="mt-1 space-y-1">
            {AVAILABLE_SCOPES.map((s) => (
              <label key={s} className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={scopes.includes(s)}
                  onChange={() => toggleScope(s)}
                />
                <span className="font-mono text-xs">{s}</span>
              </label>
            ))}
          </div>
        </div>

        <button
          onClick={() => createToken.mutate({ name: name.trim(), scopes })}
          disabled={!name.trim() || scopes.length === 0 || createToken.isPending}
          className="w-full py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50"
        >
          Создать
        </button>

        {createToken.isError && (
          <p className="text-sm text-red-400">
            {String(createToken.error)}
          </p>
        )}

        {justCreated && (
          <div className="bg-slate-900 border border-emerald-600 rounded p-3 text-xs space-y-2">
            <p className="text-emerald-400 font-semibold">
              Токен создан! Скопируй сейчас — показывается один раз.
            </p>
            <div className="font-mono break-all select-all bg-slate-950 p-2 rounded">
              {justCreated.token}
            </div>
            <button
              onClick={() => {
                navigator.clipboard.writeText(justCreated.token);
              }}
              className="text-xs px-2 py-1 rounded bg-slate-700 hover:bg-slate-600"
            >
              Копировать
            </button>
            <button
              onClick={() => setJustCreated(null)}
              className="text-xs px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 ml-2"
            >
              Скрыть
            </button>
          </div>
        )}
      </aside>
    </div>
  );
}
