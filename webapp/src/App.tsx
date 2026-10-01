import { useEffect, useState } from "react";
import { authWithInitData, fetchMe, MeResponse, setToken } from "./api";
import { friendlyError } from "./errors";
import Home from "./pages/Home";
import Plans from "./pages/Plans";
import CheckoutPending from "./pages/CheckoutPending";
import History from "./pages/History";
import Help from "./pages/Help";
import { useRoute } from "./router";
import { getTg } from "./telegram";

type Status = "loading" | "ready" | "error";

// Первый экран и фоновый рефреш — самые частые точки сетевых обрывов
// (Mini App открывают в метро/лифте). Один-два ретрая через пару секунд
// обычно проходят, поэтому не сдаёмся с первой неудачи.
const REFRESH_RETRIES = 2;
const RETRY_DELAY_MS = 2000;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Ошибки из api.ts прилетают как Error("401: ...") / Error("Failed to fetch").
function isAuthError(e: unknown): boolean {
  return /^(401|403)/.test((e as Error)?.message ?? "");
}

// Повторяет асинхронную операцию на транзиентных сбоях с фиксированной
// паузой между попытками.
async function withRetries<T>(
  op: () => Promise<T>,
  retries = REFRESH_RETRIES,
): Promise<T> {
  let lastErr: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      return await op();
    } catch (e) {
      lastErr = e;
      if (attempt < retries) await sleep(RETRY_DELAY_MS);
    }
  }
  throw lastErr;
}

// Одна попытка переавторизации через Telegram initData. initData живёт
// весь сеанс Mini App, поэтому протухший токен чиним прозрачно, не заставляя
// юзера закрывать и открывать приложение.
async function reauth(): Promise<boolean> {
  const tg = getTg();
  if (!tg || !tg.initData) return false;
  try {
    const auth = await authWithInitData(tg.initData);
    setToken(auth.token);
    return true;
  } catch {
    return false;
  }
}

// Тянет /me с прозрачной переавторизацией на 401/403 и ретраями на
// транзиентных сетевых сбоях. Заменяет прежние `.catch(() => undefined)`,
// из-за которых после протухания токена автообновление молча умирало и UI
// показывал устаревшие данные.
async function fetchMeResilient(): Promise<MeResponse> {
  return withRetries(async () => {
    try {
      return await fetchMe();
    } catch (e) {
      // Токен протух — переавторизуемся один раз и сразу повторяем запрос.
      if (isAuthError(e) && (await reauth())) {
        return await fetchMe();
      }
      throw e;
    }
  });
}

export default function App() {
  const [status, setStatus] = useState<Status>("loading");
  const [error, setError] = useState<string | null>(null);
  const [me, setMe] = useState<MeResponse | null>(null);
  const route = useRoute();

  // Стартовая авторизация + первый /me. Вынесено в bootstrap(), чтобы можно
  // было перезапустить те же шаги на транзиентном сбое (см. withRetries).
  const bootstrap = async () => {
    const tg = getTg();
    if (!tg || !tg.initData) {
      setError("Открой эту страницу из бота — initData отсутствует.");
      setStatus("error");
      return;
    }
    setStatus("loading");
    setError(null);
    try {
      const auth = await withRetries(() => authWithInitData(tg.initData));
      setToken(auth.token);
      const data = await fetchMeResilient();
      setMe(data);
      setStatus("ready");
    } catch (e) {
      // Сырой текст исключения/JSON юзеру не показываем — прогоняем через
      // friendlyError.
      setError(friendlyError((e as Error).message));
      setStatus("error");
    }
  };

  useEffect(() => {
    void bootstrap();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Реакция на восстановление связи и возврат в приложение. VPN-Mini-App живёт
  // в webview, чей сетевой маршрут рвётся при каждом toggling VPN и смене
  // Wi-Fi↔LTE, а также когда приложение сворачивают и открывают снова. Без этого
  // экран стартовой ошибки становился вечным тупиком (сеть уже вернулась, а UI
  // висит на «Ошибке»), а свёрнутый кабинет показывал устаревший остаток дней.
  // По событию: если мы в error — перезапускаем bootstrap(); если ready —
  // тихо дёргаем устойчивый рефреш /me.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    // Дебаунс: при частой смене сети во время toggling VPN события online/
    // visibility сыплются пачками — схлопываем их в один запрос.
    const trigger = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        if (status === "error") {
          void bootstrap();
        } else if (status === "ready") {
          fetchMeResilient().then(setMe).catch(() => undefined);
        }
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
    // status в зависимостях: эффект переподписывается при смене статуса, чтобы
    // обработчик всегда видел актуальное значение (свежее замыкание).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  // Re-fetch /me whenever the user lands back on the home screen — covers
  // the post-checkout case where a fresh subscription should now show up.
  useEffect(() => {
    if (status !== "ready") return;
    if (route.name !== "home") return;
    // fetchMeResilient сам переавторизуется на 401 и ретраит сеть; финальную
    // неудачу тихо глотаем (баннер «данные устарели» — новый UI-элемент, а он
    // требует отдельного согласования), но молчаливой смерти на протухшем
    // токене больше нет.
    fetchMeResilient().then(setMe).catch(() => undefined);
  }, [route.name, status]);

  // E2.8 — скелет вместо слова «Загрузка…»: запрос ретраится до ~45 с на
  // плохой сети, и всё это время экран выглядел мёртвым.
  if (status === "loading")
    return (
      <Centered>
        <div className="w-full max-w-sm space-y-3">
          <div className="card animate-pulse h-24" />
          <div className="card animate-pulse h-16" />
          <div className="text-tg-hint text-xs text-center">Загружаем кабинет…</div>
        </div>
      </Centered>
    );
  // E2.5 — раньше это был терминальный экран без единого тапа: юзер мог только
  // закрыть приложение (автоповтор случался лишь по событиям online/visibility).
  if (status === "error")
    return (
      <Centered>
        <div className="card border-red-500/40 text-red-200 max-w-sm">
          <div className="font-semibold mb-1">Не удалось загрузить кабинет</div>
          <div className="text-tg-hint text-sm">{error}</div>
          <button className="btn-primary w-full mt-4" onClick={() => bootstrap()}>
            Повторить
          </button>
          <div className="text-tg-hint text-xs mt-3">
            Если не помогает — напиши в поддержку в чате бота.
          </div>
        </div>
      </Centered>
    );

  const refreshMe = () => {
    // Единственный механизм синхронизации UI после мутаций Home.tsx: если
    // рефреш упадёт, юзер увидит состояние, противоречащее только что
    // выполненному действию. Поэтому переавторизуемся на 401 и ретраим сеть.
    fetchMeResilient().then(setMe).catch(() => undefined);
  };

  if (route.name === "plans") return <Plans onActivated={refreshMe} subLinkBase={me?.sub_link_base_url ?? ""} me={me!} changeSubscriptionId={route.subscriptionId} />;
  if (route.name === "history") return <History />;
  if (route.name === "help") return <Help botUsername={me?.bot_username} />;
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
