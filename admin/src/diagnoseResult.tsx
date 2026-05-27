// Структурированный рендер результата диагностики relay→exit линка.
//
// Backend через `playbooks/diagnose_relay_link.yml` кладёт в
// task.result поле `checks: DiagnoseCheckEntry[]` + `diagnose_meta`.
// Здесь — карточки OK/FAIL/warn/skip/info с цветом, latency и
// раскрываемыми details. Raw stdout остаётся в parent-у task.result
// и показывается отдельно через collapsible <details> в banner'е.

import { useState } from "react";
import {
  DiagnoseCheckEntry,
  DiagnoseCheckStatus,
  DiagnoseMeta,
} from "./api";

const STATUS_STYLE: Record<DiagnoseCheckStatus, { bg: string; label: string }> = {
  ok: { bg: "bg-emerald-700/40 border-emerald-700 text-emerald-200", label: "OK" },
  warn: { bg: "bg-amber-700/40 border-amber-700 text-amber-200", label: "WARN" },
  fail: { bg: "bg-red-700/40 border-red-700 text-red-200", label: "FAIL" },
  skip: { bg: "bg-slate-700/40 border-slate-700 text-slate-300", label: "SKIP" },
  info: { bg: "bg-blue-700/40 border-blue-700 text-blue-200", label: "INFO" },
};

const CHECK_LABELS: Record<string, string> = {
  peer_on_jump: "Peer on jump",
  handshake_age: "Handshake age",
  ping_endpoint: "Ping endpoint",
  ping_internet_through: "HTTPS через WG",
  xray_port: "Xray port",
  listening_sockets: "Listening sockets",
  _result_file_missing: "Result file отсутствует",
  _result_file_unreadable: "Result file не парсится",
};

export function DiagnoseResult({
  checks,
  meta,
}: {
  checks: DiagnoseCheckEntry[];
  meta?: DiagnoseMeta;
}) {
  if (checks.length === 0) {
    return (
      <div className="text-xs text-slate-400 italic">
        Нет проверок в result'е. Скорее всего role не дописала JSON —
        смотри raw stdout ниже.
      </div>
    );
  }

  const okCount = checks.filter((c) => c.status === "ok").length;
  const failCount = checks.filter((c) => c.status === "fail").length;
  const warnCount = checks.filter((c) => c.status === "warn").length;

  return (
    <div className="space-y-2">
      <div className="text-xs text-slate-400 flex flex-wrap gap-x-3 gap-y-1">
        <span>
          <span className="text-emerald-400 font-mono">{okCount}</span>{" "}
          ok
        </span>
        {warnCount > 0 && (
          <span>
            <span className="text-amber-400 font-mono">{warnCount}</span>{" "}
            warn
          </span>
        )}
        {failCount > 0 && (
          <span>
            <span className="text-red-400 font-mono">{failCount}</span>{" "}
            fail
          </span>
        )}
        {meta && (
          <span className="text-slate-500">
            · iface {meta.wg_interface} · exit #{meta.exit_id}
          </span>
        )}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {checks.map((c, idx) => (
          <DiagnoseCheckCard key={`${c.name}-${idx}`} entry={c} />
        ))}
      </div>
    </div>
  );
}

function DiagnoseCheckCard({ entry }: { entry: DiagnoseCheckEntry }) {
  const [open, setOpen] = useState(false);
  const style = STATUS_STYLE[entry.status] ?? STATUS_STYLE.info;
  const label = CHECK_LABELS[entry.name] ?? entry.name;
  const hasDetails = entry.details && Object.keys(entry.details).length > 0;
  return (
    <div className={`border rounded p-2 ${style.bg}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold">{label}</div>
        <div className="flex items-center gap-2 text-[10px] font-mono">
          {entry.latency_ms != null && (
            <span className="text-slate-200">{entry.latency_ms}ms</span>
          )}
          <span className="px-1.5 py-0.5 rounded bg-black/30">
            {style.label}
          </span>
        </div>
      </div>
      {entry.message && (
        <div className="text-xs text-slate-100/80 mt-1 break-words">
          {entry.message}
        </div>
      )}
      {hasDetails && (
        <button
          onClick={() => setOpen(!open)}
          className="text-[10px] text-slate-400 hover:text-slate-200 mt-1"
        >
          {open ? "▾ details" : "▸ details"}
        </button>
      )}
      {open && hasDetails && (
        <pre className="text-[10px] mt-1 bg-black/30 rounded p-1 overflow-x-auto whitespace-pre-wrap font-mono text-slate-200">
          {JSON.stringify(entry.details, null, 2)}
        </pre>
      )}
    </div>
  );
}
