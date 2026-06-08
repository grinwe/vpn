import { useQuery } from "@tanstack/react-query";
import {
  operatorRoutingMatrix,
  operatorRoutingReports,
  OperatorMatrixCell,
} from "../api";

const OPERATORS = [
  "mts",
  "beeline",
  "megafon",
  "tele2",
  "home_wifi",
  "other",
  "unknown",
] as const;

const OP_LABEL: Record<string, string> = {
  mts: "МТС",
  beeline: "Билайн",
  megafon: "МегаФон",
  tele2: "Tele2",
  home_wifi: "WiFi",
  other: "Другое",
  unknown: "?",
};

function cellColor(c: OperatorMatrixCell | undefined): string {
  if (!c || c.total === 0 || c.score === null) return "text-slate-600";
  // высокий score = в основном работает (зелёный); низкий = блочит (красный)
  if (c.score >= 0.7) return "text-emerald-400";
  if (c.score <= 0.3) return "text-red-400";
  return "text-amber-400";
}

export default function OperatorMatrix() {
  const matrix = useQuery({
    queryKey: ["operator-matrix"],
    queryFn: operatorRoutingMatrix,
    refetchInterval: 60_000,
  });
  const reports = useQuery({
    queryKey: ["operator-reports"],
    queryFn: () => operatorRoutingReports(200),
    refetchInterval: 60_000,
  });

  const cells = matrix.data?.cells ?? [];
  const byKey = new Map<string, OperatorMatrixCell>();
  const nodeNames = new Map<number, string | null>();
  const nodeIds: number[] = [];
  for (const c of cells) {
    byKey.set(`${c.node_id}:${c.operator}`, c);
    if (!nodeNames.has(c.node_id)) {
      nodeNames.set(c.node_id, c.node_name);
      nodeIds.push(c.node_id);
    }
  }
  nodeIds.sort((a, b) =>
    (nodeNames.get(a) ?? "").localeCompare(nodeNames.get(b) ?? ""),
  );

  return (
    <div>
      <h1 className="text-2xl font-semibold mb-2">Operator × Node</h1>
      <p className="text-xs text-slate-400 mb-4 max-w-3xl">
        Краудсорс «нода блочит оператора» из юзерских «🆘 VPN не работает».
        Окно {matrix.data?.window_hours ?? "?"}ч, порог уверенности K=
        {matrix.data?.min_devices ?? "?"} разных устройств. <b>Advisory</b> —
        choose_node это пока НЕ использует. Цвет: 🟢 в основном работает, 🔴 в
        основном блочит, 🟡 пополам. <b>Жирным</b> — confident (total ≥ K).
        В ячейке: % ok и (ok/fail) по разным устройствам.
      </p>

      {matrix.isLoading && <div className="text-slate-400">Загрузка…</div>}
      {matrix.error && (
        <div className="text-red-400 text-sm">
          {(matrix.error as Error).message}
        </div>
      )}
      {matrix.data && nodeIds.length === 0 && (
        <div className="text-slate-500 text-sm mb-8">
          Пока нет данных — ни одного «VPN не работает» за окно.
        </div>
      )}
      {nodeIds.length > 0 && (
        <table className="text-xs mb-8">
          <thead className="text-slate-400">
            <tr>
              <th className="text-left py-1 px-2">Нода</th>
              {OPERATORS.map((op) => (
                <th key={op} className="py-1 px-2">
                  {OP_LABEL[op]}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {nodeIds.map((nid) => (
              <tr key={nid} className="border-t border-slate-800">
                <td className="py-1 px-2 font-mono text-slate-300">
                  #{nid} {nodeNames.get(nid)}
                </td>
                {OPERATORS.map((op) => {
                  const c = byKey.get(`${nid}:${op}`);
                  return (
                    <td
                      key={op}
                      className={`py-1 px-2 text-center ${cellColor(c)} ${
                        c?.confident ? "font-bold" : ""
                      }`}
                    >
                      {c && c.total > 0 ? (
                        <span title={`ok ${c.ok} / fail ${c.fail} (${c.total} устр.)`}>
                          {c.score !== null
                            ? `${Math.round(c.score * 100)}%`
                            : "—"}
                          <span className="text-slate-500">
                            {" "}
                            ({c.ok}/{c.fail})
                          </span>
                        </span>
                      ) : (
                        "·"
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 className="text-sm font-semibold text-slate-300 mb-2">
        Последние репорты
      </h2>
      {reports.data && reports.data.length === 0 && (
        <div className="text-slate-500 text-xs">Пусто.</div>
      )}
      {reports.data && reports.data.length > 0 && (
        <table className="text-xs w-full">
          <thead className="text-slate-400">
            <tr>
              <th className="text-left py-1 px-2">Время</th>
              <th className="text-left py-1 px-2">User</th>
              <th className="text-left py-1 px-2">Оператор</th>
              <th className="text-left py-1 px-2">Failed → Target</th>
              <th className="text-left py-1 px-2">Исход</th>
            </tr>
          </thead>
          <tbody>
            {reports.data.map((r) => (
              <tr key={r.id} className="border-t border-slate-800">
                <td className="py-1 px-2 text-slate-400">
                  {r.reported_at
                    ? new Date(r.reported_at).toLocaleString()
                    : "—"}
                </td>
                <td className="py-1 px-2">#{r.user_id}</td>
                <td className="py-1 px-2">
                  {r.operator ? OP_LABEL[r.operator] ?? r.operator : "—"}
                </td>
                <td className="py-1 px-2 font-mono text-slate-400">
                  {r.failed_node_name ?? r.failed_node_id ?? "—"} →{" "}
                  {r.target_node_name ?? r.target_node_id ?? "—"}
                </td>
                <td
                  className={`py-1 px-2 ${
                    r.outcome === "ok"
                      ? "text-emerald-400"
                      : r.outcome === "fail"
                        ? "text-red-400"
                        : r.outcome === "pending"
                          ? "text-amber-400"
                          : "text-slate-400"
                  }`}
                >
                  {r.outcome}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
