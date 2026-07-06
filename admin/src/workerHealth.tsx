import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api";

// Светофор для статуса воркера + таблица ticks по клику. Админ видит,
// жив ли scheduler (хотя бы один worker с heartbeat моложе 90с и ни
// один tick не overdue), и если что-то красное — сразу понятно: либо
// перезапуск воркера (для bootstrap replace=True), либо Redis.

export interface TickStatusItem {
  tick_id: string;
  func_name: string;
  interval_seconds: number;
  job_status: string;
  enqueued_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  scheduled_for: string | null;
  overdue_by_seconds: number | null;
  last_exc_type: string | null;
}

export interface WorkerInfo {
  name: string;
  state: string;
  last_heartbeat: string | null;
  current_job_id: string | null;
}

export interface TicksStatusOut {
  queue_available: boolean;
  workers: WorkerInfo[];
  ticks: TickStatusItem[];
}

type Severity = "ok" | "warn" | "down";

// Пороги подобраны так, чтобы светофор не орал раньше чем тик реально
// пропустил свой слот. RQ heartbeat по default раз в 60с — 180с = три
// пропущенных, уверенный признак что воркер действительно мёртв, а не
// просто занят тяжёлым provisioning'ом.
const WORKER_DEAD_MS = 180_000;
// Базовый интервал опроса статуса. При ошибке опрашиваем реже (см. useQuery),
// а данные старше двух интервалов считаем протухшими — светофор тогда не
// имеет права показывать зелёный, потому что оценка построена на устаревшем
// снимке (backend/прокси/сеть могли отвалиться уже после него).
const REFETCH_MS = 15_000;
// Тик может быть overdue из-за того что воркер доделывает долгий job
// (ansible-run 2-3 мин). Триггерим warn только если пропустили >2 цикла
// + 60с запас — тогда это не "занят", а реально scheduler не кикает.
function tickOverdueThreshold(intervalSeconds: number): number {
  return intervalSeconds * 3 + 60;
}

function severity(
  data: TicksStatusOut | undefined,
  monitoringDown = false,
): {
  s: Severity;
  label: string;
} {
  // Мониторинг ослеп (запрос падает или последний удачный ответ протух) —
  // это важнее любой сохранённой оценки. React-Query держит последний data
  // даже когда запрос уже стабильно падает, поэтому без этой проверки бейдж
  // рисовал бы зелёный «Worker OK» во время аварии backend.
  if (monitoringDown) return { s: "down", label: "Нет связи с backend" };
  if (!data) return { s: "down", label: "…" };
  if (!data.queue_available) return { s: "down", label: "Redis down" };

  const now = Date.now();
  const liveWorkers = data.workers.filter((w) => {
    if (!w.last_heartbeat) return false;
    const ageMs = now - new Date(w.last_heartbeat).getTime();
    return ageMs < WORKER_DEAD_MS;
  });
  if (liveWorkers.length === 0) {
    const anyHb = data.workers.some((w) => w.last_heartbeat);
    return { s: "down", label: anyHb ? "Worker stalled" : "No workers" };
  }

  const overdueTick = data.ticks.find(
    (t) =>
      t.interval_seconds > 0 &&
      t.overdue_by_seconds != null &&
      t.overdue_by_seconds > tickOverdueThreshold(t.interval_seconds),
  );
  if (overdueTick) {
    return { s: "warn", label: `Tick overdue: ${overdueTick.tick_id}` };
  }

  return { s: "ok", label: `Worker OK (${liveWorkers.length})` };
}

function fmtAge(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 0) {
    const future = Math.abs(ms);
    const s = Math.round(future / 1000);
    if (s < 60) return `через ${s}с`;
    return `через ${Math.round(s / 60)}м`;
  }
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}с назад`;
  if (s < 3600) return `${Math.round(s / 60)}м назад`;
  return `${Math.round(s / 3600)}ч назад`;
}

export function WorkerHealthBadge() {
  const [open, setOpen] = useState(false);
  // Желаемое число реплик; null = "следовать текущему счётчику воркеров".
  const [desired, setDesired] = useState<number | null>(null);
  const qc = useQueryClient();
  const { data, isError, dataUpdatedAt } = useQuery<TicksStatusOut>({
    queryKey: ["ops-ticks-status"],
    queryFn: () => api.get("/ops/ticks/status"),
    // На ошибке опрашиваем вдвое реже, чтобы не долбить упавший backend
    // двумя запросами каждые 15с; в норме — штатный интервал.
    refetchInterval: (q) => (q.state.error ? REFETCH_MS * 2 : REFETCH_MS),
    retry: false,
  });

  // Данные старше двух интервалов — протухшие: даже без явной ошибки последний
  // ответ уже не отражает текущее состояние (refetch мог начать падать).
  const isStale =
    dataUpdatedAt > 0 && Date.now() - dataUpdatedAt > REFETCH_MS * 2;
  const monitoringDown = isError || isStale;

  const restart = useMutation({
    mutationFn: () =>
      api.post<{ signalled: string[]; failed: string[] }>(
        "/ops/worker/restart",
        {},
      ),
    onSuccess: (res) => {
      const ok = res.signalled.length;
      const bad = res.failed.length;
      alert(
        bad
          ? `Сигнал shutdown отправлен ${ok} воркер(ам), ошибок: ${bad}\n${res.failed.join("\n")}`
          : `Сигнал shutdown отправлен ${ok} воркер(ам). Docker перезапустит их автоматически.`,
      );
      // Дать воркеру секунд 20 на graceful exit + рестарт контейнера, потом
      // прогоним опрос заново чтобы светофор отразил новую жизнь.
      setTimeout(
        () => qc.invalidateQueries({ queryKey: ["ops-ticks-status"] }),
        20_000,
      );
    },
    onError: (e: Error) => alert(`Не удалось: ${e.message}`),
  });

  const scale = useMutation({
    mutationFn: (replicas: number) =>
      api.post<{ replicas: number; status: string; detail: string | null }>(
        "/ops/worker/scale",
        { replicas },
      ),
    onSuccess: (res) => {
      if (res.status === "applied") {
        alert(
          `Воркеры: установлено ${res.replicas} реплик.\n${res.detail ?? ""}`,
        );
      } else if (res.status === "enqueued") {
        alert(
          `Скейл до ${res.replicas} запущен — счётчик обновится через ~20 сек.`,
        );
      } else {
        alert(
          `Не удалось отскейлить до ${res.replicas}:\n${res.detail ?? "ошибка"}`,
        );
      }
      setDesired(null);
      setTimeout(
        () => qc.invalidateQueries({ queryKey: ["ops-ticks-status"] }),
        20_000,
      );
    },
    onError: (e: Error) => alert(`Не удалось: ${e.message}`),
  });

  const { s, label } = severity(data, monitoringDown);
  const colorClass =
    s === "ok"
      ? "bg-emerald-700 hover:bg-emerald-600"
      : s === "warn"
        ? "bg-amber-700 hover:bg-amber-600"
        : "bg-red-800 hover:bg-red-700";

  return (
    <div className="relative inline-block">
      <button
        onClick={() => setOpen((v) => !v)}
        className={`text-xs px-2 py-1 rounded text-white ${colorClass}`}
        title="Состояние RQ-воркера и periodic ticks. Клик — детали."
      >
        ● {label}
      </button>
      {open && data && (
        <div
          className="absolute right-0 top-full mt-1 w-[640px] max-w-[95vw] bg-slate-900 border border-slate-700 rounded-lg p-3 z-50 shadow-xl text-xs space-y-2"
        >
          <div className="flex justify-between items-center">
            <span className="font-semibold text-slate-200">Workers ({data.workers.length})</span>
            <div className="flex gap-2 items-center">
              <div
                className="flex items-center gap-0.5"
                title="Число worker-контейнеров. OK гонит docker compose --scale на mgmt-хосте (через SSH воркера)."
              >
                <span className="text-[11px] text-slate-400 mr-1">реплик</span>
                <button
                  onClick={() =>
                    setDesired(Math.max(1, (desired ?? data.workers.length) - 1))
                  }
                  disabled={
                    scale.isPending || (desired ?? data.workers.length) <= 1
                  }
                  className="text-[11px] px-1.5 py-0.5 rounded bg-slate-700 hover:bg-slate-600 text-white disabled:opacity-40"
                >
                  −
                </button>
                <span className="text-[11px] w-5 text-center font-mono text-slate-200">
                  {desired ?? data.workers.length}
                </span>
                <button
                  onClick={() =>
                    setDesired(
                      Math.min(20, (desired ?? data.workers.length) + 1),
                    )
                  }
                  disabled={
                    scale.isPending || (desired ?? data.workers.length) >= 20
                  }
                  className="text-[11px] px-1.5 py-0.5 rounded bg-slate-700 hover:bg-slate-600 text-white disabled:opacity-40"
                >
                  +
                </button>
                <button
                  onClick={() => {
                    const target = desired ?? data.workers.length;
                    if (
                      target !== data.workers.length &&
                      confirm(
                        `Отскейлить воркеры ${data.workers.length} → ${target}? ` +
                          `Выполнит docker compose --scale worker=${target} на mgmt-хосте.`,
                      )
                    ) {
                      scale.mutate(target);
                    }
                  }}
                  disabled={
                    scale.isPending ||
                    (desired ?? data.workers.length) === data.workers.length
                  }
                  className="text-[11px] px-2 py-0.5 rounded bg-sky-700 hover:bg-sky-600 text-white disabled:opacity-40 disabled:cursor-not-allowed ml-0.5"
                  title="Применить число реплик (SSH воркера → docker compose --scale на mgmt)"
                >
                  {scale.isPending ? "…" : "OK"}
                </button>
              </div>
              <button
                onClick={() => {
                  if (
                    confirm(
                      "Рестартовать воркер? Graceful shutdown — текущий job доработает до конца, docker поднимет контейнер заново (~20 сек). Юзеры не увидят обрыва.",
                    )
                  ) {
                    restart.mutate();
                  }
                }}
                disabled={restart.isPending || data.workers.length === 0}
                className="text-[11px] px-2 py-0.5 rounded bg-amber-700 hover:bg-amber-600 text-white disabled:opacity-50 disabled:cursor-not-allowed"
                title="Graceful RQ shutdown. Docker с restart: unless-stopped поднимет воркер заново, bootstrap переставит тики."
              >
                {restart.isPending ? "…" : "↻ Рестарт"}
              </button>
              <button
                onClick={() => setOpen(false)}
                className="text-slate-400 hover:text-slate-200"
              >
                ✕
              </button>
            </div>
          </div>
          {data.workers.length === 0 ? (
            <div className="text-red-400">
              Нет зарегистрированных workers. Воркер-контейнер не запущен или Redis
              не видит heartbeat'а.
            </div>
          ) : (
            <table className="w-full text-[11px]">
              <thead className="text-slate-400">
                <tr>
                  <th className="text-left py-1">Name</th>
                  <th className="text-left py-1">State</th>
                  <th className="text-left py-1">Heartbeat</th>
                  <th className="text-left py-1">Current job</th>
                </tr>
              </thead>
              <tbody>
                {data.workers.map((w) => (
                  <tr key={w.name} className="border-t border-slate-800">
                    <td className="py-1 font-mono">{w.name}</td>
                    <td className="py-1">{w.state}</td>
                    <td className="py-1 text-slate-400">{fmtAge(w.last_heartbeat)}</td>
                    <td className="py-1 font-mono text-slate-500">
                      {w.current_job_id ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="font-semibold text-slate-200 pt-1 border-t border-slate-800">
            Ticks ({data.ticks.length})
          </div>
          <table className="w-full text-[11px]">
            <thead className="text-slate-400">
              <tr>
                <th className="text-left py-1">Tick</th>
                <th className="text-left py-1">Interval</th>
                <th className="text-left py-1">Status</th>
                <th className="text-left py-1">Next run</th>
                <th className="text-left py-1">Overdue</th>
              </tr>
            </thead>
            <tbody>
              {data.ticks.map((t) => {
                const statusColor =
                  t.job_status === "started"
                    ? "text-emerald-400"
                    : t.job_status === "scheduled" || t.job_status === "queued"
                      ? t.overdue_by_seconds != null && t.overdue_by_seconds > tickOverdueThreshold(t.interval_seconds)
                        ? "text-red-400"
                        : "text-emerald-400"
                      : t.job_status === "failed"
                        ? "text-red-400"
                        : t.job_status === "missing"
                          ? "text-amber-400"
                          : "text-slate-400";
                return (
                  <tr key={t.tick_id} className="border-t border-slate-800">
                    <td className="py-1 font-mono">{t.tick_id}</td>
                    <td className="py-1 text-slate-400">{t.interval_seconds}s</td>
                    <td className={`py-1 font-medium ${statusColor}`}>{t.job_status}</td>
                    <td className="py-1 text-slate-400">{fmtAge(t.scheduled_for)}</td>
                    <td className="py-1">
                      {t.overdue_by_seconds == null ? (
                        "—"
                      ) : t.overdue_by_seconds > tickOverdueThreshold(t.interval_seconds) ? (
                        <span className="text-red-400 font-medium">
                          +{t.overdue_by_seconds}s
                        </span>
                      ) : (
                        <span className="text-slate-500">
                          {t.overdue_by_seconds > 0 ? `+${t.overdue_by_seconds}s` : "0"}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          {severity(data, monitoringDown).s !== "ok" && (
            <div className="text-[11px] text-amber-300 bg-amber-950/30 border border-amber-900/40 p-2 rounded">
              Если тики overdue / worker stalled — перезапусти воркер-контейнер.
              Bootstrap теперь использует replace=True, так что stale scheduled-job
              снесётся и тик поставится заново.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
