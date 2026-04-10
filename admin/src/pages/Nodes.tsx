import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  ProvisioningTaskOut,
  VPNConfigCreateIn,
  VPNConfigOut,
  VPNConfigProtocol,
  VPNNodeCreateIn,
  VPNNodeOut,
} from "../api";

// ── Tracked operation types ─────────────────────────────────────────
// Persisted to localStorage so banners survive page navigation.
// `kind` distinguishes the three operation types in the UI; `taskIds`
// is the full set to poll; the sub-arrays break down by phase.

type TrackedOp = {
  kind: "migration" | "bootstrap" | "resync";
  nodeId: number;
  nodeName: string;
  taskIds: number[];
  revokeTaskIds: number[];
  deviceTaskIds: number[];
  resyncTaskIds: number[];
  startedAt: number;
};

const LS_KEY = "vpn-admin-tracked-ops";

function loadTrackedOps(): TrackedOp[] {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as TrackedOp[];
    // Drop stale ops older than 4 hours to keep the list from growing
    const cutoff = Date.now() - 4 * 60 * 60 * 1000;
    return parsed.filter((op) => op.startedAt > cutoff);
  } catch {
    return [];
  }
}

function saveTrackedOps(ops: TrackedOp[]) {
  localStorage.setItem(LS_KEY, JSON.stringify(ops));
}

function addTrackedOp(op: TrackedOp) {
  const ops = loadTrackedOps().filter(
    (o) => !(o.kind === op.kind && o.nodeId === op.nodeId),
  );
  ops.unshift(op);
  saveTrackedOps(ops);
}

function removeTrackedOp(kind: string, nodeId: number, startedAt: number) {
  const ops = loadTrackedOps().filter(
    (o) => !(o.kind === kind && o.nodeId === nodeId && o.startedAt === startedAt),
  );
  saveTrackedOps(ops);
}

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
  const [createOpen, setCreateOpen] = useState(false);
  const [expandedNodeId, setExpandedNodeId] = useState<number | null>(null);
  const [trackedOps, setTrackedOps] = useState<TrackedOp[]>(loadTrackedOps);
  const qc = useQueryClient();

  // Sync tracked ops from localStorage whenever the component mounts
  // or regains focus (navigate away → back). The storage event fires
  // only from *other* tabs, so we also listen for visibilitychange.
  useEffect(() => {
    const sync = () => setTrackedOps(loadTrackedOps());
    window.addEventListener("storage", sync);
    document.addEventListener("visibilitychange", sync);
    return () => {
      window.removeEventListener("storage", sync);
      document.removeEventListener("visibilitychange", sync);
    };
  }, []);

  function addOp(op: TrackedOp) {
    addTrackedOp(op);
    setTrackedOps(loadTrackedOps());
  }
  function removeOp(op: TrackedOp) {
    removeTrackedOp(op.kind, op.nodeId, op.startedAt);
    setTrackedOps(loadTrackedOps());
  }

  const setActive = useMutation({
    mutationFn: ({ id, is_active }: { id: number; is_active: boolean }) =>
      api.post<VPNNodeOut>(`/nodes/${id}/active`, { is_active }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["nodes"] }),
    onError: (e: Error) => alert(`Не удалось изменить флаг: ${e.message}`),
  });

  const resync = useMutation({
    mutationFn: (node: { id: number; name: string }) =>
      api
        .post<{ node_id: number; task_id: number | null; clients: number }>(
          `/nodes/${node.id}/resync`,
          {},
        )
        .then((res) => ({ ...res, nodeName: node.name })),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      if (!res.task_id) {
        alert("Нечего ресинкать — активных vless подписок на ноде нет.");
        return;
      }
      addOp({
        kind: "resync",
        nodeId: res.node_id,
        nodeName: res.nodeName,
        taskIds: [res.task_id],
        revokeTaskIds: [],
        deviceTaskIds: [],
        resyncTaskIds: [res.task_id],
        startedAt: Date.now(),
      });
    },
    onError: (e: Error) => alert(`Не удалось запустить resync: ${e.message}`),
  });

  const migrate = useMutation({
    mutationFn: (node: { id: number; name: string }) =>
      api
        .post<{
          node_id: number;
          migrated_subscriptions: number[];
          task_ids: number[];
          revoke_task_ids: number[];
          device_task_ids: number[];
          resync_task_ids: number[];
        }>(`/nodes/${node.id}/migrate`, {})
        .then((res) => ({ ...res, nodeName: node.name })),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["nodes"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      if (res.task_ids.length === 0) {
        alert(
          res.migrated_subscriptions.length === 0
            ? "Активных подписок на этой ноде не было — мигрировать нечего."
            : `Мигрировано подписок: ${res.migrated_subscriptions.length}, но фоновых тасок не создано. Смотри логи.`,
        );
        return;
      }
      addOp({
        kind: "migration",
        nodeId: res.node_id,
        nodeName: res.nodeName,
        taskIds: res.task_ids,
        revokeTaskIds: res.revoke_task_ids,
        deviceTaskIds: res.device_task_ids,
        resyncTaskIds: res.resync_task_ids,
        startedAt: Date.now(),
      });
    },
    onError: (e: Error) => alert(`Не удалось мигрировать: ${e.message}`),
  });

  const bootstrap = useMutation({
    mutationFn: (node: { id: number; name: string }) =>
      api
        .post<{ node_id: number; task_id: number }>(
          `/nodes/${node.id}/bootstrap`,
          {},
        )
        .then((res) => ({ ...res, nodeName: node.name })),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["nodes"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      addOp({
        kind: "bootstrap",
        nodeId: res.node_id,
        nodeName: res.nodeName,
        taskIds: [res.task_id],
        revokeTaskIds: [],
        deviceTaskIds: [],
        resyncTaskIds: [],
        startedAt: Date.now(),
      });
    },
    onError: (e: Error) => alert(`Не удалось запустить bootstrap: ${e.message}`),
  });

  const { data, isLoading, error, refetch, isFetching } = useQuery<VPNNodeOut[]>({
    queryKey: ["nodes"],
    queryFn: () => api.get("/nodes"),
    // Во время бутстрапа хочется, чтобы статус обновлялся быстро —
    // раз в 5 секунд, а не раз в 20. После перехода в active это
    // всё равно стабильный poll, нагрузка символическая.
    // При ошибке глушим авто-refetch, чтобы не долбить 500 раз в 5 сек —
    // пусть юзер явно нажмёт «Повторить».
    refetchInterval: (q) => (q.state.error ? false : 5_000),
    retry: false,
  });

  if (isLoading) return <div>Загрузка…</div>;
  if (error) {
    const msg = error instanceof ApiError ? `${error.status}: ${error.message}` : String(error);
    return (
      <div>
        <div className="flex items-center justify-between mb-4">
          <h1 className="text-2xl font-semibold">Nodes</h1>
        </div>
        <div className="p-4 rounded border border-red-900/50 bg-red-950/30">
          <div className="text-red-400 font-semibold mb-1">Не удалось загрузить список нод</div>
          <div className="text-xs text-slate-400 font-mono mb-3">{msg}</div>
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="px-3 py-1.5 rounded bg-slate-700 hover:bg-slate-600 text-sm disabled:opacity-50"
          >
            {isFetching ? "Повторяем…" : "↻ Повторить"}
          </button>
          <p className="text-xs text-slate-500 mt-3">
            500 — это бэкенд упал на запросе. Смотри{" "}
            <span className="font-mono">docker compose logs backend --tail 100</span> на
            проде — скорее всего миграция на <span className="font-mono">pool_id</span> не
            применена или схема отстала.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-semibold">Nodes</h1>
        <button
          className="px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-sm font-semibold"
          onClick={() => setCreateOpen((v) => !v)}
        >
          {createOpen ? "Отмена" : "+ Добавить ноду"}
        </button>
      </div>

      {createOpen && (
        <CreateNodeForm
          onDone={() => setCreateOpen(false)}
        />
      )}

      {trackedOps.map((op) => (
        <OperationProgressBanner
          key={`${op.kind}-${op.nodeId}-${op.startedAt}`}
          op={op}
          onDismiss={() => removeOp(op)}
        />
      ))}

      <table className="w-full text-sm">
        <thead className="text-left text-slate-400 border-b border-slate-700">
          <tr>
            <th className="py-2 w-8"></th>
            <th>ID</th>
            <th>Имя</th>
            <th>Регион</th>
            <th>Host</th>
            <th>Pool</th>
            <th>Статус</th>
            <th>Активна</th>
            <th>Обновлена</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {data?.map((n) => {
            const expanded = expandedNodeId === n.id;
            return (
              <>
                <tr
                  key={n.id}
                  className="border-b border-slate-800 cursor-pointer hover:bg-slate-800/40"
                  onClick={() => setExpandedNodeId(expanded ? null : n.id)}
                >
                  <td className="py-2 text-slate-500">{expanded ? "▼" : "▶"}</td>
                  <td>{n.id}</td>
                  <td className="font-mono">{n.name}</td>
                  <td>{n.region}</td>
                  <td className="font-mono text-slate-400">{n.host}</td>
                  <td>{n.pool_id ?? "—"}</td>
                  <td className={statusColor(n.status)}>{n.status}</td>
                  <td>{n.is_active ? "✓" : "✕"}</td>
                  <td>{new Date(n.updated_at).toLocaleString()}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <div className="flex gap-1">
                      <button
                        disabled={setActive.isPending}
                        onClick={() => {
                          const next = !n.is_active;
                          const msg = next
                            ? `Включить ноду #${n.id} (${n.name}) в пул? Новые подписки снова смогут на ней создаваться.`
                            : `Исключить ноду #${n.id} (${n.name}) из пула? Существующие подписки продолжат работать, но новые на неё не попадут.`;
                          if (confirm(msg))
                            setActive.mutate({ id: n.id, is_active: next });
                        }}
                        className={`text-xs px-2 py-1 rounded disabled:opacity-50 ${
                          n.is_active
                            ? "bg-amber-700 hover:bg-amber-600"
                            : "bg-emerald-700 hover:bg-emerald-600"
                        }`}
                      >
                        {n.is_active ? "исключить" : "вернуть в пул"}
                      </button>
                      <button
                        disabled={migrate.isPending}
                        onClick={() => {
                          if (
                            confirm(
                              `Переселить все активные подписки с ноды #${n.id} (${n.name}) на другие ноды пула?\n\n` +
                                `Для каждой подписки будет выбрана новая нода (через _pick_node), девайсы переправлены через ansible. Процесс идёт в фоне, смотри Tasks.\n\n` +
                                `Рекомендация: сначала исключи ноду из пула, чтобы новые подписки снова не прилетели сюда.`,
                            )
                          )
                            migrate.mutate({ id: n.id, name: n.name });
                        }}
                        className="text-xs px-2 py-1 rounded bg-blue-700 hover:bg-blue-600 disabled:opacity-50"
                      >
                        переселить
                      </button>
                      <button
                        disabled={bootstrap.isPending}
                        onClick={() => {
                          if (
                            confirm(
                              `Перекатить site.yml на ноду #${n.id} (${n.name}) с нуля?\n\n` +
                                `Запустится вся цепочка ролей (bootstrap_node, install_shadowtls_stack, install_vless_reality, check_node_health). install_vless_reality preserve'ит существующих VLESS-клиентов через slurp старого config.json, плюс после успеха бэк авто-триггернёт resync всех активных UUID'ов. Безопасно для ноды с живым трафиком.`,
                            )
                          )
                            bootstrap.mutate({ id: n.id, name: n.name });
                        }}
                        className="text-xs px-2 py-1 rounded bg-purple-700 hover:bg-purple-600 disabled:opacity-50"
                      >
                        bootstrap
                      </button>
                      <button
                        disabled={resync.isPending}
                        onClick={() => {
                          if (
                            confirm(
                              `Форсированно перекатить все активные VLESS+Reality подписки на ноду #${n.id} (${n.name})?\n\n` +
                                `Бэк соберёт список активных UUID для этой ноды и дёрнет manage_vless_user.sh add для каждого. Операция идемпотентна — безопасно запускать в любом состоянии. Используй, если подозреваешь, что клиенты на ноде разошлись с БД (после ручного редактирования config.json, восстановления из бэкапа, etc).`,
                            )
                          )
                            resync.mutate({ id: n.id, name: n.name });
                        }}
                        className="text-xs px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50"
                      >
                        resync
                      </button>
                    </div>
                  </td>
                </tr>
                {expanded && (
                  <tr className="border-b border-slate-800 bg-slate-900/60">
                    <td colSpan={10} className="p-4">
                      <NodeConfigs nodeId={n.id} nodeHost={n.host} />
                    </td>
                  </tr>
                )}
              </>
            );
          })}
          {data && data.length === 0 && (
            <tr>
              <td colSpan={10} className="py-4 text-slate-500 text-center">
                Нод нет
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <p className="text-xs text-slate-500 mt-4">
        После создания ноды бэкенд автоматически ставит таску на bootstrap
        (site.yml против неё), статус будет <span className="font-mono">registering</span>{" "}
        → <span className="font-mono">active</span> через ~3-5 минут. Если застряло —
        открывай{" "}
        <Link to="/tasks" className="text-blue-400 hover:underline">
          Tasks
        </Link>{" "}
        (фильтр status=failed или target=node), там error_message и кнопка rerun.
      </p>
    </div>
  );
}

// ── Create Node form ────────────────────────────────────────────────

function CreateNodeForm({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [form, setForm] = useState<VPNNodeCreateIn>({
    name: "",
    region: "",
    host: "",
    ssh_port: 22,
    pool_id: null,
    notes: null,
  });
  const [err, setErr] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (payload: VPNNodeCreateIn) =>
      api.post<VPNNodeOut>("/nodes", payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["nodes"] });
      onDone();
    },
    onError: (e: Error) => {
      setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : e.message);
    },
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (!form.name || !form.region || !form.host) {
      setErr("name, region и host обязательны");
      return;
    }
    mutation.mutate(form);
  }

  return (
    <form
      onSubmit={submit}
      className="mb-4 p-4 rounded border border-slate-700 bg-slate-900/60 grid grid-cols-2 gap-3 text-sm"
    >
      <label className="flex flex-col">
        <span className="text-slate-400 text-xs mb-1">Имя (уникальное, kebab-case)</span>
        <input
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
          placeholder="fr-pq-01"
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 text-xs mb-1">Регион</span>
        <input
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
          placeholder="eu-west"
          value={form.region}
          onChange={(e) => setForm({ ...form, region: e.target.value })}
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 text-xs mb-1">Host (IP или DNS)</span>
        <input
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
          placeholder="185.234.64.186"
          value={form.host}
          onChange={(e) => setForm({ ...form, host: e.target.value })}
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 text-xs mb-1">SSH port</span>
        <input
          type="number"
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
          value={form.ssh_port}
          onChange={(e) => setForm({ ...form, ssh_port: Number(e.target.value) })}
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 text-xs mb-1">Pool ID (опционально)</span>
        <input
          type="number"
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
          value={form.pool_id ?? ""}
          onChange={(e) =>
            setForm({
              ...form,
              pool_id: e.target.value ? Number(e.target.value) : null,
            })
          }
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 text-xs mb-1">Заметка (опционально)</span>
        <input
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1"
          placeholder="PQ Hosting, Paris"
          value={form.notes ?? ""}
          onChange={(e) => setForm({ ...form, notes: e.target.value || null })}
        />
      </label>
      {err && <div className="col-span-2 text-red-400 text-xs">{err}</div>}
      <div className="col-span-2 flex gap-2 justify-end">
        <button
          type="button"
          onClick={onDone}
          className="px-3 py-1.5 rounded bg-slate-700 hover:bg-slate-600 text-sm"
        >
          Отмена
        </button>
        <button
          type="submit"
          disabled={mutation.isPending}
          className="px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-sm font-semibold disabled:opacity-50"
        >
          {mutation.isPending ? "Создаём…" : "Создать + bootstrap"}
        </button>
      </div>
    </form>
  );
}

// ── Node configs panel ──────────────────────────────────────────────

function NodeConfigs({ nodeId, nodeHost }: { nodeId: number; nodeHost: string }) {
  const qc = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [deleteErr, setDeleteErr] = useState<string | null>(null);

  const { data, isLoading } = useQuery<VPNConfigOut[]>({
    queryKey: ["node-configs", nodeId],
    queryFn: () => api.get(`/nodes/${nodeId}/configs`),
  });

  const deleteMutation = useMutation({
    mutationFn: (configId: number) =>
      api.del(`/nodes/${nodeId}/configs/${configId}`),
    onSuccess: () => {
      setDeleteErr(null);
      qc.invalidateQueries({ queryKey: ["node-configs", nodeId] });
      qc.invalidateQueries({ queryKey: ["nodes"] });
    },
    onError: (e: Error) => {
      setDeleteErr(e instanceof ApiError ? `${e.status}: ${e.message}` : e.message);
    },
  });

  function confirmDelete(cfg: VPNConfigOut) {
    if (!window.confirm(
      `Удалить конфиг ${cfg.name} (${cfg.protocol})? ` +
      `Устройства, привязанные к нему, получат 409 — сначала перенеси их на другую ноду.`
    )) return;
    deleteMutation.mutate(cfg.id);
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <div className="text-xs uppercase tracking-wide text-slate-400">
          Конфиги протоколов
        </div>
        <button
          onClick={() => setAddOpen((v) => !v)}
          className="px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 text-xs"
        >
          {addOpen ? "Отмена" : "+ Добавить конфиг"}
        </button>
      </div>

      {addOpen && (
        <AddConfigForm
          nodeId={nodeId}
          nodeHost={nodeHost}
          onDone={() => setAddOpen(false)}
        />
      )}

      {isLoading && <div className="text-slate-500 text-xs">Загрузка…</div>}
      {data && data.length === 0 && !addOpen && (
        <div className="text-slate-500 text-xs">
          Нет конфигов — добавь хотя бы один, иначе нода не сможет выдавать подписки.
        </div>
      )}
      {deleteErr && (
        <div className="text-red-400 text-xs mb-2">{deleteErr}</div>
      )}
      {data && data.length > 0 && (
        <table className="w-full text-xs">
          <thead className="text-left text-slate-500">
            <tr>
              <th className="py-1">Имя</th>
              <th>Протокол</th>
              <th>Порт</th>
              <th>SNI</th>
              <th>Enabled</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.map((c) => (
              <tr key={c.id} className="border-t border-slate-800">
                <td className="py-1 font-mono">{c.name}</td>
                <td className="font-mono text-slate-300">{c.protocol}</td>
                <td className="font-mono">{c.port}</td>
                <td className="font-mono text-slate-400">{c.sni ?? "—"}</td>
                <td>{c.is_enabled ? "✓" : "✕"}</td>
                <td className="text-right">
                  <button
                    onClick={() => confirmDelete(c)}
                    disabled={deleteMutation.isPending}
                    className="px-2 py-0.5 rounded bg-red-900 hover:bg-red-800 disabled:opacity-50 text-red-100"
                    title="Удалить конфиг"
                  >
                    🗑
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// Дефолты под каждый протокол — совпадают с тем, что ставит ansible
// по дефолту (см. install_shadowtls_stack/files/shadowtls_power_script.sh
// и install_vless_reality/defaults). Если операторы начнут менять
// порты в ролях — синхронизировать здесь.
const PROTOCOL_DEFAULTS: Record<
  VPNConfigProtocol,
  { port: number; sni: string; name: string }
> = {
  "shadowtls+shadowsocks": { port: 8443, sni: "www.cloudflare.com", name: "shadowtls" },
  "vless-reality": { port: 9443, sni: "www.asus.com", name: "vless-reality" },
  "vless-ws-cdn": { port: 443, sni: "", name: "vless-ws-cdn" },
  "hysteria2": { port: 8443, sni: "", name: "hysteria2" },
};

function AddConfigForm({
  nodeId,
  nodeHost,
  onDone,
}: {
  nodeId: number;
  nodeHost: string;
  onDone: () => void;
}) {
  const qc = useQueryClient();
  const [protocol, setProtocol] = useState<VPNConfigProtocol>("shadowtls+shadowsocks");
  const defaults = PROTOCOL_DEFAULTS[protocol];
  const [form, setForm] = useState<VPNConfigCreateIn>({
    name: defaults.name,
    protocol,
    port: defaults.port,
    sni: defaults.sni || null,
  });
  const [err, setErr] = useState<string | null>(null);

  // Меняем протокол — сбрасываем дефолты, чтобы юзер не оставил
  // порт 8443 у vless-reality случайно.
  function onProtocolChange(p: VPNConfigProtocol) {
    setProtocol(p);
    const d = PROTOCOL_DEFAULTS[p];
    setForm({ name: d.name, protocol: p, port: d.port, sni: d.sni || null });
  }

  const mutation = useMutation({
    mutationFn: (payload: VPNConfigCreateIn) =>
      api.post<VPNConfigOut>(`/nodes/${nodeId}/configs`, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["node-configs", nodeId] });
      qc.invalidateQueries({ queryKey: ["nodes"] });
      onDone();
    },
    onError: (e: Error) => {
      setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : e.message);
    },
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    mutation.mutate(form);
  }

  return (
    <form
      onSubmit={submit}
      className="mb-3 p-3 rounded border border-slate-700 bg-slate-900 grid grid-cols-2 gap-2 text-xs"
    >
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Протокол</span>
        <select
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
          value={protocol}
          onChange={(e) => onProtocolChange(e.target.value as VPNConfigProtocol)}
        >
          <option value="shadowtls+shadowsocks">shadowtls+shadowsocks</option>
          <option value="vless-reality">vless-reality</option>
          <option value="vless-ws-cdn">vless-ws-cdn</option>
          <option value="hysteria2">hysteria2</option>
        </select>
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Имя (для админки)</span>
        <input
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Порт</span>
        <input
          type="number"
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
          value={form.port}
          onChange={(e) => setForm({ ...form, port: Number(e.target.value) })}
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">SNI / fake domain</span>
        <input
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
          placeholder="www.cloudflare.com"
          value={form.sni ?? ""}
          onChange={(e) => setForm({ ...form, sni: e.target.value || null })}
        />
      </label>
      <div className="col-span-2 text-slate-500 text-[11px]">
        Host ноды: <span className="font-mono">{nodeHost}</span>. ShadowTLS+Shadowsocks
        не требует public_key / fallback, остальное оставляем дефолтным. Для
        vless-reality бэкенд сам генерит ключи, если оставить{" "}
        <span className="font-mono">public_key</span> пустым — см. роль{" "}
        <span className="font-mono">install_vless_reality</span>.
      </div>
      {err && <div className="col-span-2 text-red-400">{err}</div>}
      <div className="col-span-2 flex gap-2 justify-end">
        <button
          type="button"
          onClick={onDone}
          className="px-2 py-1 rounded bg-slate-700 hover:bg-slate-600"
        >
          Отмена
        </button>
        <button
          type="submit"
          disabled={mutation.isPending}
          className="px-2 py-1 rounded bg-emerald-600 hover:bg-emerald-500 font-semibold disabled:opacity-50"
        >
          {mutation.isPending ? "Сохраняем…" : "Создать"}
        </button>
      </div>
    </form>
  );
}

const KIND_LABELS: Record<string, string> = {
  migration: "Миграция",
  bootstrap: "Bootstrap",
  resync: "Resync",
};

function OperationProgressBanner({
  op,
  onDismiss,
}: {
  op: TrackedOp;
  onDismiss: () => void;
}) {
  const { data } = useQuery<ProvisioningTaskOut[]>({
    queryKey: ["provisioning-tasks", "tracked-op", op.kind, op.nodeId, op.startedAt],
    queryFn: () => api.get("/provisioning/tasks?limit=500"),
    refetchInterval: 3_000,
    retry: false,
  });

  const idSet = new Set(op.taskIds);
  const related = (data ?? []).filter((t) => idSet.has(t.id));
  const seen = new Set(related.map((t) => t.id));
  const missing = op.taskIds.filter((id) => !seen.has(id));

  const counts = {
    pending: related.filter((t) => t.status === "pending").length + missing.length,
    running: related.filter((t) => t.status === "running").length,
    success: related.filter((t) => t.status === "success").length,
    failed: related.filter((t) => t.status === "failed").length,
  };
  const total = op.taskIds.length;
  const done = counts.success + counts.failed;
  const allDone = done === total && total > 0;
  const pct = total === 0 ? 0 : Math.round((done / total) * 100);

  // Phase breakdown (only for migration which has multiple phases)
  const isMigration = op.kind === "migration";
  const phaseDone = (ids: number[]) =>
    related
      .filter((t) => ids.includes(t.id))
      .filter((t) => t.status === "success" || t.status === "failed").length;

  return (
    <div
      className={`mb-3 p-3 rounded border ${
        counts.failed > 0
          ? "border-red-900/60 bg-red-950/30"
          : allDone
            ? "border-emerald-900/60 bg-emerald-950/30"
            : "border-blue-900/60 bg-blue-950/30"
      }`}
    >
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-semibold">
          {KIND_LABELS[op.kind] ?? op.kind}{" "}
          <span className="font-mono">{op.nodeName}</span>:{" "}
          {done}/{total} задач
          {counts.failed > 0 && ` (${counts.failed} failed)`}
          {allDone && " — готово"}
        </div>
        <div className="flex gap-2">
          <Link
            to="/tasks"
            className="text-xs px-2 py-1 rounded bg-slate-700 hover:bg-slate-600"
          >
            Tasks
          </Link>
          <button
            onClick={onDismiss}
            className="text-xs px-2 py-1 rounded bg-slate-700 hover:bg-slate-600"
          >
            {allDone ? "Закрыть" : "Скрыть"}
          </button>
        </div>
      </div>
      <div className="h-2 bg-slate-800 rounded overflow-hidden mb-2">
        <div
          className={`h-full transition-all ${
            counts.failed > 0
              ? "bg-red-600"
              : allDone
                ? "bg-emerald-600"
                : "bg-blue-600"
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex gap-4 text-xs text-slate-400 flex-wrap">
        {isMigration && op.revokeTaskIds.length > 0 && (
          <span>
            revoke:{" "}
            <span className="font-mono text-slate-200">
              {phaseDone(op.revokeTaskIds)}/{op.revokeTaskIds.length}
            </span>
          </span>
        )}
        {isMigration && op.deviceTaskIds.length > 0 && (
          <span>
            apply:{" "}
            <span className="font-mono text-slate-200">
              {phaseDone(op.deviceTaskIds)}/{op.deviceTaskIds.length}
            </span>
          </span>
        )}
        {isMigration && op.resyncTaskIds.length > 0 && (
          <span>
            resync:{" "}
            <span className="font-mono text-slate-200">
              {phaseDone(op.resyncTaskIds)}/{op.resyncTaskIds.length}
            </span>
          </span>
        )}
        {isMigration && <span className="text-slate-500">|</span>}
        <span>pending: {counts.pending}</span>
        <span>running: {counts.running}</span>
        <span className="text-emerald-400">ok: {counts.success}</span>
        {counts.failed > 0 && (
          <span className="text-red-400">failed: {counts.failed}</span>
        )}
      </div>
    </div>
  );
}
