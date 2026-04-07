import { useEffect, useState } from "react";
import { authWithInitData, fetchMe, MeResponse, setToken } from "./api";
import Home from "./pages/Home";
import Plans from "./pages/Plans";
import CheckoutPending from "./pages/CheckoutPending";
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

  if (status === "loading") return <Centered>Загрузка…</Centered>;
  if (status === "error")
    return (
      <Centered>
        <div className="text-red-400 mb-2">Ошибка</div>
        <div className="text-tg-hint text-sm">{error}</div>
      </Centered>
    );

  if (route.name === "plans") return <Plans />;
  if (route.name === "checkout") return <CheckoutPending invoiceId={route.invoiceId} />;
  return <Home me={me!} />;
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex items-center justify-center p-6 text-center">
      <div>{children}</div>
    </div>
  );
}
