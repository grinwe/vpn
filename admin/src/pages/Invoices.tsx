import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, InvoiceListItem } from "../api";

const STATUSES = ["pending", "paid", "failed"] as const;

export default function Invoices() {
  const qc = useQueryClient();
  const [status, setStatus] = useState<(typeof STATUSES)[number] | "">("pending");
  const [limit, setLimit] = useState(50);

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

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <h1 className="text-2xl font-semibold">Invoices</h1>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value as typeof status)}
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
      </div>

      {isLoading ? (
        <div>Загрузка…</div>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-slate-400 border-b border-slate-700">
            <tr>
              <th className="py-2">ID</th>
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
              <tr key={inv.id} className="border-b border-slate-800">
                <td className="py-2">{inv.id}</td>
                <td>
                  #{inv.user_id}
                  {inv.user_telegram_id ? ` · tg:${inv.user_telegram_id}` : ""}
                </td>
                <td>{inv.plan_name}</td>
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
                  {inv.status === "pending" && (
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
                </td>
              </tr>
            ))}
            {data && data.length === 0 && (
              <tr>
                <td colSpan={8} className="py-4 text-slate-500 text-center">
                  Ничего нет
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      {markPaid.isError && (
        <p className="text-red-400 mt-3 text-sm">
          Ошибка: {String(markPaid.error)}
        </p>
      )}
    </div>
  );
}
