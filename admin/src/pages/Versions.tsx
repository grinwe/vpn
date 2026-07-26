import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  ApiError,
  fetchVersionsOverview,
  upgradeNodesXray,
  VersionsOverview,
  VPNNodeOut,
} from "../api";

// Версия самой админки — Vite запекает её в бандл на сборке (build-arg
// VITE_APP_VERSION из того же файла VERSION). Нужна, чтобы отличить «бэкенд
// выкатили, а фронт в браузере старый из кэша» — иначе такие расхождения
// диагностируются гаданием.
const ADMIN_BUILD_VERSION = import.meta.env.VITE_APP_VERSION ?? "0.0.0-dev";

const strip = (v: string | null | undefined) => (v ? v.replace(/^v/, "") : null);

function Cell({
  value,
  drift,
  title,
}: {
  value: string | null;
  drift?: boolean;
  title?: string;
}) {
  return (
    <span
      className={`font-mono ${drift ? "text-yellow-400" : "text-slate-200"}`}
      title={title}
    >
      {value ?? "—"}
    </span>
  );
}

function SummaryCard({
  label,
  value,
  hint,
  tone = "normal",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "normal" | "warn";
}) {
  return (
    <div className="rounded border border-slate-800 p-3 bg-slate-900/40">
      <div className="text-xs text-slate-400">{label}</div>
      <div
        className={`text-lg font-mono ${
          tone === "warn" ? "text-yellow-400" : "text-slate-100"
        }`}
      >
        {value}
      </div>
      {hint && <div className="text-xs text-slate-500 mt-1">{hint}</div>}
    </div>
  );
}

export default function Versions() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const overview = useQuery<VersionsOverview>({
    queryKey: ["versions-overview"],
    queryFn: fetchVersionsOverview,
    refetchInterval: 300_000,
    retry: false,
  });
  const nodes = useQuery<VPNNodeOut[]>({
    queryKey: ["nodes"],
    queryFn: () => api.get("/nodes"),
    refetchInterval: 60_000,
    retry: false,
  });

  const pinned = overview.data?.xray.pinned ?? null;
  const appVersion = overview.data?.app_version ?? null;

  const rows = useMemo(() => {
    const list = nodes.data ?? [];
    return [...list].sort((a, b) => a.name.localeCompare(b.name));
  }, [nodes.data]);

  const outdated = useMemo(
    () =>
      rows.filter(
        (n) => n.xray_version && pinned && strip(n.xray_version) !== strip(pinned),
      ),
    [rows, pinned],
  );

  const upgrade = useMutation({
    mutationFn: (ids: number[]) => upgradeNodesXray(ids, "versions-page"),
    onSuccess: (res) => {
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["nodes"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      const skipped = res.skipped.length ? `, пропущено: ${res.skipped.join(", ")}` : "";
      alert(`Запущено задач: ${res.started.length}${skipped}`);
    },
    onError: (e) => alert(`Не удалось запустить апгрейд: ${String(e)}`),
  });

  function toggle(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function runUpgrade(ids: number[]) {
    if (!ids.length) return;
    if (
      !window.confirm(
        `Обновить xray на ${ids.length} нод(е/ах) до ${pinned ?? "целевой версии"}?\n` +
          "На каждой ноде рестарт ядра разорвёт активные соединения на ~100 мс.",
      )
    )
      return;
    upgrade.mutate(ids);
  }

  if (nodes.error || overview.error) {
    const err = nodes.error ?? overview.error;
    const msg = err instanceof ApiError ? `${err.status}: ${err.message}` : String(err);
    return (
      <div>
        <h1 className="text-2xl font-semibold mb-4">Версии</h1>
        <div className="p-4 rounded border border-red-900/50 bg-red-950/30">{msg}</div>
      </div>
    );
  }

  const xray = overview.data?.xray;
  const pinBehind = !!xray?.pin_behind_upstream;

  return (
    <div>
      <h1 className="text-2xl font-semibold mb-4">Версии</h1>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 mb-6">
        <SummaryCard
          label="Наш код (бэкенд)"
          value={appVersion ?? "—"}
          hint="Файл VERSION в репозитории, бампается руками перед выкатом"
        />
        <SummaryCard
          label="Админка (этот бандл)"
          value={ADMIN_BUILD_VERSION}
          hint={
            appVersion && ADMIN_BUILD_VERSION !== appVersion
              ? "Отличается от бэкенда — обнови страницу (Ctrl+Shift+R)"
              : "Совпадает с бэкендом"
          }
          tone={appVersion && ADMIN_BUILD_VERSION !== appVersion ? "warn" : "normal"}
        />
        <SummaryCard
          label="xray: целевая (пин в роли)"
          value={pinned ?? "—"}
          hint="xray_core_version в roles/xray_core; меняется коммитом вместе с sha256"
        />
        <SummaryCard
          label="xray: последняя upstream"
          value={xray?.latest ?? "—"}
          tone={pinBehind ? "warn" : "normal"}
          hint={
            xray?.last_error
              ? `Проверка не удалась: ${xray.last_error}`
              : xray?.checked_at
                ? `Проверено ${new Date(xray.checked_at).toLocaleString()}`
                : "Ещё не проверялось"
          }
        />
      </div>

      {pinBehind && (
        <div className="mb-6 p-3 rounded border border-yellow-900/50 bg-yellow-950/20 text-sm">
          Вышла {xray?.latest}, у нас пин {pinned}. Апгрейд не автоматический:
          <code className="mx-1 font-mono">xray_core_version</code> и
          <code className="mx-1 font-mono">xray_core_sha256</code> меняются парой
          (иначе установка отвергнет каждый источник по несовпадению хэша), затем
          деплой и обновление нод.
          {xray?.html_url && (
            <>
              {" "}
              <a
                href={xray.html_url}
                target="_blank"
                rel="noreferrer"
                className="underline"
              >
                Релиз на GitHub
              </a>
            </>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 mb-3">
        <button
          onClick={() => runUpgrade([...selected])}
          disabled={!selected.size || upgrade.isPending}
          className="text-sm px-3 py-1.5 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-40"
        >
          Обновить xray на выбранных ({selected.size})
        </button>
        <button
          onClick={() => runUpgrade(outdated.map((n) => n.id))}
          disabled={!outdated.length || upgrade.isPending}
          className="text-sm px-3 py-1.5 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-40"
          title="Все ноды, чья версия xray не совпадает с пином"
        >
          Обновить все отставшие ({outdated.length})
        </button>
        <span className="text-xs text-slate-500">
          Точечный прогон роли xray_core, ~30-60с на ноду. Полный бутстрап — на
          странице Nodes.
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-slate-400 border-b border-slate-700">
            <tr>
              <th className="py-2 w-8"></th>
              <th>Нода</th>
              <th>xray</th>
              <th className="hidden sm:table-cell">Код на ноде</th>
              <th className="hidden md:table-cell">Проверено</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((n) => {
              const xrayDrift =
                !!n.xray_version && !!pinned && strip(n.xray_version) !== strip(pinned);
              const releaseDrift =
                !!n.release_version && !!appVersion && n.release_version !== appVersion;
              return (
                <tr key={n.id} className="border-b border-slate-800">
                  <td className="py-2">
                    <input
                      type="checkbox"
                      checked={selected.has(n.id)}
                      onChange={() => toggle(n.id)}
                      aria-label={`Выбрать ${n.name}`}
                    />
                  </td>
                  <td className="font-mono">{n.name}</td>
                  <td>
                    <Cell
                      value={strip(n.xray_version)}
                      drift={xrayDrift}
                      title={xrayDrift ? `Целевая версия ${pinned}` : undefined}
                    />
                  </td>
                  <td className="hidden sm:table-cell">
                    <Cell
                      value={n.release_version}
                      drift={releaseDrift}
                      title={
                        releaseDrift
                          ? "Нода прошита старой версией кода — нужен полный бутстрап"
                          : undefined
                      }
                    />
                  </td>
                  <td className="hidden md:table-cell text-slate-400">
                    {n.versions_checked_at
                      ? new Date(n.versions_checked_at).toLocaleString()
                      : "ни разу"}
                  </td>
                  <td className="text-right">
                    <button
                      onClick={() => runUpgrade([n.id])}
                      disabled={upgrade.isPending}
                      className="text-xs px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-40"
                    >
                      Обновить
                    </button>
                  </td>
                </tr>
              );
            })}
            {!rows.length && (
              <tr>
                <td colSpan={6} className="py-4 text-slate-500 text-center">
                  Нод нет
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <p className="text-xs text-slate-500 mt-4">
        Версии с нод снимает тик <code className="font-mono">tick-node-versions</code>{" "}
        раз в час по SSH: <code className="font-mono">xray version</code> и маркер{" "}
        <code className="font-mono">/etc/vpn-node-release.json</code>, который
        бутстрап пишет только после успешного прогона всех ролей. Поэтому «код на
        ноде» отстаёт ровно тогда, когда ноду ещё не перекатывали после выката.
      </p>
    </div>
  );
}
