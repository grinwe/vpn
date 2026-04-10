import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, InvoiceListItem } from "../api";

const STATUSES = ["pending", "paid", "failed"] as const;

type BatchResult = { ok: number[]; skipped: number[]; not_found: number[] };

export default function Invoices() {
  const qc = useQueryClient();
  const [status, setStatus] = useState<(typeof STATUSES)[number] | "">("pending");
  const [limit, setLimit] = useState(50);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const { data, isLoading } = useQuery<InvoiceListItem[]>({
    queryKey: ["invoices", { status, limit }],
    queryFn: () =>
      api.get(
        `/invoices?limit=${limit}${status ? `&status=${status}` : ""}`
      ),
    refetchInterval: 10_000,
  });

  const markPaid = useMutation({
    mutationFn: (id: number) => api.post(`/invoices/${id}/mark_paid`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["invoices"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
    onError: (e: Error) => alert(`Не удалось пометить как paid: ${e.message}`),
  });

  const markUnpaid = useMutation({
    mutationFn: (id: number) => api.post(`/invoices/${id}/mark_unpaid`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["invoices"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
    onError: (e: Error) => alert(`Не удалось вернуть в pending: ${e.message}`),
  });

  const cancelInvoice = useMutation({
    mutationFn: (id: number) => api.post(`/invoices/${id}/cancel`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["invoices"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
    onError: (e: Error) => alert(`Не удалось отменить: ${e.message}`),
  });

  const batch = useMutation({
    mutationFn: (args: { ids: number[]; action: string }) =>
      api.post<BatchResult>("/invoices/batch", args),
    onSuccess: (res, vars) => {
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: ["invoices"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
      alert(
        `${vars.action}: ${res.ok.length} ok` +
          (res.skipped.length ? `, ${res.skipped.length} пропущено` : "") +
          (res.not_found.length ? `, ${res.not_found.length} не найдено` : ""),
      );
    },
    onError: (e: Error) => alert(`Batch ошибка: ${e.message}`),
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
    if (!data) return;
    if (selected.size === data.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(data.map((inv) => inv.id)));
    }
  };

  const selArr = Array.from(selected);
  const hasSelection = selArr.length > 0;

  return (
    <div>
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <h1 className="text-2xl font-semibold">Invoices</h1>
        <select
          value={status}
          onChange={(e) => {
            setStatus(e.target.value as typeof status);
            setSelected(new Set());
          }}
          className="px-2 py-1 rounded bg-slate-800 border border-slate-700 text-sm"
        >
          <option value="">все</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
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

        {hasSelection && (
          <div className="flex items-center gap-2 ml-auto">
            <span className="text-xs text-slate-400">
              {selArr.length} выбрано
            </span>
            <button
              disabled={batch.isPending}
              onClick={() => {
                if (confirm(`Отменить ${selArr.length} инвойс(ов)? (pending → failed)`))
                  batch.mutate({ ids: selArr, action: "cancel" });
              }}
              className="text-xs px-2 py-1 rounded bg-red-800 hover:bg-red-700 disabled:opacity-50"
            >
              cancel all
            </button>
            <button
              disabled={batch.isPending}
              onClick={() => {
                if (confirm(`Mark paid ${selArr.length} инвойс(ов)?`))
                  batch.mutate({ ids: selArr, action: "mark_paid" });
              }}
              className="text-xs px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50"
            >
              mark paid all
            </button>
            <button
              disabled={batch.isPending}
              onClick={() => {
                if (confirm(`Mark unpaid ${selArr.length} инвойс(ов)?`))
                  batch.mutate({ ids: selArr, action: "mark_unpaid" });
              }}
              className="text-xs px-2 py-1 rounded bg-yellow-700 hover:bg-yellow-600 disabled:opacity-50"
            >
              mark unpaid all
            </button>
          </div>
        )}
      </div>

      {isLoading ? (
        <div>Загрузка…</div>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-slate-400 border-b border-slate-700">
            <tr>
              <th className="py-2 w-8">
                <input
                  type="checkbox"
                  checked={!!data && data.length > 0 && selected.size === data.length}
                  onChange={toggleAll}
                  className="accent-blue-600"
                />
              </th>
              <th>ID</th>
              <th>Пользователь</th>
              <th>План</th>
              <th>Сумма</th>
              <th>Статус</th>
              <th>Действие</th>
              <th>Создан</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data?.map((inv) => (
              <tr
                key={inv.id}
                className={`border-b border-slate-800 ${
                  selected.has(inv.id) ? "bg-blue-950/30" : ""
                }`}
              >
                <td className="py-2">
                  <input
                    type="checkbox"
                    checked={selected.has(inv.id)}
                    onChange={() => toggleSelect(inv.id)}
                    className="accent-blue-600"
                  />
                </td>
                <td>{inv.id}</td>
                <td>
                  #{inv.user_id}
                  {inv.user_telegram_id ? ` · tg:${inv.user_telegram_id}` : ""}
                </td>
                <td>{inv.plan_name || "—"}</td>
                <td>
                  {inv.amount} {inv.currency}
                </td>
                <td>
                  <span
                    className={
                      inv.status === "paid"
                        ? "text-emerald-400"
                        : inv.status === "failed"
                          ? "text-red-400"
                          : "text-yellow-400"
                    }
                  >
                    {inv.status}
                  </span>
                </td>
                <td>{inv.action}</td>
                <td>{new Date(inv.created_at).toLocaleString()}</td>
                <td>
                  <div className="flex gap-1">
                    {inv.status === "pending" && (
                      <>
                        <button
                          disabled={markPaid.isPending}
                          onClick={() => {
                            if (confirm(`Пометить инвойс #${inv.id} как paid?`))
                              markPaid.mutate(inv.id);
                          }}
                          className="text-xs px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50"
                        >
                          mark paid
                        </button>
                        <button
                          disabled={cancelInvoice.isPending}
                          onClick={() => {
                            if (confirm(`Отменить инвойс #${inv.id}? (pending → failed)`))
                              cancelInvoice.mutate(inv.id);
                          }}
                          className="text-xs px-2 py-1 rounded bg-red-800 hover:bg-red-700 disabled:opacity-50"
                        >
                          cancel
                        </button>
                      </>
                    )}
                    {inv.status === "paid" && (
                      <button
                        disabled={markUnpaid.isPending}
                        onClick={() => {
                          if (
                            confirm(
                              `Вернуть инвойс #${inv.id} в pending?\n\nПодписка/девайсы НЕ будут отозваны — это только bookkeeping-фикс.`
                            )
                          )
                            markUnpaid.mutate(inv.id);
                        }}
                        className="text-xs px-2 py-1 rounded bg-yellow-700 hover:bg-yellow-600 disabled:opacity-50"
                      >
                        mark unpaid
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {data && data.length === 0 && (
              <tr>
                <td colSpan={9} className="py-4 text-slate-500 text-center">
                  Ничего нет
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
