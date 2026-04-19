import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BroadcastOut,
  BroadcastStatus,
  BroadcastTargetFilter,
  cancelBroadcast,
  createBroadcast,
  listBroadcasts,
  previewBroadcast,
} from "../api";

const TEXT_MAX = 4000;
const IDS_MAX = 5000;

type TargetKind = "all" | "active" | "ids";

const STATUS_COLORS: Record<BroadcastStatus, string> = {
  queued: "bg-slate-600",
  sending: "bg-blue-600",
  completed: "bg-emerald-700",
  cancelled: "bg-amber-700",
  failed: "bg-red-700",
};

function parseIds(raw: string): number[] {
  // Режем по запятым/пробелам/новым строкам. Молча выбрасываем мусор —
  // форма и так должна показывать распознанный список до submit'а.
  return raw
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
    .map((s) => Number(s))
    .filter((n) => Number.isInteger(n) && n > 0);
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return "—";
  return new Date(ts).toLocaleString();
}

function progressText(b: BroadcastOut): string {
  const total = b.total_recipients;
  if (total == null) return `${b.sent_count}`;
  return `${b.sent_count} / ${total}`;
}

function progressPct(b: BroadcastOut): number {
  const total = b.total_recipients;
  if (!total || total <= 0) return 100;
  return Math.min(100, Math.round((b.sent_count / total) * 100));
}

export default function Broadcasts() {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [kind, setKind] = useState<TargetKind>("active");
  const [idsText, setIdsText] = useState("");
  const [previewCount, setPreviewCount] = useState<number | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const parsedIds = useMemo(() => parseIds(idsText), [idsText]);

  const targetFilter: BroadcastTargetFilter = useMemo(() => {
    if (kind === "all") return { type: "all" };
    if (kind === "active") return { type: "active" };
    return { type: "ids", ids: parsedIds };
  }, [kind, parsedIds]);

  const listQuery = useQuery({
    queryKey: ["broadcasts"],
    queryFn: () => listBroadcasts({ limit: 100 }),
    // Пока есть активные (queued/sending) — бьём каждые 5s, чтобы
    // прогресс-бар полз в реальном времени. Если все completed/cancelled/failed
    // — отключаем автообновление, пусть пользователь сам рефрешит.
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 5000;
      const hasActive = data.items.some(
        (b) => b.status === "queued" || b.status === "sending",
      );
      return hasActive ? 5000 : false;
    },
  });

  const previewMut = useMutation({
    mutationFn: previewBroadcast,
    onSuccess: (res) => {
      setPreviewCount(res.recipient_count);
      setPreviewError(null);
    },
    onError: (e: unknown) => {
      setPreviewCount(null);
      setPreviewError(String(e));
    },
  });

  const createMut = useMutation({
    mutationFn: createBroadcast,
    onSuccess: () => {
      setText("");
      setIdsText("");
      setPreviewCount(null);
      setPreviewError(null);
      qc.invalidateQueries({ queryKey: ["broadcasts"] });
    },
  });

  const cancelMut = useMutation({
    mutationFn: (id: number) => cancelBroadcast(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["broadcasts"] }),
  });

  const textLen = text.length;
  const idsTooMany = kind === "ids" && parsedIds.length > IDS_MAX;
  const idsEmpty = kind === "ids" && parsedIds.length === 0;
  const canSubmit =
    textLen > 0 && textLen <= TEXT_MAX && !idsTooMany && !idsEmpty;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-2 space-y-4">
        <h1 className="text-2xl font-semibold">Broadcasts</h1>

        {listQuery.isLoading ? (
          <div>Загрузка…</div>
        ) : listQuery.isError ? (
          <div className="text-red-400">
            Ошибка загрузки: {String(listQuery.error)}
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-slate-400 border-b border-slate-700">
              <tr>
                <th className="py-2">ID</th>
                <th>Создана</th>
                <th>Автор</th>
                <th>Аудитория</th>
                <th>Прогресс</th>
                <th>Статус</th>
                <th>Текст</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {listQuery.data?.items.map((b) => (
                <tr key={b.id} className="border-b border-slate-800 align-top">
                  <td className="py-2">{b.id}</td>
                  <td className="text-slate-400 whitespace-nowrap">
                    {formatTimestamp(b.created_at)}
                  </td>
                  <td className="font-mono text-xs">{b.created_by}</td>
                  <td className="text-xs">
                    {b.target_filter.type === "all" && "все"}
                    {b.target_filter.type === "active" && "активные"}
                    {b.target_filter.type === "ids" &&
                      `ids (${b.target_filter.ids?.length ?? 0})`}
                  </td>
                  <td className="min-w-[140px]">
                    <div className="text-xs mb-1">{progressText(b)}</div>
                    <div className="h-1.5 bg-slate-700 rounded">
                      <div
                        className={`h-full rounded ${STATUS_COLORS[b.status]}`}
                        style={{ width: `${progressPct(b)}%` }}
                      />
                    </div>
                  </td>
                  <td>
                    <span
                      className={`text-xs px-2 py-0.5 rounded ${STATUS_COLORS[b.status]}`}
                    >
                      {b.status}
                    </span>
                  </td>
                  <td className="max-w-[260px] truncate text-slate-300">
                    {b.text}
                  </td>
                  <td>
                    {(b.status === "queued" || b.status === "sending") && (
                      <button
                        onClick={() => {
                          if (confirm(`Отменить рассылку #${b.id}?`))
                            cancelMut.mutate(b.id);
                        }}
                        className="text-xs px-2 py-1 rounded bg-amber-700 hover:bg-amber-600"
                        disabled={cancelMut.isPending}
                      >
                        отменить
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {listQuery.data && listQuery.data.items.length === 0 && (
                <tr>
                  <td colSpan={8} className="py-4 text-slate-500 text-center">
                    Рассылок пока нет
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      <aside className="bg-slate-800 rounded-lg p-4 border border-slate-700 h-fit space-y-4">
        <h2 className="font-semibold">Новая рассылка</h2>

        <label className="block text-sm">
          Текст сообщения
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={6}
            placeholder="Привет! С 15:00 будут тех-работы…"
            className="mt-1 w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 font-mono text-xs"
          />
          <div
            className={`text-xs mt-1 ${
              textLen > TEXT_MAX ? "text-red-400" : "text-slate-500"
            }`}
          >
            {textLen} / {TEXT_MAX}
          </div>
        </label>

        <div className="text-sm">
          Кому
          <div className="mt-1 space-y-1">
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="target"
                checked={kind === "all"}
                onChange={() => setKind("all")}
              />
              <span>Всем юзерам с telegram_id</span>
            </label>
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="target"
                checked={kind === "active"}
                onChange={() => setKind("active")}
              />
              <span>Только с активной подпиской</span>
            </label>
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="target"
                checked={kind === "ids"}
                onChange={() => setKind("ids")}
              />
              <span>Список User.id</span>
            </label>
          </div>
        </div>

        {kind === "ids" && (
          <label className="block text-sm">
            User IDs (через запятую/пробел/newlines)
            <textarea
              value={idsText}
              onChange={(e) => setIdsText(e.target.value)}
              rows={3}
              placeholder="1, 2, 3"
              className="mt-1 w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 font-mono text-xs"
            />
            <div
              className={`text-xs mt-1 ${
                idsTooMany ? "text-red-400" : "text-slate-500"
              }`}
            >
              распознано: {parsedIds.length}
              {idsTooMany && ` — лимит ${IDS_MAX}`}
            </div>
          </label>
        )}

        <div className="flex gap-2">
          <button
            onClick={() => previewMut.mutate({ target_filter: targetFilter })}
            disabled={idsTooMany || idsEmpty || previewMut.isPending}
            className="flex-1 py-2 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50"
          >
            Preview
          </button>
          <button
            onClick={() => {
              if (
                !confirm(
                  `Отправить рассылку${
                    previewCount != null ? ` ${previewCount} получателям` : ""
                  }?`,
                )
              )
                return;
              createMut.mutate({ text, target_filter: targetFilter });
            }}
            disabled={!canSubmit || createMut.isPending}
            className="flex-1 py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50"
          >
            Send
          </button>
        </div>

        {previewCount != null && (
          <div className="text-sm text-emerald-400">
            Preview: {previewCount} получателей
          </div>
        )}
        {previewError && (
          <div className="text-sm text-red-400">{previewError}</div>
        )}
        {createMut.isError && (
          <div className="text-sm text-red-400">
            Ошибка: {String(createMut.error)}
          </div>
        )}
      </aside>
    </div>
  );
}
