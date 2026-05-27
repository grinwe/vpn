import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  NodeActiveUsersOut,
  NodeHealthOut,
  NodeHealthPingStatsOut,
  NodeRefreshDestOut,
  NodeRelayLinkOut,
  NodeTrafficHistoryOut,
  ProvisioningTaskOut,
  REALITY_DEST_POOL_SUGGESTIONS,
  VPNConfigCreateIn,
  VPNConfigOut,
  VPNConfigProtocol,
  VPNConfigUpdateIn,
  VPNNodeCreateIn,
  VPNNodeOut,
  refreshNodeRealityDest,
} from "../api";
import { HealthDots } from "../linkHealth";
import { DiagnoseResult } from "../diagnoseResult";
import {
  diagnoseRelayLink,
  DiagnoseCheckEntry,
  DiagnoseMeta,
} from "../api";
import { WorkerHealthBadge } from "../workerHealth";

// ── Tracked operation types ─────────────────────────────────────────
// Persisted to localStorage so banners survive page navigation.
// `kind` distinguishes the three operation types in the UI; `taskIds`
// is the full set to poll; the sub-arrays break down by phase.

type TrackedOp = {
  kind: "migration" | "bootstrap" | "resync" | "diagnose" | "diagnose_link";
  nodeId: number;
  nodeName: string;
  // For kind=diagnose_link only — id of the RelayExitLink the diagnose was
  // run against, so the banner can render the structured `checks` block
  // out of task.result.checks (set by ansible role + orchestrator).
  linkId?: number;
  exitName?: string;
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

function HealthBadge({ score, blocked }: { score: number | null; blocked: string[] }) {
  if (score == null) return <span className="text-slate-500">—</span>;
  const color =
    score >= 80 ? "bg-emerald-600" : score >= 50 ? "bg-yellow-600" : "bg-red-600";
  return (
    <span className="inline-flex items-center gap-1">
      <span className={`text-xs px-1.5 py-0.5 rounded font-mono ${color}`}>{score}</span>
      {blocked.length > 0 && (
        <span className="text-xs px-1 py-0.5 rounded bg-red-900 text-red-300">
          {blocked.length} blocked
        </span>
      )}
    </span>
  );
}

// Бейдж «когда в последний раз tick реально дошёл до ноды по SSH».
// Источник — max(NodeTrafficSample.observed_at), а traffic_stats-тик
// пишет эти row'ы каждые 5 минут. Значит без всяких админских кликов
// видно: жива нода или нет.
// Пороги: 10 мин = 2 tick-цикла (healthy), 30 мин = tick сломался или
// SSH отвалился (alarm).
function SSHStatusBadge({ lastSshAt }: { lastSshAt: string | null }) {
  if (!lastSshAt) {
    return (
      <span
        className="text-xs px-1 py-0.5 rounded bg-slate-800 text-slate-500"
        title="Ни одного успешного traffic-stats тика ещё не было"
      >
        SSH —
      </span>
    );
  }
  const ageMin = (Date.now() - Date.parse(lastSshAt)) / 60000;
  let color = "bg-emerald-900/60 text-emerald-300";
  if (ageMin > 30) color = "bg-red-900 text-red-300";
  else if (ageMin > 10) color = "bg-amber-900 text-amber-300";
  const label =
    ageMin < 60 ? `${Math.round(ageMin)}m` : `${Math.round(ageMin / 60)}h`;
  return (
    <span
      className={`text-xs px-1 py-0.5 rounded ${color}`}
      title={`Последний tick дошёл ${new Date(lastSshAt).toLocaleString()}`}
    >
      SSH {label}
    </span>
  );
}

// 2026-04 incident: 3/4 нод застряли с cooldown_until в будущем, UI показывал
// только is_active=✓, админ не видел, что choose_node их игнорит. Теперь в
// колонке "Активна" рядом с ✓/✕ висит бейдж с оставшимся временем, если
// cooldown ещё действует. После истечения — скрыт.
function CooldownBadge({ until }: { until: string | null }) {
  if (!until) return null;
  const ms = new Date(until).getTime() - Date.now();
  if (ms <= 0) return null;
  const hours = Math.floor(ms / 3_600_000);
  const label =
    hours >= 24 ? `${Math.floor(hours / 24)}d ${hours % 24}h` : `${hours}h`;
  return (
    <span
      className="text-xs px-1 py-0.5 rounded bg-red-900 text-red-300"
      title={`cooldown до ${new Date(until).toLocaleString()}`}
    >
      cooldown {label}
    </span>
  );
}

function AutoDiagnoseBadge({
  at,
  taskId,
  symptom,
}: {
  at: string;
  taskId: number | null;
  symptom: string | null;
}) {
  // Возраст последнего auto-trigger'а в человеко-читаемом виде.
  // Backend tick — раз в 5 мин, debounce — 30 мин на link, так что
  // практический диапазон тут — минуты-часы, не дни.
  const ageMs = Date.now() - new Date(at).getTime();
  const ageMin = Math.max(0, Math.round(ageMs / 60_000));
  const label =
    ageMin < 60 ? `${ageMin} мин назад` : `${Math.round(ageMin / 60)} ч назад`;
  const tooltip = `Автодиагностика по симптому "${symptom ?? "?"}" в ${new Date(at).toLocaleString()}. Клик → задача в /tasks.`;
  const badge = (
    <span
      title={tooltip}
      className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-amber-900/60 border border-amber-700 text-amber-200"
    >
      ⚙ авто {label}
    </span>
  );
  if (taskId) {
    return (
      <Link to={`/tasks?id=${taskId}`} className="hover:opacity-80">
        {badge}
      </Link>
    );
  }
  return badge;
}

function LinkDiagnoseButton({
  linkId,
  exitName,
  nodeId,
  nodeName,
  addOp,
}: {
  linkId: number;
  exitName: string;
  nodeId: number;
  nodeName: string;
  addOp: (op: TrackedOp) => void;
}) {
  const [busy, setBusy] = useState(false);
  async function handleClick() {
    if (busy) return;
    setBusy(true);
    try {
      // V1: дефолтный набор check_types (роль возьмёт свои defaults).
      // Multi-select UI добавим в V1.1 — сейчас один клик → весь набор.
      const res = await diagnoseRelayLink(linkId);
      addOp({
        kind: "diagnose_link",
        nodeId,
        nodeName,
        linkId,
        exitName,
        taskIds: [res.task_id],
        revokeTaskIds: [],
        deviceTaskIds: [],
        resyncTaskIds: [],
        startedAt: Date.now(),
      });
    } catch (e) {
      alert(`Не удалось запустить diagnose: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }
  return (
    <button
      onClick={handleClick}
      disabled={busy}
      title="Прогнать read-only диагностику WG-линка: peer, handshake, ping, curl, xray-port"
      className="text-[10px] px-2 py-0.5 rounded bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50"
    >
      {busy ? "…" : "Диагностировать"}
    </button>
  );
}

function RelayLinksSection({
  nodeId,
  nodeName,
  addOp,
}: {
  nodeId: number;
  nodeName: string;
  addOp: (op: TrackedOp) => void;
}) {
  const { data, isLoading, error } = useQuery<NodeRelayLinkOut[]>({
    queryKey: ["node-relay-links", nodeId],
    queryFn: () => api.get(`/nodes/${nodeId}/links`),
  });

  if (isLoading) return <div className="text-xs text-slate-400">Загрузка relay-линков…</div>;
  if (error) return <div className="text-xs text-red-400">{(error as Error).message}</div>;
  if (!data || data.length === 0) return null;

  const total = data.reduce((acc, l) => acc + l.credentials_count, 0);

  return (
    <div className="rounded border border-slate-700 p-3 text-xs">
      <div className="flex items-center justify-between mb-2">
        <div>
          <span className="text-slate-400">Relay links: </span>
          <span className="font-mono">{data.length}</span>
          <span className="text-slate-500"> · creds pinned: </span>
          <span className="font-mono">{total}</span>
        </div>
        <Link to="/exits" className="text-blue-400 hover:underline">
          Управление → Exits
        </Link>
      </div>
      <table className="w-full">
        <thead className="text-slate-400">
          <tr>
            <th className="text-left py-1 px-2">Iface</th>
            <th className="text-left py-1 px-2">Exit</th>
            <th className="text-left py-1 px-2">WG client addr</th>
            <th className="text-left py-1 px-2">Creds</th>
            <th className="text-left py-1 px-2">Public key</th>
            <th className="text-left py-1 px-2">Создан</th>
            <th className="text-left py-1 px-2">Диагностика</th>
          </tr>
        </thead>
        <tbody>
          {data.map((l) => (
            <tr key={l.link_id} className="border-t border-slate-800">
              <td className="py-1 px-2 font-mono text-slate-300">{l.wg_interface_name}</td>
              <td className="py-1 px-2 font-mono">
                #{l.exit_id} {l.exit_name}
              </td>
              <td className="py-1 px-2 font-mono text-slate-400">
                {l.wg_client_address_v4}
              </td>
              <td className="py-1 px-2 font-mono">{l.credentials_count}</td>
              <td className="py-1 px-2 font-mono" title={l.wg_client_public_key}>
                {l.wg_client_public_key.slice(0, 12)}…
              </td>
              <td className="py-1 px-2 text-slate-400">
                {new Date(l.created_at).toLocaleString()}
              </td>
              <td className="py-1 px-2 space-y-1">
                <LinkDiagnoseButton
                  linkId={l.link_id}
                  exitName={l.exit_name}
                  nodeId={nodeId}
                  nodeName={nodeName}
                  addOp={addOp}
                />
                {l.last_auto_diagnose_at && (
                  <AutoDiagnoseBadge
                    at={l.last_auto_diagnose_at}
                    taskId={l.last_auto_diagnose_task_id ?? null}
                    symptom={l.last_auto_diagnose_symptom ?? null}
                  />
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-slate-500 mt-2">
        Это relay-нода: трафик клиентов туннелируется через wgN в соответствующий
        exit. Распределение creds по линкам — least-loaded (см. G.4 в{" "}
        <code>docs/RELAY_ROADMAP.md</code>). Кнопка «Диагностировать» —
        read-only прогон <code>diagnose_relay_link.yml</code>: peer на jump,
        handshake age, ping/curl через WG, xray port. Прогресс и результат —
        в баннере наверху страницы.
      </p>
    </div>
  );
}

function NodeHealth({ nodeId }: { nodeId: number }) {
  const { data, isLoading, error } = useQuery<NodeHealthOut>({
    queryKey: ["node-health", nodeId],
    queryFn: () => api.get(`/nodes/${nodeId}/health`),
  });

  if (isLoading) return <div className="text-xs text-slate-400">Загрузка health…</div>;
  if (error) return <div className="text-xs text-red-400">{(error as Error).message}</div>;
  if (!data) return null;

  const regions = Object.entries(data.per_region).sort(([, a], [, b]) => a - b);

  return (
    <div className="rounded border border-slate-700 p-3 text-xs">
      <div className="flex items-center gap-4 mb-2">
        <span className="text-slate-400">Health score:</span>
        <HealthBadge score={data.health_score} blocked={data.blocked_regions} />
        <span className="text-slate-400 ml-4">Overall success rate:</span>
        <span className="font-mono">{(data.overall_success_rate * 100).toFixed(1)}%</span>
      </div>
      {data.blocked_regions.length > 0 && (
        <div className="mb-2">
          <span className="text-red-400">Blocked regions: </span>
          <span className="font-mono text-red-300">{data.blocked_regions.join(", ")}</span>
        </div>
      )}
      {regions.length > 0 && (
        <div>
          <div className="text-slate-400 mb-1">Per-region success rate:</div>
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-x-4 gap-y-1">
            {regions.map(([region, rate]) => (
              <div key={region} className="flex items-center gap-2">
                <span className="text-slate-300 w-20 truncate">{region}</span>
                <div className="flex-1 h-2 bg-slate-800 rounded overflow-hidden">
                  <div
                    className={`h-full ${rate >= 0.8 ? "bg-emerald-600" : rate >= 0.5 ? "bg-yellow-600" : "bg-red-600"}`}
                    style={{ width: `${rate * 100}%` }}
                  />
                </div>
                <span className="font-mono text-slate-400 w-12 text-right">
                  {(rate * 100).toFixed(0)}%
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
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
  const [migrateToModal, setMigrateToModal] = useState<
    { from_id: number; from_name: string } | null
  >(null);
  const [refreshDestModal, setRefreshDestModal] = useState<{
    node_id: number;
    node_name: string;
  } | null>(null);
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

  const backfillCreds = useMutation({
    mutationFn: (node: { id: number; name: string }) =>
      api
        .post<{
          node_id: number;
          created: Record<string, number>;
          total_created: number;
        }>(`/nodes/${node.id}/backfill-missing-creds`, {})
        .then((res) => ({ ...res, nodeName: node.name })),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      if (res.total_created === 0) {
        alert(
          `На ноде #${res.node_id} (${res.nodeName}) пропущенных кредов нет — все активные девайсы уже имеют Credential под каждый enabled-протокол.`,
        );
        return;
      }
      const perConfig = Object.entries(res.created)
        .map(([cfg_id, n]) => `config #${cfg_id}: +${n}`)
        .join("\n");
      alert(
        `Backfill на ноде #${res.node_id} (${res.nodeName}): создано ${res.total_created} Credential'ов.\n\n${perConfig}\n\nАвто-resync по bootstrap-хвосту подтянет их в xray в течение минуты.`,
      );
    },
    onError: (e: Error) => alert(`Backfill не удался: ${e.message}`),
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
          considered_count: number;
          no_target_count: number;
        }>(`/nodes/${node.id}/migrate`, {})
        .then((res) => ({ ...res, nodeName: node.name })),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["nodes"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      if (res.task_ids.length === 0) {
        // Three cases, previously conflated under "нечего мигрировать":
        //   considered=0                 → на ноде нет активных subs.
        //   considered>0, no_target=all  → subs есть, но choose_node
        //                                  не нашёл куда (все остальные
        //                                  ноды в cooldown / unhealthy /
        //                                  вне pool'а плана).
        //   considered>0, migrated=0     → иначе: провалились на фазе
        //                                  reprovision (логи).
        if (res.considered_count === 0) {
          alert("Активных подписок на этой ноде не было — мигрировать нечего.");
        } else if (res.no_target_count >= res.considered_count) {
          alert(
            `Нашли ${res.considered_count} активных подписок на ноде, но выбрать целевую ноду не удалось ни для одной: все остальные ноды в cooldown, unhealthy, или вне пулов планов.\n\n` +
              `Проверь список нод — cooldown чистится через кнопку "вернуть в пул" или смену статуса на active.`,
          );
        } else {
          alert(
            `Обработано подписок: ${res.considered_count}, мигрировано: ${res.migrated_subscriptions.length}, без цели: ${res.no_target_count}. Фоновых тасок не создано — смотри логи.`,
          );
        }
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

  const migrateTo = useMutation({
    mutationFn: (args: {
      from_id: number;
      from_name: string;
      to_id: number;
      to_name: string;
    }) =>
      api
        .post<{
          from_node_id: number;
          to_node_id: number;
          considered_count: number;
          migrated: number[];
          failed: { subscription_id: number; error: string }[];
          task_ids: number[];
          revoke_task_ids: number[];
          device_task_ids: number[];
          resync_task_ids: number[];
        }>(`/nodes/${args.from_id}/migrate-to/${args.to_id}`, {})
        .then((res) => ({ ...res, from_name: args.from_name, to_name: args.to_name })),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["nodes"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      setMigrateToModal(null);
      if (res.considered_count === 0) {
        alert(
          `На ноде #${res.from_node_id} (${res.from_name}) активных подписок не было — мигрировать нечего.`,
        );
        return;
      }
      const failedNote = res.failed.length
        ? `\n\nПровалились: ${res.failed.length}. Первые 3:\n` +
          res.failed
            .slice(0, 3)
            .map((f) => `  sub #${f.subscription_id}: ${f.error}`)
            .join("\n")
        : "";
      if (res.task_ids.length === 0) {
        alert(
          `Обработано ${res.considered_count}, мигрировано ${res.migrated.length}. Фоновых тасок не создано — смотри логи.${failedNote}`,
        );
        return;
      }
      addOp({
        kind: "migration",
        nodeId: res.to_node_id,
        nodeName: `${res.from_name} → ${res.to_name}`,
        taskIds: res.task_ids,
        revokeTaskIds: res.revoke_task_ids,
        deviceTaskIds: res.device_task_ids,
        resyncTaskIds: res.resync_task_ids,
        startedAt: Date.now(),
      });
      if (res.failed.length) {
        alert(
          `Переселено ${res.migrated.length}/${res.considered_count} подписок.${failedNote}`,
        );
      }
    },
    onError: (e: Error) =>
      alert(`Не удалось мигрировать целевой: ${e.message}`),
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

  // Smart delete: walk the 409 → migrate → delete path so admins can
  // remove a node without poking migrate first. Cloud-provisioned nodes
  // still go through /destroy (teardown playbook + VPS deprovision);
  // only raw DB rows take the migrate-then-delete branch.
  const deleteNode = useMutation({
    mutationFn: async (node: { id: number; name: string; provider_id: number | null }) => {
      if (node.provider_id) {
        return api.post<{ node_id: number }>(`/nodes/${node.id}/destroy`, {});
      }

      const tryDelete = () =>
        api.del<{
          node_id: number;
          deleted: boolean;
          warm_credentials_deleted?: number;
          bound_credentials_detached?: number;
        }>(`/nodes/${node.id}`);

      try {
        return await tryDelete();
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 409) throw err;
        const detail = err.detail as { error?: string; active_subs?: number } | string;
        if (typeof detail !== "object" || detail.error !== "active_subs") throw err;

        const n = detail.active_subs ?? 0;
        const confirmed = window.confirm(
          `На ноде "${node.name}" ещё ${n} активных/замороженных подписок.\n\n` +
            `Перенести их на другие ноды (как при обычном переселении), а затем удалить?\n\n` +
            `OK — перенести и удалить.\nОтмена — ничего не делать.`,
        );
        if (!confirmed) throw new Error("отменено пользователем");

        // /migrate kicks off per-sub migrations synchronously at DB level
        // (subscription.node_id flips immediately), so on return the node
        // has zero active subs and DELETE can proceed. The device task
        // fan-out continues in background — doesn't block node removal.
        const mig = await api.post<{
          node_id: number;
          migrated_subscriptions: number[];
          task_ids: number[];
          considered_count: number;
          no_target_count: number;
        }>(`/nodes/${node.id}/migrate`, {});
        if (mig.no_target_count > 0 && mig.migrated_subscriptions.length === 0) {
          throw new Error(
            `Не удалось выбрать целевую ноду ни для одной из ${mig.considered_count} ` +
              `подписок — все остальные ноды в cooldown/unhealthy/вне пула. ` +
              `Разберись с остальными нодами и повтори.`,
          );
        }
        qc.invalidateQueries({ queryKey: ["user-subs"] });
        qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
        return tryDelete();
      }
    },
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["nodes"] });
      const bits: string[] = ["Нода удалена."];
      if ("warm_credentials_deleted" in res && res.warm_credentials_deleted) {
        bits.push(`warm-креды удалены: ${res.warm_credentials_deleted}`);
      }
      if ("bound_credentials_detached" in res && res.bound_credentials_detached) {
        bits.push(`исторических кредов отвязано: ${res.bound_credentials_detached}`);
      }
      alert(bits.join(" "));
    },
    onError: (e: Error) => {
      if (e.message === "отменено пользователем") return;
      const extra =
        e instanceof ApiError && typeof e.detail === "object" && e.detail
          ? `\n\nДетали: ${JSON.stringify(e.detail, null, 2)}`
          : "";
      alert(`Не удалось удалить: ${e.message}${extra}`);
    },
  });

  const diagnose = useMutation({
    mutationFn: (node: { id: number; name: string }) =>
      api
        .post<{ node_id: number; task_id: number }>(
          `/nodes/${node.id}/diagnose`,
          {},
        )
        .then((res) => ({ ...res, nodeName: node.name })),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      addOp({
        kind: "diagnose",
        nodeId: res.node_id,
        nodeName: res.nodeName,
        taskIds: [res.task_id],
        revokeTaskIds: [],
        deviceTaskIds: [],
        resyncTaskIds: [],
        startedAt: Date.now(),
      });
    },
    onError: (e: Error) => alert(`Не удалось запустить диагностику: ${e.message}`),
  });

  const refreshRealityDest = useMutation({
    mutationFn: (args: {
      node_id: number;
      node_name: string;
      sni: string | null;
    }) =>
      refreshNodeRealityDest(args.node_id, { sni: args.sni }).then(
        (res) => ({ ...res, node_name: args.node_name }),
      ),
    onSuccess: (res: NodeRefreshDestOut & { node_name: string }) => {
      qc.invalidateQueries({ queryKey: ["nodes"] });
      qc.invalidateQueries({ queryKey: ["node-configs", res.node_id] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      setRefreshDestModal(null);
      const failedNote =
        res.failed_subs.length > 0
          ? `\n\n⚠ Проваленные подписки (${res.failed_subs.length}): ${res.failed_subs.slice(0, 10).join(", ")}${res.failed_subs.length > 10 ? "…" : ""}`
          : "";
      alert(
        `Reality dest на «${res.node_name}» обновлён: ${res.old_sni} → ${res.new_sni}.\n` +
          `Затронуто активных подписок: ${res.sub_count}, задач в фоне: ${res.task_ids.length}.` +
          failedNote,
      );
    },
    onError: (e: Error) => {
      const extra =
        e instanceof ApiError && typeof e.detail === "object" && e.detail
          ? `\n\nДетали: ${JSON.stringify(e.detail, null, 2)}`
          : "";
      alert(`Не удалось обновить Reality dest: ${e.message}${extra}`);
    },
  });

  const setStatus = useMutation({
    mutationFn: ({ id, status }: { id: number; status: string }) =>
      api.patch<VPNNodeOut>(`/nodes/${id}/status`, { status }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["nodes"] }),
    onError: (e: Error) => alert(`Не удалось сменить статус: ${e.message}`),
  });

  // Тот же endpoint, что и на странице Exits — форс-прогоняет тик
  // run_relay_link_health, обновляя last_handshake_at/last_observed_at
  // на всех relay-нодах разом. Инвалидируем nodes-query с задержкой,
  // потому что тику нужно время на SSH к relay'ям (10–20 сек).
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
        qc.invalidateQueries({ queryKey: ["nodes"] });
        qc.invalidateQueries({ queryKey: ["node-relay-links"] });
      }, 15_000);
    },
    onError: (e: Error) => alert(`Ошибка: ${e.message}`),
  });

  // Форс-прогон tick-traffic-stats: воркер SSH'ит на все
  // active/draining ноды, пишет NodeTrafficSample — после этого
  // list_nodes увидит свежий max(observed_at) и колонка "SSH ·
  // обновлено" перестанет быть мёртвой. Нужен если scheduler
  // раскис (redis restart, воркер лёг) и автоматический 5-мин
  // тик не догоняет; обычно с ним ничего делать не нужно.
  const refreshSshMut = useMutation({
    mutationFn: () =>
      api.post<{
        enqueued: boolean;
        job_id?: string;
        reason?: string;
        note?: string;
      }>(`/nodes/ssh/ping/refresh`),
    onSuccess: (res) => {
      if (!res.enqueued) {
        alert(`Не удалось запустить: ${res.reason ?? "очередь недоступна"}`);
        return;
      }
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["nodes"] });
      }, 20_000);
    },
    onError: (e: Error) => alert(`Ошибка: ${e.message}`),
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
        <div className="flex gap-2 items-center">
          <WorkerHealthBadge />
          <button
            disabled={refreshSshMut.isPending}
            onClick={() => refreshSshMut.mutate()}
            title="Форс-прогнать traffic_stats тик — воркер SSH'нет на все active/draining ноды и запишет observed_at. Обновит колонку SSH через 15–30 сек. Нужно если автоматический 5-мин scheduler раскис."
            className="text-sm px-3 py-1.5 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50"
          >
            {refreshSshMut.isPending ? "Пингуем…" : "↻ SSH-пинг всех"}
          </button>
          <button
            disabled={refreshAllHealthMut.isPending}
            onClick={() => refreshAllHealthMut.mutate()}
            title="Форс-прогнать relay_link_health тик — воркер SSH'нет на все relay сразу, обновит WG-индикаторы через 10–20 сек"
            className="text-sm px-3 py-1.5 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50"
          >
            {refreshAllHealthMut.isPending ? "Обновляем…" : "↻ health всех"}
          </button>
          <button
            className="px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-sm font-semibold"
            onClick={() => setCreateOpen((v) => !v)}
          >
            {createOpen ? "Отмена" : "+ Добавить ноду"}
          </button>
        </div>
      </div>

      {createOpen && (
        <CreateNodeForm
          onDone={() => setCreateOpen(false)}
        />
      )}

      {migrateToModal && data && (
        <MigrateToModal
          fromId={migrateToModal.from_id}
          fromName={migrateToModal.from_name}
          nodes={data}
          pending={migrateTo.isPending}
          onCancel={() => setMigrateToModal(null)}
          onSubmit={(to_id, to_name) =>
            migrateTo.mutate({
              from_id: migrateToModal.from_id,
              from_name: migrateToModal.from_name,
              to_id,
              to_name,
            })
          }
        />
      )}

      {refreshDestModal && (
        <RefreshRealityDestModal
          nodeId={refreshDestModal.node_id}
          nodeName={refreshDestModal.node_name}
          pending={refreshRealityDest.isPending}
          onCancel={() => setRefreshDestModal(null)}
          onSubmit={(sni) =>
            refreshRealityDest.mutate({
              node_id: refreshDestModal.node_id,
              node_name: refreshDestModal.node_name,
              sni,
            })
          }
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
            <th>Health</th>
            <th>WG</th>
            <th>Активна</th>
            <th>SSH · обновлено</th>
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
                  <td onClick={(e) => e.stopPropagation()}>
                    <select
                      value={n.status}
                      disabled={setStatus.isPending}
                      onChange={(e) => {
                        const next = e.target.value;
                        if (next === n.status) return;
                        if (
                          confirm(
                            `Сменить статус ноды #${n.id} (${n.name}): ${n.status} → ${next}?` +
                              (next === "active"
                                ? "\n\nCooldown, suspect и blocked_regions будут сброшены."
                                : "\n\nis_active будет выключен."),
                          )
                        )
                          setStatus.mutate({ id: n.id, status: next });
                        else e.target.value = n.status;
                      }}
                      className={`text-xs px-1 py-0.5 rounded bg-slate-800 border border-slate-700 ${statusColor(n.status)}`}
                    >
                      <option value="active">active</option>
                      <option value="registering" disabled>
                        registering
                      </option>
                      <option value="error">error</option>
                      <option value="disabled">disabled</option>
                      <option value="draining" disabled>
                        draining
                      </option>
                    </select>
                  </td>
                  <td>
                    <HealthBadge score={n.health_score} blocked={n.blocked_regions} />
                  </td>
                  <td>
                    <HealthDots
                      links={n.exit_links}
                      peerLabel={(l) => `${l.exit_name} · ${l.wg_interface_name}`}
                      peerKey={(l) => `${l.exit_id}-${l.wg_interface_name}`}
                    />
                  </td>
                  <td>
                    <span className="inline-flex items-center gap-1">
                      <span>{n.is_active ? "✓" : "✕"}</span>
                      <CooldownBadge until={n.cooldown_until} />
                    </span>
                  </td>
                  <td>
                    <div className="flex flex-col gap-0.5">
                      <SSHStatusBadge lastSshAt={n.last_ssh_at} />
                      <span className="text-[10px] text-slate-500">
                        {new Date(n.updated_at).toLocaleString()}
                      </span>
                    </div>
                  </td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <div className="flex gap-1">
                      <button
                        disabled={setActive.isPending}
                        onClick={() => {
                          const next = !n.is_active;
                          const msg = next
                            ? `Включить ноду #${n.id} (${n.name}) в пул? Новые подписки снова смогут на ней создаваться.\n\nCooldown, suspect_since и blocked_regions будут сброшены.`
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
                        disabled={migrateTo.isPending}
                        onClick={() =>
                          setMigrateToModal({ from_id: n.id, from_name: n.name })
                        }
                        className="text-xs px-2 py-1 rounded bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50"
                        title="Переселить все активные подписки на выбранную ноду (обходит pool/health фильтры)"
                      >
                        переселить на…
                      </button>
                      <button
                        disabled={bootstrap.isPending}
                        onClick={() => {
                          if (
                            confirm(
                              `Перекатить site.yml на ноду #${n.id} (${n.name}) с нуля?\n\n` +
                                `Запустится вся цепочка ролей (bootstrap_node, install_vless_reality, install_vless_ws_cdn, install_vless_xhttp, check_node_health). install_vless_reality preserve'ит существующих VLESS-клиентов через slurp старого config.json, плюс после успеха бэк авто-триггернёт resync всех активных UUID'ов. Безопасно для ноды с живым трафиком.`,
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
                      <button
                        disabled={backfillCreds.isPending}
                        onClick={() => {
                          if (
                            confirm(
                              `Backfill пропущенных кредов на ноде #${n.id} (${n.name})?\n\n` +
                                `Для каждого enabled-протокола ноды проверим все активные девайсы и допишем недостающие Credential-строки. Починит случай, когда /sub/{token} не отдаёт второй/третий протокол ранее провижённому юзеру (обычно после добавления нового VPNConfig на уже живую ноду). Идемпотентно — повторный запуск на здоровой ноде ничего не создаст.`,
                            )
                          )
                            backfillCreds.mutate({ id: n.id, name: n.name });
                        }}
                        className="text-xs px-2 py-1 rounded bg-amber-700 hover:bg-amber-600 disabled:opacity-50"
                        title="Backfill Credential-строк под enabled протоколы ноды"
                      >
                        backfill креды
                      </button>
                      <button
                        disabled={diagnose.isPending}
                        onClick={() => {
                          diagnose.mutate({ id: n.id, name: n.name });
                        }}
                        className="text-xs px-2 py-1 rounded bg-teal-700 hover:bg-teal-600 disabled:opacity-50"
                      >
                        диагностика
                      </button>
                      <button
                        disabled={refreshRealityDest.isPending}
                        onClick={() =>
                          setRefreshDestModal({ node_id: n.id, node_name: n.name })
                        }
                        className="text-xs px-2 py-1 rounded bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50"
                        title="Сменить Reality SNI/dest и перепровижинить активные подписки ноды"
                      >
                        обновить reality dest
                      </button>
                      <button
                        disabled={deleteNode.isPending}
                        onClick={() => {
                          if (
                            confirm(
                              `Удалить ноду #${n.id} (${n.name})?\n\n` +
                                (n.provider_id
                                  ? "Cloud-нода — VM будет уничтожена через API провайдера."
                                  : "Manual-нода — запись будет удалена из БД." +
                                    "\n\nЕсли на ноде есть подписки — будет " +
                                    "предложено переселить их и удалить ноду.") +
                                "",
                            )
                          )
                            deleteNode.mutate({
                              id: n.id,
                              name: n.name,
                              provider_id: n.provider_id,
                            });
                        }}
                        className="text-xs px-2 py-1 rounded bg-red-800 hover:bg-red-700 disabled:opacity-50"
                      >
                        удалить
                      </button>
                    </div>
                  </td>
                </tr>
                {expanded && (
                  <tr className="border-b border-slate-800 bg-slate-900/60">
                    <td colSpan={12} className="p-4 space-y-4">
                      <RelayLinksSection
                        nodeId={n.id}
                        nodeName={n.name}
                        addOp={addOp}
                      />
                      <NodeHealth nodeId={n.id} />
                      <NodeActiveUsers nodeId={n.id} />
                      <NodeTrafficChart nodeId={n.id} />
                      <NodeHealthPings nodeId={n.id} />
                      <NodeConfigs nodeId={n.id} nodeHost={n.host} />
                    </td>
                  </tr>
                )}
              </>
            );
          })}
          {data && data.length === 0 && (
            <tr>
              <td colSpan={12} className="py-4 text-slate-500 text-center">
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

// ── Bulk migrate-to modal ───────────────────────────────────────────
// Targeted bulk migration: funnels every active sub on ``fromId`` to a
// single chosen target (relay cut-over per RELAY_ROADMAP.md D.3).
// Bypasses pool/health/cooldown filters — admin takes responsibility.

function MigrateToModal({
  fromId,
  fromName,
  nodes,
  pending,
  onCancel,
  onSubmit,
}: {
  fromId: number;
  fromName: string;
  nodes: VPNNodeOut[];
  pending: boolean;
  onCancel: () => void;
  onSubmit: (to_id: number, to_name: string) => void;
}) {
  const candidates = nodes.filter((n) => n.id !== fromId && n.is_active);
  const [toId, setToId] = useState<number | null>(
    candidates.length > 0 ? candidates[0].id : null,
  );
  const target = candidates.find((n) => n.id === toId) ?? null;

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50"
      onClick={onCancel}
    >
      <div
        className="bg-slate-800 border border-slate-700 rounded-lg p-6 max-w-lg w-full mx-4"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold mb-3">
          Переселить подписки с «{fromName}» на выбранную ноду
        </h2>
        <p className="text-sm text-slate-400 mb-4">
          Все активные подписки с ноды #{fromId} ({fromName}) будут переселены
          на выбранную целевую ноду. <span className="font-semibold">Проверки
          пула, health и cooldown обходятся</span> — выбор админа считается
          осознанным. <code className="font-mono">sub_token</code> сохраняется,
          клиенты подхватят новый профиль на следующем рефетче sub-link.
        </p>

        {candidates.length === 0 ? (
          <div className="text-sm text-amber-400 mb-4">
            Нет других активных нод — некуда переселять.
          </div>
        ) : (
          <>
            <label className="block text-sm mb-1">Целевая нода</label>
            <select
              className="w-full bg-slate-900 border border-slate-600 rounded px-2 py-1.5 mb-4"
              value={toId ?? ""}
              onChange={(e) => setToId(Number(e.target.value))}
            >
              {candidates.map((n) => (
                <option key={n.id} value={n.id}>
                  #{n.id} · {n.name} ({n.region}) · {n.host}
                </option>
              ))}
            </select>
          </>
        )}

        <div className="flex justify-end gap-2">
          <button
            className="px-3 py-1.5 rounded bg-slate-700 hover:bg-slate-600 text-sm"
            onClick={onCancel}
            disabled={pending}
          >
            Отмена
          </button>
          <button
            className="px-3 py-1.5 rounded bg-indigo-700 hover:bg-indigo-600 text-sm disabled:opacity-50"
            disabled={pending || !target}
            onClick={() => {
              if (!target) return;
              if (
                confirm(
                  `Переселить все активные подписки с #${fromId} (${fromName}) на #${target.id} (${target.name})?\n\n` +
                    `Pool/health/cooldown-фильтры обходятся. Это то, что нужно при cut-over на RU-relay.`,
                )
              )
                onSubmit(target.id, target.name);
            }}
          >
            {pending ? "Переселяем…" : "Переселить"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Refresh Reality dest modal ──────────────────────────────────────
// Меняет sni/dest у vless_reality конфига ноды + перепровижинит все
// активные подписки (revoke старого Device + cold reprovision нового).
// Клиенты подхватят новый URI через sub-refresh (окно деградации 1-2
// реконнекта). "auto" = _pick_reality_sni по пулу на бэке.

function RefreshRealityDestModal({
  nodeId,
  nodeName,
  pending,
  onCancel,
  onSubmit,
}: {
  nodeId: number;
  nodeName: string;
  pending: boolean;
  onCancel: () => void;
  onSubmit: (sni: string | null) => void;
}) {
  type Mode = "auto" | "pool" | "custom";
  const [mode, setMode] = useState<Mode>("auto");
  const [poolChoice, setPoolChoice] = useState<string>(
    REALITY_DEST_POOL_SUGGESTIONS[0],
  );
  const [custom, setCustom] = useState<string>("");

  const resolvedSni =
    mode === "auto" ? null : mode === "pool" ? poolChoice : custom.trim();
  const submitDisabled =
    pending || (mode === "custom" && !custom.trim());

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50"
      onClick={onCancel}
    >
      <div
        className="bg-slate-800 border border-slate-700 rounded-lg p-6 max-w-lg w-full mx-4"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold mb-3">
          Обновить Reality dest на «{nodeName}»
        </h2>
        <p className="text-sm text-slate-400 mb-2">
          На ноде #{nodeId} обновится sni/dest у vless_reality конфига, и все
          активные подписки будут перепровижинены с новым UUID + новым sni.
        </p>
        <p className="text-sm text-red-400 mb-4">
          Окно деградации: клиенты, уже подключённые к ноде, увидят 1-2
          реконнекта. Sub-refresh у клиента (~каждые 6ч или при reject) подтянет
          новый URI — fix сам.
        </p>

        <div className="space-y-2 mb-4">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name="refresh-mode"
              checked={mode === "auto"}
              onChange={() => setMode("auto")}
              disabled={pending}
            />
            <span>
              Авто-выбор из пула{" "}
              <span className="text-slate-500">
                (бэк возьмёт наименее используемый SNI)
              </span>
            </span>
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name="refresh-mode"
              checked={mode === "pool"}
              onChange={() => setMode("pool")}
              disabled={pending}
            />
            <span>Явно из пула:</span>
            <select
              className="bg-slate-900 border border-slate-600 rounded px-2 py-1 text-sm disabled:opacity-50"
              value={poolChoice}
              onChange={(e) => setPoolChoice(e.target.value)}
              disabled={pending || mode !== "pool"}
            >
              {REALITY_DEST_POOL_SUGGESTIONS.map((sni) => (
                <option key={sni} value={sni}>
                  {sni}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name="refresh-mode"
              checked={mode === "custom"}
              onChange={() => setMode("custom")}
              disabled={pending}
            />
            <span>Свой домен:</span>
            <input
              type="text"
              placeholder="example.com"
              className="flex-1 bg-slate-900 border border-slate-600 rounded px-2 py-1 text-sm font-mono disabled:opacity-50"
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              disabled={pending || mode !== "custom"}
            />
          </label>
        </div>

        <div className="flex justify-end gap-2">
          <button
            className="px-3 py-1.5 rounded bg-slate-700 hover:bg-slate-600 text-sm"
            onClick={onCancel}
            disabled={pending}
          >
            Отмена
          </button>
          <button
            className="px-3 py-1.5 rounded bg-indigo-700 hover:bg-indigo-600 text-sm disabled:opacity-50"
            disabled={submitDisabled}
            onClick={() => {
              const label =
                mode === "auto" ? "авто-выбор из пула" : `sni = ${resolvedSni}`;
              if (
                confirm(
                  `Сменить Reality dest на ноде #${nodeId} (${nodeName})?\n\n` +
                    `Режим: ${label}.\n\n` +
                    `Все активные подписки ноды будут перепровижинены. Клиенты увидят 1-2 реконнекта.`,
                )
              )
                onSubmit(resolvedSni);
            }}
          >
            {pending ? "Обновляем…" : "Обновить"}
          </button>
        </div>
      </div>
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
  const [editingId, setEditingId] = useState<number | null>(null);
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
            {data.map((c) => {
              const isEditing = editingId === c.id;
              return (
                <>
                  <tr key={c.id} className="border-t border-slate-800">
                    <td className="py-1 font-mono">{c.name}</td>
                    <td className="font-mono text-slate-300">{c.protocol}</td>
                    <td className="font-mono">{c.port}</td>
                    <td className="font-mono text-slate-400">{c.sni ?? "—"}</td>
                    <td>{c.is_enabled ? "✓" : "✕"}</td>
                    <td className="text-right space-x-1">
                      <button
                        onClick={() =>
                          setEditingId(isEditing ? null : c.id)
                        }
                        className="px-2 py-0.5 rounded bg-slate-700 hover:bg-slate-600 text-slate-100"
                        title="Редактировать"
                      >
                        ✏
                      </button>
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
                  {isEditing && (
                    <tr key={`${c.id}-edit`} className="border-t border-slate-800 bg-slate-950/60">
                      <td colSpan={6} className="p-2">
                        <EditConfigForm
                          nodeId={nodeId}
                          config={c}
                          onDone={() => setEditingId(null)}
                        />
                      </td>
                    </tr>
                  )}
                </>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function EditConfigForm({
  nodeId,
  config,
  onDone,
}: {
  nodeId: number;
  config: VPNConfigOut;
  onDone: () => void;
}) {
  const qc = useQueryClient();
  const [form, setForm] = useState<VPNConfigUpdateIn>({
    name: config.name,
    port: config.port,
    sni: config.sni,
    fallback: config.fallback,
    public_key: config.public_key,
    is_enabled: config.is_enabled,
    protocol: config.protocol, // read-only, just echoed back for backend validation
  });
  const [err, setErr] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (payload: VPNConfigUpdateIn) =>
      api.put<VPNConfigOut>(`/nodes/${nodeId}/configs/${config.id}`, payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["node-configs", nodeId] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      onDone();
    },
    onError: (e: Error) => {
      setErr(e instanceof ApiError ? `${e.status}: ${e.message}` : e.message);
    },
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    // settings intentionally omitted — backend merges if sent, otherwise
    // leaves existing (encrypted) secrets in place. UI никогда не трогает
    // сырые secrets, так что мы их и не шлём обратно.
    mutation.mutate({
      name: form.name ?? undefined,
      port: form.port ?? undefined,
      sni: form.sni ?? null,
      fallback: form.fallback ?? null,
      public_key: form.public_key ?? null,
      is_enabled: form.is_enabled ?? undefined,
      protocol: form.protocol ?? undefined,
    });
  }

  return (
    <form onSubmit={submit} className="grid grid-cols-2 gap-2 text-xs">
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Протокол (нельзя менять)</span>
        <input
          disabled
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono text-slate-400"
          value={config.protocol}
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Имя</span>
        <input
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
          value={form.name ?? ""}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Порт</span>
        <input
          type="number"
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
          value={form.port ?? ""}
          onChange={(e) => setForm({ ...form, port: Number(e.target.value) })}
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">SNI / fake domain</span>
        <input
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
          value={form.sni ?? ""}
          onChange={(e) => setForm({ ...form, sni: e.target.value || null })}
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Fallback (REALITY dest)</span>
        <input
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
          placeholder="www.asus.com:443"
          value={form.fallback ?? ""}
          onChange={(e) =>
            setForm({ ...form, fallback: e.target.value || null })
          }
        />
      </label>
      <label className="flex flex-col">
        <span className="text-slate-400 mb-1">Public key (REALITY)</span>
        <input
          className="bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono"
          value={form.public_key ?? ""}
          onChange={(e) =>
            setForm({ ...form, public_key: e.target.value || null })
          }
        />
      </label>
      <label className="flex items-center gap-2 col-span-2">
        <input
          type="checkbox"
          checked={!!form.is_enabled}
          onChange={(e) => setForm({ ...form, is_enabled: e.target.checked })}
        />
        <span className="text-slate-300">Enabled</span>
      </label>
      <div className="col-span-2 text-slate-500 text-[11px]">
        Сохранение запустит bootstrap-таску на ноде — ansible перегенерит xray
        config. Существующие клиенты на этом протоколе, возможно, переподключатся.
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
          {mutation.isPending ? "Сохраняем…" : "Сохранить + bootstrap"}
        </button>
      </div>
    </form>
  );
}

// ── Active users panel ─────────────────────────────────────────────

function NodeActiveUsers({ nodeId }: { nodeId: number }) {
  const { data, isLoading, error } = useQuery<NodeActiveUsersOut>({
    queryKey: ["node-active-users", nodeId],
    queryFn: () => api.get(`/nodes/${nodeId}/users`),
    refetchInterval: 60_000,
  });

  if (isLoading)
    return <div className="text-xs text-slate-500">Загрузка списка юзеров…</div>;
  if (error)
    return (
      <div className="text-xs text-red-400">{(error as Error).message}</div>
    );
  if (!data) return null;

  const observed = data.observed_at
    ? new Date(data.observed_at).toLocaleTimeString()
    : null;

  return (
    <div className="rounded border border-slate-700 p-3 text-xs">
      <div className="flex items-center justify-between mb-2">
        <div className="text-xs uppercase tracking-wide text-slate-400">
          Активные юзеры
        </div>
        <div className="text-[11px] text-slate-500">
          {data.stale ? (
            <span className="text-yellow-400">
              Нет свежих данных{observed ? ` (последний снимок ${observed})` : ""}
            </span>
          ) : (
            <span>
              {data.users.length} юзер(ов){observed ? `, снимок ${observed}` : ""}
            </span>
          )}
        </div>
      </div>
      {data.users.length === 0 ? (
        <div className="text-slate-500">
          На ноде нет активных юзеров в последнем тике traffic-stats.
        </div>
      ) : (
        <table className="w-full">
          <thead className="text-left text-slate-500">
            <tr>
              <th className="py-1">Telegram / username</th>
              <th>Device</th>
              <th>План</th>
              <th>Истекает</th>
              <th>Протоколы</th>
            </tr>
          </thead>
          <tbody>
            {data.users.map((u) => {
              const orphan = u.device_id === null;
              return (
                <tr
                  key={u.access_username}
                  className={`border-t border-slate-800 ${
                    orphan ? "bg-yellow-950/30" : ""
                  }`}
                >
                  <td className="py-1">
                    {u.user_telegram_id ? (
                      <Link
                        to={`/users?telegram_id=${encodeURIComponent(u.user_telegram_id)}`}
                        className="text-blue-400 hover:underline font-mono"
                      >
                        {u.user_telegram_id}
                      </Link>
                    ) : (
                      <span
                        className="font-mono text-yellow-400"
                        title="username на ноде, в БД нет связанного device — вероятно, orphan после миграции"
                      >
                        {u.access_username}
                      </span>
                    )}
                  </td>
                  <td>
                    {u.device_id ? (
                      <Link
                        to={`/subscriptions?device_id=${u.device_id}`}
                        className="text-blue-400 hover:underline"
                      >
                        {u.device_name ?? `#${u.device_id}`}
                      </Link>
                    ) : (
                      <span className="text-slate-500">—</span>
                    )}
                  </td>
                  <td className="text-slate-300">
                    {u.plan_name ?? (u.plan_id ? `#${u.plan_id}` : "—")}
                  </td>
                  <td className="font-mono text-slate-400">
                    {u.subscription_expires_at
                      ? new Date(u.subscription_expires_at).toLocaleDateString()
                      : "—"}
                  </td>
                  <td className="font-mono text-slate-400">
                    {u.protocols.join(", ")}
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

// ── Traffic chart (SVG sparkline) ───────────────────────────────────

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function NodeTrafficChart({ nodeId }: { nodeId: number }) {
  const { data, isLoading, error } = useQuery<NodeTrafficHistoryOut>({
    queryKey: ["node-traffic-history", nodeId],
    queryFn: () => api.get(`/nodes/${nodeId}/traffic-history?hours=24`),
    refetchInterval: 60_000,
  });

  if (isLoading)
    return <div className="text-xs text-slate-500">Загрузка графика…</div>;
  if (error)
    return (
      <div className="text-xs text-red-400">{(error as Error).message}</div>
    );
  if (!data) return null;

  const samples = data.samples;
  if (samples.length === 0) {
    return (
      <div className="rounded border border-slate-700 p-3 text-xs text-slate-500">
        Нет данных traffic-stats за последние 24 часа.
      </div>
    );
  }

  // Layout constants
  const W = 720;
  const H = 120;
  const padL = 32;
  const padR = 48;
  const padT = 12;
  const padB = 20;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  const fromMs = new Date(data.from_ts).getTime();
  const toMs = new Date(data.to_ts).getTime();
  const spanMs = Math.max(1, toMs - fromMs);

  const maxUsers = Math.max(1, ...samples.map((s) => s.active_users));
  const maxTraffic = Math.max(
    1,
    ...samples.map((s) => s.uplink_bytes + s.downlink_bytes),
  );

  const xFor = (iso: string) => {
    const t = new Date(iso).getTime();
    return padL + ((t - fromMs) / spanMs) * plotW;
  };
  const yForUsers = (u: number) =>
    padT + plotH - (u / maxUsers) * plotH;
  const yForTraffic = (b: number) =>
    padT + plotH - (b / maxTraffic) * plotH;

  const usersPath = samples
    .map((s, i) => `${i === 0 ? "M" : "L"}${xFor(s.observed_at).toFixed(1)},${yForUsers(s.active_users).toFixed(1)}`)
    .join(" ");
  const trafficPath = samples
    .map(
      (s, i) =>
        `${i === 0 ? "M" : "L"}${xFor(s.observed_at).toFixed(1)},${yForTraffic(
          s.uplink_bytes + s.downlink_bytes,
        ).toFixed(1)}`,
    )
    .join(" ");

  // Vertical gridlines every 6 hours
  const gridTimes: number[] = [];
  const step = 6 * 60 * 60 * 1000;
  for (let t = Math.ceil(fromMs / step) * step; t <= toMs; t += step) {
    gridTimes.push(t);
  }

  return (
    <div className="rounded border border-slate-700 p-3 text-xs">
      <div className="flex items-center justify-between mb-2">
        <div className="text-xs uppercase tracking-wide text-slate-400">
          Трафик и юзеры за 24ч
        </div>
        <div className="flex gap-4 text-[11px] text-slate-500">
          <span>
            <span className="inline-block w-3 h-0.5 bg-emerald-400 align-middle mr-1" />
            active_users (max {maxUsers})
          </span>
          <span>
            <span className="inline-block w-3 h-0.5 bg-blue-400 align-middle mr-1" />
            traffic up+down (max {formatBytes(maxTraffic)})
          </span>
        </div>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="w-full"
        style={{ height: H }}
      >
        {/* X-axis grid */}
        {gridTimes.map((t) => {
          const x = padL + ((t - fromMs) / spanMs) * plotW;
          const label = new Date(t).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
          });
          return (
            <g key={t}>
              <line
                x1={x}
                x2={x}
                y1={padT}
                y2={padT + plotH}
                stroke="#334155"
                strokeDasharray="2,3"
              />
              <text
                x={x}
                y={H - 4}
                fill="#64748b"
                fontSize="10"
                textAnchor="middle"
              >
                {label}
              </text>
            </g>
          );
        })}
        {/* Plot frame */}
        <rect
          x={padL}
          y={padT}
          width={plotW}
          height={plotH}
          fill="none"
          stroke="#1e293b"
        />
        {/* Y-axis labels */}
        <text x={4} y={padT + 4} fill="#34d399" fontSize="10">
          {maxUsers}
        </text>
        <text x={4} y={padT + plotH} fill="#34d399" fontSize="10">
          0
        </text>
        <text
          x={W - 4}
          y={padT + 4}
          fill="#60a5fa"
          fontSize="10"
          textAnchor="end"
        >
          {formatBytes(maxTraffic)}
        </text>
        <text
          x={W - 4}
          y={padT + plotH}
          fill="#60a5fa"
          fontSize="10"
          textAnchor="end"
        >
          0
        </text>
        {/* Lines */}
        <path d={trafficPath} fill="none" stroke="#60a5fa" strokeWidth="1.5" />
        <path d={usersPath} fill="none" stroke="#34d399" strokeWidth="1.5" />
        {/* Hover tooltips via <title> on point circles */}
        {samples.map((s, i) => (
          <circle
            key={i}
            cx={xFor(s.observed_at)}
            cy={yForUsers(s.active_users)}
            r={2}
            fill="#34d399"
          >
            <title>
              {new Date(s.observed_at).toLocaleString()}
              {"\n"}users: {s.active_users}
              {"\n"}up: {formatBytes(s.uplink_bytes)}
              {"\n"}down: {formatBytes(s.downlink_bytes)}
            </title>
          </circle>
        ))}
      </svg>
    </div>
  );
}

// Дефолты под каждый протокол — совпадают с тем, что ставит ansible
// по дефолту (см. install_vless_reality/defaults). Если операторы
// начнут менять порты в ролях — синхронизировать здесь.
// shadowtls+shadowsocks убран из UI (0.2), hysteria2 убран (0.3).
// Легаси-типы остаются в VPNConfigProtocol для строк со старых нод.
type CreatableProtocol = Exclude<
  VPNConfigProtocol,
  "shadowtls+shadowsocks" | "hysteria2"
>;
const PROTOCOL_DEFAULTS: Record<
  CreatableProtocol,
  { port: number; sni: string; name: string }
> = {
  "vless-reality": { port: 9443, sni: "www.asus.com", name: "vless-reality" },
  "vless-ws-cdn": { port: 443, sni: "", name: "vless-ws-cdn" },
  "vless-xhttp": { port: 443, sni: "", name: "vless-xhttp" },
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
  const [protocol, setProtocol] = useState<CreatableProtocol>("vless-reality");
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
  function onProtocolChange(p: CreatableProtocol) {
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
          onChange={(e) => onProtocolChange(e.target.value as CreatableProtocol)}
        >
          <option value="vless-reality">vless-reality</option>
          <option value="vless-ws-cdn">vless-ws-cdn</option>
          <option value="vless-xhttp">vless-xhttp</option>
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
        Host ноды: <span className="font-mono">{nodeHost}</span>. Для
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
  diagnose: "Диагностика",
  diagnose_link: "Диагностика link",
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
      {op.kind === "diagnose_link" && (
        <DiagnoseLinkResultPane op={op} task={related[0]} />
      )}
    </div>
  );
}

// Структурированный блок результата под progress-bar для kind=diagnose_link.
// Показывает карточки `checks` из task.result. Пока task ещё running —
// отображает spinner-аналог; после completion — раскладку DiagnoseResult.
function DiagnoseLinkResultPane({
  op,
  task,
}: {
  op: TrackedOp;
  task: ProvisioningTaskOut | undefined;
}) {
  if (!task) {
    return (
      <div className="mt-3 text-xs text-slate-400 italic">
        Ждём, когда воркер подхватит таску #{op.taskIds[0]}…
      </div>
    );
  }
  if (task.status === "pending" || task.status === "running") {
    return (
      <div className="mt-3 text-xs text-slate-400 italic">
        Прогоняется ansible на ноде {op.nodeName}
        {op.exitName ? ` (link → ${op.exitName})` : ""}…
      </div>
    );
  }
  const result = (task.result ?? {}) as {
    checks?: DiagnoseCheckEntry[];
    diagnose_meta?: DiagnoseMeta;
    stdout?: string;
    stderr?: string;
    returncode?: number;
  };
  const checks = result.checks ?? [];
  return (
    <div className="mt-3 space-y-2">
      <div className="text-xs text-slate-300">
        Link → <span className="font-mono">{op.exitName ?? `exit#${op.linkId}`}</span>
        {result.diagnose_meta && (
          <span className="text-slate-500">
            {" "}· iface {result.diagnose_meta.wg_interface}
          </span>
        )}
      </div>
      <DiagnoseResult checks={checks} meta={result.diagnose_meta} />
      {(result.stdout || result.stderr) && (
        <details className="text-xs text-slate-400">
          <summary className="cursor-pointer hover:text-slate-200">
            raw ansible stdout / stderr (для отладки)
          </summary>
          {result.stdout && (
            <pre className="mt-1 bg-black/40 p-2 rounded font-mono text-[10px] overflow-x-auto whitespace-pre-wrap text-slate-300">
              {result.stdout}
            </pre>
          )}
          {result.stderr && (
            <pre className="mt-1 bg-black/40 p-2 rounded font-mono text-[10px] overflow-x-auto whitespace-pre-wrap text-red-300">
              {result.stderr}
            </pre>
          )}
        </details>
      )}
    </div>
  );
}

// Health-ping stats widget for the node expand-row. Shows 24ч summary;
// клик ведёт на /health-pings?node_id=... с автофильтром recent-bad.
function NodeHealthPings({ nodeId }: { nodeId: number }) {
  const { data, isLoading, error } = useQuery<NodeHealthPingStatsOut>({
    queryKey: ["node-health-pings", nodeId],
    queryFn: () => api.get(`/nodes/${nodeId}/health-pings?hours=24`),
    refetchInterval: 120_000,
  });

  if (isLoading)
    return (
      <div className="text-xs text-slate-500">Загрузка health-pings…</div>
    );
  if (error)
    return (
      <div className="text-xs text-red-400">{(error as Error).message}</div>
    );
  if (!data) return null;

  const resp = data.ok + data.bad;
  const ratio = data.bad_ratio;
  const toneCls =
    resp === 0
      ? "border-slate-700 text-slate-500"
      : ratio === 0
        ? "border-emerald-700 text-emerald-300"
        : ratio <= 0.2
          ? "border-yellow-700 text-yellow-300"
          : "border-red-700 text-red-300";

  const lastBadLabel = data.last_bad_at
    ? new Date(data.last_bad_at).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;

  return (
    <Link
      to={`/health-pings?node_id=${nodeId}`}
      className={`block rounded border p-3 text-xs hover:bg-slate-800/60 ${toneCls}`}
    >
      <div className="flex items-center justify-between">
        <div>
          <span className="uppercase tracking-wide mr-2">
            Health-pings 24ч
          </span>
          {resp === 0 ? (
            <span>нет ответов</span>
          ) : (
            <span>
              {data.ok} ok / {data.bad} bad ({(ratio * 100).toFixed(0)}% bad)
            </span>
          )}
          {lastBadLabel && (
            <span className="ml-3 text-slate-400">
              · последняя жалоба {lastBadLabel}
            </span>
          )}
        </div>
        <span className="text-slate-400">детали →</span>
      </div>
    </Link>
  );
}
