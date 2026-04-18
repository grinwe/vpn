import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { api, ApiError } from "../api";
import { HealthDots, linkHealth } from "../linkHealth";

interface WGExitNodeOut {
  id: number;
  name: string;
  region: string;
  host: string;
  ssh_port: number;
  wg_port: number;
  wg_address_v4: string;
  wg_public_key: string | null;
  has_private_key: boolean;
  provider_id: number | null;
  provider_external_id: string | null;
  provider_region: string | null;
  status: string;
  is_active: boolean;
  notes: string | null;
  peers_count: number;
  links: ExitLinkHealthMini[];
  created_at: string;
  updated_at: string;
}

interface KeygenOut {
  id: number;
  wg_public_key: string;
}

interface CloudProviderOut {
  id: number;
  name: string;
  kind: string;
}

interface VPNNodeMini {
  id: number;
  name: string;
  region: string;
  host: string;
  status: string;
  is_active: boolean;
  has_relay_config: boolean;
}

interface RelayExitLinkOut {
  id: number;
  relay_node_id: number;
  relay_node_name: string;
  exit_id: number;
  exit_name: string;
  wg_interface_name: string;
  wg_client_public_key: string;
  wg_client_address_v4: string;
  created_at: string;
  last_handshake_at: string | null;
  last_rx_bytes: number | null;
  last_tx_bytes: number | null;
  last_observed_at: string | null;
}

interface ExitLinkHealthMini {
  relay_node_id: number;
  relay_node_name: string;
  wg_interface_name: string;
  last_handshake_at: string | null;
  last_observed_at: string | null;
}

function fmtBytes(n: number | null): string {
  if (n === null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

const STATUSES = ["registering", "active", "error", "disabled"] as const;

export default function Exits() {
  const qc = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const { data, isLoading, error } = useQuery<WGExitNodeOut[]>({
    queryKey: ["wg-exits"],
    queryFn: () => api.get("/exits"),
  });

  const providers = useQuery<CloudProviderOut[]>({
    queryKey: ["cloud-providers"],
    queryFn: () => api.get("/cloud/providers"),
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => api.del(`/exits/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["wg-exits"] }),
    onError: (e: Error) => alert(`Ошибка: ${e.message}`),
  });

  const keygenMut = useMutation({
    mutationFn: (id: number) => api.post<KeygenOut>(`/exits/${id}/keygen`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["wg-exits"] }),
    onError: (e: Error) => alert(`Ошибка: ${e.message}`),
  });

  const bootstrapMut = useMutation({
    mutationFn: (id: number) =>
      api.post<{ exit_id: number; task_id: number }>(
        `/exits/${id}/bootstrap`,
        {},
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["wg-exits"] }),
    onError: (e: Error) => alert(`Ошибка: ${e.message}`),
  });

  const diagnoseMut = useMutation({
    mutationFn: (id: number) =>
      api.post<{ exit_id: number; task_id: number }>(
        `/exits/${id}/diagnose`,
        {},
      ),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      alert(
        `Диагностика запущена (task #${res.task_id}).\n\n` +
          "Открой раздел Tasks чтобы увидеть вывод — " +
          "wg show, systemd, listening sockets, routing.",
      );
    },
    onError: (e: Error) => alert(`Не удалось запустить диагностику: ${e.message}`),
  });

  // Глобальный форс relay_link_health тика: один вызов, воркер обходит
  // все relay и обновляет health-колонки сразу у всех линков. Реюзает
  // tick-relay-link-health id на сервере, так что повторные клики не
  // плодят копии — см. api/exits.py:refresh_relay_link_health.
  const refreshAllHealthMut = useMutation({
    mutationFn: () =>
      api.post<{
        enqueued: boolean;
        job_id?: string;
        reason?: string;
        note?: string;
      }>(`/exits/links/health/refresh`),
    onSuccess: (res) => {
      if (!res.enqueued) {
        alert(`Не удалось запустить: ${res.reason ?? "очередь недоступна"}`);
        return;
      }
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["wg-exits"] });
        qc.invalidateQueries({ queryKey: ["wg-exit-links"] });
      }, 15_000);
    },
    onError: (e: Error) => alert(`Ошибка: ${e.message}`),
  });

  const toggleExpanded = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const providerLabel = (pid: number | null) => {
    if (pid == null) return "—";
    const p = providers.data?.find((x) => x.id === pid);
    return p ? `${p.name} (${p.kind})` : `#${pid}`;
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-bold">WG Exit Nodes</h1>
        <div className="flex gap-2">
          <button
            disabled={refreshAllHealthMut.isPending}
            onClick={() => refreshAllHealthMut.mutate()}
            title="Форс-прогнать relay_link_health тик — воркер SSH'нет на все relay сразу, обновит индикаторы через 10–20 сек"
            className="text-sm px-3 py-1 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50"
          >
            {refreshAllHealthMut.isPending ? "Обновляем…" : "↻ health всех"}
          </button>
          <button
            onClick={() => { setShowForm(true); setEditId(null); }}
            className="text-sm px-3 py-1 rounded bg-green-700 hover:bg-green-600"
          >
            + Добавить
          </button>
        </div>
      </div>

      <p className="text-xs text-slate-400 mb-4">
        Foreign exit nodes — RU relay jump nodes тоннелируют сюда через WireGuard.
        См. <code>docs/RELAY_ROADMAP.md</code>.
      </p>

      {isLoading && <div className="text-slate-400">Загрузка…</div>}
      {error && <div className="text-red-400">{(error as Error).message}</div>}

      {showForm && (
        <ExitForm
          editExit={editId != null ? data?.find((e) => e.id === editId) : undefined}
          providers={providers.data ?? []}
          onDone={() => { setShowForm(false); setEditId(null); }}
        />
      )}

      {data && (
        <table className="w-full text-sm table-fixed">
          <thead className="text-slate-400 border-b border-slate-700">
            <tr>
              <th className="w-6"></th>
              <th className="text-left py-2 px-2 w-10">ID</th>
              <th className="text-left py-2 px-2 w-32">Name</th>
              <th className="text-left py-2 px-2 w-20">Region</th>
              <th className="text-left py-2 px-2 w-40">Host</th>
              <th className="text-left py-2 px-2 w-14">WG port</th>
              <th className="text-left py-2 px-2 w-28">WG addr</th>
              <th className="text-left py-2 px-2 w-32">Public key</th>
              <th className="text-left py-2 px-2 w-28">Provider</th>
              <th className="text-left py-2 px-2 w-12">Peers</th>
              <th className="text-left py-2 px-2 w-24">Health</th>
              <th className="text-left py-2 px-2 w-20">Status</th>
              <th className="text-left py-2 px-2 w-12">Active</th>
              <th className="text-left py-2 px-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {data.map((e) => {
              const isOpen = expanded.has(e.id);
              return (
                <Fragment key={e.id}>
                  <tr
                    className="border-b border-slate-800 hover:bg-slate-800/50 cursor-pointer"
                    onClick={() => toggleExpanded(e.id)}
                  >
                    <td className="px-2 text-slate-500">{isOpen ? "▼" : "▶"}</td>
                    <td className="py-2 px-2">{e.id}</td>
                    <td className="py-2 px-2 font-mono truncate" title={e.name}>{e.name}</td>
                    <td className="py-2 px-2 truncate" title={e.region}>{e.region}</td>
                    <td className="py-2 px-2 font-mono text-slate-300 truncate" title={e.host}>{e.host}</td>
                    <td className="py-2 px-2">{e.wg_port}</td>
                    <td className="py-2 px-2 font-mono text-slate-400 truncate" title={e.wg_address_v4}>{e.wg_address_v4}</td>
                    <td className="py-2 px-2 font-mono text-xs truncate">
                      {e.wg_public_key ? (
                        <span title={e.wg_public_key}>{e.wg_public_key.slice(0, 12)}…</span>
                      ) : (
                        <span className="text-yellow-500">—</span>
                      )}
                      {!e.has_private_key && e.wg_public_key && (
                        <span className="ml-2 text-yellow-500" title="private key missing">⚠</span>
                      )}
                    </td>
                    <td className="py-2 px-2 text-slate-400 truncate" title={providerLabel(e.provider_id)}>{providerLabel(e.provider_id)}</td>
                    <td className="py-2 px-2">{e.peers_count}</td>
                    <td className="py-2 px-2" onClick={(ev) => ev.stopPropagation()}>
                      <HealthDots
                        links={e.links}
                        peerLabel={(l) => `${l.relay_node_name} · ${l.wg_interface_name}`}
                        peerKey={(l) => `${l.relay_node_id}-${l.wg_interface_name}`}
                      />
                    </td>
                    <td className="py-2 px-2">{e.status}</td>
                    <td className="py-2 px-2">{e.is_active ? "✓" : "✕"}</td>
                    <td className="py-2 px-2" onClick={(ev) => ev.stopPropagation()}>
                      <div className="flex gap-1">
                        <button
                          onClick={() => { setEditId(e.id); setShowForm(true); }}
                          className="text-xs px-2 py-1 rounded bg-blue-700 hover:bg-blue-600"
                        >
                          edit
                        </button>
                        <button
                          disabled={keygenMut.isPending}
                          onClick={() => {
                            const msg = e.has_private_key
                              ? `Сгенерировать новый ключ для ${e.name}? Текущий будет перезаписан.`
                              : `Сгенерировать ключ для ${e.name}?`;
                            if (confirm(msg)) keygenMut.mutate(e.id);
                          }}
                          className="text-xs px-2 py-1 rounded bg-purple-700 hover:bg-purple-600 disabled:opacity-50"
                        >
                          keygen
                        </button>
                        <button
                          disabled={bootstrapMut.isPending || !e.has_private_key}
                          title={
                            !e.has_private_key
                              ? "Сначала сгенерируйте ключ (keygen)"
                              : undefined
                          }
                          onClick={() => {
                            if (
                              confirm(
                                `Перекатить bootstrap_exit.yml на exit #${e.id} (${e.name})?\n\n` +
                                  "Роль идемпотентна — peer list перерендерится из текущих привязок, " +
                                  "активные тоннели не пострадают.",
                              )
                            )
                              bootstrapMut.mutate(e.id);
                          }}
                          className="text-xs px-2 py-1 rounded bg-purple-700 hover:bg-purple-600 disabled:opacity-50"
                        >
                          bootstrap
                        </button>
                        <button
                          disabled={diagnoseMut.isPending}
                          title="Read-only: wg show, systemd, routing, listen port — статус exit'а не меняется"
                          onClick={() => diagnoseMut.mutate(e.id)}
                          className="text-xs px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50"
                        >
                          diagnose
                        </button>
                        <button
                          disabled={deleteMut.isPending || e.peers_count > 0}
                          title={e.peers_count > 0 ? "Сначала отсоедините relay'и" : undefined}
                          onClick={() => {
                            if (confirm(`Удалить exit ${e.name}?`))
                              deleteMut.mutate(e.id);
                          }}
                          className="text-xs px-2 py-1 rounded bg-red-800 hover:bg-red-700 disabled:opacity-50"
                        >
                          delete
                        </button>
                      </div>
                    </td>
                  </tr>
                  {isOpen && (
                    <tr className="bg-slate-900/50">
                      <td colSpan={14} className="p-4">
                        <ExitLinksPanel exitNode={e} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ExitLinksPanel({ exitNode }: { exitNode: WGExitNodeOut }) {
  const qc = useQueryClient();
  const [showAttach, setShowAttach] = useState(false);

  const links = useQuery<RelayExitLinkOut[]>({
    queryKey: ["wg-exit-links", exitNode.id],
    queryFn: () => api.get(`/exits/${exitNode.id}/links`),
  });

  const nodes = useQuery<VPNNodeMini[]>({
    queryKey: ["nodes-mini"],
    queryFn: () => api.get("/nodes"),
    enabled: showAttach,
  });

  const detachMut = useMutation({
    mutationFn: (relayId: number) =>
      api.del<{
        exit_id: number;
        relay_node_id: number;
        deleted: boolean;
        task_id: number | null;
        credentials: {
          migrated?: number;
          cleared?: number;
          distribution?: Record<string, number>;
        };
      }>(`/exits/${exitNode.id}/links/${relayId}`),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["wg-exit-links", exitNode.id] });
      qc.invalidateQueries({ queryKey: ["wg-exits"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      const { migrated = 0, cleared = 0, distribution } = res.credentials ?? {};
      let credMsg = "";
      if (migrated > 0) {
        const parts = distribution
          ? Object.entries(distribution)
              .map(([ex, n]) => `exit #${ex}: ${n}`)
              .join(", ")
          : "";
        credMsg = `\nПеренесено creds: ${migrated}${parts ? ` (${parts})` : ""}.`;
      } else if (cleared > 0) {
        credMsg = `\nОчищен exit_id у ${cleared} creds — это был последний линк, релей становится direct-нодой.`;
      }
      if (res.task_id) {
        alert(
          `Relay отсоединён. Ansible крутится в фоне, задача #${res.task_id} — ` +
            `см. вкладку Tasks.${credMsg}`,
        );
      } else {
        alert(
          "Relay отсоединён из БД. Ansible не запущен — relay-нода уже удалена." +
            credMsg,
        );
      }
    },
    onError: (e: Error) => alert(`Ошибка: ${e.message}`),
  });

  const refreshHealthMut = useMutation({
    mutationFn: () =>
      api.post<{
        enqueued: boolean;
        job_id?: string;
        reason?: string;
      }>(`/exits/links/health/refresh`),
    onSuccess: (res) => {
      if (!res.enqueued) {
        alert(`Не удалось запустить: ${res.reason ?? "очередь недоступна"}`);
        return;
      }
      // Воркер делает SSH на каждый relay, 5–15 сек на пачку.
      // Ждём с запасом и инвалидируем запрос — табличка перерисуется
      // сама с обновлёнными last_handshake_at/observed_at.
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["wg-exit-links", exitNode.id] });
      }, 15_000);
      alert(
        `Health-тик поставлен в очередь (job ${res.job_id}). Подождём 15 сек и обновим список.`,
      );
    },
    onError: (e: Error) => alert(`Ошибка: ${e.message}`),
  });

  const reconnectMut = useMutation({
    mutationFn: (relayId: number) =>
      api.post<{
        exit_id: number;
        relay_node_id: number;
        task_id: number;
      }>(`/exits/${exitNode.id}/links/${relayId}/reconnect`),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      alert(
        `Ansible перезапущен, задача #${res.task_id} — следи в Tasks.`,
      );
    },
    onError: (e: Error) => alert(`Не удалось перезапустить: ${e.message}`),
  });

  // G.7: a relay may be attached to multiple exits simultaneously.
  // The one-relay-one-exit filter is lifted; the backend still rejects
  // duplicate (relay, exit) pairs with 409, so the UI offers all
  // active nodes here and lets the server own uniqueness. Links
  // already on *this* exit are excluded because re-attaching the
  // same pair is always a mistake.
  const alreadyAttached = new Set(
    (links.data ?? []).map((l) => l.relay_node_id),
  );
  const attachableNodes = (nodes.data ?? []).filter(
    (n) => n.is_active && !alreadyAttached.has(n.id),
  );

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-300">Attached relays</h3>
        <div className="flex gap-2">
          <button
            disabled={refreshHealthMut.isPending}
            onClick={() => refreshHealthMut.mutate()}
            title="Форс-прогнать tick health прямо сейчас (воркер SSH'нет на каждый relay, прочитает wg show all dump и обновит индикаторы). Нужно если после деплоя колонка пустая — обычно тик сам идёт каждые 5 минут."
            className="text-xs px-3 py-1 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50"
          >
            {refreshHealthMut.isPending ? "Обновляем…" : "обновить health"}
          </button>
          <button
            disabled={!exitNode.is_active || !exitNode.wg_public_key}
            title={
              !exitNode.wg_public_key
                ? "Сначала сгенерируйте ключ (keygen)"
                : !exitNode.is_active
                  ? "Exit не активен"
                  : undefined
            }
            onClick={() => setShowAttach((v) => !v)}
            className="text-xs px-3 py-1 rounded bg-green-700 hover:bg-green-600 disabled:opacity-50"
          >
            {showAttach ? "Отмена" : "+ Прикрепить relay"}
          </button>
        </div>
      </div>

      {showAttach && (
        <AttachRelayForm
          exitId={exitNode.id}
          nodes={attachableNodes}
          loadingNodes={nodes.isLoading}
          onDone={() => {
            setShowAttach(false);
            qc.invalidateQueries({ queryKey: ["wg-exit-links", exitNode.id] });
            qc.invalidateQueries({ queryKey: ["wg-exits"] });
          }}
        />
      )}

      {links.isLoading && <div className="text-slate-400 text-xs">Загрузка…</div>}
      {links.error && (
        <div className="text-red-400 text-xs">{(links.error as Error).message}</div>
      )}
      {links.data && links.data.length === 0 && (
        <div className="text-slate-500 text-xs italic">Нет прикреплённых relay-нод.</div>
      )}
      {links.data && links.data.length > 0 && (
        <table className="w-full text-xs">
          <thead className="text-slate-400">
            <tr>
              <th className="text-left py-1 px-2">Relay</th>
              <th className="text-left py-1 px-2">Iface</th>
              <th className="text-left py-1 px-2">WG client addr</th>
              <th className="text-left py-1 px-2">Status</th>
              <th className="text-left py-1 px-2">RX / TX</th>
              <th className="text-left py-1 px-2">Создан</th>
              <th className="py-1 px-2"></th>
            </tr>
          </thead>
          <tbody>
            {links.data.map((l) => {
              const h = linkHealth(l);
              return (
              <tr key={l.id} className="border-t border-slate-800">
                <td className="py-1 px-2 font-mono">
                  #{l.relay_node_id} {l.relay_node_name}
                </td>
                <td className="py-1 px-2 font-mono text-slate-300">
                  {l.wg_interface_name}
                </td>
                <td className="py-1 px-2 font-mono text-slate-400">
                  {l.wg_client_address_v4}
                </td>
                <td className="py-1 px-2" title={h.title}>
                  <span
                    className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium text-white ${h.color}`}
                  >
                    <span className="w-1.5 h-1.5 rounded-full bg-white/70" />
                    {h.label}
                  </span>
                </td>
                <td className="py-1 px-2 font-mono text-slate-400">
                  {fmtBytes(l.last_rx_bytes)} / {fmtBytes(l.last_tx_bytes)}
                </td>
                <td className="py-1 px-2 text-slate-400">
                  {new Date(l.created_at).toLocaleString()}
                </td>
                <td className="py-1 px-2">
                  <div className="flex gap-1 justify-end">
                    <button
                      disabled={reconnectMut.isPending || detachMut.isPending}
                      onClick={() => reconnectMut.mutate(l.relay_node_id)}
                      title="Перезапустить ansible без detach/attach — полезно, если таска упала на test-connectivity"
                      className="text-xs px-2 py-0.5 rounded bg-blue-800 hover:bg-blue-700 disabled:opacity-50"
                    >
                      reconnect
                    </button>
                    <button
                      disabled={detachMut.isPending || reconnectMut.isPending}
                      onClick={() => {
                        if (
                          confirm(
                            `Отсоединить ${l.relay_node_name} (${l.wg_interface_name}) от ${exitNode.name}?\n\n` +
                              "Запустится ansible: bootstrap_exit.yml на exit'е " +
                              `(peer уйдёт из его wg0.conf) и relay_tunnel_apply.yml ` +
                              `на relay (${l.wg_interface_name} down, Xray direct-${l.wg_interface_name} ` +
                              "outbound/rule удалятся, остальные линки на этом relay остаются).",
                          )
                        )
                          detachMut.mutate(l.relay_node_id);
                      }}
                      className="text-xs px-2 py-0.5 rounded bg-red-800 hover:bg-red-700 disabled:opacity-50"
                    >
                      detach
                    </button>
                  </div>
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function AttachRelayForm({
  exitId,
  nodes,
  loadingNodes,
  onDone,
}: {
  exitId: number;
  nodes: VPNNodeMini[];
  loadingNodes: boolean;
  onDone: () => void;
}) {
  // Мультивыбор: чекбоксы по каждой node, кнопка прикрепляет всё
  // выбранное одним батчем. Ручной ввод WG client addr убран —
  // смысла при массовом прикреплении нет, backend сам выделяет
  // свободные /32.
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [err, setErr] = useState<string | null>(null);
  // Гоним POST'ы последовательно: backend allocate_client_address
  // не защищён от гонки, параллельные запросы могут выдать один
  // и тот же /32 и упасть на уникальном индексе.
  const [progress, setProgress] = useState<{
    done: number;
    total: number;
    current: string | null;
    failures: { name: string; error: string }[];
  } | null>(null);

  function toggle(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function selectAll() {
    setSelected(new Set(nodes.map((n) => n.id)));
  }

  function clearAll() {
    setSelected(new Set());
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (selected.size === 0) {
      setErr("Выберите хотя бы одну relay ноду");
      return;
    }
    const chosen = nodes.filter((n) => selected.has(n.id));
    const failures: { name: string; error: string }[] = [];
    setProgress({ done: 0, total: chosen.length, current: null, failures });
    for (let i = 0; i < chosen.length; i++) {
      const node = chosen[i];
      setProgress({
        done: i,
        total: chosen.length,
        current: node.name,
        failures,
      });
      try {
        await api.post<RelayExitLinkOut>(`/exits/${exitId}/links`, {
          relay_node_id: node.id,
        });
      } catch (apiErr) {
        const msg =
          apiErr instanceof ApiError
            ? `${apiErr.status}: ${apiErr.message}`
            : apiErr instanceof Error
              ? apiErr.message
              : String(apiErr);
        failures.push({ name: node.name, error: msg });
      }
    }
    setProgress({
      done: chosen.length,
      total: chosen.length,
      current: null,
      failures,
    });
    if (failures.length === 0) {
      onDone();
    } else {
      setErr(
        `Прикреплено ${chosen.length - failures.length}/${chosen.length}. ` +
          `Ошибки: ${failures.map((f) => `${f.name} — ${f.error}`).join("; ")}`,
      );
    }
  }

  const running = progress !== null && progress.done < progress.total;

  return (
    <form
      onSubmit={submit}
      className="p-3 rounded border border-slate-700 bg-slate-900 space-y-3"
    >
      <div className="flex items-center justify-between">
        <span className="text-slate-400 text-xs">
          Выбрано {selected.size} из {nodes.length} доступных
        </span>
        <div className="flex gap-2 text-xs">
          <button
            type="button"
            onClick={selectAll}
            disabled={running || nodes.length === 0}
            className="px-2 py-0.5 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50"
          >
            Все
          </button>
          <button
            type="button"
            onClick={clearAll}
            disabled={running || selected.size === 0}
            className="px-2 py-0.5 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50"
          >
            Сброс
          </button>
        </div>
      </div>

      {loadingNodes && <div className="text-slate-400 text-xs">Загрузка…</div>}
      {!loadingNodes && nodes.length === 0 && (
        <div className="text-slate-500 text-xs italic">
          Нет доступных relay-нод для прикрепления.
        </div>
      )}
      {nodes.length > 0 && (
        <div className="max-h-64 overflow-y-auto border border-slate-700 rounded">
          {nodes.map((n) => (
            <label
              key={n.id}
              className="flex items-center gap-2 px-2 py-1 text-sm hover:bg-slate-800 cursor-pointer"
            >
              <input
                type="checkbox"
                checked={selected.has(n.id)}
                onChange={() => toggle(n.id)}
                disabled={running}
              />
              <span className="font-mono text-slate-200">
                #{n.id} {n.name}
              </span>
              <span className="text-slate-400 text-xs">
                ({n.region}, {n.status})
              </span>
            </label>
          ))}
        </div>
      )}

      {progress && (
        <div className="text-xs text-slate-300">
          Прогресс: {progress.done}/{progress.total}
          {progress.current && ` — сейчас: ${progress.current}`}
        </div>
      )}
      {err && <div className="text-red-400 text-xs">{err}</div>}

      <div className="flex gap-2 justify-end text-xs">
        <button
          type="button"
          onClick={onDone}
          disabled={running}
          className="px-3 py-1 rounded bg-slate-700 disabled:opacity-50"
        >
          {progress && progress.failures.length > 0 ? "Закрыть" : "Отмена"}
        </button>
        <button
          type="submit"
          disabled={running || selected.size === 0}
          className="px-3 py-1 rounded bg-green-700 hover:bg-green-600 disabled:opacity-50"
        >
          {running
            ? `Прикрепляю… ${progress?.done}/${progress?.total}`
            : `Прикрепить (${selected.size})`}
        </button>
      </div>
    </form>
  );
}

function ExitForm({
  editExit,
  providers,
  onDone,
}: {
  editExit?: WGExitNodeOut;
  providers: CloudProviderOut[];
  onDone: () => void;
}) {
  const qc = useQueryClient();
  const isEdit = !!editExit;

  const [name, setName] = useState(editExit?.name ?? "");
  const [region, setRegion] = useState(editExit?.region ?? "");
  const [host, setHost] = useState(editExit?.host ?? "");
  const [sshPort, setSshPort] = useState(editExit?.ssh_port ?? 22);
  const [wgPort, setWgPort] = useState(editExit?.wg_port ?? 51820);
  const [wgAddress, setWgAddress] = useState(editExit?.wg_address_v4 ?? "10.77.0.1/24");
  const [wgPublic, setWgPublic] = useState(editExit?.wg_public_key ?? "");
  const [wgPrivate, setWgPrivate] = useState("");
  const [providerId, setProviderId] = useState<string>(
    editExit?.provider_id != null ? String(editExit.provider_id) : ""
  );
  const [providerExternalId, setProviderExternalId] = useState(editExit?.provider_external_id ?? "");
  const [providerRegion, setProviderRegion] = useState(editExit?.provider_region ?? "");
  const [status, setStatus] = useState(editExit?.status ?? "registering");
  const [isActive, setIsActive] = useState(editExit?.is_active ?? true);
  const [notes, setNotes] = useState(editExit?.notes ?? "");
  const [err, setErr] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      isEdit
        ? api.patch(`/exits/${editExit!.id}`, body)
        : api.post("/exits", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["wg-exits"] });
      onDone();
    },
    onError: (e: Error) => setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : e.message),
  });

  const keygenPreviewMut = useMutation({
    mutationFn: () =>
      api.post<{ wg_public_key: string; wg_private_key: string }>(
        "/exits/_keygen",
        {},
      ),
    onSuccess: (res) => {
      setWgPublic(res.wg_public_key);
      setWgPrivate(res.wg_private_key);
      setErr(null);
    },
    onError: (e: Error) => setErr(`Ошибка генерации ключа: ${e.message}`),
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    const body: Record<string, unknown> = {
      name,
      region,
      host,
      ssh_port: Number(sshPort),
      wg_port: Number(wgPort),
      wg_address_v4: wgAddress,
      provider_id: providerId ? Number(providerId) : null,
      provider_external_id: providerExternalId || null,
      provider_region: providerRegion || null,
      is_active: isActive,
      notes: notes || null,
    };
    if (wgPublic) body.wg_public_key = wgPublic;
    if (wgPrivate) body.wg_private_key = wgPrivate;
    if (isEdit) body.status = status;
    mutation.mutate(body);
  }

  return (
    <form
      onSubmit={submit}
      className="mb-4 p-4 rounded border border-slate-700 bg-slate-900 grid grid-cols-2 gap-3 text-sm"
    >
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Name</span>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Region</span>
        <input
          value={region}
          onChange={(e) => setRegion(e.target.value)}
          required
          placeholder="de, nl, fi, ..."
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col col-span-2">
        <span className="text-slate-400 mb-1">Host (IP or FQDN)</span>
        <input
          value={host}
          onChange={(e) => setHost(e.target.value)}
          required
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">SSH port</span>
        <input
          type="number"
          value={sshPort}
          onChange={(e) => setSshPort(Number(e.target.value))}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">WG port</span>
        <input
          type="number"
          value={wgPort}
          onChange={(e) => setWgPort(Number(e.target.value))}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col col-span-2">
        <span className="text-slate-400 mb-1">WG address (CIDR)</span>
        <input
          value={wgAddress}
          onChange={(e) => setWgAddress(e.target.value)}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
        />
      </label>
      <label className="flex flex-col col-span-2">
        <div className="flex items-center justify-between mb-1">
          <span className="text-slate-400">WG public key (опционально)</span>
          <button
            type="button"
            disabled={keygenPreviewMut.isPending}
            onClick={() => keygenPreviewMut.mutate()}
            className="text-xs px-2 py-0.5 rounded bg-purple-700 hover:bg-purple-600 disabled:opacity-50"
          >
            {keygenPreviewMut.isPending ? "…" : "Сгенерировать пару"}
          </button>
        </div>
        <input
          value={wgPublic}
          onChange={(e) => setWgPublic(e.target.value)}
          autoComplete="new-password"
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
        />
      </label>
      <label className="flex flex-col col-span-2">
        <span className="text-slate-400 mb-1">
          WG private key {isEdit && "(оставь пустым чтобы не менять)"}
        </span>
        <input
          type="password"
          value={wgPrivate}
          onChange={(e) => setWgPrivate(e.target.value)}
          placeholder={isEdit && editExit?.has_private_key ? "••••••••" : ""}
          autoComplete="new-password"
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Cloud provider</span>
        <select
          value={providerId}
          onChange={(e) => setProviderId(e.target.value)}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        >
          <option value="">—</option>
          {providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} ({p.kind})
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Provider region</span>
        <input
          value={providerRegion}
          onChange={(e) => setProviderRegion(e.target.value)}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>
      <label className="flex flex-col col-span-2">
        <span className="text-slate-400 mb-1">Provider external id</span>
        <input
          value={providerExternalId}
          onChange={(e) => setProviderExternalId(e.target.value)}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
        />
      </label>
      {isEdit && (
        <label className="flex flex-col">
          <span className="text-slate-400 mb-1">Status</span>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
          >
            {STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
      )}
      <label className="flex items-center gap-2 self-end">
        <input
          type="checkbox"
          checked={isActive}
          onChange={(e) => setIsActive(e.target.checked)}
        />
        <span className="text-slate-400">Active</span>
      </label>
      <label className="flex flex-col col-span-2">
        <span className="text-slate-400 mb-1">Notes</span>
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={2}
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
        />
      </label>

      {err && <div className="col-span-2 text-red-400 text-xs">{err}</div>}
      <div className="col-span-2 flex gap-2 justify-end">
        <button type="button" onClick={onDone} className="text-xs px-3 py-1 rounded bg-slate-700">
          Отмена
        </button>
        <button
          type="submit"
          disabled={mutation.isPending}
          className="text-xs px-3 py-1 rounded bg-green-700 hover:bg-green-600 disabled:opacity-50"
        >
          {isEdit ? "Сохранить" : "Создать"}
        </button>
      </div>
    </form>
  );
}
