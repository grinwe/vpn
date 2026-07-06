// Shared traffic-light для relay↔exit WG-туннелей. Используется на
// двух страницах (Exits.tsx — по exit'у, Nodes.tsx — по relay'ю): точки
// в строке таблицы + легенда в раскрытой панели. Поэтому пороги и цвета
// живут здесь, чтобы не расходиться при правках.

export interface LinkHealthInput {
  last_handshake_at: string | null;
  last_observed_at: string | null;
}

export interface LinkHealthVerdict {
  color: string;
  label: string;
  title: string;
}

export function linkHealth(
  l: LinkHealthInput,
  activeUsers?: number,
  // Опорное «сейчас» в мс. Возраст handshake/observed надо мерить от
  // серверного времени, а не от локальных часов оператора: перекос часов
  // браузера (WSL/VM/ноут после сна) инвертирует диагностику — свежий
  // handshake уезжает в жёлтый/красный, а протухший — в зелёный. Callers
  // (Exits/Nodes) должны прокидывать server_now, полученный из ответа
  // backend (поле server_now / заголовок Date). Дефолт Date.now() —
  // обратная совместимость, пока источник серверного времени не прокинут.
  nowMs?: number,
): LinkHealthVerdict {
  // Светофор для relay↔exit туннеля. Цвет считаем от возраста
  // последнего handshake'а (WG keepalive = 25s, значит healthy peer
  // handshook в последние 3 минуты). SSH-фейл (observed_at stale)
  // рендерится как красный, потому что за >15 min должен был успеть
  // пройти хотя бы один тик.
  const now = nowMs ?? Date.now();
  if (!l.last_observed_at) {
    return {
      color: "bg-slate-600",
      label: "—",
      title: "Тик ещё не прошёл — данных нет",
    };
  }
  const observedAgeMin = (now - Date.parse(l.last_observed_at)) / 60000;
  if (observedAgeMin > 15) {
    return {
      color: "bg-red-500",
      label: `ssh ${Math.round(observedAgeMin)}m`,
      title: `SSH-тик не доходил ${Math.round(observedAgeMin)} минут — relay недоступен?`,
    };
  }
  if (!l.last_handshake_at) {
    // Idle: на ноде 0 юзеров → нет трафика → WG не делает handshake.
    // Это простой, а не обрыв — не красим в красный (см. node 4vds-ru-kmr:
    // нода жива, SSH/observed свежий, но юзеров нет → туннели «горели»).
    if (activeUsers === 0) {
      return {
        color: "bg-slate-500",
        label: "idle",
        title:
          "0 юзеров на ноде — WG без трафика не делает handshake; туннель простаивает, не порван",
      };
    }
    return {
      color: "bg-red-500",
      label: "no hs",
      title: "WG peer в dump есть, но handshake ни разу не случился",
    };
  }
  const handshakeAgeMin = (now - Date.parse(l.last_handshake_at)) / 60000;
  if (handshakeAgeMin < 3) {
    return {
      color: "bg-green-500",
      label: `${Math.round(handshakeAgeMin)}m`,
      title: `Последний handshake ${Math.round(handshakeAgeMin)} минут назад`,
    };
  }
  if (handshakeAgeMin < 15) {
    return {
      color: "bg-yellow-500",
      label: `${Math.round(handshakeAgeMin)}m`,
      title: `Последний handshake ${Math.round(handshakeAgeMin)} минут назад — туннель простаивает`,
    };
  }
  // Stale handshake, НО 0 юзеров → idle, не обрыв. Красный оставляем
  // только когда юзеры есть, а handshake протух (вот это реальная поломка).
  if (activeUsers === 0) {
    return {
      color: "bg-slate-500",
      label: `idle ${Math.round(handshakeAgeMin)}m`,
      title: `handshake ${Math.round(handshakeAgeMin)} мин назад, но 0 юзеров на ноде — туннель простаивает (нет трафика → нет keepalive), не порван`,
    };
  }
  return {
    color: "bg-red-500",
    label: `${Math.round(handshakeAgeMin)}m`,
    title: `Последний handshake ${Math.round(handshakeAgeMin)} минут назад — скорее всего порвался`,
  };
}

interface HealthDotsProps<L extends LinkHealthInput> {
  links: L[];
  // Как назвать пир в тултипе точки. На Exits это "relay-01 · wg0",
  // на Nodes — "exit-foreign-01 · wg0". Ключ — стабильный id для React.
  peerLabel: (l: L) => string;
  peerKey: (l: L) => string | number;
  // Активные юзеры на relay-ноде. 0 → stale/нет-handshake трактуем как
  // idle (серый), а не обрыв (красный). Не передан (Exits) → старое
  // поведение. Передаётся из Nodes.tsx (n.active_users).
  activeUsers?: number;
  // Опорное «сейчас» с сервера (мс). Прокидывается в linkHealth(), чтобы
  // возраст мерялся от серверного времени, а не от часов оператора. Не
  // передан → Date.now() (обратная совместимость).
  nowMs?: number;
}

export function HealthDots<L extends LinkHealthInput>({
  links,
  peerLabel,
  peerKey,
  activeUsers,
  nowMs,
}: HealthDotsProps<L>) {
  // Ряд цветных точек — по одной на relay↔exit линк. Пустой массив →
  // серый дефис (линков нет).
  if (links.length === 0) {
    return <span className="text-slate-600 text-xs">—</span>;
  }
  return (
    <div className="flex gap-1 items-center">
      {links.map((l) => {
        const h = linkHealth(l, activeUsers, nowMs);
        return (
          <span
            key={peerKey(l)}
            className={`inline-block w-2.5 h-2.5 rounded-full ${h.color}`}
            title={`${peerLabel(l)}: ${h.label} — ${h.title}`}
          />
        );
      })}
    </div>
  );
}
