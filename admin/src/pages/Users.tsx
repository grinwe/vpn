import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, UserOut, SubscriptionOut } from "../api";

export default function Users() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  // Debounce the search input so we don't hammer the backend on every
  // keystroke — 300ms is the sweet spot between "feels instant" and
  // "one request per full word".
  const [debouncedSearch, setDebouncedSearch] = useState("");
  useEffect(() => {
    const id = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(id);
  }, [search]);
  const [selected, setSelected] = useState<UserOut | null>(null);

  const { data: users, isLoading } = useQuery<UserOut[]>({
    queryKey: ["users", { search: debouncedSearch }],
    queryFn: () =>
      api.get(
        `/users?limit=100${debouncedSearch ? `&search=${encodeURIComponent(debouncedSearch)}` : ""}`
      ),
  });

  const { data: subs } = useQuery<SubscriptionOut[]>({
    queryKey: ["user-subs", selected?.id],
    queryFn: () => api.get(`/users/${selected!.id}`),
    enabled: selected !== null,
  });

  const revokeNow = useMutation({
    mutationFn: (subId: number) =>
      api.post(`/subscriptions/${subId}/disable`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
    onError: (e: Error) => alert(`Не удалось отозвать подписку: ${e.message}`),
  });

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-2">
        <h1 className="text-2xl font-semibold mb-4">Users</h1>
        <input
          type="text"
          placeholder="Поиск по telegram_id или email…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full mb-4 px-3 py-2 rounded bg-slate-800 border border-slate-700"
        />
        {isLoading ? (
          <div>Загрузка…</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-slate-400 border-b border-slate-700">
              <tr>
                <th className="py-2">ID</th>
                <th>Telegram</th>
                <th>Email</th>
                <th>Subs</th>
                <th>Создан</th>
              </tr>
            </thead>
            <tbody>
              {users?.map((u) => (
                <tr
                  key={u.id}
                  onClick={() => setSelected(u)}
                  className={`cursor-pointer border-b border-slate-800 hover:bg-slate-800 ${
                    selected?.id === u.id ? "bg-slate-800" : ""
                  }`}
                >
                  <td className="py-2">{u.id}</td>
                  <td>{u.telegram_id ?? "—"}</td>
                  <td>{u.email ?? "—"}</td>
                  <td>{u.subscription_count}</td>
                  <td>{new Date(u.created_at).toLocaleDateString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <aside className="bg-slate-800 rounded-lg p-4 border border-slate-700 h-fit">
        <h2 className="font-semibold mb-2">Детали</h2>
        {!selected ? (
          <p className="text-sm text-slate-400">Выбери пользователя в таблице.</p>
        ) : (
          <div className="space-y-2 text-sm">
            <div>ID: {selected.id}</div>
            <div>Telegram: {selected.telegram_id ?? "—"}</div>
            <div>Email: {selected.email ?? "—"}</div>
            <div className="pt-2 border-t border-slate-700">
              <div className="font-semibold mb-1">Подписки</div>
              {subs === undefined ? (
                <div className="text-slate-400">Загрузка…</div>
              ) : subs.length === 0 ? (
                <div className="text-slate-400">Нет активных</div>
              ) : (
                <ul className="space-y-2">
                  {subs.map((s) => (
                    <li key={s.id} className="bg-slate-900 rounded p-2">
                      <div>{s.plan_name}</div>
                      <div className="text-slate-400">
                        {s.node} ({s.region}) · {s.status}
                      </div>
                      <div className="text-slate-500 text-xs">
                        до {new Date(s.expires_at).toLocaleDateString()}
                      </div>
                      {s.status !== "blocked" && s.status !== "expired" && (
                        <button
                          disabled={revokeNow.isPending}
                          onClick={() => {
                            if (
                              confirm(
                                `Отозвать подписку #${s.id} прямо сейчас?\n\nЮзер будет отключён от ноды через Ansible (1–2 мин).`
                              )
                            )
                              revokeNow.mutate(s.id);
                          }}
                          className="mt-2 text-xs px-2 py-1 rounded bg-red-700 hover:bg-red-600 disabled:opacity-50"
                        >
                          revoke now
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </aside>
    </div>
  );
}
