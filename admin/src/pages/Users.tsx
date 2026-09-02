import { useSearchParams } from "react-router-dom";
import { useEffect, useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  adminReportFailureForSubscription,
  api,
  batchBanUsers,
  bulkMigrateAuto,
  bulkRebuildConfig,
  bulkRegenerateSublink,
  claimOrphanSubscription,
  DeviceMigrateOut,
  DeviceNodeOut,
  DeviceNodeSetOut,
  DeviceOut,
  DeviceSwitchExitOut,
  getDeviceNodes,
  listUserNodeBans,
  swapDeviceNode,
  migrateSubscriptionAuto,
  NodeRelayLinkOut,
  NodeUserBanOut,
  removeUserNodeBan,
  SubscriptionMigrateOut,
  SubscriptionOut,
  SubscriptionSwitchExitOut,
  UserOut,
  VPNNodeOut,
  adminTopupByTelegram,
} from "../api";

// Backend caps `limit` at 200; 50 keeps each page snappy and makes "Load
// more" feel incremental rather than dumping a wall of rows at once.
const PAGE_SIZE = 50;

// Filter tab values.  Keep in sync with backend _apply_banned_filter
// (backend/app/api/users.py) — "all" means "no filter", not "empty".
type BannedFilter = "all" | "active" | "banned";

// Человекочитаемый текст сетевой ошибки для показа в существующем слоте UI.
/** Светофор «активен за 24ч»: зелёный — трафик юзера видели за последние
 *  сутки (Device.last_seen_at, штампует тик traffic_stats), красный — нет,
 *  в том числе «не подключался ни разу». Точное время — в тултипе. */
function ActivityDot({ lastActiveAt }: { lastActiveAt: string | null }) {
  const active =
    lastActiveAt !== null &&
    Date.now() - new Date(lastActiveAt).getTime() < 24 * 60 * 60 * 1000;
  return (
    <span
      className={`inline-block w-2.5 h-2.5 rounded-full ${
        active ? "bg-green-500" : "bg-red-500"
      }`}
      title={
        lastActiveAt
          ? `Последняя активность: ${new Date(lastActiveAt).toLocaleString(
              "ru-RU",
              { dateStyle: "short", timeStyle: "short" },
            )}`
          : "Активности не было"
      }
    />
  );
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export default function Users() {
  const qc = useQueryClient();
  // Переход из других разделов: /users?telegram_id=123 (ссылки из списка нод,
  // из активных юзеров ноды). До 2026-07-26 параметр не читался вовсе, и такая
  // ссылка открывала просто общий список — то есть переход «кред → юзер»
  // формально существовал, но никуда не вёл.
  const [searchParams, setSearchParams] = useSearchParams();
  const urlTelegramId = searchParams.get("telegram_id") ?? "";
  const [search, setSearch] = useState(urlTelegramId);
  // Debounce the search input so we don't hammer the backend on every
  // keystroke — 300ms is the sweet spot between "feels instant" and
  // "one request per full word".
  const [debouncedSearch, setDebouncedSearch] = useState("");
  useEffect(() => {
    const id = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(id);
  }, [search]);
  // Tab filter.  Default "active" — banned accounts are usually ban-waves
  // of hundreds of rows that the operator only wants to see intentionally
  // (either to review the wave or to batch-unban a false positive).
  // Приходя по ссылке на конкретного юзера, показываем ВСЕХ: дефолтный
  // фильтр «активные» скрыл бы забаненного, и переход выглядел бы как
  // «юзер не найден».
  const [banned, setBanned] = useState<BannedFilter>(
    urlTelegramId ? "all" : "active",
  );
  // Выбранный юзер храним как id, а сам объект деривим из загруженного
  // списка (см. `selected` ниже). Так любой refetch списка (после ban,
  // batch-операций, пополнения) автоматически перерисовывает сайдбар —
  // не нужно вручную патчить снапшот в каждом onSuccess (иначе сайдбар
  // показывает устаревшее состояние: незабаненного юзера с кнопкой ban).
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [topupRub, setTopupRub] = useState("");
  const [topupNote, setTopupNote] = useState("");
  // claim-orphan form state — operator pastes either a bare UUID or
  // the full vless:// URL the user sent from Hiddify. Device name is
  // optional (existing device's name is kept if blank).
  const [claimInput, setClaimInput] = useState("");
  const [claimDeviceName, setClaimDeviceName] = useState("");
  // Bulk selection lives next to single-row selection. Single-row
  // selection (`selected`) drives the detail sidebar; `selectedIds` is
  // the set used by bulk ban/unban. They are intentionally independent —
  // you can check a row for a bulk op without opening its details, and
  // vice versa.
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  // Anchor for shift+click range selection.  Stores the *index* in the
  // currently-rendered list so the range is stable even while the list
  // is being paginated (new pages append at the bottom, existing rows
  // don't shift).  Reset whenever the filter or search changes — the
  // list would reorder and the anchor would be meaningless.
  const [lastClickedIndex, setLastClickedIndex] = useState<number | null>(null);
  useEffect(() => {
    setLastClickedIndex(null);
    setSelectedIds(new Set());
  }, [banned, debouncedSearch]);

  const {
    data: usersData,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isFetchNextPageError,
    isLoading,
    // error/isError раньше не читались: при падении /users список молча
    // рендерил пустую таблицу — оператор принимал упавший backend за
    // «юзеров нет». Теперь показываем текст ошибки вместо пустого списка.
    isError,
    error,
  } = useInfiniteQuery({
    queryKey: ["users", { search: debouncedSearch, banned }],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams();
      params.set("limit", String(PAGE_SIZE));
      params.set("offset", String(pageParam));
      if (debouncedSearch) params.set("search", debouncedSearch);
      if (banned !== "all") params.set("banned", banned);
      return api.get<UserOut[]>(`/users?${params.toString()}`);
    },
    // Short page = we drained the server. Otherwise bump offset by the
    // total count so far — the list is ordered by id DESC on the backend,
    // which is stable enough for paginated admin browsing (new signups
    // appear at the top, not mid-page).
    getNextPageParam: (lastPage, allPages) => {
      if (lastPage.length < PAGE_SIZE) return undefined;
      return allPages.reduce((n, p) => n + p.length, 0);
    },
  });
  const users = usersData?.pages.flat() ?? [];
  // Деривим выбранного юзера из актуальных данных списка — единственный
  // источник правды. Если юзер выпал из текущего фильтра (напр. забанен
  // на вкладке «Активные»), selected → null и сайдбар закрывается: это
  // корректно отражает реальность, а не показывает застывший снапшот.
  const selected = users.find((u) => u.id === selectedId) ?? null;

  // Пришли по ссылке — открываем карточку сами, как только юзер нашёлся.
  // Ждать, пока оператор ткнёт в единственную строку, незачем: он уже сказал,
  // кого хочет увидеть.
  useEffect(() => {
    if (!urlTelegramId || selectedId !== null) return;
    const hit = users.find((u) => String(u.telegram_id) === urlTelegramId);
    if (hit) {
      setSelectedId(hit.id);
      // Параметр отработал — убираем из URL, чтобы «назад» и последующая
      // ручная фильтрация не тянули его обратно.
      searchParams.delete("telegram_id");
      setSearchParams(searchParams, { replace: true });
    }
  }, [urlTelegramId, users, selectedId, searchParams, setSearchParams]);

  const {
    data: subs,
    // Раньше состояние сайдбара определялось только по `subs === undefined`:
    // при ошибке запроса data остаётся undefined и панель висела в вечном
    // «Загрузка…». Теперь различаем loading / error / empty.
    isLoading: subsLoading,
    isError: subsError,
    error: subsErrObj,
  } = useQuery<SubscriptionOut[]>({
    queryKey: ["user-subs", selectedId],
    queryFn: () => api.get(`/users/${selectedId}`),
    enabled: selectedId !== null,
  });

  // Only loaded when a user is selected — the list is used to populate
  // per-sub migrate dropdowns. Filtered client-side to is_active=true
  // (the endpoint validates is_active anyway, but hiding disabled nodes
  // up-front avoids the "why does my pick 400?" surprise).
  const { data: allNodes } = useQuery<VPNNodeOut[]>({
    queryKey: ["nodes-for-migrate"],
    queryFn: () => api.get(`/nodes`),
    enabled: selectedId !== null,
  });

  const topup = useMutation({
    mutationFn: ({
      telegramId,
      amountKopecks,
      note,
    }: {
      telegramId: string;
      amountKopecks: number;
      note: string;
    }) => adminTopupByTelegram(telegramId, amountKopecks, note),
    onSuccess: () => {
      // Баланс в сайдбаре обновится сам через invalidateQueries(["users"])
      // → refetch списка → derive selected. Ручной патч снапшота больше
      // не нужен.
      setTopupRub("");
      setTopupNote("");
      qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (e: Error) => alert(`Не удалось пополнить: ${e.message}`),
  });

  const claimOrphan = useMutation({
    mutationFn: ({
      userId,
      uuidOrUrl,
      deviceName,
    }: {
      userId: number;
      uuidOrUrl: string;
      deviceName: string;
    }) =>
      claimOrphanSubscription({
        user_id: userId,
        uuid: uuidOrUrl,
        device_name: deviceName || null,
      }),
    onSuccess: (res) => {
      setClaimInput("");
      setClaimDeviceName("");
      const expires = new Date(res.new_expires_at).toLocaleString();
      const protos = res.claimed_credentials.map((c) => c.proto).join(", ");
      alert(
        `Подписка #${res.subscription_id} передана user_id=${res.new_user_id}.\n` +
          `Девайс #${res.device_id}. Протоколы: ${protos}.\n` +
          `expires_at: ${expires}.\n` +
          `Existing connection в Hiddify не прерывается — UUID остался прежним.`,
      );
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (e: Error) => alert(`Не удалось восстановить: ${e.message}`),
  });

  const revokeNow = useMutation({
    mutationFn: (subId: number) =>
      api.post(`/subscriptions/${subId}/disable`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
    onError: (e: Error) => alert(`Не удалось отозвать подписку: ${e.message}`),
  });

  const enableSub = useMutation({
    mutationFn: (subId: number) =>
      api.post(`/subscriptions/${subId}/enable`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
    onError: (e: Error) => alert(`Не удалось включить подписку: ${e.message}`),
  });

  const revokeDevice = useMutation({
    mutationFn: (deviceId: number) =>
      api.post(`/devices/${deviceId}/revoke`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
    onError: (e: Error) => alert(`Не удалось отвязать девайс: ${e.message}`),
  });

  const addDevice = useMutation({
    mutationFn: (subId: number) =>
      api.post(`/subscriptions/${subId}/devices`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
    },
    onError: (e: Error) => alert(`Не удалось привязать девайс: ${e.message}`),
  });

  const unblockSharing = useMutation({
    mutationFn: (subId: number) =>
      api.post(`/subscriptions/${subId}/unblock-sharing`, {}),
    onSuccess: () => {
      alert("Unblock отправлен на ноду. Enforcer подхватит в течение 10 секунд.");
      qc.invalidateQueries({ queryKey: ["user-subs"] });
    },
    onError: (e: Error) => alert(`Не удалось разблокировать: ${e.message}`),
  });

  const migrateSub = useMutation({
    mutationFn: ({ subId, targetNodeId }: { subId: number; targetNodeId: number }) =>
      api.post<SubscriptionMigrateOut>(`/subscriptions/${subId}/migrate`, {
        target_node_id: targetNodeId,
      }),
    onSuccess: (res) => {
      alert(
        `Подписка #${res.subscription_id} переведена: ${res.old_node_name} → ${res.new_node_name}. Провижнинг-таска #${res.provisioning_task_id ?? "—"} запущена, следи в Tasks.`,
      );
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
    },
    onError: (e: Error) => alert(`Не удалось перевести: ${e.message}`),
  });

  // «Обновить подписку»: авто-выбор свободного сервера из пула (исключая
  // ноды из бан-листа юзера) + миграция + авто-бан старой ноды. В отличие
  // от ручного MigrateSubControl админ НЕ выбирает target — backend сам
  // берёт наименее загруженный healthy сервер.
  const migrateAuto = useMutation({
    mutationFn: (subId: number) => migrateSubscriptionAuto(subId),
    onSuccess: (res) => {
      const banMsg = res.banned_old_node
        ? " Старая нода добавлена в бан-лист юзера (авто-выбор туда больше не вернёт)."
        : "";
      alert(
        `Подписка #${res.subscription_id} переведена на свободный сервер: ` +
          `${res.old_node_name} → ${res.new_node_name}. Таск #${res.provisioning_task_id ?? "—"} — следи в Tasks.${banMsg}`,
      );
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      qc.invalidateQueries({ queryKey: ["user-node-bans"] });
    },
    onError: (e: Error) => alert(`Не удалось обновить подписку: ${e.message}`),
  });

  const switchExit = useMutation({
    mutationFn: ({ subId, exitId }: { subId: number; exitId: number }) =>
      api.post<SubscriptionSwitchExitOut>(
        `/subscriptions/${subId}/switch-exit`,
        { exit_id: exitId },
      ),
    onSuccess: (res) => {
      alert(
        `Подписка #${res.subscription_id} переведена на exit #${res.new_exit_id} (${res.new_interface}). Запущено таск: ${res.task_ids.length} — следи в Tasks.`,
      );
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      qc.invalidateQueries({ queryKey: ["relay-links"] });
    },
    onError: (e: Error) => alert(`Не удалось сменить exit: ${e.message}`),
  });

  // Control-channel admin trigger — оператор имитирует сигнал клиента,
  // backend select_target_node + migrate_subscription_to_new_node.
  // Используется когда юзер написал в саппорт через 2й канал (e-mail,
  // друг с работающим VPN), и его нужно срочно переселить на healthy
  // ноду без custom-клиента (Phase B).
  const reportFailure = useMutation({
    mutationFn: (subId: number) =>
      adminReportFailureForSubscription({
        subscription_id: subId,
        kind: "user_reported",
      }),
    onSuccess: (res) => {
      const detail =
        res.action === "migrated"
          ? `Подписка #${res.subscription_id} переведена на ноду #${res.target_node_id} (${res.target_node_name}). Таск #${res.task_id ?? "—"} — следи в /tasks.`
          : res.action === "throttled"
            ? `Уже мигрировали #${res.subscription_id} в последние 5 мин — подожди ${res.retry_after_sec}s.`
            : res.action === "no_target_available"
              ? `❌ Нет healthy target ноды для #${res.subscription_id}. Проверь /admin/nodes — все active/unmuted?`
              : res.action === "subscription_inactive"
                ? `Подписка #${res.subscription_id} не active — нечего мигрировать.`
                : `action=${res.action} (retry через ${res.retry_after_sec}s)`;
      alert(detail);
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
    },
    onError: (e: Error) =>
      alert(`Не удалось имитировать сигнал: ${e.message}`),
  });

  const migrateDevice = useMutation({
    mutationFn: ({
      deviceId,
      targetNodeId,
    }: {
      deviceId: number;
      targetNodeId: number;
    }) =>
      api.post<DeviceMigrateOut>(`/devices/${deviceId}/migrate`, {
        target_node_id: targetNodeId,
      }),
    onSuccess: (res) => {
      alert(
        `Device #${res.old_device_id} → #${res.device_id}: ${res.old_node_name} → ${res.new_node_name}. Таск провиженинга #${res.provisioning_task_id ?? "—"} в фоне.`,
      );
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
    },
    onError: (e: Error) => alert(`Не удалось переселить устройство: ${e.message}`),
  });

  const switchDeviceExit = useMutation({
    mutationFn: ({ deviceId, exitId }: { deviceId: number; exitId: number }) =>
      api.post<DeviceSwitchExitOut>(`/devices/${deviceId}/switch-exit`, {
        exit_id: exitId,
      }),
    onSuccess: (res) => {
      alert(
        `Device #${res.device_id} переключён на exit #${res.new_exit_id} (${res.new_interface}). Запущено таск: ${res.task_ids.length}.`,
      );
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      qc.invalidateQueries({ queryKey: ["relay-links"] });
    },
    onError: (e: Error) => alert(`Не удалось сменить exit устройства: ${e.message}`),
  });

  const batchBan = useMutation({
    mutationFn: ({
      ids,
      action,
    }: {
      ids: number[];
      action: "ban" | "unban";
    }) => batchBanUsers(ids, action),
    onSuccess: (res) => {
      alert(
        `${res.action === "ban" ? "Забанено" : "Разбанено"}: ${res.done.length}` +
          (res.skipped.length
            ? `, пропущено: ${res.skipped.length} (уже в нужном состоянии)`
            : "") +
          (res.not_found.length
            ? `, не найдено: ${res.not_found.length}`
            : ""),
      );
      setSelectedIds(new Set());
      qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (e: Error) => alert(`Batch ban/unban ошибка: ${e.message}`),
  });

  // Single-user ban / unban toggle — reused by the details sidebar.
  // Piggybacks on batch_ban so there's one audit path on the backend.
  const singleBan = useMutation({
    mutationFn: ({ id, action }: { id: number; action: "ban" | "unban" }) =>
      batchBanUsers([id], action),
    onSuccess: () => {
      // banned_at в сайдбаре подтянется из refetch списка (derive selected).
      // На вкладке «Активные» забаненный юзер выпадет из списка и сайдбар
      // закроется — это ожидаемо и отражает реальное состояние.
      qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (e: Error) => alert(`Не удалось изменить статус бана: ${e.message}`),
  });

  // Shift+click range selection.  When the user clicks a checkbox with
  // shift held we toggle every row between the last-clicked index and
  // the current index to the *new* state of the current row (matches
  // GitHub / Gmail semantics — one click selects, shift-click extends
  // the same action to the range).  Without shift we just toggle one
  // row and remember its index as the new anchor.
  const toggleRowSelected = (id: number, index: number, shift: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      const willSelect = !next.has(id);
      if (shift && lastClickedIndex !== null) {
        const [lo, hi] = [
          Math.min(lastClickedIndex, index),
          Math.max(lastClickedIndex, index),
        ];
        for (let i = lo; i <= hi; i++) {
          const row = users[i];
          if (!row) continue;
          if (willSelect) next.add(row.id);
          else next.delete(row.id);
        }
      } else if (willSelect) {
        next.add(id);
      } else {
        next.delete(id);
      }
      return next;
    });
    setLastClickedIndex(index);
  };

  // "Select all filtered" — pulls every id matching current search/tab
  // (capped server-side at 5000) and adds them to the selection set.
  // The admin UI scenario: spam-wave of 250 bots, filter tab → Banned
  // (or search for "bot_"), one click to select all, batch_ban in 500-
  // chunks.  We don't clear existing selection — the operator may have
  // hand-picked some rows first and want to extend.
  const selectAllFiltered = useMutation({
    mutationFn: async () => {
      const params = new URLSearchParams();
      if (debouncedSearch) params.set("search", debouncedSearch);
      if (banned !== "all") params.set("banned", banned);
      const qs = params.toString();
      return api.get<number[]>(`/users/ids${qs ? `?${qs}` : ""}`);
    },
    onSuccess: (ids) => {
      setSelectedIds((prev) => {
        const next = new Set(prev);
        for (const id of ids) next.add(id);
        return next;
      });
    },
    onError: (e: Error) => alert(`Не удалось получить список id: ${e.message}`),
  });

  const selArr = Array.from(selectedIds);
  const hasSelection = selArr.length > 0;

  // batch_ban hard-caps at 500 ids per request.  For large waves we
  // chunk the selection and fire sequential requests so the user can
  // one-click ban 2000+ rows without hitting the validator.
  const BATCH_CHUNK = 500;
  const runBatchBan = async (ids: number[], action: "ban" | "unban") => {
    if (ids.length <= BATCH_CHUNK) {
      batchBan.mutate({ ids, action });
      return;
    }
    const chunks: number[][] = [];
    for (let i = 0; i < ids.length; i += BATCH_CHUNK) {
      chunks.push(ids.slice(i, i + BATCH_CHUNK));
    }
    let done = 0;
    let skipped = 0;
    let notFound = 0;
    for (const c of chunks) {
      try {
        const res = await batchBanUsers(c, action);
        done += res.done.length;
        skipped += res.skipped.length;
        notFound += res.not_found.length;
      } catch (e) {
        alert(
          `Batch ${action} упал на чанке: ${e instanceof Error ? e.message : String(e)}.\n\nУспешно обработано до падения: ${done}.`,
        );
        break;
      }
    }
    alert(
      `${action === "ban" ? "Забанено" : "Разбанено"}: ${done}` +
        (skipped ? `, пропущено: ${skipped} (уже в нужном состоянии)` : "") +
        (notFound ? `, не найдено: ${notFound}` : ""),
    );
    setSelectedIds(new Set());
    qc.invalidateQueries({ queryKey: ["users"] });
  };

  // Both bulk-subscription ops fire ansible per device and the backend
  // caps user_ids at 25/request (ansible backlog safety — see the 2026
  // incidents). Chunk the selection the same way runBatchBan does. One
  // shared `bulkBusy` flag disables both buttons while either runs so the
  // operator can't double-fire a heavy provisioning wave.
  const BULK_SUBS_CHUNK = 25;
  const [bulkBusy, setBulkBusy] = useState(false);
  // Слать ли Telegram-нудж «возьми новую ссылку» при bulk-регенерации.
  // Дефолт OFF — массовая регенерация почти всегда операционная (старая
  // ссылка живёт), спамить юзеров не надо. Галкой включаешь для честного
  // «выдать новый линк».
  const [notifyOnRegen, setNotifyOnRegen] = useState(false);

  const runBulkRegenerate = async (ids: number[], notify: boolean) => {
    setBulkBusy(true);
    let done = 0,
      skipped = 0,
      notFound = 0,
      failed = 0,
      notified = 0,
      subs = 0,
      devices = 0;
    try {
      for (let i = 0; i < ids.length; i += BULK_SUBS_CHUNK) {
        const res = await bulkRegenerateSublink(
          ids.slice(i, i + BULK_SUBS_CHUNK),
          notify,
        );
        done += res.done.length;
        skipped += res.skipped.length;
        notFound += res.not_found.length;
        failed += res.failed.length;
        notified += res.notified.length;
        subs += res.subscriptions_regenerated;
        devices += res.devices_created;
      }
    } catch (e) {
      alert(
        `Перегенерация упала на чанке: ${e instanceof Error ? e.message : String(e)}.\n\n` +
          `Успешно до падения: юзеров ${done}, подписок ${subs}.`,
      );
      setBulkBusy(false);
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      return;
    }
    alert(
      `Перегенерация sub-link готова.\n` +
        `Юзеров обновлено: ${done}` +
        (skipped ? `, без активных подписок: ${skipped}` : "") +
        (notFound ? `, не найдено: ${notFound}` : "") +
        (failed ? `, ошибок по подпискам: ${failed}` : "") +
        `.\nПодписок: ${subs}, новых устройств: ${devices}, уведомлено в Telegram: ${notified}.\n\n` +
        `Провижининг идёт в фоне — следи в Tasks. Старые ссылки остаются живыми до перехода.`,
    );
    setSelectedIds(new Set());
    setBulkBusy(false);
    qc.invalidateQueries({ queryKey: ["users"] });
    qc.invalidateQueries({ queryKey: ["user-subs"] });
    qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
  };

  const runBulkMigrate = async (ids: number[]) => {
    setBulkBusy(true);
    let done = 0,
      skipped = 0,
      notFound = 0,
      failed = 0,
      subs = 0;
    try {
      for (let i = 0; i < ids.length; i += BULK_SUBS_CHUNK) {
        const res = await bulkMigrateAuto(ids.slice(i, i + BULK_SUBS_CHUNK));
        done += res.done.length;
        skipped += res.skipped.length;
        notFound += res.not_found.length;
        failed += res.failed.length;
        subs += res.subscriptions_migrated;
      }
    } catch (e) {
      alert(
        `Переезд упал на чанке: ${e instanceof Error ? e.message : String(e)}.\n\n` +
          `Успешно до падения: юзеров ${done}, подписок ${subs}.`,
      );
      setBulkBusy(false);
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
      return;
    }
    alert(
      `Массовый переезд запущен.\n` +
        `Юзеров: ${done}` +
        (skipped ? `, без активных подписок: ${skipped}` : "") +
        (notFound ? `, не найдено: ${notFound}` : "") +
        (failed ? `, ошибок (нет свободной ноды и т.п.): ${failed}` : "") +
        `.\nПодписок переезжает: ${subs}. Старые ноды забанены для этих юзеров.\n\n` +
        `Провижининг в фоне — следи в Tasks.`,
    );
    setSelectedIds(new Set());
    setBulkBusy(false);
    qc.invalidateQueries({ queryKey: ["users"] });
    qc.invalidateQueries({ queryKey: ["user-subs"] });
    qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
    qc.invalidateQueries({ queryKey: ["user-node-bans"] });
  };

  // Тихая пересборка config_text из текущего VPNConfig: без ротации
  // токена, нового устройства, ansible и пуша. Под починку вшитых URI
  // после правки конфигов (xhttp sni/port и т.п.).
  const runBulkRebuild = async (ids: number[]) => {
    setBulkBusy(true);
    let done = 0,
      skipped = 0,
      notFound = 0,
      failed = 0,
      creds = 0;
    try {
      for (let i = 0; i < ids.length; i += BULK_SUBS_CHUNK) {
        const res = await bulkRebuildConfig(ids.slice(i, i + BULK_SUBS_CHUNK));
        done += res.done.length;
        skipped += res.skipped.length;
        notFound += res.not_found.length;
        failed += res.failed.length;
        creds += res.credentials_rebuilt;
      }
    } catch (e) {
      alert(
        `Пересборка упала на чанке: ${e instanceof Error ? e.message : String(e)}.\n\n` +
          `Успешно до падения: юзеров ${done}.`,
      );
      setBulkBusy(false);
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      return;
    }
    alert(
      `Пересборка config_text готова (тихо, без пуша).\n` +
        `Юзеров: ${done}` +
        (skipped ? `, без активных подписок: ${skipped}` : "") +
        (notFound ? `, не найдено: ${notFound}` : "") +
        (failed ? `, ошибок по подпискам: ${failed}` : "") +
        `.\nПересобрано кредов: ${creds}. Клиенты подтянут исправленный URI на следующем рефреше сабки (sub_token не менялся).`,
    );
    setSelectedIds(new Set());
    setBulkBusy(false);
    qc.invalidateQueries({ queryKey: ["users"] });
    qc.invalidateQueries({ queryKey: ["user-subs"] });
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-2">
        <div className="flex items-center gap-3 mb-4 flex-wrap">
          <h1 className="text-2xl font-semibold">Users</h1>
          {hasSelection && (
            <div className="flex items-center gap-2 ml-auto">
              <span className="text-xs text-slate-400">
                {selArr.length} выбрано
              </span>
              <button
                disabled={batchBan.isPending}
                onClick={() => setSelectedIds(new Set())}
                className="text-xs px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50"
              >
                снять выделение
              </button>
              <button
                disabled={batchBan.isPending}
                onClick={() => {
                  if (
                    confirm(
                      `Забанить ${selArr.length} юзер(ов)?\n\nБот будет молча дропать все апдейты от этих Telegram-аккаунтов. Подписки НЕ затрагиваются.`,
                    )
                  )
                    runBatchBan(selArr, "ban");
                }}
                className="text-xs px-2 py-1 rounded bg-red-700 hover:bg-red-600 disabled:opacity-50"
              >
                ban all
              </button>
              <button
                disabled={batchBan.isPending}
                onClick={() => {
                  if (confirm(`Разбанить ${selArr.length} юзер(ов)?`))
                    runBatchBan(selArr, "unban");
                }}
                className="text-xs px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50"
              >
                unban all
              </button>
              <span className="w-px h-4 bg-slate-600" />
              <label
                className="flex items-center gap-1 text-[11px] text-slate-400 select-none"
                title="Слать ли юзерам Telegram «возьми новую ссылку» при регенерации"
              >
                <input
                  type="checkbox"
                  checked={notifyOnRegen}
                  onChange={(e) => setNotifyOnRegen(e.target.checked)}
                  disabled={bulkBusy}
                />
                уведомить
              </label>
              <button
                disabled={bulkBusy}
                onClick={() => {
                  if (
                    confirm(
                      `Перегенерировать sub-link для ${selArr.length} юзер(ов)?\n\n` +
                        `Каждому активному устройству выдаётся НОВАЯ ссылка (появится в ЛК), ` +
                        `старая остаётся рабочей до перехода.\n` +
                        (notifyOnRegen
                          ? `Юзерам УЙДЁТ Telegram: «возьми новую ссылку в ЛК».\n`
                          : `Telegram-уведомление НЕ шлётся (галка «уведомить» снята).\n`) +
                        `\nЭто НЕ переезд на другой сервер. sub_token сменится, стоимость НЕ изменится.\n` +
                        `Идёт ansible-провижининг — для больших пачек подними воркеры (scripts/workers.sh).`,
                    )
                  )
                    runBulkRegenerate(selArr, notifyOnRegen);
                }}
                title="Новая ссылка в ЛК (+ Telegram по галке «уведомить»), старая ссылка остаётся живой. sub_token меняется, сервер тот же, цена та же."
                className="text-xs px-2 py-1 rounded bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50"
              >
                {bulkBusy ? "…" : "🔁 регенерация sub-link"}
              </button>
              <button
                disabled={bulkBusy}
                onClick={() => {
                  if (
                    confirm(
                      `Переселить ${selArr.length} юзер(ов) на свободные серверы?\n\n` +
                        `Каждая активная подписка авто-переезжает на свободную здоровую ноду, ` +
                        `старая нода банится для юзера. sub_token СОХРАНЯЕТСЯ — ссылка та же, меняется только сервер.\n\n` +
                        `Это НЕ регенерация ссылки. Тяжёлый ansible (revoke+apply на каждое устройство) — ` +
                        `подними воркеры (scripts/workers.sh) перед большой пачкой.`,
                    )
                  )
                    runBulkMigrate(selArr);
                }}
                title="bulk-версия карточной «обновить подписку»: авто-выбор свободной ноды + бан старой. sub_token сохраняется, уведомления нет."
                className="text-xs px-2 py-1 rounded bg-sky-700 hover:bg-sky-600 disabled:opacity-50"
              >
                {bulkBusy ? "…" : "🚚 переезд на сервер"}
              </button>
              <button
                disabled={bulkBusy}
                onClick={() => {
                  if (
                    confirm(
                      `Пересобрать config_text для ${selArr.length} юзер(ов)?\n\n` +
                        `ТИХО пересобирает вшитые ссылки из ТЕКУЩЕГО VPNConfig — ` +
                        `без смены sub_token, нового устройства, ansible и пуша. ` +
                        `Клиент сам подтянет исправленный URI на рефреше сабки.\n\n` +
                        `Под починку конфигов (напр. xhttp sni/port после правок в БД). Безопасно.`,
                    )
                  )
                    runBulkRebuild(selArr);
                }}
                title="Тихо пересобрать config_text из текущего VPNConfig: без токена/ansible/пуша. Под починку вшитых URI."
                className="text-xs px-2 py-1 rounded bg-teal-700 hover:bg-teal-600 disabled:opacity-50"
              >
                {bulkBusy ? "…" : "🧩 пересобрать конфиг"}
              </button>
            </div>
          )}
        </div>
        {/* Tabs — filter by ban state.  "all" shows both, "active"
            hides banned (default), "banned" isolates the wave so the
            operator can select-all + unban if it was a false positive. */}
        <div className="flex items-center gap-2 mb-3 text-xs">
          {(
            [
              ["active", "Активные"],
              ["banned", "Забаненные"],
              ["all", "Все"],
            ] as [BannedFilter, string][]
          ).map(([val, label]) => (
            <button
              key={val}
              onClick={() => setBanned(val)}
              className={`px-3 py-1 rounded ${
                banned === val
                  ? "bg-blue-700 text-white"
                  : "bg-slate-800 hover:bg-slate-700 text-slate-300"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2 mb-4">
          <input
            type="text"
            placeholder="Поиск по telegram_id или email…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="flex-1 px-3 py-2 rounded bg-slate-800 border border-slate-700"
          />
          <button
            onClick={() => {
              if (
                !confirm(
                  `Выделить всех юзеров по текущему фильтру (${banned === "all" ? "все" : banned === "active" ? "активные" : "забаненные"}${debouncedSearch ? `, поиск «${debouncedSearch}»` : ""})?\n\nБэкенд отдаёт до 5000 id за один запрос.`,
                )
              )
                return;
              selectAllFiltered.mutate();
            }}
            disabled={selectAllFiltered.isPending}
            className="px-3 py-2 text-xs rounded bg-slate-700 hover:bg-slate-600 whitespace-nowrap disabled:opacity-50"
            title="Загружает id всех юзеров под фильтром и добавляет их в выделение"
          >
            {selectAllFiltered.isPending
              ? "Загружаем…"
              : "выделить всё по фильтру"}
          </button>
        </div>
        {isLoading ? (
          <div>Загрузка…</div>
        ) : isError ? (
          // Сетевой сбой /users показываем в том же слоте, где рендерился
          // «Загрузка…», а не пустой таблицей — иначе «Загружено: 0» неотличимо
          // от реального отсутствия юзеров. Кнопку повтора не добавляем:
          // react-query сам перезапросит при фокусе окна / восстановлении сети.
          <div className="text-red-400 text-sm">
            Не удалось загрузить список пользователей: {errText(error)}
          </div>
        ) : (
          <>
            <table className="w-full text-sm">
              <thead className="text-left text-slate-400 border-b border-slate-700">
                <tr>
                  <th className="py-2 w-8">
                    <input
                      type="checkbox"
                      checked={
                        users.length > 0 &&
                        users.every((u) => selectedIds.has(u.id))
                      }
                      onChange={() => {
                        const allVisibleSelected = users.every((u) =>
                          selectedIds.has(u.id),
                        );
                        setSelectedIds((prev) => {
                          const next = new Set(prev);
                          if (allVisibleSelected) {
                            for (const u of users) next.delete(u.id);
                          } else {
                            for (const u of users) next.add(u.id);
                          }
                          return next;
                        });
                      }}
                      className="accent-blue-600"
                      title="Выделить / снять выделение со всех видимых"
                    />
                  </th>
                  <th className="py-2">ID</th>
                  <th>Telegram</th>
                  <th>Email</th>
                  <th>Subs</th>
                  <th title="Был ли трафик юзера за последние 24 часа">24ч</th>
                  <th>Баланс</th>
                  <th>Создан</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u, idx) => (
                  <tr
                    key={u.id}
                    onClick={() => setSelectedId(u.id)}
                    className={`cursor-pointer border-b border-slate-800 hover:bg-slate-800 ${
                      selected?.id === u.id ? "bg-slate-800" : ""
                    } ${selectedIds.has(u.id) ? "bg-blue-950/30" : ""} ${
                      u.banned_at ? "text-red-300" : ""
                    }`}
                  >
                    <td
                      className="py-2"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <input
                        type="checkbox"
                        checked={selectedIds.has(u.id)}
                        onClick={(e) =>
                          toggleRowSelected(u.id, idx, e.shiftKey)
                        }
                        onChange={() => {
                          /* handled in onClick so we get shiftKey */
                        }}
                        className="accent-blue-600"
                      />
                    </td>
                    <td className="py-2">{u.id}</td>
                    <td>
                      {u.telegram_id ?? "—"}
                      {u.banned_at && (
                        <span className="ml-2 inline-block text-[10px] px-1.5 py-0.5 rounded bg-red-900/60 text-red-200 border border-red-700/50 align-middle">
                          banned
                        </span>
                      )}
                    </td>
                    <td>{u.email ?? "—"}</td>
                    <td>{u.subscription_count}</td>
                    <td>
                      <ActivityDot lastActiveAt={u.last_active_at} />
                    </td>
                    <td>{(u.balance_kopecks / 100).toFixed(2)} ₽</td>
                    <td>
                      {new Date(u.created_at).toLocaleString("ru-RU", {
                        dateStyle: "short",
                        timeStyle: "short",
                      })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mt-4 flex items-center gap-3 text-sm text-slate-400">
              <span>Загружено: {users.length}</span>
              {hasNextPage && (
                <button
                  onClick={() => fetchNextPage()}
                  disabled={isFetchingNextPage}
                  className="px-3 py-1 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50"
                >
                  {isFetchingNextPage ? "Загружаем…" : "Загрузить ещё"}
                </button>
              )}
              {/* Ошибку подгрузки следующей страницы раньше проглатывали молча —
                  показываем её рядом с кнопкой (повторный клик перезапросит). */}
              {isFetchNextPageError && (
                <span className="text-red-400">
                  не удалось подгрузить — нажми «Загрузить ещё» ещё раз
                </span>
              )}
            </div>
          </>
        )}
      </div>

      <aside className="bg-slate-800 rounded-lg p-4 border border-slate-700 h-fit">
        <h2 className="font-semibold mb-2">Детали</h2>
        {!selected ? (
          <p className="text-sm text-slate-400">Выбери пользователя в таблице.</p>
        ) : (
          <div className="space-y-2 text-sm">
            <div>ID: {selected.id}</div>
            <div>
              Telegram: {selected.telegram_id ?? "—"}
              {selected.banned_at && (
                <span className="ml-2 inline-block text-[10px] px-1.5 py-0.5 rounded bg-red-900/60 text-red-200 border border-red-700/50 align-middle">
                  banned
                </span>
              )}
            </div>
            <div>Email: {selected.email ?? "—"}</div>
            <div>
              Баланс:{" "}
              <span className="font-mono">
                {(selected.balance_kopecks / 100).toFixed(2)} ₽
              </span>
            </div>
            <div className="flex gap-2">
              {selected.banned_at ? (
                <button
                  disabled={singleBan.isPending}
                  onClick={() => {
                    if (confirm(`Разбанить юзера #${selected.id}?`))
                      singleBan.mutate({ id: selected.id, action: "unban" });
                  }}
                  className="text-xs px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50"
                >
                  unban
                </button>
              ) : (
                <button
                  disabled={singleBan.isPending}
                  onClick={() => {
                    if (
                      confirm(
                        `Забанить юзера #${selected.id}?\n\nБот будет молча дропать все апдейты. Подписки НЕ затрагиваются.`,
                      )
                    )
                      singleBan.mutate({ id: selected.id, action: "ban" });
                  }}
                  className="text-xs px-2 py-1 rounded bg-red-700 hover:bg-red-600 disabled:opacity-50"
                >
                  ban
                </button>
              )}
            </div>

            <div className="pt-2 border-t border-slate-700">
              <div className="font-semibold mb-1">Пополнить баланс</div>
              {selected.telegram_id ? (
                <div className="space-y-2">
                  <input
                    type="number"
                    step="0.01"
                    min="0.01"
                    placeholder="Сумма, ₽"
                    value={topupRub}
                    onChange={(e) => setTopupRub(e.target.value)}
                    className="w-full px-2 py-1 rounded bg-slate-900 border border-slate-700"
                  />
                  <input
                    type="text"
                    placeholder="Комментарий (необязательно)"
                    value={topupNote}
                    onChange={(e) => setTopupNote(e.target.value)}
                    maxLength={200}
                    className="w-full px-2 py-1 rounded bg-slate-900 border border-slate-700"
                  />
                  <button
                    disabled={topup.isPending || !topupRub}
                    onClick={() => {
                      const rub = parseFloat(topupRub);
                      if (!isFinite(rub) || rub <= 0) {
                        alert("Введи положительную сумму в рублях");
                        return;
                      }
                      const kop = Math.round(rub * 100);
                      if (
                        !confirm(
                          `Начислить ${rub.toFixed(2)} ₽ юзеру ${selected.telegram_id}?\n\nЗаписывается как kind=adjust с пометкой admin_topup, в ledger появится отдельная строка.`,
                        )
                      )
                        return;
                      topup.mutate({
                        telegramId: selected.telegram_id!,
                        amountKopecks: kop,
                        note: topupNote,
                      });
                    }}
                    className="w-full text-xs px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50"
                  >
                    {topup.isPending ? "Начисляем…" : "Пополнить"}
                  </button>
                </div>
              ) : (
                <div className="text-xs text-slate-400">
                  Нет telegram_id — пополнение по ID не поддержано (у юзера
                  только email). Добавь telegram_id, чтобы пополнять.
                </div>
              )}
            </div>

            <div className="pt-2 border-t border-slate-700">
              <div className="font-semibold mb-1">
                Восстановить orphan-подписку
              </div>
              <div className="text-xs text-slate-400 mb-2">
                Передаёт подписку, висящую на placeholder-юзере{" "}
                <span className="font-mono">999999</span>, текущему
                выбранному юзеру. Подключение в Hiddify не прерывается —
                UUID на ноде не меняется. Подробнее: docs/operations/
                admin_claim_orphans.md.
              </div>
              <div className="space-y-2">
                <textarea
                  placeholder="UUID или vless://… ссылка от юзера"
                  rows={2}
                  value={claimInput}
                  onChange={(e) => setClaimInput(e.target.value)}
                  className="w-full px-2 py-1 rounded bg-slate-900 border border-slate-700 font-mono text-xs"
                />
                <input
                  type="text"
                  placeholder="Имя устройства (необязательно)"
                  value={claimDeviceName}
                  onChange={(e) => setClaimDeviceName(e.target.value)}
                  maxLength={64}
                  className="w-full px-2 py-1 rounded bg-slate-900 border border-slate-700"
                />
                <button
                  disabled={claimOrphan.isPending || !claimInput.trim()}
                  onClick={() => {
                    const value = claimInput.trim();
                    if (!value) return;
                    if (
                      !confirm(
                        `Восстановить orphan-подписку юзеру #${selected.id}` +
                          (selected.telegram_id
                            ? ` (tg ${selected.telegram_id})`
                            : "") +
                          `?\n\nПодписка будет передана от placeholder-юзера 999999. Existing connection в Hiddify не прервётся — UUID на ноде тот же.`,
                      )
                    )
                      return;
                    claimOrphan.mutate({
                      userId: selected.id,
                      uuidOrUrl: value,
                      deviceName: claimDeviceName.trim(),
                    });
                  }}
                  className="w-full text-xs px-2 py-1 rounded bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50"
                >
                  {claimOrphan.isPending
                    ? "Восстанавливаем…"
                    : "Восстановить"}
                </button>
              </div>
            </div>

            <div className="pt-2 border-t border-slate-700">
              <div className="font-semibold mb-1">Подписки</div>
              {subsLoading ? (
                <div className="text-slate-400">Загрузка…</div>
              ) : subsError ? (
                // Ошибка загрузки деталей юзера — показываем текст в том же
                // слоте, где висел «Загрузка…», размыкая вечный спиннер.
                // react-query перезапросит при фокусе окна / reconnect.
                <div className="text-red-400">
                  Не удалось загрузить подписки: {errText(subsErrObj)}
                </div>
              ) : !subs || subs.length === 0 ? (
                <div className="text-slate-400">Нет активных</div>
              ) : (
                <ul className="space-y-2">
                  {subs.map((s) => (
                    <li key={s.id} className="bg-slate-900 rounded p-2">
                      <div>{s.plan_name}</div>
                      <div className="text-slate-400">
                        {s.node} ({s.region}) · {s.status}
                      </div>
                      {/* Exit-нода для текущего роутинга юзера. Показываем
                          только если есть current_exit_id — на legacy 1:1
                          релеях поле NULL, показывать "exit: —" шумно. */}
                      {s.current_exit_id != null && (
                        <div className="text-slate-400 text-xs">
                          exit:{" "}
                          {s.current_exit_name ?? `#${s.current_exit_id}`}
                          {s.current_exit_name && (
                            <span className="text-slate-500">
                              {" "}
                              (#{s.current_exit_id})
                            </span>
                          )}
                        </div>
                      )}
                      <div className="text-slate-500 text-xs">
                        до {new Date(s.expires_at).toLocaleDateString()}
                      </div>
                      {s.devices && s.devices.length > 0 && (
                        <DeviceList
                          devices={s.devices}
                          revokeDevice={revokeDevice}
                          nodes={allNodes}
                          migrateDevice={migrateDevice}
                          switchDeviceExit={switchDeviceExit}
                        />
                      )}
                      <div className="mt-2 flex gap-2">
                        {s.status !== "blocked" && s.status !== "expired" && (
                          <button
                            disabled={revokeNow.isPending}
                            onClick={() => {
                              if (
                                confirm(
                                  `Отозвать подписку #${s.id} прямо сейчас?\n\nЮзер будет отключён от ноды через Ansible (1–2 мин).`
                                )
                              )
                                revokeNow.mutate(s.id);
                            }}
                            className="text-xs px-2 py-1 rounded bg-red-700 hover:bg-red-600 disabled:opacity-50"
                          >
                            ⏸ Отключить
                          </button>
                        )}
                        {s.status === "active" && (
                          <button
                            disabled={addDevice.isPending}
                            onClick={() => {
                              if (
                                confirm(
                                  `Добавить новый девайс к подписке #${s.id}?\n\nБудет запущен ansible-провижининг новой конфигурации.`
                                )
                              )
                                addDevice.mutate(s.id);
                            }}
                            className="text-xs px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50"
                          >
                            + add device
                          </button>
                        )}
                        {s.status === "active" && (
                          <button
                            disabled={reportFailure.isPending}
                            onClick={() => reportFailure.mutate(s.id)}
                            title="Имитирует client report failure: backend выберет healthy target ноду и перенесёт подписку. Используй когда юзер написал через 2й канал что VPN сломан и нет custom-клиента чтобы сигнал прислать."
                            className="text-xs px-2 py-1 rounded bg-orange-700 hover:bg-orange-600 disabled:opacity-50"
                          >
                            {reportFailure.isPending
                              ? "…"
                              : "🚨 report failure"}
                          </button>
                        )}
                        {s.status === "active" && (
                          <button
                            disabled={migrateAuto.isPending}
                            onClick={() => {
                              if (
                                confirm(
                                  `Обновить подписку #${s.id}?\n\nBackend выберет свободный сервер из пула (исключая текущий и ноды из бан-листа юзера), мигрирует подписку (sub_token сохранится) и забанит старую ноду для этого юзера.`
                                )
                              )
                                migrateAuto.mutate(s.id);
                            }}
                            title="Авто-выбор свободного сервера из пула + миграция + авто-бан старой ноды. В будущем — кнопка в ЛК юзера."
                            className="text-xs px-2 py-1 rounded bg-sky-700 hover:bg-sky-600 disabled:opacity-50"
                          >
                            {migrateAuto.isPending ? "…" : "🔄 обновить подписку"}
                          </button>
                        )}
                        {s.status !== "active" && (
                          <button
                            disabled={enableSub.isPending}
                            onClick={() => {
                              if (
                                confirm(
                                  `Реактивировать подписку #${s.id} (сейчас ${s.status})?\n\n` +
                                    `Статус → active, перепровижн одного девайса через Ansible. ` +
                                    `frozen разморозится с сохранением токена и годового бюджета. ` +
                                    `Если срок истёк (expired) — продлится на срок плана.`
                                )
                              )
                                enableSub.mutate(s.id);
                            }}
                            title="Вернуть подписку в active: expired/blocked → active+reprovision (expired продлевается на срок плана), frozen → unfreeze."
                            className="text-xs px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50"
                          >
                            {enableSub.isPending ? "…" : "✅ Реактивировать"}
                          </button>
                        )}
                        {s.sharing_blocked && (
                          <button
                            disabled={unblockSharing.isPending}
                            onClick={() => {
                              if (
                                confirm(
                                  `Снять sharing-бан для подписки #${s.id}?\n\nEnforcer заблокировал юзера за раздачу конфига. Команда unblock будет отправлена на ноду.`
                                )
                              )
                                unblockSharing.mutate(s.id);
                            }}
                            className="text-xs px-2 py-1 rounded bg-amber-700 hover:bg-amber-600 disabled:opacity-50"
                          >
                            снять sharing-бан
                          </button>
                        )}
                      </div>
                      {s.status === "active" && (
                        <MigrateSubControl
                          sub={s}
                          nodes={allNodes}
                          mutation={migrateSub}
                        />
                      )}
                      {(s.status === "active" || s.status === "frozen") && (
                        <SwitchExitControl
                          sub={s}
                          mutation={switchExit}
                        />
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <UserNodeBans userId={selected.id} />
          </div>
        )}
      </aside>
    </div>
  );
}

function DeviceCard({
  d,
  revokeDevice,
  nodes,
  migrateDevice,
  switchDeviceExit,
}: {
  d: DeviceOut;
  revokeDevice: { mutate: (id: number) => void; isPending: boolean };
  nodes: VPNNodeOut[] | undefined;
  migrateDevice: {
    mutate: (args: { deviceId: number; targetNodeId: number }) => void;
    isPending: boolean;
  };
  switchDeviceExit: {
    mutate: (args: { deviceId: number; exitId: number }) => void;
    isPending: boolean;
  };
}) {
  const [copied, setCopied] = useState(false);
  const uri = d.connection_uri ?? "";
  const exitLabel = d.exit_name
    ? `${d.exit_name}${d.exit_id ? ` (#${d.exit_id})` : ""}`
    : d.exit_id
      ? `#${d.exit_id}`
      : null;
  const isLive = d.status !== "revoked" && d.status !== "disabled";

  async function copyUri() {
    if (!uri) return;
    try {
      await navigator.clipboard.writeText(uri);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = uri;
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } catch {
        /* empty */
      }
      document.body.removeChild(ta);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="bg-slate-800 rounded px-2 py-1.5 text-xs space-y-1">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate">
          #{d.id}
          {d.name ? ` · ${d.name}` : ""} · {d.status}
          {d.is_relay && (
            <span className="ml-1 px-1 rounded bg-red-900/60 text-red-300">
              relay
            </span>
          )}
        </span>
        <button
          disabled={revokeDevice.isPending}
          onClick={() => {
            if (
              confirm(
                `Отвязать девайс #${d.id}?\n\nЮзер будет отключён от ноды через Ansible.`,
              )
            )
              revokeDevice.mutate(d.id);
          }}
          className="text-xs px-2 py-0.5 rounded bg-red-800 hover:bg-red-700 disabled:opacity-50"
        >
          unbind
        </button>
      </div>
      {(d.node_name || d.node_region || exitLabel) && (
        <div className="text-slate-400 text-[11px]">
          {d.node_name ?? "—"}
          {d.node_region && ` · ${d.node_region}`}
          {exitLabel && (
            <>
              {" → exit: "}
              <span className="text-slate-300">{exitLabel}</span>
            </>
          )}
        </div>
      )}
      {uri && (
        <div className="flex items-center gap-1">
          <code
            className="flex-1 truncate font-mono text-[11px] text-slate-300 bg-slate-900/60 rounded px-1 py-0.5"
            title={uri}
          >
            {uri}
          </code>
          <button
            onClick={copyUri}
            className="text-[11px] px-2 py-0.5 rounded bg-slate-700 hover:bg-slate-600"
          >
            {copied ? "✓" : "copy"}
          </button>
        </div>
      )}
      {isLive && (
        <DeviceNodeSet
          device={d}
          nodes={nodes}
          migrateDevice={migrateDevice}
        />
      )}
      {isLive && d.is_relay && d.node_id != null && (
        <SwitchDeviceExitControl
          device={d}
          mutation={switchDeviceExit}
        />
      )}
    </div>
  );
}

// Диверсная подписка (DIVERSE_SUB_NODES>1): набор RU-нод, на которых сидит
// device, + per-node «↻ заменить». Однонодовый device (набор ≤1 или ещё
// грузится) → показываем legacy-миграцию как было; для диверс-набора legacy
// migrate запрещён на бэке (схлопнул бы набор в одну ноду), поэтому вместо
// него — точечная замена одной ноды через swap_node_out.
function DeviceNodeSet({
  device,
  nodes,
  migrateDevice,
}: {
  device: DeviceOut;
  nodes: VPNNodeOut[] | undefined;
  migrateDevice: {
    mutate: (args: { deviceId: number; targetNodeId: number }) => void;
    isPending: boolean;
  };
}) {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery<DeviceNodeSetOut>({
    queryKey: ["device-nodes", device.id],
    queryFn: () => getDeviceNodes(device.id),
  });
  const swap = useMutation({
    mutationFn: (nodeId: number) => swapDeviceNode(device.id, nodeId),
    onSuccess: (res) => {
      alert(
        `Нода #${res.removed_node_id} убрана из набора device #${res.device_id}. ` +
          `Добрано свежих диверсных: ${res.added_nodes}. sub_token не менялся — ` +
          `клиент подхватит на обновлении подписки.`,
      );
      qc.invalidateQueries({ queryKey: ["device-nodes", device.id] });
      qc.invalidateQueries({ queryKey: ["user-subs"] });
      qc.invalidateQueries({ queryKey: ["provisioning-tasks"] });
    },
    onError: (e: Error) => alert(`Не удалось заменить ноду: ${e.message}`),
  });

  const set = data?.nodes ?? [];
  // Однонодовый девайс (или набор ещё грузится) → legacy «перевести на ноду».
  if (isLoading || set.length <= 1) {
    return (
      <MigrateDeviceControl
        device={device}
        nodes={nodes}
        mutation={migrateDevice}
      />
    );
  }

  // Диверс-набор (>1 ноды): список нод + точечная замена. Legacy-миграцию не
  // показываем — она схлопнула бы набор (бэк её для таких device запрещает).
  return (
    <div className="pt-1 border-t border-slate-700/60 space-y-1">
      <div className="text-[11px] text-slate-400">
        Ноды подписки ({set.length}):
      </div>
      {set.map((n: DeviceNodeOut) => (
        <div
          key={n.node_id}
          className="flex items-center justify-between gap-2 bg-slate-900/50 rounded px-1.5 py-1"
        >
          <span className="truncate text-[11px]">
            <span className="text-slate-300">{n.name ?? `#${n.node_id}`}</span>
            {n.region && <span className="text-slate-500"> · {n.region}</span>}
            {n.status && n.status !== "active" && (
              <span className="ml-1 px-1 rounded bg-amber-900/60 text-amber-300">
                {n.status}
              </span>
            )}
            {n.protocols.length > 0 && (
              <span className="text-slate-500"> · {n.protocols.join(", ")}</span>
            )}
          </span>
          <button
            disabled={swap.isPending}
            onClick={() => {
              if (
                confirm(
                  `Заменить ноду «${n.name ?? `#${n.node_id}`}» (#${n.node_id}) в наборе device #${device.id}?\n\n` +
                    `Нода убирается из подписки, взамен добирается свежая диверсная ` +
                    `(если есть тёплый запас на другом регионе). sub_token не меняется.`,
                )
              )
                swap.mutate(n.node_id);
            }}
            className="shrink-0 text-[11px] px-2 py-0.5 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50"
            title="Убрать эту ноду и добрать свежую взамен"
          >
            ↻ заменить
          </button>
        </div>
      ))}
    </div>
  );
}

// Per-device "move to specific node" control. Unlike MigrateSubControl,
// this keeps sub.node_id in place — the sub becomes split across nodes.
// Confirmation copy warns the admin that pool/health/cooldown bypass
// the same way as the sub-level override.
function MigrateDeviceControl({
  device,
  nodes,
  mutation,
}: {
  device: DeviceOut;
  nodes: VPNNodeOut[] | undefined;
  mutation: {
    mutate: (args: { deviceId: number; targetNodeId: number }) => void;
    isPending: boolean;
  };
}) {
  const [targetId, setTargetId] = useState<string>("");
  const candidates = (nodes ?? []).filter(
    (n) => n.is_active && n.id !== device.node_id,
  );
  if (candidates.length === 0) return null;
  const target = candidates.find((n) => String(n.id) === targetId);
  return (
    <div className="flex gap-1 items-center">
      <select
        value={targetId}
        onChange={(e) => setTargetId(e.target.value)}
        className="text-[11px] px-1 py-0.5 rounded bg-slate-800 border border-slate-700 flex-1"
      >
        <option value="">— переселить на ноду —</option>
        {candidates.map((n) => (
          <option key={n.id} value={String(n.id)}>
            #{n.id} {n.name} ({n.region})
          </option>
        ))}
      </select>
      <button
        disabled={mutation.isPending || !target}
        onClick={() => {
          if (!target) return;
          if (
            confirm(
              `Перевести устройство #${device.id} с ноды «${device.node_name ?? "—"}» на «${target.name}» (#${target.id}, ${target.region})?\n\n` +
                `Остальные устройства подписки остаются на текущей ноде. Пул/health/cooldown НЕ проверяются — ручной override. Старое устройство revoke'нется в фоне, новое поднимется через ansible.`,
            )
          )
            mutation.mutate({
              deviceId: device.id,
              targetNodeId: target.id,
            });
        }}
        className="text-[11px] px-2 py-0.5 rounded bg-blue-700 hover:bg-blue-600 disabled:opacity-50"
      >
        migrate
      </button>
    </div>
  );
}

// Per-device exit switch for devices on multi-link relays. Обновляет
// Credential.exit_id только у кредов этого device'а — соседи по
// подписке остаются на своём exit'е. Видно только когда links≥2.
function SwitchDeviceExitControl({
  device,
  mutation,
}: {
  device: DeviceOut;
  mutation: {
    mutate: (args: { deviceId: number; exitId: number }) => void;
    isPending: boolean;
  };
}) {
  const [targetId, setTargetId] = useState<string>("");
  const { data: links } = useQuery<NodeRelayLinkOut[]>({
    queryKey: ["relay-links", device.node_id],
    queryFn: () => api.get(`/nodes/${device.node_id}/links`),
    enabled: device.node_id != null,
  });
  if (!links || links.length < 2) return null;
  const candidates = links.filter((l) => l.exit_id !== device.exit_id);
  if (candidates.length === 0) return null;
  const target = candidates.find((l) => String(l.exit_id) === targetId);
  return (
    <div className="flex gap-1 items-center">
      <select
        value={targetId}
        onChange={(e) => setTargetId(e.target.value)}
        className="text-[11px] px-1 py-0.5 rounded bg-slate-800 border border-slate-700 flex-1"
      >
        <option value="">— сменить exit —</option>
        {candidates.map((l) => (
          <option key={l.exit_id} value={String(l.exit_id)}>
            #{l.exit_id} {l.exit_name ?? ""} ({l.wg_interface_name})
          </option>
        ))}
      </select>
      <button
        disabled={mutation.isPending || !target}
        onClick={() => {
          if (!target) return;
          if (
            confirm(
              `Переключить устройство #${device.id} с exit #${device.exit_id ?? "—"} на exit #${target.exit_id} (${target.wg_interface_name})?\n\n` +
                `Остальные устройства подписки не трогаются. reconcile_xray на relay'е сам переместит email в новый direct-wgN. sub_token не меняется.`,
            )
          )
            mutation.mutate({ deviceId: device.id, exitId: target.exit_id });
        }}
        className="text-[11px] px-2 py-0.5 rounded bg-purple-700 hover:bg-purple-600 disabled:opacity-50"
      >
        switch exit
      </button>
    </div>
  );
}

function DeviceList({
  devices,
  revokeDevice,
  nodes,
  migrateDevice,
  switchDeviceExit,
}: {
  devices: DeviceOut[];
  revokeDevice: { mutate: (id: number) => void; isPending: boolean };
  nodes: VPNNodeOut[] | undefined;
  migrateDevice: {
    mutate: (args: { deviceId: number; targetNodeId: number }) => void;
    isPending: boolean;
  };
  switchDeviceExit: {
    mutate: (args: { deviceId: number; exitId: number }) => void;
    isPending: boolean;
  };
}) {
  const [showDead, setShowDead] = useState(false);
  const live = devices.filter(
    (d) => d.status !== "revoked" && d.status !== "disabled",
  );
  const dead = devices.filter(
    (d) => d.status === "revoked" || d.status === "disabled",
  );

  return (
    <div className="mt-2 space-y-1">
      {live.map((d) => (
        <DeviceCard
          key={d.id}
          d={d}
          revokeDevice={revokeDevice}
          nodes={nodes}
          migrateDevice={migrateDevice}
          switchDeviceExit={switchDeviceExit}
        />
      ))}
      {dead.length > 0 && (
        <>
          <button
            onClick={() => setShowDead((v) => !v)}
            className="text-xs text-slate-500 hover:text-slate-400"
          >
            {showDead ? "▼" : "▶"} {dead.length} отвязанных
          </button>
          {showDead &&
            dead.map((d) => (
              <div
                key={d.id}
                className="flex items-center gap-2 bg-slate-900/60 rounded px-2 py-1 text-xs text-slate-500"
              >
                <span className="truncate">
                  #{d.id}
                  {d.name ? ` · ${d.name}` : ""} · {d.status}
                </span>
              </div>
            ))}
        </>
      )}
    </div>
  );
}

// Per-sub "move to specific node" control — admin override.
// Шлёт POST /subscriptions/{id}/migrate с target_node_id. На бэке
// выбор ноды не проходит пул/health/cooldown-фильтры (см. docstring
// migrate_subscription_to_new_node) — админ осознанно берёт
// ответственность. Мы всё равно прячем ноды с is_active=false из
// дропдауна, чтобы не собирать 400 на пустом месте.
// Бан-лист нод юзера: ноды, на которые авто-выбор («обновить подписку»)
// его не селит. Заполняется авто-баном при миграции + ручным баном.
// Здесь — просмотр + разбан (снять, чтобы авто-выбор снова мог вернуть).
function UserNodeBans({ userId }: { userId: number }) {
  const qc = useQueryClient();
  const bans = useQuery<NodeUserBanOut[]>({
    queryKey: ["user-node-bans", userId],
    queryFn: () => listUserNodeBans(userId),
  });
  const unban = useMutation({
    mutationFn: (nodeId: number) => removeUserNodeBan(userId, nodeId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["user-node-bans", userId] });
    },
    onError: (e: Error) => alert(`Не удалось снять бан: ${e.message}`),
  });
  return (
    <div className="pt-2 border-t border-slate-700">
      <div className="font-semibold mb-1">
        Бан-лист нод{" "}
        <span className="text-slate-500 text-xs font-normal">
          (авто-выбор сюда не селит)
        </span>
      </div>
      {bans.isLoading && (
        <div className="text-slate-400 text-xs">Загрузка…</div>
      )}
      {bans.data && bans.data.length === 0 && (
        <div className="text-slate-500 text-xs">Пусто.</div>
      )}
      {bans.data && bans.data.length > 0 && (
        <ul className="space-y-1">
          {bans.data.map((b) => (
            <li
              key={b.id}
              className="flex items-center justify-between gap-2 text-xs bg-slate-900 rounded px-2 py-1"
            >
              <div className="min-w-0">
                <span className="font-mono">
                  #{b.node_id} {b.node_name ?? ""}
                </span>
                {b.reason && (
                  <span
                    className="text-slate-500 block truncate"
                    title={b.reason}
                  >
                    {b.reason}
                  </span>
                )}
              </div>
              <button
                disabled={unban.isPending}
                onClick={() => unban.mutate(b.node_id)}
                className="text-xs px-2 py-0.5 rounded bg-slate-700 hover:bg-slate-600 disabled:opacity-50 shrink-0"
              >
                разбан
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function MigrateSubControl({
  sub,
  nodes,
  mutation,
}: {
  sub: SubscriptionOut;
  nodes: VPNNodeOut[] | undefined;
  mutation: {
    mutate: (args: { subId: number; targetNodeId: number }) => void;
    isPending: boolean;
  };
}) {
  const [targetId, setTargetId] = useState<string>("");
  const candidates = (nodes ?? []).filter(
    (n) => n.is_active && n.id !== sub.node_id,
  );
  if (candidates.length === 0) {
    return (
      <div className="mt-2 text-xs text-slate-500">
        Нет других активных нод для переселения.
      </div>
    );
  }
  const target = candidates.find((n) => String(n.id) === targetId);
  return (
    <div className="mt-2 flex gap-1 items-center">
      <select
        value={targetId}
        onChange={(e) => setTargetId(e.target.value)}
        className="text-xs px-1 py-0.5 rounded bg-slate-800 border border-slate-700 flex-1"
      >
        <option value="">— выбери ноду —</option>
        {candidates.map((n) => (
          <option key={n.id} value={String(n.id)}>
            #{n.id} {n.name} ({n.region})
            {n.cooldown_until && new Date(n.cooldown_until).getTime() > Date.now()
              ? " ⚠ cooldown"
              : ""}
          </option>
        ))}
      </select>
      <button
        disabled={mutation.isPending || !target}
        onClick={() => {
          if (!target) return;
          if (
            confirm(
              `Перевести подписку #${sub.id} с ноды «${sub.node}» на «${target.name}» (#${target.id}, ${target.region})?\n\n` +
                `Пул/health/cooldown НЕ проверяются — это ручной admin-override. Старые девайсы будут revoke'нуты в фоне, новый девайс поднимется через ansible (1–2 мин). sub_token сохраняется.`,
            )
          )
            mutation.mutate({ subId: sub.id, targetNodeId: target.id });
        }}
        className="text-xs px-2 py-1 rounded bg-blue-700 hover:bg-blue-600 disabled:opacity-50"
      >
        переселить
      </button>
    </div>
  );
}

// Per-sub "switch exit" control for subs living on multi-link relays.
// Шлёт POST /subscriptions/{id}/switch-exit — бэкенд перепишет
// Credential.exit_id у всех живых кредов и перезапустит
// provision_device.yml с новым EXIT_INTERFACE (manage_vless_*_user.sh
// идемпотентно снимет email со всех direct-wg* и добавит в нужный).
// sub_token + VLESS UUID не меняются, клиент просто видит другой IP
// на следующем реконнекте.
function SwitchExitControl({
  sub,
  mutation,
}: {
  sub: SubscriptionOut;
  mutation: {
    mutate: (args: { subId: number; exitId: number }) => void;
    isPending: boolean;
  };
}) {
  const [targetId, setTargetId] = useState<string>("");
  // Links запрашиваются только когда знаем node_id — для удалённых нод
  // (node_id=null) кнопка смены exit'а бессмысленна.
  const { data: links } = useQuery<NodeRelayLinkOut[]>({
    queryKey: ["relay-links", sub.node_id],
    queryFn: () => api.get(`/nodes/${sub.node_id}/links`),
    enabled: sub.node_id !== null && sub.node_id !== undefined,
  });
  if (sub.node_id == null) return null;
  // Релей без линков (или нода не релей) = нечего показывать.
  if (!links || links.length === 0) return null;
  // Один линк — смена exit'а не имеет смысла, нет куда переключать.
  if (links.length < 2) return null;
  const candidates = links.filter((l) => l.exit_id !== sub.current_exit_id);
  const target = candidates.find((l) => String(l.exit_id) === targetId);
  return (
    <div className="mt-2 flex gap-1 items-center">
      <select
        value={targetId}
        onChange={(e) => setTargetId(e.target.value)}
        className="text-xs px-1 py-0.5 rounded bg-slate-800 border border-slate-700 flex-1"
      >
        <option value="">
          — exit: {sub.current_exit_id ?? "—"} —
        </option>
        {candidates.map((l) => (
          <option key={l.exit_id} value={String(l.exit_id)}>
            #{l.exit_id} {l.exit_name} ({l.wg_interface_name})
          </option>
        ))}
      </select>
      <button
        disabled={mutation.isPending || !target}
        onClick={() => {
          if (!target) return;
          if (
            confirm(
              `Сменить exit подписки #${sub.id} на «${target.exit_name}» (#${target.exit_id}, ${target.wg_interface_name})?\n\n` +
                `Релей остаётся тот же. Все живые девайсы получат перепровижн (xray перепишет routing в direct-${target.wg_interface_name}). VLESS UUID + sub_token сохраняются. Hysteria2 пропускается (идёт напрямую с релея).`,
            )
          )
            mutation.mutate({ subId: sub.id, exitId: target.exit_id });
        }}
        className="text-xs px-2 py-1 rounded bg-purple-700 hover:bg-purple-600 disabled:opacity-50"
      >
        сменить exit
      </button>
    </div>
  );
}
