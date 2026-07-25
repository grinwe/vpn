"""Воронка онбординга в консоли: бот → кабинет → триал → ссылка → оплата.

Считает тот же сервис, что и админка (`GET /api/admin/onboarding-funnel`) —
`app.services.onboarding_funnel`, чтобы цифры не разъезжались между двумя
реализациями. Роадмап: docs/operations/onboarding_roadmap_2026_07_25.md.

Запуск:
    docker compose exec -T backend python -m scripts.onboarding_funnel --days 7
    docker compose exec -T backend python -m scripts.onboarding_funnel --days 0   # всё время
"""
from __future__ import annotations

import argparse

from app.db import SessionLocal
from app.services import onboarding_funnel as funnel_svc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="окно когорты в днях (0 = всё время)")
    args = ap.parse_args()

    s = SessionLocal()
    try:
        data = funnel_svc.compute(s, args.days or None)
    finally:
        s.close()

    if not data["total"]:
        print("в окне нет юзеров")
        return 0

    window = f"за {args.days} дн." if args.days else "за всё время"
    print(f"\nОнбординг-воронка {window}: пришло в бота {data['total']}\n")
    for row in data["steps"][1:]:
        print(f"  {row['label']:<34}{row['count']:>4}  ({row['pct']:>5.1f}%)")
    print("\nгде теряем:")
    for row in data["losses"]:
        print(f"  {row['label']:<34}{row['count']:>4}  ({row['pct']:>5.1f}%)")
    if data["trial_failures"]:
        print(f"\n  ⚠️ у {data['trial_failures']} юзеров активация триала ОТКАЗАЛА "
              f"(AuditLog.action='trial_activate_rejected', extra.reason)")
    if data["telemetry_partial"]:
        print("\n  ⓘ часть когорты старше телеметрии (2026-07-25) — «открыли")
        print("    кабинет» занижено: события тогда ещё не писались.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
