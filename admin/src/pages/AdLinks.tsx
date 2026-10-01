import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, AdLinkOut, AdLinkCreateIn } from "../api";

const EMPTY_FORM: AdLinkCreateIn = { name: "", tag: "", notes: "", cost_kopecks: null };

function rub(kopecks: number): string {
  return (kopecks / 100).toLocaleString("ru-RU", { maximumFractionDigits: 0 });
}

function conv(paid: number, started: number): string {
  return started > 0 ? `${Math.round((paid / started) * 100)}%` : "—";
}

export default function AdLinks() {
  const qc = useQueryClient();
  const [form, setForm] = useState<AdLinkCreateIn>(EMPTY_FORM);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  // Живая воронка: рефреш раз в 15с (как Dashboard).
  const { data, isLoading } = useQuery<AdLinkOut[]>({
    queryKey: ["ad-links"],
    queryFn: () => api.get("/admin/ad-links"),
    refetchInterval: 15_000,
  });

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setEditingId(null);
    setError(null);
  };

  const createLink = useMutation({
    mutationFn: (payload: AdLinkCreateIn) =>
      api.post<AdLinkOut>("/admin/ad-links", payload),
    onSuccess: () => {
      resetForm();
      qc.invalidateQueries({ queryKey: ["ad-links"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  const updateLink = useMutation({
    mutationFn: (payload: {
      id: number;
      body: { name?: string; notes?: string | null; cost_kopecks?: number | null };
    }) =>
      api.patch<AdLinkOut>(`/admin/ad-links/${payload.id}`, payload.body),
    onSuccess: () => {
      resetForm();
      qc.invalidateQueries({ queryKey: ["ad-links"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  const toggleActive = useMutation({
    mutationFn: (link: AdLinkOut) =>
      api.patch<AdLinkOut>(`/admin/ad-links/${link.id}`, { is_active: !link.is_active }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ad-links"] }),
    onError: (e: Error) => alert(`Ошибка: ${e.message}`),
  });

  const deleteLink = useMutation({
    mutationFn: (id: number) => api.del(`/admin/ad-links/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ad-links"] }),
    onError: (e: Error) => alert(`Не удалось удалить: ${e.message}`),
  });

  const startEdit = (link: AdLinkOut) => {
    setEditingId(link.id);
    setForm({
      name: link.name,
      tag: link.tag,
      notes: link.notes ?? "",
      cost_kopecks: link.cost_kopecks,
    });
    setError(null);
  };

  const submit = () => {
    setError(null);
    if (!form.name.trim()) {
      setError("Имя обязательно");
      return;
    }
    if (editingId != null) {
      // tag неизменяем — правим только ярлык/заметки.
      updateLink.mutate({
        id: editingId,
        body: {
          name: form.name,
          notes: form.notes || null,
          cost_kopecks: form.cost_kopecks ?? 0,
        },
      });
    } else {
      createLink.mutate(form);
    }
  };

  const copyLink = (link: AdLinkOut) => {
    const url = link.share_url ?? `?start=${link.tag}`;
    navigator.clipboard?.writeText(url);
    setCopied(link.tag);
    setTimeout(() => setCopied((c) => (c === link.tag ? null : c)), 1500);
  };

  const busy = createLink.isPending || updateLink.isPending;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-2">
        <h1 className="text-2xl font-semibold mb-1">Рекламные ссылки</h1>
        <p className="text-sm text-slate-400 mb-4">
          Воронка обновляется каждые 15с. Дай рекламщику ссылку — считаем переходы,
          триалы и оплаты по метке.
        </p>

        {isLoading ? (
          <div>Загрузка…</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-slate-400 border-b border-slate-700">
              <tr>
                <th className="py-2">Метка / ссылка</th>
                <th>Имя</th>
                <th className="text-right">Старт</th>
                <th className="text-right">Триал</th>
                <th className="text-right">Оплата</th>
                <th className="text-right">Конв.</th>
                <th className="text-right">Выручка ₽</th>
                <th className="text-right">Затраты ₽</th>
                <th className="text-right">CAC ₽</th>
                <th className="text-right">ROI</th>
                <th>Акт.</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data?.map((l) => (
                <tr
                  key={l.id}
                  className={`border-b border-slate-800 ${l.is_active ? "" : "opacity-50"}`}
                >
                  <td className="py-2">
                    <span className="font-mono">{l.tag}</span>
                    <button
                      onClick={() => copyLink(l)}
                      className="ml-2 text-xs px-2 py-0.5 rounded bg-slate-700 hover:bg-slate-600"
                      title={l.share_url ?? `?start=${l.tag}`}
                    >
                      {copied === l.tag ? "✓" : "копи"}
                    </button>
                  </td>
                  <td>{l.name}</td>
                  <td className="text-right">{l.started}</td>
                  <td className="text-right">{l.trial}</td>
                  <td className="text-right">{l.paid}</td>
                  <td className="text-right">{conv(l.paid, l.started)}</td>
                  <td className="text-right">
                    {l.cost_kopecks ? rub(l.cost_kopecks) : "—"}
                  </td>
                  <td className="text-right" title="Затраты ÷ число оплативших">
                    {l.cac_kopecks ? rub(l.cac_kopecks) : "—"}
                  </td>
                  {/* ROI < 1 — канал не отбился: подсвечиваем, потому что это
                      единственная цифра, ради которой заводят затраты. */}
                  <td
                    className={`text-right ${
                      l.roi === null ? "" : l.roi >= 1 ? "text-emerald-400" : "text-yellow-400"
                    }`}
                    title="Выручка ÷ затраты"
                  >
                    {l.roi === null ? "—" : `${l.roi}×`}
                  </td>
                  <td>{l.is_active ? "✓" : "✕"}</td>
                  <td className="text-right space-x-1 whitespace-nowrap">
                    <button
                      onClick={() => startEdit(l)}
                      className="text-xs px-2 py-1 rounded bg-slate-700 hover:bg-slate-600"
                    >
                      edit
                    </button>
                    <button
                      onClick={() => toggleActive.mutate(l)}
                      className="text-xs px-2 py-1 rounded bg-slate-700 hover:bg-slate-600"
                      title={l.is_active ? "Выключить (новые заходы не считаются)" : "Включить"}
                    >
                      {l.is_active ? "off" : "on"}
                    </button>
                    <button
                      onClick={() => {
                        if (
                          confirm(
                            `Удалить ссылку "${l.name}" (${l.tag})?\n\nИсторическая статистика по метке сохранится, но ярлык пропадёт. Чтобы просто остановить набор — используйте off.`,
                          )
                        )
                          deleteLink.mutate(l.id);
                      }}
                      className="text-xs px-2 py-1 rounded bg-red-700 hover:bg-red-600"
                    >
                      del
                    </button>
                  </td>
                </tr>
              ))}
              {data && data.length === 0 && (
                <tr>
                  <td colSpan={12} className="py-4 text-slate-500 text-center">
                    Рекламных ссылок пока нет — создай первую справа.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      <aside className="bg-slate-800 rounded-lg p-4 border border-slate-700 h-fit space-y-3">
        <h2 className="font-semibold">
          {editingId != null ? `Редактировать #${editingId}` : "Новая ссылка"}
        </h2>

        <label className="block text-sm">
          Имя (для тебя)
          <input
            type="text"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="Блогер Вася — июнь"
            className="mt-1 w-full px-3 py-2 rounded bg-slate-900 border border-slate-700"
          />
        </label>

        <label className="block text-sm">
          Метка (в ссылке)
          <input
            type="text"
            value={form.tag ?? ""}
            onChange={(e) => setForm({ ...form, tag: e.target.value })}
            placeholder="tg_vasya — пусто = авто"
            disabled={editingId != null}
            className="mt-1 w-full px-3 py-2 rounded bg-slate-900 border border-slate-700 disabled:opacity-50 font-mono"
          />
          <span className="text-[11px] text-slate-500">
            Только латиница/цифры/_/-, ≤64. Менять у созданной нельзя (осиротит статистику).
          </span>
        </label>

        <label className="block text-sm">
          Затраты, ₽
          <input
            type="number"
            min={0}
            value={form.cost_kopecks != null ? form.cost_kopecks / 100 : ""}
            onChange={(e) =>
              setForm({
                ...form,
                cost_kopecks: e.target.value ? Math.round(Number(e.target.value) * 100) : null,
              })
            }
            placeholder="сколько заплатили за размещение"
            className="mt-1 w-full px-3 py-2 rounded bg-slate-900 border border-slate-700"
          />
          <span className="text-[11px] text-slate-500">
            Пусто или 0 — бесплатно (обмен, свой канал). Из этого считаются CAC и ROI.
          </span>
        </label>

        <label className="block text-sm">
          Заметки
          <input
            type="text"
            value={form.notes ?? ""}
            onChange={(e) => setForm({ ...form, notes: e.target.value })}
            placeholder="канал, договорённости…"
            className="mt-1 w-full px-3 py-2 rounded bg-slate-900 border border-slate-700"
          />
        </label>

        <div className="flex gap-2">
          <button
            onClick={submit}
            disabled={busy}
            className="flex-1 py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50"
          >
            {editingId != null ? "Сохранить" : "Создать"}
          </button>
          {editingId != null && (
            <button
              onClick={resetForm}
              className="py-2 px-3 rounded bg-slate-700 hover:bg-slate-600"
            >
              Отмена
            </button>
          )}
        </div>

        {error && <p className="text-sm text-red-400">{error}</p>}
      </aside>
    </div>
  );
}
