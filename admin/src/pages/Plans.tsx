import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, PlanOut, PlanCreateIn } from "../api";

const EMPTY_FORM: PlanCreateIn = {
  name: "",
  duration_days: 30,
  max_devices: 3,
  price: 0,
  traffic_limit_mb: null,
  is_visible: true,
};

export default function Plans() {
  const qc = useQueryClient();
  const [form, setForm] = useState<PlanCreateIn>(EMPTY_FORM);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { data, isLoading } = useQuery<PlanOut[]>({
    queryKey: ["plans"],
    queryFn: () => api.get("/plans"),
  });

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setEditingId(null);
    setError(null);
  };

  const createPlan = useMutation({
    mutationFn: (payload: PlanCreateIn) => api.post<PlanOut>("/plans", payload),
    onSuccess: () => {
      resetForm();
      qc.invalidateQueries({ queryKey: ["plans"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  const updatePlan = useMutation({
    mutationFn: (payload: { id: number; body: Partial<PlanCreateIn> }) =>
      api.put<PlanOut>(`/plans/${payload.id}`, payload.body),
    onSuccess: () => {
      resetForm();
      qc.invalidateQueries({ queryKey: ["plans"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  const deletePlan = useMutation({
    mutationFn: (id: number) => api.del(`/plans/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["plans"] }),
    onError: (e: Error) => {
      // Backend returns 409 when the plan still has subscriptions —
      // surface that next to the action the user just clicked, not in
      // the sidebar form they aren't looking at.
      alert(`Не удалось удалить тариф: ${e.message}`);
      setError(e.message);
    },
  });

  const toggleVisibility = useMutation({
    mutationFn: (plan: PlanOut) =>
      api.put<PlanOut>(`/plans/${plan.id}`, { is_visible: !plan.is_visible }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["plans"] }),
    onError: (e: Error) => alert(`Ошибка: ${e.message}`),
  });

  const startEdit = (plan: PlanOut) => {
    setEditingId(plan.id);
    setForm({
      name: plan.name,
      duration_days: plan.duration_days,
      max_devices: plan.max_devices,
      price: plan.price,
      traffic_limit_mb: plan.traffic_limit_mb,
      is_visible: plan.is_visible,
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
      updatePlan.mutate({ id: editingId, body: form });
    } else {
      createPlan.mutate(form);
    }
  };

  const busy = createPlan.isPending || updatePlan.isPending;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-2">
        <h1 className="text-2xl font-semibold mb-4">Тарифы</h1>

        {isLoading ? (
          <div>Загрузка…</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-slate-400 border-b border-slate-700">
              <tr>
                <th className="py-2">ID</th>
                <th>Имя</th>
                <th>Дней</th>
                <th>Устройств</th>
                <th>Цена ₽</th>
                <th>Трафик МБ</th>
                <th>Видим</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data?.map((p) => (
                <tr key={p.id} className="border-b border-slate-800">
                  <td className="py-2">{p.id}</td>
                  <td className="font-mono">{p.name}</td>
                  <td>{p.duration_days}</td>
                  <td>{p.max_devices}</td>
                  <td>{p.price}</td>
                  <td>{p.traffic_limit_mb ?? "∞"}</td>
                  <td>{p.is_visible ? "✓" : "✕"}</td>
                  <td className="text-right space-x-1">
                    <button
                      onClick={() => startEdit(p)}
                      className="text-xs px-2 py-1 rounded bg-slate-700 hover:bg-slate-600"
                    >
                      edit
                    </button>
                    <button
                      onClick={() => toggleVisibility.mutate(p)}
                      className="text-xs px-2 py-1 rounded bg-slate-700 hover:bg-slate-600"
                      title={p.is_visible ? "Скрыть от пользователей" : "Показывать пользователям"}
                    >
                      {p.is_visible ? "hide" : "show"}
                    </button>
                    <button
                      onClick={() => {
                        if (confirm(`Удалить тариф "${p.name}"?\n\nЕсли у тарифа есть активные подписки — удаление не пройдёт, используйте hide.`))
                          deletePlan.mutate(p.id);
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
                  <td colSpan={8} className="py-4 text-slate-500 text-center">
                    Тарифов пока нет
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      <aside className="bg-slate-800 rounded-lg p-4 border border-slate-700 h-fit space-y-3">
        <h2 className="font-semibold">
          {editingId != null ? `Редактировать #${editingId}` : "Новый тариф"}
        </h2>

        <label className="block text-sm">
          Имя
          <input
            type="text"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="Месяц"
            className="mt-1 w-full px-3 py-2 rounded bg-slate-900 border border-slate-700"
          />
        </label>

        <label className="block text-sm">
          Длительность (дней)
          <input
            type="number"
            min={1}
            value={form.duration_days}
            onChange={(e) =>
              setForm({ ...form, duration_days: Number(e.target.value) })
            }
            className="mt-1 w-full px-3 py-2 rounded bg-slate-900 border border-slate-700"
          />
        </label>

        <label className="block text-sm">
          Макс. устройств
          <input
            type="number"
            min={1}
            value={form.max_devices}
            onChange={(e) =>
              setForm({ ...form, max_devices: Number(e.target.value) })
            }
            className="mt-1 w-full px-3 py-2 rounded bg-slate-900 border border-slate-700"
          />
        </label>

        <label className="block text-sm">
          Цена (₽)
          <input
            type="number"
            min={0}
            step="0.01"
            value={form.price}
            onChange={(e) => setForm({ ...form, price: Number(e.target.value) })}
            className="mt-1 w-full px-3 py-2 rounded bg-slate-900 border border-slate-700"
          />
        </label>

        <label className="block text-sm">
          Лимит трафика (МБ, пусто = без лимита)
          <input
            type="number"
            min={0}
            value={form.traffic_limit_mb ?? ""}
            onChange={(e) =>
              setForm({
                ...form,
                traffic_limit_mb:
                  e.target.value === "" ? null : Number(e.target.value),
              })
            }
            className="mt-1 w-full px-3 py-2 rounded bg-slate-900 border border-slate-700"
          />
        </label>

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={form.is_visible}
            onChange={(e) => setForm({ ...form, is_visible: e.target.checked })}
          />
          Виден пользователям
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
