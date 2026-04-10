import { useEffect, useState } from "react";
import { authWithInitData, fetchMe, MeResponse, setToken } from "./api";
import Home from "./pages/Home";
import Plans from "./pages/Plans";
import CheckoutPending from "./pages/CheckoutPending";
import History from "./pages/History";
import { useRoute } from "./router";
import { getTg } from "./telegram";

type Status = "loading" | "ready" | "error";

export default function App() {
  const [status, setStatus] = useState<Status>("loading");
  const [error, setError] = useState<string | null>(null);
  const [me, setMe] = useState<MeResponse | null>(null);
  const route = useRoute();

  useEffect(() => {
    (async () => {
      const tg = getTg();
      if (!tg || !tg.initData) {
        setError("Открой эту страницу из бота — initData отсутствует.");
        setStatus("error");
        return;
      }
      try {
        const auth = await authWithInitData(tg.initData);
        setToken(auth.token);
        const data = await fetchMe();
        setMe(data);
        setStatus("ready");
      } catch (e) {
        setError((e as Error).message);
        setStatus("error");
      }
    })();
  }, []);

  // Re-fetch /me whenever the user lands back on the home screen — covers
  // the post-checkout case where a fresh subscription should now show up.
  useEffect(() => {
    if (status !== "ready") return;
    if (route.name !== "home") return;
    fetchMe().then(setMe).catch(() => undefined);
  }, [route.name, status]);

  if (status === "loading")
    return (
      <Centered>
        <div className="card animate-pulse text-tg-hint">Загрузка…</div>
      </Centered>
    );
  if (status === "error")
    return (
      <Centered>
        <div className="card border-red-500/40 text-red-200">
          <div className="font-semibold mb-1">Ошибка</div>
          <div className="text-tg-hint text-sm">{error}</div>
        </div>
      </Centered>
    );

  const refreshMe = () => {
    fetchMe().then(setMe).catch(() => undefined);
  };

  if (route.name === "plans") return <Plans onActivated={refreshMe} />;
  if (route.name === "history") return <History />;
  if (route.name === "checkout") return <CheckoutPending invoiceId={route.invoiceId} />;
  return <Home me={me!} onRefresh={refreshMe} />;
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex items-center justify-center p-6 text-center">
      <div>{children}</div>
    </div>
  );
}
