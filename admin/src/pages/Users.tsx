import { useEffect, useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  api,
  batchBanUsers,
  DeviceOut,
  SubscriptionMigrateOut,
  SubscriptionOut,
  UserOut,
  VPNNodeOut,
  adminTopupByTelegram,
} from "../api";

// Backend caps `limit` at 200; 50 keeps each page snappy and makes "Load
// more" feel incremental rather than dumping a wall of rows at once.
const PAGE_SIZE = 50;

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
  // Bulk selection lives next to single-row selection. Single-row
  // selection (`selected`) drives the detail sidebar; `selectedIds` is
  // the set used by bulk ban/unban. They are intentionally independent —
  // you can check a row for a bulk op without opening its details, and
  // vice versa.
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());

  const {
    data: usersData,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isLoading,
  } = useInfiniteQuery({
    queryKey: ["users", { search: debouncedSearch }],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      api.get<UserOut[]>(
        `/users?limit=${PAGE_SIZE}&offset=${pageParam}${
          debouncedSearch
            ? `&search=${encodeURIComponent(debouncedSearch)}`
            : ""
        }`,
      ),
    // Short page = we drained the server. Otherwise bump offset by the
    // total count so far — the list is ordered by id DESC on the backend,
    // which is stable enough for paginated admin browsing (new signups
    // appear at the top, not mid-page).
    getNextPageParam: (lastPage, allPages) => {
      if (lastPage.length < PAGE_SIZE) return undefined;
      return allPages.reduce((n, p) => n + p.length, 0);
    },
  });
  const users = usersData?.pages.flat() ?? [];

  const { data: subs } = useQuery<SubscriptionOut[]>({
    queryKey: ["user-subs", selected?.id],
    queryFn: () => api.get(`/users/${selected!.id}`),
    enabled: selected !== null,
  });

  // Only loaded when a user is selected — the list is used to populate
  // per-sub migrate dropdowns. Filtered client-side to is_active=true
  // (the endpoint validates is_active anyway, but hiding disabled nodes
  // up-front avoids the "why does my pick 400?" surprise).
  const { data: allNodes } = useQuery<VPNNodeOut[]>({
    queryKey: ["nodes-for-migrate"],
    queryFn: () => api.get(`/nodes`),
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

  const unblockSharing = useMutation({
    mutationFn: (subId: number) =>
      api.post(`/subscriptions/${subId}/unblock-sharing`, {}),
    onSuccess: () => {
      alert("Unblock отправлен на ноду. Enforcer подхватит в течение 10 секунд.");
      qc.invalidateQueries({ queryKey: ["user-subs"] });
    },
    onError: (e: Error) => alert(`Не удалось разблокировать: ${e.message}`),
  });

  const migrateSub = useMutation({
    mutationFn: ({ subId, targetNodeId }: { subId: number; targetNodeId: number }) =>
      api.post<SubscriptionMigrateOut>(`/subscriptions/${subId}/migrate`, {
        target_node_id: targetNodeId,
      }),
    onSuccess: (res) => {
      alert(
        `Подписка #${res.subscription_id} переведена: ${res.old_node_name} → ${res.new_node_name}. Провижнинг-таска #${res.provisioning_task_id ?? "—"} запущена, следи в Tasks.`,
      );
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
    },
    onError: (e: Error) => alert(`Не удалось перевести: ${e.message}`),
  });

  const batchBan = useMutation({
    mutationFn: ({
      ids,
      action,
    }: {
      ids: number[];
      action: "ban" | "unban";
    }) => batchBanUsers(ids, action),
    onSuccess: (res) => {
      alert(
        `${res.action === "ban" ? "Забанено" : "Разбанено"}: ${res.done.length}` +
          (res.skipped.length
            ? `, пропущено: ${res.skipped.length} (уже в нужном состоянии)`
            : "") +
          (res.not_found.length
            ? `, не найдено: ${res.not_found.length}`
            : ""),
      );
      setSelectedIds(new Set());
      qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (e: Error) => alert(`Batch ban/unban ошибка: ${e.message}`),
  });

  const toggleRowSelected = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selArr = Array.from(selectedIds);
  const hasSelection = selArr.length > 0;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-2">
        <div className="flex items-center gap-3 mb-4 flex-wrap">
          <h1 className="text-2xl font-semibold">Users</h1>
          {hasSelection && (
            <div className="flex items-center gap-2 ml-auto">
              <span className="text-xs text-slate-400">
                {selArr.length} выбрано
              </span>
              <button
                disabled={batchBan.isPending}
                onClick={() => {
                  if (
                    confirm(
                      `Забанить ${selArr.length} юзер(ов)?\n\nБот будет молча дропать все апдейты от этих Telegram-аккаунтов. Подписки НЕ затрагиваются.`,
                    )
                  )
                    batchBan.mutate({ ids: selArr, action: "ban" });
                }}
                className="text-xs px-2 py-1 rounded bg-red-700 hover:bg-red-600 disabled:opacity-50"
              >
                ban all
              </button>
              <button
                disabled={batchBan.isPending}
                onClick={() => {
                  if (confirm(`Разбанить ${selArr.length} юзер(ов)?`))
                    batchBan.mutate({ ids: selArr, action: "unban" });
                }}
                className="text-xs px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50"
              >
                unban all
              </button>
            </div>
          )}
        </div>
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
          <>
            <table className="w-full text-sm">
              <thead className="text-left text-slate-400 border-b border-slate-700">
                <tr>
                  <th className="py-2 w-8">
                    <input
                      type="checkbox"
                      checked={
                        users.length > 0 && selectedIds.size === users.length
                      }
                      onChange={() => {
                        if (selectedIds.size === users.length) {
                          setSelectedIds(new Set());
                        } else {
                          setSelectedIds(new Set(users.map((u) => u.id)));
                        }
                      }}
                      className="accent-blue-600"
                    />
                  </th>
                  <th className="py-2">ID</th>
                  <th>Telegram</th>
                  <th>Email</th>
                  <th>Subs</th>
                  <th>Баланс</th>
                  <th>Создан</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr
                    key={u.id}
                    onClick={() => setSelected(u)}
                    className={`cursor-pointer border-b border-slate-800 hover:bg-slate-800 ${
                      selected?.id === u.id ? "bg-slate-800" : ""
                    } ${selectedIds.has(u.id) ? "bg-blue-950/30" : ""} ${
                      u.banned_at ? "text-red-300" : ""
                    }`}
                  >
                    <td
                      className="py-2"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <input
                        type="checkbox"
                        checked={selectedIds.has(u.id)}
                        onChange={() => toggleRowSelected(u.id)}
                        className="accent-blue-600"
                      />
                    </td>
                    <td className="py-2">{u.id}</td>
                    <td>
                      {u.telegram_id ?? "—"}
                      {u.banned_at && (
                        <span className="ml-2 inline-block text-[10px] px-1.5 py-0.5 rounded bg-red-900/60 text-red-200 border border-red-700/50 align-middle">
                          banned
                        </span>
                      )}
                    </td>
                    <td>{u.email ?? "—"}</td>
                    <td>{u.subscription_count}</td>
                    <td>{(u.balance_kopecks / 100).toFixed(2)} ₽</td>
                    <td>
                      {new Date(u.created_at).toLocaleString("ru-RU", {
                        dateStyle: "short",
                        timeStyle: "short",
                      })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mt-4 flex items-center gap-3 text-sm text-slate-400">
              <span>Загружено: {users.length}</span>
              {hasNextPage && (
                <button
                  onClick={() => fetchNextPage()}
                  disabled={isFetchingNextPage}
                  className="px-3 py-1 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50"
                >
                  {isFetchingNextPage ? "Загружаем…" : "Загрузить ещё"}
                </button>
              )}
            </div>
          </>
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
                        {s.sharing_blocked && (
                          <button
                            disabled={unblockSharing.isPending}
                            onClick={() => {
                              if (
                                confirm(
                                  `Снять sharing-бан для подписки #${s.id}?\n\nEnforcer заблокировал юзера за раздачу конфига. Команда unblock будет отправлена на ноду.`
                                )
                              )
                                unblockSharing.mutate(s.id);
                            }}
                            className="text-xs px-2 py-1 rounded bg-amber-700 hover:bg-amber-600 disabled:opacity-50"
                          >
                            снять sharing-бан
                          </button>
                        )}
                      </div>
                      {s.status === "active" && (
                        <MigrateSubControl
                          sub={s}
                          nodes={allNodes}
                          mutation={migrateSub}
                        />
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

// Per-sub "move to specific node" control — admin override.
// Шлёт POST /subscriptions/{id}/migrate с target_node_id. На бэке
// выбор ноды не проходит пул/health/cooldown-фильтры (см. docstring
// migrate_subscription_to_new_node) — админ осознанно берёт
// ответственность. Мы всё равно прячем ноды с is_active=false из
// дропдауна, чтобы не собирать 400 на пустом месте.
function MigrateSubControl({
  sub,
  nodes,
  mutation,
}: {
  sub: SubscriptionOut;
  nodes: VPNNodeOut[] | undefined;
  mutation: {
    mutate: (args: { subId: number; targetNodeId: number }) => void;
    isPending: boolean;
  };
}) {
  const [targetId, setTargetId] = useState<string>("");
  const candidates = (nodes ?? []).filter(
    (n) => n.is_active && n.id !== sub.node_id,
  );
  if (candidates.length === 0) {
    return (
      <div className="mt-2 text-xs text-slate-500">
        Нет других активных нод для переселения.
      </div>
    );
  }
  const target = candidates.find((n) => String(n.id) === targetId);
  return (
    <div className="mt-2 flex gap-1 items-center">
      <select
        value={targetId}
        onChange={(e) => setTargetId(e.target.value)}
        className="text-xs px-1 py-0.5 rounded bg-slate-800 border border-slate-700 flex-1"
      >
        <option value="">— выбери ноду —</option>
        {candidates.map((n) => (
          <option key={n.id} value={String(n.id)}>
            #{n.id} {n.name} ({n.region})
            {n.cooldown_until && new Date(n.cooldown_until).getTime() > Date.now()
              ? " ⚠ cooldown"
              : ""}
          </option>
        ))}
      </select>
      <button
        disabled={mutation.isPending || !target}
        onClick={() => {
          if (!target) return;
          if (
            confirm(
              `Перевести подписку #${sub.id} с ноды «${sub.node}» на «${target.name}» (#${target.id}, ${target.region})?\n\n` +
                `Пул/health/cooldown НЕ проверяются — это ручной admin-override. Старые девайсы будут revoke'нуты в фоне, новый девайс поднимется через ansible (1–2 мин). sub_token сохраняется.`,
            )
          )
            mutation.mutate({ subId: sub.id, targetNodeId: target.id });
        }}
        className="text-xs px-2 py-1 rounded bg-blue-700 hover:bg-blue-600 disabled:opacity-50"
      >
        переселить
      </button>
    </div>
  );
}
