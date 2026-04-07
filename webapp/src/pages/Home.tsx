import { useState } from "react";
import { MeResponse, Subscription } from "../api";
import { navigate } from "../router";

export default function Home({ me }: { me: MeResponse }) {
  const active = me.subscriptions.filter((s) => s.status === "active");
  const nearestExpiry = active
    .map((s) => new Date(s.expires_at).getTime())
    .sort((a, b) => a - b)[0];

  return (
    <div className="min-h-screen p-4 max-w-xl mx-auto">
      <header className="mb-6">
        <div className="text-tg-hint text-sm">Личный кабинет</div>
        <div className="text-2xl font-semibold">
          {me.user.telegram_id ? `@${me.user.telegram_id}` : "Гость"}
        </div>
        {nearestExpiry ? (
          <div className="text-tg-hint text-sm mt-1">
            Активна до {new Date(nearestExpiry).toLocaleDateString("ru-RU")}
          </div>
        ) : (
          <div className="text-tg-hint text-sm mt-1">Нет активных подписок</div>
        )}
      </header>

      <section className="space-y-3 mb-6">
        <h2 className="text-sm uppercase tracking-wide text-tg-hint">Мои подписки</h2>
        {me.subscriptions.length === 0 ? (
          <div className="bg-tg-secondaryBg rounded-xl p-4 text-tg-hint text-sm">
            У вас пока нет подписок. Купите тариф ниже.
          </div>
        ) : (
          me.subscriptions.map((s) => <SubscriptionCard key={s.id} sub={s} />)
        )}
      </section>

      <button
        className="w-full py-3 rounded-xl bg-tg-button text-tg-buttonText font-semibold"
        onClick={() => navigate({ name: "plans" })}
      >
        Купить тариф
      </button>
    </div>
  );
}

function SubscriptionCard({ sub }: { sub: Subscription }) {
  const [showConfig, setShowConfig] = useState(false);
  const subUrl = sub.sub_token ? `/api/sub/${sub.sub_token}` : null;

  return (
    <div className="bg-tg-secondaryBg rounded-xl p-4">
      <div className="flex justify-between items-start mb-1">
        <div className="font-semibold">{sub.plan_name}</div>
        <StatusBadge status={sub.status} />
      </div>
      <div className="text-tg-hint text-sm">
        {sub.node} · {sub.region}
      </div>
      <div className="text-tg-hint text-xs mt-1">
        до {new Date(sub.expires_at).toLocaleDateString("ru-RU")}
      </div>
      {subUrl && (
        <button
          onClick={() => setShowConfig((v) => !v)}
          className="mt-3 text-sm text-tg-link underline"
        >
          {showConfig ? "Скрыть конфиг" : "Показать конфиг"}
        </button>
      )}
      {showConfig && subUrl && (
        <div className="mt-2 bg-tg-bg rounded p-2 text-xs break-all font-mono">
          {window.location.origin}
          {subUrl}
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const color =
    status === "active"
      ? "bg-green-700 text-green-100"
      : status === "expired"
        ? "bg-yellow-700 text-yellow-100"
        : "bg-red-700 text-red-100";
  return (
    <span className={`text-xs px-2 py-0.5 rounded ${color}`}>{status}</span>
  );
}
