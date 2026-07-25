"""Воронка онбординга: бот → кабинет → триал → устройство.

Роадмап: docs/operations/onboarding_roadmap_2026_07_25.md.
Ключевой вопрос, ради которого добавлена телеметрия E0: люди, которые «зашли и
вышли», не дошли до кабинета — или открыли его и ушли? От ответа зависит, чинить
бота или кабинет.

Шаги ДО E0 (2026-07-25) не наблюдаемы задним числом: `webapp_open` и
`trial_activate_*` пишутся только с этого момента, поэтому для старых когорт
колонки «открыл ЛК» будут пустыми — это ожидаемо, а не баг.

Запуск:
    docker compose exec -T backend python -m scripts.onboarding_funnel --days 7
"""
from __future__ import annotations

import argparse
from datetime import timedelta

from app import models
from app.db import SessionLocal
from app.time_utils import utcnow


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="окно когорты в днях (0 = всё время)")
    args = ap.parse_args()

    s = SessionLocal()
    try:
        q = s.query(models.User)
        if args.days:
            q = q.filter(models.User.created_at >= utcnow() - timedelta(days=args.days))
        users = q.all()
        if not users:
            print("в окне нет юзеров")
            return 0
        ids = [u.id for u in users]
        n = len(users)

        opened = {
            t for (t,) in s.query(models.AuditLog.target_id)
            .filter(models.AuditLog.action == "webapp_open",
                    models.AuditLog.target_id.in_(ids)).distinct().all()
        }
        trial_ok = {
            t for (t,) in s.query(models.AuditLog.target_id)
            .filter(models.AuditLog.action == "trial_activated",
                    models.AuditLog.target_id.in_(ids)).distinct().all()
        }
        trial_fail = {
            t for (t,) in s.query(models.AuditLog.target_id)
            .filter(models.AuditLog.action == "trial_activate_rejected",
                    models.AuditLog.target_id.in_(ids)).distinct().all()
        }
        claimed = {u.id for u in users if u.trial_activated_at is not None}
        with_device = {
            uid for (uid,) in s.query(models.Device.user_id)
            .filter(models.Device.user_id.in_(ids)).distinct().all()
        }
        paid = {
            uid for (uid,) in s.query(models.Invoice.user_id)
            .filter(models.Invoice.user_id.in_(ids),
                    models.Invoice.status == models.InvoiceStatus.paid).distinct().all()
        }

        def row(label: str, value: int) -> None:
            print(f"  {label:<34}{value:>4}  ({100 * value / n:>5.1f}%)")

        window = f"за {args.days} дн." if args.days else "за всё время"
        print(f"\nОнбординг-воронка {window}: пришло в бота {n}\n")
        row("открыли кабинет", len(opened))
        row("забрали триал", len(claimed))
        row("получили устройство (ссылку)", len(with_device))
        row("оплатили хоть раз", len(paid))

        never_opened = [u.id for u in users if u.id not in opened]
        opened_but_no_trial = [u.id for u in users if u.id in opened and u.id not in claimed]
        trial_no_device = [u.id for u in users if u.id in claimed and u.id not in with_device]

        print("\nгде теряем:")
        row("НЕ открыли кабинет вовсе", len(never_opened))
        row("открыли, но не забрали триал", len(opened_but_no_trial))
        row("забрали триал, но без ссылки", len(trial_no_device))
        if trial_fail:
            print(f"\n  ⚠️ у {len(trial_fail)} юзеров активация триала ОТКАЗАЛА "
                  f"(смотри AuditLog.action='trial_activate_rejected', поле extra.reason)")
        if not opened and not trial_ok:
            print("\n  ⓘ событий телеметрии нет — либо когорта старше 2026-07-25,")
            print("    либо E0 ещё не задеплоен.")
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    raise SystemExit(main())
