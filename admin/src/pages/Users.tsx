import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  DeviceOut,
  UserOut,
  SubscriptionOut,
  adminTopupByTelegram,
} from "../api";

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
  const [topupRub, setTopupRub] = useState("");
  const [topupNote, setTopupNote] = useState("");

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

  const topup = useMutation({
    mutationFn: ({
      telegramId,
      amountKopecks,
      note,
    }: {
      telegramId: string;
      amountKopecks: number;
      note: string;
    }) => adminTopupByTelegram(telegramId, amountKopecks, note),
    onSuccess: (res) => {
      // Optimistically patch the selected user's balance so the UI
      // updates instantly without waiting for the users-list refetch.
      if (selected && selected.id === res.user_id) {
        setSelected({ ...selected, balance_kopecks: res.balance_kopecks });
      }
      setTopupRub("");
      setTopupNote("");
      qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (e: Error) => alert(`Не удалось пополнить: ${e.message}`),
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

  const enableSub = useMutation({
    mutationFn: (subId: number) =>
      api.post(`/subscriptions/${subId}/enable`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
    onError: (e: Error) => alert(`Не удалось включить подписку: ${e.message}`),
  });

  const revokeDevice = useMutation({
    mutationFn: (deviceId: number) =>
      api.post(`/devices/${deviceId}/revoke`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
    onError: (e: Error) => alert(`Не удалось отвязать девайс: ${e.message}`),
  });

  const addDevice = useMutation({
    mutationFn: (subId: number) =>
      api.post(`/subscriptions/${subId}/devices`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
    },
    onError: (e: Error) => alert(`Не удалось привязать девайс: ${e.message}`),
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
                <th>Баланс</th>
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
                  <td>{(u.balance_kopecks / 100).toFixed(2)} ₽</td>
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
            <div>
              Баланс:{" "}
              <span className="font-mono">
                {(selected.balance_kopecks / 100).toFixed(2)} ₽
              </span>
            </div>

            <div className="pt-2 border-t border-slate-700">
              <div className="font-semibold mb-1">Пополнить баланс</div>
              {selected.telegram_id ? (
                <div className="space-y-2">
                  <input
                    type="number"
                    step="0.01"
                    min="0.01"
                    placeholder="Сумма, ₽"
                    value={topupRub}
                    onChange={(e) => setTopupRub(e.target.value)}
                    className="w-full px-2 py-1 rounded bg-slate-900 border border-slate-700"
                  />
                  <input
                    type="text"
                    placeholder="Комментарий (необязательно)"
                    value={topupNote}
                    onChange={(e) => setTopupNote(e.target.value)}
                    maxLength={200}
                    className="w-full px-2 py-1 rounded bg-slate-900 border border-slate-700"
                  />
                  <button
                    disabled={topup.isPending || !topupRub}
                    onClick={() => {
                      const rub = parseFloat(topupRub);
                      if (!isFinite(rub) || rub <= 0) {
                        alert("Введи положительную сумму в рублях");
                        return;
                      }
                      const kop = Math.round(rub * 100);
                      if (
                        !confirm(
                          `Начислить ${rub.toFixed(2)} ₽ юзеру ${selected.telegram_id}?\n\nЗаписывается как kind=adjust с пометкой admin_topup, в ledger появится отдельная строка.`,
                        )
                      )
                        return;
                      topup.mutate({
                        telegramId: selected.telegram_id!,
                        amountKopecks: kop,
                        note: topupNote,
                      });
                    }}
                    className="w-full text-xs px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50"
                  >
                    {topup.isPending ? "Начисляем…" : "Пополнить"}
                  </button>
                </div>
              ) : (
                <div className="text-xs text-slate-400">
                  Нет telegram_id — пополнение по ID не поддержано (у юзера
                  только email). Добавь telegram_id, чтобы пополнять.
                </div>
              )}
            </div>

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
                      {s.devices && s.devices.length > 0 && (
                        <DeviceList
                          devices={s.devices}
                          revokeDevice={revokeDevice}
                        />
                      )}
                      <div className="mt-2 flex gap-2">
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
                            className="text-xs px-2 py-1 rounded bg-red-700 hover:bg-red-600 disabled:opacity-50"
                          >
                            revoke now
                          </button>
                        )}
                        {s.status === "active" && (
                          <button
                            disabled={addDevice.isPending}
                            onClick={() => {
                              if (
                                confirm(
                                  `Добавить новый девайс к подписке #${s.id}?\n\nБудет запущен ansible-провижининг новой конфигурации.`
                                )
                              )
                                addDevice.mutate(s.id);
                            }}
                            className="text-xs px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50"
                          >
                            + add device
                          </button>
                        )}
                        {s.status !== "active" && (
                          <button
                            disabled={enableSub.isPending}
                            onClick={() => {
                              if (
                                confirm(
                                  `Включить подписку #${s.id}?\n\nБудет перепровижнен один девайс через Ansible.`
                                )
                              )
                                enableSub.mutate(s.id);
                            }}
                            className="text-xs px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50"
                          >
                            enable
                          </button>
                        )}
                      </div>
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

function DeviceList({
  devices,
  revokeDevice,
}: {
  devices: DeviceOut[];
  revokeDevice: { mutate: (id: number) => void; isPending: boolean };
}) {
  const [showDead, setShowDead] = useState(false);
  const live = devices.filter(
    (d) => d.status !== "revoked" && d.status !== "disabled",
  );
  const dead = devices.filter(
    (d) => d.status === "revoked" || d.status === "disabled",
  );

  return (
    <div className="mt-2 space-y-1">
      {live.map((d) => (
        <div
          key={d.id}
          className="flex items-center justify-between gap-2 bg-slate-800 rounded px-2 py-1 text-xs"
        >
          <span className="truncate">
            #{d.id} · {d.status}
          </span>
          <button
            disabled={revokeDevice.isPending}
            onClick={() => {
              if (
                confirm(
                  `Отвязать девайс #${d.id}?\n\nЮзер будет отключён от ноды через Ansible.`,
                )
              )
                revokeDevice.mutate(d.id);
            }}
            className="text-xs px-2 py-0.5 rounded bg-red-800 hover:bg-red-700 disabled:opacity-50"
          >
            unbind
          </button>
        </div>
      ))}
      {dead.length > 0 && (
        <>
          <button
            onClick={() => setShowDead((v) => !v)}
            className="text-xs text-slate-500 hover:text-slate-400"
          >
            {showDead ? "▼" : "▶"} {dead.length} отвязанных
          </button>
          {showDead &&
            dead.map((d) => (
              <div
                key={d.id}
                className="flex items-center gap-2 bg-slate-900/60 rounded px-2 py-1 text-xs text-slate-500"
              >
                <span className="truncate">
                  #{d.id} · {d.status}
                </span>
              </div>
            ))}
        </>
      )}
    </div>
  );
}
