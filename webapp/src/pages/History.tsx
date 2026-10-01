import { useEffect, useRef, useState } from "react";
import { fetchTransactions, TransactionRow } from "../api";
import { friendlyError } from "../errors";
import { navigate } from "../router";

const PAGE = 50;

export default function History() {
  const [items, setItems] = useState<TransactionRow[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Рефы для обработчиков online/visibility: замыкание живёт весь сеанс и
  // должно видеть актуальные loading/error без переподписки.
  const loadingRef = useRef(false);
  const errorRef = useRef<string | null>(null);
  const loadMoreRef = useRef<() => void>(() => {});
  loadingRef.current = loading;
  errorRef.current = error;

  async function loadMore() {
    // Не запускаем параллельную загрузку (авто-ретрай + ручной тап).
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    // Сбрасываем прошлый баннер, иначе он висит навсегда даже после успеха.
    setError(null);
    try {
      const res = await fetchTransactions(PAGE, items.length);
      setItems((prev) => [...prev, ...res.items]);
      setHasMore(res.has_more);
    } catch (e) {
      // Человекочитаемый текст вместо сырого «timeout»/«401: {...}».
      setError(
        friendlyError((e as Error).message, { fallback: "загрузить историю" }),
      );
    } finally {
      setLoading(false);
    }
  }

  // Держим актуальную ссылку на loadMore, чтобы обработчики online/visibility
  // видели свежий items.length (иначе авто-ретрай дублировал бы страницу 0).
  loadMoreRef.current = loadMore;

  useEffect(() => {
    loadMore();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Авто-повтор зависшей загрузки при восстановлении сети/возврате в приложение,
  // чтобы пустой экран с ошибкой не оставался тупиком без способа обновиться.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const trigger = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        if (errorRef.current && !loadingRef.current) loadMoreRef.current();
      }, 500);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") trigger();
    };
    window.addEventListener("online", trigger);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      if (timer) clearTimeout(timer);
      window.removeEventListener("online", trigger);
      document.removeEventListener("visibilitychange", onVisibility);
    };
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
