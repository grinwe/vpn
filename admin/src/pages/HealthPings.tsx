import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  api,
  HealthPingRecentBadOut,
  HealthPingSummaryOut,
  HealthPingTimeseriesPoint,
} from "../api";

// Period options — hours encoded in URL query ?hours=24|168|720.
// Bucket размер решает бэкенд: hour для <=168, day для >168.
const PERIODS: { label: string; hours: number }[] = [
  { label: "24ч", hours: 24 },
  { label: "7д", hours: 168 },
  { label: "30д", hours: 720 },
];
const DEFAULT_HOURS = 168;

function Card({
  title,
  value,
  tone = "default",
  hint,
}: {
  title: string;
  value: string | number;
  tone?: "default" | "warn" | "bad" | "good" | "danger";
  hint?: string;
}) {
  const toneCls =
    tone === "warn"
      ? "border-yellow-600"
      : tone === "bad"
        ? "border-red-600"
        : tone === "good"
          ? "border-emerald-600"
          : tone === "danger"
            // Self-report — самый сильный сигнал, выделяем заливкой.
            ? "border-red-500 bg-red-950/40"
            : "border-slate-700";
  return (
    <div className={`bg-slate-800 rounded-lg p-4 border ${toneCls}`}>
      <div className="text-xs uppercase text-slate-400">{title}</div>
      <div className="text-2xl font-semibold mt-1">{value}</div>
      {hint && <div className="text-[11px] text-slate-500 mt-1">{hint}</div>}
    </div>
  );
}

function HealthPingChart({
  points,
  bucket,
}: {
  points: HealthPingTimeseriesPoint[];
  bucket: string;
}) {
  // Layout mirrors NodeTrafficChart в Nodes.tsx для консистентности.
  const W = 720;
  const H = 160;
  const padL = 32;
  const padR = 16;
  const padT = 12;
  const padB = 22;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  if (points.length === 0) {
    return (
      <div className="rounded border border-slate-700 p-6 text-center text-sm text-slate-500">
        За выбранный период ответов не было — график пустой.
      </div>
    );
  }

  const fromMs = new Date(points[0].bucket_ts).getTime();
  const toMs = new Date(points[points.length - 1].bucket_ts).getTime();
  const spanMs = Math.max(1, toMs - fromMs);
  const maxY = Math.max(1, ...points.map((p) => p.ok + p.bad));

  const xFor = (iso: string) => {
    const t = new Date(iso).getTime();
    // Если всего одна точка — располагаем в центре.
    if (spanMs <= 1) return padL + plotW / 2;
    return padL + ((t - fromMs) / spanMs) * plotW;
  };
  const yFor = (v: number) => padT + plotH - (v / maxY) * plotH;

  // Stacked: bad внизу (красное), ok сверху (зелёное). Bad снизу потому
  // что он важнее — глаз оператора сразу уходит к "фундаменту" плохих ответов.
  const badPath = points
    .map(
      (p, i) =>
        `${i === 0 ? "M" : "L"}${xFor(p.bucket_ts).toFixed(1)},${yFor(p.bad).toFixed(1)}`,
    )
    .join(" ");
  const totalPath = points
    .map(
      (p, i) =>
        `${i === 0 ? "M" : "L"}${xFor(p.bucket_ts).toFixed(1)},${yFor(p.ok + p.bad).toFixed(1)}`,
    )
    .join(" ");

  return (
    <div className="rounded border border-slate-700 p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-xs uppercase tracking-wide text-slate-400">
          Ответы по времени (bucket: {bucket}, max {maxY} в бакете)
        </div>
        <div className="flex gap-4 text-[11px] text-slate-500">
          <span>
            <span className="inline-block w-3 h-0.5 bg-red-400 align-middle mr-1" />
            bad
          </span>
          <span>
            <span className="inline-block w-3 h-0.5 bg-emerald-400 align-middle mr-1" />
            ok (поверх bad)
          </span>
        </div>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="w-full"
        style={{ height: H }}
      >
        {/* Plot frame */}
        <rect
          x={padL}
          y={padT}
          width={plotW}
          height={plotH}
          fill="none"
          stroke="#1e293b"
        />
        {/* bad line */}
        <path
          d={badPath}
          stroke="#f87171"
          strokeWidth="2"
          fill="none"
        />
        {/* total (ok+bad) — поверх, чтобы видна была вся масса */}
        <path
          d={totalPath}
          stroke="#34d399"
          strokeWidth="2"
          fill="none"
        />
        {/* Точки с tooltip */}
        {points.map((p) => {
          const x = xFor(p.bucket_ts);
          return (
            <g key={p.bucket_ts}>
              <circle
                cx={x}
                cy={yFor(p.bad)}
                r={2.5}
                fill="#f87171"
              >
                <title>
                  {new Date(p.bucket_ts).toLocaleString()}: bad={p.bad}
                </title>
              </circle>
              <circle
                cx={x}
                cy={yFor(p.ok + p.bad)}
                r={2.5}
                fill="#34d399"
              >
                <title>
                  {new Date(p.bucket_ts).toLocaleString()}: ok={p.ok}, bad=
                  {p.bad}
                </title>
              </circle>
            </g>
          );
        })}
        {/* Y-axis labels */}
        <text x={4} y={padT + 4} fill="#94a3b8" fontSize="10">
          {maxY}
        </text>
        <text x={4} y={padT + plotH} fill="#94a3b8" fontSize="10">
          0
        </text>
      </svg>
    </div>
  );
}

export default function HealthPings() {
  const [searchParams, setSearchParams] = useSearchParams();
  const hoursRaw = searchParams.get("hours");
  const hours = useMemo(() => {
    const parsed = hoursRaw ? parseInt(hoursRaw, 10) : DEFAULT_HOURS;
    const match = PERIODS.find((p) => p.hours === parsed);
    return match ? match.hours : DEFAULT_HOURS;
  }, [hoursRaw]);
  const nodeIdFilter = searchParams.get("node_id");

  const summaryQ = useQuery<HealthPingSummaryOut>({
    queryKey: ["health-pings-summary", hours],
    queryFn: () => api.get(`/health-pings/summary?hours=${hours}`),
    refetchInterval: 60_000,
  });

  const recentParams = new URLSearchParams();
  recentParams.set("limit", "50");
  if (nodeIdFilter) recentParams.set("node_id", nodeIdFilter);
  const recentQ = useQuery<HealthPingRecentBadOut>({
    queryKey: ["health-pings-recent-bad", nodeIdFilter],
    queryFn: () =>
      api.get(`/health-pings/recent-bad?${recentParams.toString()}`),
    refetchInterval: 60_000,
  });

  const setHours = (h: number) => {
    const next = new URLSearchParams(searchParams);
    next.set("hours", String(h));
    setSearchParams(next, { replace: true });
  };
  const clearNodeFilter = () => {
    const next = new URLSearchParams(searchParams);
    next.delete("node_id");
    setSearchParams(next, { replace: true });
  };

  const summary = summaryQ.data;
  const recent = recentQ.data;

  // Sort per-node: ноды с <5 ответов в конце — это шум; дальше по bad_ratio desc.
  const perNodeSorted = useMemo(() => {
    if (!summary) return [];
    return [...summary.per_node].sort((a, b) => {
      const aResp = a.ok + a.bad;
      const bResp = b.ok + b.bad;
      const aLow = aResp < 5 ? 1 : 0;
      const bLow = bResp < 5 ? 1 : 0;
      if (aLow !== bLow) return aLow - bLow;
      return b.bad_ratio - a.bad_ratio;
    });
  }, [summary]);

  return (
    <div>
      <div className="flex items-center mb-4 gap-3">
        <h1 className="text-2xl font-semibold">Health pings</h1>
        <div className="flex gap-1 ml-4">
          {PERIODS.map((p) => (
            <button
              key={p.hours}
              onClick={() => setHours(p.hours)}
              className={`text-sm px-3 py-1 rounded ${
                hours === p.hours
                  ? "bg-slate-700 text-white"
                  : "bg-slate-800 text-slate-300 hover:bg-slate-700"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
        <button
          onClick={() => {
            summaryQ.refetch();
            recentQ.refetch();
          }}
          className="ml-auto text-sm px-2 py-1 rounded bg-slate-800 hover:bg-slate-700"
        >
          {summaryQ.isFetching || recentQ.isFetching ? "…" : "↻"}
        </button>
      </div>

      <p className="text-xs text-slate-500 mb-4">
        Бот спрашивает активных подписчиков «как работает VPN?» — не чаще
        раза в сутки и только в обеденное окно МСК. Юзер может ответить
        <b>ok</b>/<b>bad</b>, отписаться (<b>opt-out</b>), либо — если VPN
        сломался прямо сейчас — нажать кнопку «🆘 VPN не работает» в
        боте/вебапе (<b>self-reported</b> — отдельный счётчик ниже).
        Источник всех данных — AuditLog.
      </p>

      {summaryQ.isLoading && (
        <div className="text-slate-400">Загрузка сводки…</div>
      )}
      {summaryQ.error && (
        <div className="text-red-400">
          Ошибка: {(summaryQ.error as Error).message}
        </div>
      )}

      {summary && (
        <>
          <section className="mb-6">
            <h2 className="text-xs uppercase text-slate-400 mb-2">Сводка</h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <Card title="Запросов отправлено" value={summary.totals.requests} />
              <Card
                title="Ответов получено"
                value={summary.totals.responses}
                hint={`response rate: ${(summary.totals.response_rate * 100).toFixed(0)}%`}
              />
              <Card
                title="OK"
                value={summary.totals.ok}
                tone="good"
              />
              <Card
                title="BAD (всего)"
                value={summary.totals.bad}
                tone={summary.totals.bad > 0 ? "bad" : "default"}
                hint={`из них по опросу: ${summary.totals.bad_prompted}`}
              />
              <Card
                title="🆘 Self-reported BAD"
                value={summary.totals.bad_self_reported}
                tone={
                  summary.totals.bad_self_reported > 0 ? "danger" : "default"
                }
                hint="юзер сам пожаловался через кнопку"
              />
              <Card title="Opt-outs" value={summary.totals.opt_outs} />
            </div>
          </section>

          <section className="mb-6">
            <h2 className="text-xs uppercase text-slate-400 mb-2">
              Динамика
            </h2>
            <HealthPingChart
              points={summary.timeseries}
              bucket={summary.bucket}
            />
          </section>

          <section className="mb-6">
            <h2 className="text-xs uppercase text-slate-400 mb-2">
              По нодам ({perNodeSorted.length})
            </h2>
            {perNodeSorted.length === 0 ? (
              <div className="text-slate-500 text-sm">Данных нет.</div>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase text-slate-400 border-b border-slate-700">
                    <th className="py-2 px-2">Нода</th>
                    <th className="py-2 px-2 text-right">Запросов</th>
                    <th className="py-2 px-2 text-right">OK</th>
                    <th className="py-2 px-2 text-right">BAD</th>
                    <th className="py-2 px-2 text-right">% BAD</th>
                    <th className="py-2 px-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {perNodeSorted.map((n) => {
                    const resp = n.ok + n.bad;
                    const lowVolume = resp < 5;
                    const ratioPct = (n.bad_ratio * 100).toFixed(0);
                    const ratioCls =
                      n.bad_ratio === 0
                        ? "text-emerald-400"
                        : n.bad_ratio <= 0.2
                          ? "text-yellow-400"
                          : "text-red-400";
                    return (
                      <tr
                        key={n.node_id ?? "null"}
                        className={`border-b border-slate-800 ${lowVolume ? "text-slate-500" : ""}`}
                      >
                        <td className="py-2 px-2">
                          {n.node_id !== null ? (
                            <Link
                              to={`/nodes`}
                              className="text-blue-400 hover:underline"
                            >
                              {n.node_name ?? `#${n.node_id}`}
                            </Link>
                          ) : (
                            <span className="text-slate-500">
                              (без node_id)
                            </span>
                          )}
                        </td>
                        <td className="py-2 px-2 text-right tabular-nums">
                          {n.requests}
                        </td>
                        <td className="py-2 px-2 text-right tabular-nums">
                          {n.ok}
                        </td>
                        <td className="py-2 px-2 text-right tabular-nums">
                          {n.bad}
                        </td>
                        <td
                          className={`py-2 px-2 text-right tabular-nums ${ratioCls}`}
                        >
                          {resp > 0 ? `${ratioPct}%` : "—"}
                        </td>
                        <td className="py-2 px-2">
                          {n.node_id !== null && (
                            <button
                              onClick={() => {
                                const next = new URLSearchParams(searchParams);
                                next.set("node_id", String(n.node_id));
                                setSearchParams(next, { replace: true });
                              }}
                              className="text-xs text-slate-400 hover:text-white"
                            >
                              фильтр →
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}

      <section>
        <div className="flex items-baseline justify-between mb-2">
          <h2 className="text-xs uppercase text-slate-400">
            Последние «bad» ответы
          </h2>
          {nodeIdFilter && (
            <div className="text-xs">
              <span className="text-slate-400">Фильтр: node_id={nodeIdFilter}</span>
              <button
                onClick={clearNodeFilter}
                className="ml-2 text-blue-400 hover:underline"
              >
                сбросить
              </button>
            </div>
          )}
        </div>
        {recentQ.isLoading && (
          <div className="text-slate-400 text-sm">Загрузка…</div>
        )}
        {recentQ.error && (
          <div className="text-red-400 text-sm">
            {(recentQ.error as Error).message}
          </div>
        )}
        {recent && recent.items.length === 0 && (
          <div className="text-slate-500 text-sm">
            Пока нет жалоб за выбранный период.
          </div>
        )}
        {recent && recent.items.length > 0 && (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase text-slate-400 border-b border-slate-700">
                <th className="py-2 px-2">Когда</th>
                <th className="py-2 px-2">Источник</th>
                <th className="py-2 px-2">Юзер</th>
                <th className="py-2 px-2">Нода</th>
                <th className="py-2 px-2">План</th>
              </tr>
            </thead>
            <tbody>
              {recent.items.map((it, idx) => {
                const isSelf = it.source === "self_reported";
                return (
                  <tr
                    key={`${it.created_at}-${idx}`}
                    className={`border-b border-slate-800 ${isSelf ? "bg-red-950/30" : ""}`}
                  >
                    <td className="py-2 px-2 text-slate-300 tabular-nums">
                      {new Date(it.created_at).toLocaleString()}
                    </td>
                    <td className="py-2 px-2">
                      {isSelf ? (
                        <span className="text-red-400 font-medium">
                          🆘 self-reported
                        </span>
                      ) : (
                        <span className="text-slate-400">prompted</span>
                      )}
                    </td>
                    <td className="py-2 px-2">
                      {it.user_id !== null ? (
                        <Link
                          to={`/users`}
                          className="text-blue-400 hover:underline"
                        >
                          {it.telegram_id ?? `#${it.user_id}`}
                        </Link>
                      ) : (
                        <span className="text-slate-500">
                          {it.telegram_id ?? "?"}
                        </span>
                      )}
                    </td>
                    <td className="py-2 px-2">
                      {it.node_id !== null ? (
                        <Link
                          to={`/nodes`}
                          className="text-blue-400 hover:underline"
                        >
                          {it.node_name ?? `#${it.node_id}`}
                        </Link>
                      ) : (
                        <span className="text-slate-500">—</span>
                      )}
                    </td>
                    <td className="py-2 px-2 text-slate-300">
                      {it.plan_name ?? "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
