import { useEffect, useState } from "react";
import { fetchTransactions, TransactionRow } from "../api";
import { navigate } from "../router";

const PAGE = 50;

export default function History() {
  const [items, setItems] = useState<TransactionRow[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function loadMore() {
    setLoading(true);
    try {
      const res = await fetchTransactions(PAGE, items.length);
      setItems((prev) => [...prev, ...res.items]);
      setHasMore(res.has_more);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadMore();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="min-h-screen p-4 max-w-xl mx-auto">
      <header className="mb-4 flex items-center justify-between">
        <button onClick={() => navigate({ name: "home" })} className="text-tg-link">
          ← Назад
        </button>
        <h1 className="text-xl font-semibold">История</h1>
        <span className="w-12" />
      </header>

      {error && (
        <div className="card border-red-500/40 text-red-200 text-sm mb-3">
          {error}
        </div>
      )}

      {items.length === 0 && !loading && !error && (
        <div className="card text-center text-tg-hint text-sm">
          Пока нет операций.
        </div>
      )}

      <ul className="space-y-2">
        {items.map((t) => (
          <li
            key={t.id}
            className="card !p-3 flex items-start justify-between"
          >
            <div className="min-w-0 pr-2">
              <div className="text-sm font-medium">{kindLabel(t.kind)}</div>
              <div className="text-tg-hint text-xs">
                {new Date(t.created_at).toLocaleString("ru-RU")}
              </div>
              {t.note && (
                <div className="text-tg-hint text-xs truncate">{t.note}</div>
              )}
            </div>
            <div
              className={`text-sm font-bold whitespace-nowrap ${
                t.amount_kopecks >= 0 ? "text-green-400" : "text-red-300"
              }`}
            >
              {t.amount_kopecks >= 0 ? "+" : ""}
              {(t.amount_kopecks / 100).toFixed(0)} ₽
            </div>
          </li>
        ))}
      </ul>

      {hasMore && (
        <button
          onClick={loadMore}
          disabled={loading}
          className="btn-ghost w-full mt-4"
        >
          {loading ? "Загрузка…" : "Показать ещё"}
        </button>
      )}
    </div>
  );
}

function kindLabel(kind: TransactionRow["kind"]): string {
  switch (kind) {
    case "topup":
      return "Пополнение";
    case "spend":
      return "Списание";
    case "refund":
      return "Возврат";
    case "bonus":
      return "Бонус";
    case "adjust":
      return "Корректировка";
  }
}
