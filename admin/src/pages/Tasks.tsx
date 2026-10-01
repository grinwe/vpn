import { Fragment, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  DiagnoseCheckEntry,
  DiagnoseMeta,
  ProvisioningTaskOut,
} from "../api";
import { DiagnoseResult } from "../diagnoseResult";

const STATUSES = ["", "pending", "running", "success", "failed", "cancelled"] as const;
const TARGETS = ["", "node", "device", "subscription"] as const;
const EXPECTED_TICKS = 8;

type BatchResult = { ok: number[]; skipped: number[]; not_found: number[] };

type QueueTick = {
  tick_id: string;
  func: string;
  status: string | null;
  enqueued_at: string | null;
};

type QueueWorker = {
  name: string;
  state: string;
  current_job_id: string | null;
};

type QueueStatus = {
  redis_available: boolean;
  queue_name: string;
  queued: number;
  started: number;
  scheduled: number;
  deferred: number;
  failed: number;
  finished: number;
  workers: QueueWorker[];
  ticks: QueueTick[];
  stuck_tasks: number;
};

type ResetStuckResult = {
  queue_available: boolean;
  requeued: number;
  failed: number;
  pending_requeued: number;
};

function tickHealthy(t: QueueTick): boolean {
  return t.status === "scheduled" || t.status === "queued" || t.status === "started";
}

function QueueBanner() {
  const qc = useQueryClient();
  const { data, error, refetch } = useQuery<QueueStatus>({
    queryKey: ["provisioning-queue-status"],
    queryFn: () => api.get("/provisioning/queue-status"),
    refetchInterval: (q) => (q.state.error ? false : 15_000),
    retry: false,
  });

  const resetStuck = useMutation({
    mutationFn: () =>
      api.post<ResetStuckResult>("/provisioning/queue/reset-stuck", {}),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["provisioning-queue-status"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      alert(
        `Reset-stuck: requeued=${res.requeued}, failed=${res.failed}, ` +
          `pending_requeued=${res.pending_requeued}, queue_available=${res.queue_available}.`,
      );
    },
    onError: (e: Error) => alert(`Reset-stuck ошибка: ${e.message}`),
  });

  if (error) {
    return (
      <div className="mb-4 p-3 rounded border border-red-700 bg-red-900/30 text-sm">
        <div className="text-red-300 font-semibold">Queue-status недоступен</div>
        <div className="text-red-400/80 mt-1 font-mono text-xs break-all">
          {String(error)}
        </div>
        <button
          onClick={() => refetch()}
          className="mt-2 text-xs px-2 py-1 rounded bg-red-800 hover:bg-red-700"
        >
          Повторить
        </button>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="mb-4 p-3 rounded border border-slate-700 bg-slate-900/50 text-sm text-slate-400">
        Загружаем состояние очереди…
      </div>
    );
  }

  const healthyTicks = data.ticks.filter(tickHealthy).length;
  const totalTicks = data.ticks.length;
  const tickDrift = totalTicks > 0 && healthyTicks < totalTicks;
  const scheduledDrift = data.scheduled > EXPECTED_TICKS * 3;
  const critical = !data.redis_available || data.stuck_tasks > 0;
  const warning = !critical && (tickDrift || scheduledDrift || data.failed > 0);

  const borderClass = critical
    ? "border-red-700 bg-red-900/20"
    : warning
    ? "border-amber-700 bg-amber-900/20"
    : "border-slate-700 bg-slate-900/40";

  const badge = (label: string, value: string | number, tone?: "red" | "amber") => {
    const toneClass =
      tone === "red"
        ? "bg-red-900/40 text-red-300 border-red-800"
        : tone === "amber"
        ? "bg-amber-900/40 text-amber-200 border-amber-800"
        : "bg-slate-800/80 text-slate-300 border-slate-700";
    return (
      <span
        key={label}
        className={`text-xs px-2 py-0.5 rounded border ${toneClass}`}
      >
        <span className="text-slate-400 mr-1">{label}:</span>
        {value}
      </span>
    );
  };

  const resetDisabled = resetStuck.isPending || !data.redis_available;

  return (
    <div className={`mb-4 p-3 rounded border text-sm ${borderClass}`}>
      <div className="flex items-center flex-wrap gap-2">
        <span className="text-xs uppercase text-slate-400 mr-1">Queue</span>
        {badge(
          "redis",
          data.redis_available ? "OK" : "DOWN",
          data.redis_available ? undefined : "red",
        )}
        {badge("queued", data.queued)}
        {badge("running", data.started)}
        {badge("scheduled", data.scheduled, scheduledDrift ? "amber" : undefined)}
        {badge("failed", data.failed, data.failed > 0 ? "amber" : undefined)}
        {badge(
          "stuck (db)",
          data.stuck_tasks,
          data.stuck_tasks > 0 ? "red" : undefined,
        )}
        {badge("workers", data.workers.length)}
        {badge(
          "ticks",
          `${healthyTicks}/${totalTicks}`,
          tickDrift ? "amber" : undefined,
        )}

        <button
          disabled={resetDisabled}
          onClick={() => {
            if (
              confirm(
                `Запустить reset-stuck?\n\n` +
                  `Работает как startup-recovery: таски в running будут переведены в pending и переотправлены в RQ (если redis доступен) или помечены failed. Заодно заново заэнкьюит все pending строки. Безопасно для живого воркера — DDL не трогается, но это spike ansible-нагрузки.`,
              )
            )
              resetStuck.mutate();
          }}
          className="text-xs px-2 py-1 rounded bg-amber-700 hover:bg-amber-600 disabled:opacity-50 ml-auto"
          title={
            data.redis_available
              ? "Перепушить застрявшие таски"
              : "Redis недоступен — нечего переэнкьюить"
          }
        >
          {resetStuck.isPending ? "…" : "reset-stuck"}
        </button>
      </div>

      {(tickDrift || scheduledDrift) && (
        <div className="mt-2 text-xs text-slate-400">
          {tickDrift && (
            <div>
              Не все ticks живы:{" "}
              {data.ticks
                .filter((t) => !tickHealthy(t))
                .map((t) => `${t.tick_id} (${t.status ?? "null"})`)
                .join(", ")}
            </div>
          )}
          {scheduledDrift && (
            <div>
              scheduled={data.scheduled} — похоже на зомби-цепочки. Проверь{" "}
              <code>rq info</code> и перезапусти воркер.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function statusColor(s: string): string {
  switch (s) {
    case "success":
      return "text-emerald-400";
    case "failed":
      return "text-red-400";
    case "running":
      return "text-blue-400";
    case "pending":
      return "text-yellow-400";
    case "cancelled":
      return "text-orange-400";
    default:
      return "text-slate-400";
  }
}

function fmtDuration(start: string | null, end: string | null): string {
  if (!start) return "—";
  const s = new Date(start).getTime();
  const e = end ? new Date(end).getTime() : Date.now();
  const secs = Math.max(0, Math.round((e - s) / 1000));
  if (secs < 60) return `${secs}s`;
  return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

function TaskDetails({ task }: { task: ProvisioningTaskOut }) {
  // Structured diagnose-результат лежит в task.result.checks (массив
  // DiagnoseCheckEntry, дописанный ansible-ролью + orchestrator'ом). Для
  // обычных (non-diagnose) тасок поля нет — рендерим только raw result.
  const diagnoseChecks = task.result?.checks;
  const checks: DiagnoseCheckEntry[] | null = Array.isArray(diagnoseChecks)
    ? (diagnoseChecks as DiagnoseCheckEntry[])
    : null;
  const diagnoseMeta = (task.result?.diagnose_meta ?? undefined) as
    | DiagnoseMeta
    | undefined;

  return (
    <div className="bg-slate-950 border border-slate-800 rounded p-3 space-y-3 text-xs">
      <div className="grid grid-cols-2 gap-4">
        <div>
          <div className="text-slate-500 uppercase text-[10px]">Создан</div>
          <div>{new Date(task.created_at).toLocaleString()}</div>
        </div>
        <div>
          <div className="text-slate-500 uppercase text-[10px]">Длительность</div>
          <div>{fmtDuration(task.started_at, task.finished_at)}</div>
        </div>
      </div>

      {task.error_message && (
        <div>
          <div className="text-slate-500 uppercase text-[10px] mb-1">
            error_message
          </div>
          <pre className="bg-black/40 p-2 rounded whitespace-pre-wrap break-words max-h-96 overflow-auto font-mono">
            {task.error_message}
          </pre>
        </div>
      )}

      {task.payload && (
        <details>
          <summary className="cursor-pointer text-slate-400 uppercase text-[10px]">
            payload
          </summary>
          <pre className="bg-black/40 p-2 rounded whitespace-pre-wrap break-words max-h-64 overflow-auto font-mono mt-1">
            {JSON.stringify(task.payload, null, 2)}
          </pre>
        </details>
      )}

      {checks && (
        <div>
          <div className="text-slate-500 uppercase text-[10px] mb-1">
            диагностика
          </div>
          <DiagnoseResult checks={checks} meta={diagnoseMeta} />
        </div>
      )}

      {task.result && (
        // Когда есть structured checks — raw result схлопнут (детали выше),
        // иначе открыт по умолчанию, как раньше.
        <details open={!checks}>
          <summary className="cursor-pointer text-slate-400 uppercase text-[10px]">
            result (stdout/stderr/ansible exit)
          </summary>
          <pre className="bg-black/40 p-2 rounded whitespace-pre-wrap break-words max-h-96 overflow-auto font-mono mt-1">
            {JSON.stringify(task.result, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}

export default function Tasks() {
  const qc = useQueryClient();
  const [status, setStatus] = useState<(typeof STATUSES)[number]>("");
  const [target, setTarget] = useState<(typeof TARGETS)[number]>("");
  const [limit, setLimit] = useState(50);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [search, setSearch] = useState("");
  const [tgFilter, setTgFilter] = useState("");
  const [debouncedTg, setDebouncedTg] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());

  useEffect(() => {
    const t = setTimeout(() => setDebouncedTg(tgFilter), 300);
    return () => clearTimeout(t);
  }, [tgFilter]);

  const { data, isLoading, error, refetch, isFetching } = useQuery<
    ProvisioningTaskOut[]
  >({
    queryKey: ["provisioning-tasks", { status, target, limit, tg: debouncedTg }],
    queryFn: () => {
      const qs = new URLSearchParams({ limit: String(limit) });
      if (status) qs.set("status", status);
      if (target) qs.set("target_type", target);
      if (debouncedTg) qs.set("telegram_id", debouncedTg);
      return api.get(`/provisioning/tasks?${qs.toString()}`);
    },
    refetchInterval: (q) => (q.state.error ? false : 5_000),
    retry: false,
  });

  const rerun = useMutation({
    mutationFn: (id: number) =>
      api.post<ProvisioningTaskOut>(`/provisioning/tasks/${id}/rerun`, {}),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] }),
    onError: (e: Error) => alert(`Не удалось перезапустить: ${e.message}`),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.del(`/provisioning/tasks/${id}`),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] }),
    onError: (e: Error) => alert(`Не удалось удалить: ${e.message}`),
  });

  const cancel = useMutation({
    mutationFn: (id: number) =>
      api.post<ProvisioningTaskOut>(`/provisioning/tasks/${id}/cancel`, {}),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] }),
    onError: (e: Error) => alert(`Не удалось отменить: ${e.message}`),
  });

  const batch = useMutation({
    mutationFn: (args: { ids: number[]; action: string }) =>
      api.post<BatchResult>("/provisioning/tasks/batch", args),
    onSuccess: (res, vars) => {
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      alert(
        `${vars.action}: ${res.ok.length} ok` +
          (res.skipped.length ? `, ${res.skipped.length} пропущено (running)` : "") +
          (res.not_found.length ? `, ${res.not_found.length} не найдено` : ""),
      );
    },
    onError: (e: Error) => alert(`Batch ошибка: ${e.message}`),
  });

  const filtered = (data ?? []).filter((t) => {
    if (!search) return true;
    const needle = search.toLowerCase();
    return (
      t.action.toLowerCase().includes(needle) ||
      `${t.target_type}:${t.target_id}`.toLowerCase().includes(needle) ||
      String(t.id).includes(needle) ||
      (t.telegram_id ?? "").toLowerCase().includes(needle)
    );
  });

  const toggleSelect = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    if (selected.size === filtered.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(filtered.map((t) => t.id)));
    }
  };

  const selArr = Array.from(selected);
  const hasSelection = selArr.length > 0;

  return (
    <div>
      <QueueBanner />
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <h1 className="text-2xl font-semibold">Provisioning tasks</h1>

        <select
          value={status}
          onChange={(e) => {
            setStatus(e.target.value as typeof status);
            setSelected(new Set());
          }}
          className="px-2 py-1 rounded bg-slate-800 border border-slate-700 text-sm"
        >
          {STATUSES.map((s) => (
            <option key={s || "all"} value={s}>
              {s || "все статусы"}
            </option>
          ))}
        </select>

        <select
          value={target}
          onChange={(e) => {
            setTarget(e.target.value as typeof target);
            setSelected(new Set());
          }}
          className="px-2 py-1 rounded bg-slate-800 border border-slate-700 text-sm"
        >
          {TARGETS.map((t) => (
            <option key={t || "all"} value={t}>
              {t || "все типы"}
            </option>
          ))}
        </select>

        <select
          value={limit}
          onChange={(e) => setLimit(Number(e.target.value))}
          className="px-2 py-1 rounded bg-slate-800 border border-slate-700 text-sm"
        >
          {[20, 50, 100, 200].map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>

        <input
          type="text"
          placeholder="telegram_id"
          value={tgFilter}
          onChange={(e) => setTgFilter(e.target.value)}
          className="px-2 py-1 rounded bg-slate-800 border border-slate-700 text-sm w-36"
        />

        <input
          type="text"
          placeholder="поиск (action / target / id)"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="px-2 py-1 rounded bg-slate-800 border border-slate-700 text-sm w-64"
        />

        <button
          onClick={() => refetch()}
          className="text-sm px-2 py-1 rounded bg-slate-800 hover:bg-slate-700"
        >
          {isFetching ? "…" : "↻"}
        </button>

        {hasSelection && (
          <div className="flex items-center gap-2 ml-auto">
            <span className="text-xs text-slate-400">
              {selArr.length} выбрано
            </span>
            <button
              disabled={batch.isPending}
              onClick={() => {
                if (confirm(`Перезапустить ${selArr.length} задач(у)?`))
                  batch.mutate({ ids: selArr, action: "rerun" });
              }}
              className="text-xs px-2 py-1 rounded bg-blue-700 hover:bg-blue-600 disabled:opacity-50"
            >
              rerun all
            </button>
            <button
              disabled={batch.isPending}
              onClick={() => {
                if (confirm(`Удалить ${selArr.length} задач(у)?`))
                  batch.mutate({ ids: selArr, action: "delete" });
              }}
              className="text-xs px-2 py-1 rounded bg-red-800 hover:bg-red-700 disabled:opacity-50"
            >
              delete all
            </button>
          </div>
        )}
      </div>

      {error && (
        <div className="mb-4 p-3 rounded border border-red-700 bg-red-900/30 text-sm">
          <div className="text-red-300 font-semibold">
            Не удалось загрузить список задач
          </div>
          <div className="text-red-400/80 mt-1 font-mono text-xs break-all">
            {String(error)}
          </div>
          <button
            onClick={() => refetch()}
            className="mt-2 text-xs px-2 py-1 rounded bg-red-800 hover:bg-red-700"
          >
            Повторить
          </button>
        </div>
      )}

      {isLoading ? (
        <div>Загрузка…</div>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-slate-400 border-b border-slate-700">
            <tr>
              <th className="py-2 w-8">
                <input
                  type="checkbox"
                  checked={filtered.length > 0 && selected.size === filtered.length}
                  onChange={toggleAll}
                  className="accent-blue-600"
                />
              </th>
              <th className="w-8"></th>
              <th>ID</th>
              <th>Target</th>
              <th>Telegram</th>
              <th>Action</th>
              <th>Статус</th>
              <th>Длит.</th>
              <th>Создан</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((t) => {
              const isOpen = expanded === t.id;
              return (
                <Fragment key={t.id}>
                  <tr
                    className={`border-b border-slate-800 hover:bg-slate-800 cursor-pointer ${
                      selected.has(t.id) ? "bg-blue-950/30" : ""
                    }`}
                  >
                    <td
                      className="py-2"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <input
                        type="checkbox"
                        checked={selected.has(t.id)}
                        onChange={() => toggleSelect(t.id)}
                        className="accent-blue-600"
                      />
                    </td>
                    <td
                      className="text-slate-500"
                      onClick={() => setExpanded(isOpen ? null : t.id)}
                    >
                      {isOpen ? "▼" : "▶"}
                    </td>
                    <td onClick={() => setExpanded(isOpen ? null : t.id)}>
                      {t.id}
                    </td>
                    <td onClick={() => setExpanded(isOpen ? null : t.id)}>
                      {t.target_type}:{t.target_id}
                    </td>
                    <td
                      className="font-mono text-xs text-slate-400"
                      onClick={() => setExpanded(isOpen ? null : t.id)}
                    >
                      {t.telegram_id ?? "—"}
                    </td>
                    <td
                      className="font-mono text-xs"
                      onClick={() => setExpanded(isOpen ? null : t.id)}
                    >
                      {t.action}
                    </td>
                    <td
                      className={statusColor(t.status)}
                      onClick={() => setExpanded(isOpen ? null : t.id)}
                    >
                      {t.status}
                    </td>
                    <td
                      className="text-slate-400"
                      onClick={() => setExpanded(isOpen ? null : t.id)}
                    >
                      {fmtDuration(t.started_at, t.finished_at)}
                    </td>
                    <td
                      className="text-slate-400"
                      onClick={() => setExpanded(isOpen ? null : t.id)}
                    >
                      {new Date(t.created_at).toLocaleString()}
                    </td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <div className="flex gap-1">
                        {(t.status === "pending" || t.status === "running") && (
                          <button
                            disabled={cancel.isPending || !!t.cancel_requested_at}
                            onClick={() => {
                              if (
                                confirm(
                                  `Отменить задачу #${t.id} (${t.action} на ${t.target_type}:${t.target_id})?` +
                                    (t.status === "running"
                                      ? " Идущий ansible получит SIGTERM."
                                      : "")
                                )
                              )
                                cancel.mutate(t.id);
                            }}
                            className="text-xs px-2 py-1 rounded bg-orange-700 hover:bg-orange-600 disabled:opacity-50"
                          >
                            {t.cancel_requested_at ? "отменяется…" : "cancel"}
                          </button>
                        )}
                        {t.status !== "running" && (
                          <button
                            disabled={rerun.isPending}
                            onClick={() => {
                              if (
                                confirm(
                                  `Перезапустить задачу #${t.id} (${t.action} на ${t.target_type}:${t.target_id})?`
                                )
                              )
                                rerun.mutate(t.id);
                            }}
                            className="text-xs px-2 py-1 rounded bg-blue-700 hover:bg-blue-600 disabled:opacity-50"
                          >
                            rerun
                          </button>
                        )}
                        {t.status !== "running" && (
                          <button
                            disabled={del.isPending}
                            onClick={() => {
                              if (confirm(`Удалить задачу #${t.id}?`))
                                del.mutate(t.id);
                            }}
                            className="text-xs px-2 py-1 rounded bg-red-800 hover:bg-red-700 disabled:opacity-50"
                          >
                            del
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                  {isOpen && (
                    <tr className="border-b border-slate-800">
                      <td colSpan={10} className="py-3 px-2 bg-slate-900/50">
                        <TaskDetails task={t} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
            {filtered.length === 0 && (
              <tr>
                <td colSpan={10} className="py-4 text-slate-500 text-center">
                  Пусто
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      <p className="text-xs text-slate-500 mt-4">
        Поллится раз в 5 секунд. Клик по строке — раскрыть детали
        (error_message, payload, result). Кнопка <em>rerun</em> — на любой таск кроме running.
      </p>
    </div>
  );
}
