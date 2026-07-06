"""Серверная валидация ops-плана перед исполнением (AI_AGENT_ROADMAP Phase 3).

Между «оператор подтвердил план» и «бэкенд дёрнул order/destroy» стоит ЭТОТ слой.
План приходит от LLM и потому НЕДОВЕРЕННЫЙ: ``submit_plan.params`` —
``additionalProperties:true``, ``kind``/``tier``/``est_cost`` пишет модель. Доверять
этому на исполнении нельзя (галлюцинация ``count:50`` или ``destroy`` живой ноды =
реальные деньги/снос). ``validate_plan`` приводит план к исполнимому виду или
ОТКЛОНЯЕТ его, проверяя:

- gate 2: ``kind`` — закрытый allowlist (никаких ``other``/неизвестных); обязательные
  params присутствуют и нужного типа; каждый ``provider_id``/``node_id``/``exit_id``
  резолвится в существующую (активную) строку БД. tier — СЕРВЕРНЫЙ (из ``kind``),
  ``needs_confirmation`` — серверный (любой costly/destructive ⇒ true). Поля модели
  для гейтинга игнорируются.
- gate 3 (структурная часть): ``count`` ограничен ``OPS_MAX_ORDER_COUNT``; число
  заказов в плане ≤ ``OPS_MAX_NODES_PER_PLAN``. Денежный spend-cap по ЖИВЫМ ценам +
  баланс — pre-flight исполнителя (там сеть/драйвер), не здесь.
- gate 6: destructive-инвариант — ``destroy``/``reinstall`` ноды с
  ``assigned_subscriptions>0`` отклоняется, ПОКА в плане раньше нет ``migrate_users``
  с этой ноды-источника.

Сетевую валидацию (регион/тариф/ОС по живым offerings, цена×count, баланс) делает
pre-flight исполнителя — отдельно, чтобы этот слой оставался детерминированным и
полностью покрывался тестами без сети. Здесь НИЧЕГО не исполняется.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ... import models
from ...time_utils import utcnow
from . import _runtime

logger = logging.getLogger(__name__)


def _env_num(name: str, default: str, cast):
    """Безопасный разбор env-кнобов: пустое/мусор → дефолт (+warning), не падаем
    и НЕ обнуляем cap молча (set-but-empty иначе вернул бы '' и упал/обнулил)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return cast(default)
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning("ops: bad %s=%r — беру дефолт %s", name, raw, default)
        return cast(default)

# Серверная карта kind → tier (НЕ доверяем tier от LLM).
_KIND_TIER: dict[str, str] = {
    "order_node": "costly",
    "order_exit": "costly",
    "attach_tunnel": "reversible",
    "migrate_users": "destructive",
    "reinstall": "destructive",
    "destroy": "destructive",
    "set_active": "reversible",
}
_ALLOWED_KINDS = frozenset(_KIND_TIER)
_DESTRUCTIVE_KINDS = frozenset({"reinstall", "destroy"})


def _max_order_count() -> int:
    return max(1, _env_num("OPS_MAX_ORDER_COUNT", "5", int))


def _max_nodes_per_plan() -> int:
    return max(1, _env_num("OPS_MAX_NODES_PER_PLAN", "5", int))


def _assigned_subscriptions(db: Session, node_id: int) -> int:
    """Сколько живых подписок провижинено на ноде (для destructive-инварианта)."""
    return int(
        db.query(func.count(func.distinct(models.Credential.subscription_id)))
        .filter(
            models.Credential.node_id == node_id,
            models.Credential.subscription_id.isnot(None),
            models.Credential.pool_state == models.CredentialPoolState.assigned,
        )
        .scalar()
        or 0
    )


def _as_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):  # bool — подтип int, но не то, что нужно
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_plan(db: Session, ops_plan: models.OpsPlan) -> dict[str, Any]:
    """Привести сохранённый план к исполнимому виду или отклонить.

    Возвращает ``{ok, rejections, needs_confirmation, steps, totals}``. ``ok`` —
    можно ли исполнять; ``rejections`` — почему нельзя; ``steps`` — провалидированные
    шаги с серверным tier и резолвнутыми id; ``totals`` — агрегаты (число заказов,
    нод). НИЧЕГО не исполняет и не ходит в сеть."""
    rejections: list[str] = []
    out_steps: list[dict[str, Any]] = []

    raw_plan = ops_plan.plan or {}
    if raw_plan.get("feasible") is False:
        rejections.append("план помечен планировщиком как невыполнимый (feasible=false)")

    steps = raw_plan.get("steps") or []
    if not isinstance(steps, list) or not steps:
        rejections.append("в плане нет шагов")
        steps = []

    # Источники миграции, увиденные ДО текущего шага (для destructive-инварианта:
    # снести/реинсталлить ноду можно только если её юзеров раньше увели).
    migrated_sources: set[int] = set()
    order_count = 0

    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            rejections.append(f"шаг #{idx + 1}: не объект")
            continue
        kind = str(step.get("kind") or "").strip()
        params = step.get("params") or {}
        if not isinstance(params, dict):
            rejections.append(f"шаг #{idx + 1} ({kind or '?'}): params не объект")
            params = {}

        if kind not in _ALLOWED_KINDS:
            rejections.append(
                f"шаг #{idx + 1}: kind={kind!r} вне allowlist "
                f"({', '.join(sorted(_ALLOWED_KINDS))})"
            )
            continue

        tier = _KIND_TIER[kind]  # серверный, не от LLM
        resolved: dict[str, Any] = {}
        step_errors: list[str] = []

        if kind in ("order_node", "order_exit"):
            provider_id = _as_int(params.get("provider_id"))
            if provider_id is None:
                step_errors.append("нет/битый provider_id")
            else:
                provider = db.get(models.CloudProvider, provider_id)
                if not provider:
                    step_errors.append(f"provider_id={provider_id} не найден")
                elif not provider.is_active:
                    step_errors.append(f"provider_id={provider_id} неактивен")
                else:
                    resolved["provider_id"] = provider_id
            count = _as_int(params.get("count", 1)) or 1
            if count < 1 or count > _max_order_count():
                step_errors.append(
                    f"count={count} вне [1..{_max_order_count()}] (OPS_MAX_ORDER_COUNT)"
                )
            else:
                resolved["count"] = count
                order_count += count
            # pool_id (FK): если задан — обязан резолвиться, иначе spawn потратит
            # деньги и упадёт на db.commit() с битым FK → осиротевший платный сервер.
            pool_id = params.get("pool_id")
            if pool_id is not None:
                pool_int = _as_int(pool_id)
                if pool_int is None or not db.get(models.ServerPool, pool_int):
                    step_errors.append(f"pool_id={pool_id!r} не найден")
                else:
                    resolved["pool_id"] = pool_int
            # region/plan/image кладём в resolved как ЕДИНЫЙ авторитетный источник
            # для диспетчера (region/plan валидирует сетевой pre-flight по offerings;
            # image валидирует драйвер при заказе — битый → провайдер отклоняет заказ
            # ДО списания). Диспетчер читает ТОЛЬКО resolved, не сырые params.
            resolved["region"] = params.get("region")
            resolved["plan"] = params.get("plan")
            resolved["image"] = params.get("image")

        elif kind in _DESTRUCTIVE_KINDS:  # destroy | reinstall
            node_id = _as_int(params.get("node_id"))
            if node_id is None:
                step_errors.append("нет/битый node_id")
            else:
                node = db.get(models.VPNNode, node_id)
                if not node:
                    step_errors.append(f"node_id={node_id} не найдена во флоте")
                else:
                    resolved["node_id"] = node_id
                    assigned = _assigned_subscriptions(db, node_id)
                    if assigned > 0 and node_id not in migrated_sources:
                        step_errors.append(
                            f"{kind} ноды #{node_id} с {assigned} живыми подписками "
                            "без предшествующей migrate_users — запрещено (gate 6)"
                        )
                    resolved["assigned_subscriptions"] = assigned

        elif kind == "migrate_users":
            src = _as_int(params.get("from_node_id") or params.get("node_id"))
            dst = _as_int(params.get("to_node_id") or params.get("target_node_id"))
            if src is None:
                step_errors.append("нет/битый from_node_id")
            elif not db.get(models.VPNNode, src):
                step_errors.append(f"from_node_id={src} не найдена")
            else:
                resolved["from_node_id"] = src
                migrated_sources.add(src)
            if dst is None:
                step_errors.append("нет/битый to_node_id")
            else:
                tgt = db.get(models.VPNNode, dst)
                if not tgt:
                    step_errors.append(f"to_node_id={dst} не найдена")
                elif not tgt.is_active:
                    step_errors.append(f"to_node_id={dst} неактивна как цель миграции")
                else:
                    resolved["to_node_id"] = dst

        # attach_tunnel / set_active — reversible; глубокую валидацию целей делает
        # pre-flight исполнителя (нужны relay/exit-связки). Здесь пропускаем.

        if step_errors:
            for e in step_errors:
                rejections.append(f"шаг #{idx + 1} ({kind}): {e}")

        out_steps.append(
            {
                "index": idx,
                "kind": kind,
                "tier": tier,
                "params": params,
                "resolved": resolved,
                "errors": step_errors,
            }
        )

    if order_count > _max_nodes_per_plan():
        rejections.append(
            f"заказов в плане {order_count} > потолка {_max_nodes_per_plan()} "
            "(OPS_MAX_NODES_PER_PLAN)"
        )

    # Серверный needs_confirmation: любой costly/destructive шаг ⇒ нужно подтверждение
    # (флаг модели игнорируем).
    needs_confirmation = any(
        s["tier"] in ("costly", "destructive") for s in out_steps
    )

    return {
        "ok": not rejections,
        "rejections": rejections,
        "needs_confirmation": needs_confirmation,
        "steps": out_steps,
        "totals": {
            "order_count": order_count,
            "steps": len(out_steps),
            "destructive": sum(1 for s in out_steps if s["tier"] == "destructive"),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Исполнитель (Phase 3). За флагом OPS_EXECUTE_ENABLED (по умолчанию OFF). MVP:
# реально исполняется только order_node (заказ нод) через ОБКАТАННЫЙ
# spawn_node_async (тот же путь, что кнопка «Заказать ноду» — gate 5, не raw
# driver). Остальные kind'ы исполнитель пока ЯВНО пропускает (skipped), чтобы
# непротестированные destructive-пути не стреляли. destroy/reinstall/migrate/
# tunnel — отдельными ревьюируемыми проходами.
# ─────────────────────────────────────────────────────────────────────────────

# Терминальные статусы — повторное исполнение запрещено.
_TERMINAL_STATUS = frozenset({"executed", "partial", "failed", "cancelled", "expired"})
# Что MVP реально исполняет (остальное — skipped).
_SUPPORTED_EXEC_KINDS = frozenset({"order_node"})


class OpsExecError(RuntimeError):
    """Исполнение невозможно/запрещено/упало."""


def execute_enabled() -> bool:
    return os.getenv("OPS_EXECUTE_ENABLED", "").lower() in ("1", "true", "yes", "on")


def _max_spend_rub() -> float:
    return float(os.getenv("OPS_MAX_SPEND_RUB", "5000"))


def _finalize_wait_s() -> float:
    """Бюджет ожидания фоновой достройки нод (сек). Должен быть МЕНЬШЕ
    job_timeout энкьюера (1800с, см. api/agent.py), чтобы осталось время
    записать результат в ops_plan.execution."""
    return max(0.0, _env_num("OPS_FINALIZE_WAIT_SEC", "1500", float))


def _new_finalize_threads(
    before: set[threading.Thread],
) -> list[threading.Thread]:
    """Потоки ``_finalize_spawn``, стартовавшие после снапшота ``before``.

    ``spawn_node_async`` уводит долгую достройку (poll IP до 600с → host →
    SSH-wait → bootstrap-таска) в daemon-поток и НЕ возвращает его — ловим
    поток диффом ``threading.enumerate()``. Фильтр по имени (py3.10+ кладёт
    имя target-функции в ``Thread.name``); fallback — любые новые
    daemon-потоки (если схема имён Thread изменится)."""
    new = [t for t in threading.enumerate() if t not in before and t.is_alive()]
    named = [t for t in new if "_finalize_spawn" in (t.name or "")]
    return named or [t for t in new if t.daemon]


def _wait_spawn_finalize(
    watch: list[tuple[int, list[int], list[threading.Thread]]],
) -> list[dict[str, Any]]:
    """Дождаться daemon-потоков достройки нод ПЕРЕД возвратом из RQ-джобы.

    execute_plan гоняется в RQ work-horse, который завершает процесс сразу
    после возврата джобы — daemon-потоки ``_finalize_spawn`` при этом
    убиваются посреди ожидания IP, и оплаченная нода навсегда зависает в
    ``registering`` с placeholder-host (bootstrap не стартует). Поэтому в
    RQ-контексте достройку ЯВНО дожидаемся здесь (общий дедлайн
    ``OPS_FINALIZE_WAIT_SEC``; сами потоки идут параллельно). Bootstrap'у
    переживать выход не нужно — он энкьюится отдельной RQ-джобой
    (``run_task_async`` → ``enqueue_task``).

    ``watch`` — [(index шага, созданные node-id, потоки)]. Возвращает шаги,
    чьи потоки НЕ успели в дедлайн (их ноды рискуют остаться в registering)."""
    if not watch:
        return []
    deadline = time.monotonic() + _finalize_wait_s()
    pending: list[dict[str, Any]] = []
    for index, node_ids, threads in watch:
        for t in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(remaining)
        if any(t.is_alive() for t in threads):
            pending.append({"index": index, "node_ids": node_ids})
    return pending


def _preflight(db: Session, validated: dict[str, Any]) -> dict[str, Any]:
    """Сетевой pre-flight для order-шагов: регион/тариф по ЖИВЫМ offerings +
    cost = цена×count + проверка баланса провайдера + общий spend-cap.

    Авторитетная денежная проверка (не доверяем est_cost от LLM). Бьёт по API
    провайдера — выполняется в RQ-воркере, без HTTP-таймаута."""
    from ..cloud import DriverError, get_driver

    reasons: list[str] = []
    step_costs: dict[int, float] = {}
    total = 0.0

    by_provider: dict[int, list[dict]] = {}
    for s in validated["steps"]:
        if s["kind"] != "order_node":
            continue
        pid = s["resolved"].get("provider_id")
        if pid is None:
            continue  # уже отклонён валидатором
        by_provider.setdefault(pid, []).append(s)

    for pid, steps in by_provider.items():
        provider = db.get(models.CloudProvider, pid)
        if not provider:
            reasons.append(f"provider {pid} исчез между планом и исполнением")
            continue
        try:
            driver = get_driver(provider)
            plans = {
                str(pl.get("id")): pl
                for pl in (driver.list_plans() if hasattr(driver, "list_plans") else [])
            }
            dcs = {
                str(d.get("id"))
                for d in (driver.list_datacenters() if hasattr(driver, "list_datacenters") else [])
            }
            balance = driver.get_balance() if hasattr(driver, "get_balance") else None
        except DriverError as exc:
            reasons.append(f"provider {pid}: offerings/баланс недоступны ({exc})")
            continue

        prov_cost = 0.0
        for s in steps:
            r = s["resolved"]
            count = int(r.get("count", 1))
            plan_id = str(r.get("plan") or "")
            region = str(r.get("region") or "")
            if dcs and region not in dcs:
                reasons.append(
                    f"шаг #{s['index'] + 1}: регион {region!r} вне offerings провайдера {pid}"
                )
            pl = plans.get(plan_id)
            if pl is None:
                reasons.append(
                    f"шаг #{s['index'] + 1}: тариф {plan_id!r} вне offerings провайдера {pid}"
                )
                continue
            cost = float(pl.get("price") or 0) * count
            step_costs[s["index"]] = cost
            prov_cost += cost
        total += prov_cost
        if balance is not None and balance < prov_cost:
            reasons.append(
                f"provider {pid}: баланс {balance}₽ < нужно {prov_cost}₽"
            )

    if total > _max_spend_rub():
        reasons.append(
            f"итого {total}₽ > spend-cap {_max_spend_rub()}₽ (OPS_MAX_SPEND_RUB)"
        )

    return {"ok": not reasons, "reasons": reasons, "total_cost": total, "step_costs": step_costs}


def _exec_order_node(
    db: Session, step: dict[str, Any], plan_id: int
) -> tuple[list[int], str | None]:
    """Заказать count нод через spawn_node_async (быстрый order + bg bootstrap).
    Читает ТОЛЬКО step["resolved"] (валидированный спек, не сырые params).
    Возвращает (созданные node-id, ошибка|None) — created сохраняется ДАЖЕ при
    сбое на середине, чтобы оператор не потерял уже оплаченные серверы."""
    from ..node_spawner import resolve_spawn_name, spawn_node_async

    r = step["resolved"]
    pid = int(r["provider_id"])
    count = int(r.get("count", 1))
    region = str(r.get("region") or "")
    plan = str(r.get("plan") or "")
    image = r.get("image")
    pool_id = r.get("pool_id")
    created: list[int] = []
    try:
        for _ in range(count):
            name = resolve_spawn_name(db, pid, region, None)  # авто-имя
            node = spawn_node_async(
                db,
                provider_id=pid,
                name=name,
                region=region,
                plan=plan,
                image=image,
                pool_id=pool_id,
                notes=f"ops-agent plan #{plan_id}",
            )
            created.append(node.id)
    except Exception as exc:  # noqa: BLE001
        return created, _runtime.redact(f"{type(exc).__name__}: {exc}")
    return created, None


def execute_plan(db: Session, ops_plan: models.OpsPlan) -> dict[str, Any]:
    """Исполнить сохранённый план. Гоняется в RQ-воркере (см.
    ``app.worker.run_ops_plan_execute``). Идемпотентен по ``status``: терминальный
    план не переисполняется. Перед исполнением — ре-валидация (gate 2/3/6) и
    сетевой pre-flight (живые цены/баланс/spend-cap). Пишет результат в
    ``ops_plan.execution`` и финальный ``status``."""
    if not execute_enabled():
        raise OpsExecError("исполнение выключено (OPS_EXECUTE_ENABLED=0)")
    if ops_plan.status in _TERMINAL_STATUS:
        raise OpsExecError(
            f"план в статусе {ops_plan.status} — повторное исполнение запрещено"
        )

    # TTL — авторитетно на пути воркера (не только в эндпоинте): джоба могла
    # отлежаться в очереди/пережить простой воркера и стартовать после протухания.
    if ops_plan.expires_at and ops_plan.expires_at < utcnow():
        ops_plan.status = "expired"
        db.commit()
        raise OpsExecError("план протух (TTL) — построй заново")

    # Целостность: ловим мутацию ops_plans.plan в обход (план write-once; если
    # хэш не сходится — исполняем НЕ то, что подтвердил оператор).
    canonical = json.dumps(ops_plan.plan or {}, sort_keys=True, ensure_ascii=False)
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != ops_plan.content_hash:
        ops_plan.status = "failed"
        ops_plan.execution = {"phase": "integrity", "reason": "content_hash не совпал"}
        db.commit()
        raise OpsExecError("content_hash не совпал — план изменён после сохранения")

    # Ре-валидация на момент исполнения (флот мог измениться после планирования).
    validated = validate_plan(db, ops_plan)
    if not validated["ok"]:
        ops_plan.status = "failed"
        ops_plan.execution = {"phase": "validate", "rejections": validated["rejections"]}
        db.commit()
        raise OpsExecError("план не прошёл валидацию: " + "; ".join(validated["rejections"][:5]))

    # Авторитетный денежный pre-flight (живые цены/баланс/spend-cap).
    pre = _preflight(db, validated)
    if not pre["ok"]:
        ops_plan.status = "failed"
        ops_plan.execution = {
            "phase": "preflight",
            "reasons": pre["reasons"],
            "total_cost": pre["total_cost"],
        }
        db.commit()
        raise OpsExecError("pre-flight: " + "; ".join(pre["reasons"][:5]))

    ops_plan.status = "executing"
    db.commit()

    results: list[dict[str, Any]] = []
    failed = False
    # Потоки фоновой достройки (см. _wait_spawn_finalize): их надо дождаться
    # до возврата из RQ-джобы, иначе work-horse убьёт их вместе с процессом.
    finalize_watch: list[tuple[int, list[int], list[threading.Thread]]] = []
    for s in validated["steps"]:
        kind = s["kind"]
        if kind not in _SUPPORTED_EXEC_KINDS:
            results.append({
                "index": s["index"], "kind": kind, "status": "skipped",
                "detail": "kind не поддержан исполнителем (MVP — только order_node)",
            })
            continue
        # _exec_order_node возвращает (created, error): created сохраняем ВСЕГДА,
        # даже при сбое на середине — иначе оплаченные серверы теряются из отчёта.
        threads_before = set(threading.enumerate())
        created, err = _exec_order_node(db, s, ops_plan.id)
        spawn_threads = _new_finalize_threads(threads_before)
        if spawn_threads:
            # Даже при err потоки уже заказанных нод шага дожидаемся — сервер
            # оплачен, достройка должна дойти до конца.
            finalize_watch.append((s["index"], created, spawn_threads))
        entry = {
            "index": s["index"], "kind": kind,
            "created_node_ids": created,
            "est_cost_rub": pre["step_costs"].get(s["index"]),
        }
        if err:
            logger.warning("ops plan %s step %s (%s) failed: %s", ops_plan.id, s["index"], kind, err)
            entry["status"] = "failed"
            entry["detail"] = err
            results.append(entry)
            failed = True
            break  # stop-on-first-failure: не продолжаем после сбоя заказа
        entry["status"] = "done"
        results.append(entry)

    # Дожидаемся достройки заказанных нод ДО возврата (fix: daemon-поток
    # _finalize_spawn умирал вместе с RQ work-horse → нода вечно в registering
    # с placeholder-host, деньги списаны, bootstrap не стартовал).
    finalize_pending = _wait_spawn_finalize(finalize_watch)
    if finalize_pending:
        logger.warning(
            "ops plan %s: достройка не подтверждена в OPS_FINALIZE_WAIT_SEC для "
            "шагов %s — ноды %s могут остаться в registering (проверь руками)",
            ops_plan.id,
            [p["index"] for p in finalize_pending],
            [p["node_ids"] for p in finalize_pending],
        )

    done_count = sum(1 for r in results if r["status"] == "done")
    if failed:
        status = "failed"
    elif done_count == 0:
        status = "partial"  # ничего реально не исполнено (всё skipped вне MVP)
    elif finalize_pending:
        # Заказ прошёл, но достройка не подтверждена в дедлайн — честный
        # partial, чтобы оператор не увидел «executed» по зависшим нодам.
        status = "partial"
    else:
        status = "executed"  # исполнимая часть прошла; skipped помечены в execution
    ops_plan.status = status
    execution: dict[str, Any] = {
        "phase": "done",
        "total_cost": pre["total_cost"],
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "steps": results,
    }
    if finalize_pending:
        execution["finalize_pending"] = finalize_pending
    ops_plan.execution = execution
    db.commit()
    return {
        "status": status,
        "plan_id": ops_plan.id,
        "total_cost": pre["total_cost"],
        "steps": results,
    }
