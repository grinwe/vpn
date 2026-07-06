"""Регрессионные проверки конфигурации CI (.github/workflows/ci.yml).

Аудит-находки 196/197: pull_request+push дублировали пайплайн и не
отменяли устаревшие прогоны; pytest гонялся без измерения покрытия.
Тест сторожит, что фиксы не откатят правкой YAML.
"""

from pathlib import Path

import pytest
import yaml

CI_YML = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

# ci.yml лежит в корне репозитория (../../.github от tests/). В backend-only
# тест-образе смонтирован только backend/, поэтому файла нет — тогда проверять
# нечего. Полный чекаут (CI: `cd backend && pytest`, корень репо на месте)
# резолвит parents[2] в корень и гоняет ассерты по-настоящему.
if not CI_YML.is_file():
    pytest.skip(
        "ci.yml недоступен (backend-only тест-образ) — проверяется в полном "
        "чекауте/CI",
        allow_module_level=True,
    )


def _load_ci():
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))


def test_push_trigger_scoped_to_main_branches():
    # Находка 196: push без фильтра веток дублировал pull_request-прогон на
    # каждый коммит в feature-ветку. Оставляем push только на main/dev.
    ci = _load_ci()
    # ключ `on` в YAML 1.1 парсится как булев True.
    triggers = ci.get("on", ci.get(True))
    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main", "dev"]


def test_concurrency_cancels_stale_runs():
    # Находка 196: без concurrency старые прогоны доезжали до конца впустую.
    ci = _load_ci()
    concurrency = ci["concurrency"]
    assert concurrency["cancel-in-progress"] is True
    assert "github.ref" in concurrency["group"]


def test_pytest_measures_coverage():
    # Находка 197: интеграционный шаг должен собирать покрытие app/.
    ci = _load_ci()
    steps = ci["jobs"]["lint-test"]["steps"]
    # Матчим именно шаг запуска pytest (``-m pytest``), а не install-шаг: тот
    # содержит подстроку «pytest» из пакета ``pytest-cov`` и раньше ошибочно
    # выбирался первым, из-за чего ассерт --cov=app падал не по делу.
    pytest_step = next(
        s for s in steps if "-m pytest" in s.get("run", "")
    )
    assert "--cov=app" in pytest_step["run"]


def test_pytest_cov_installed():
    # Находка 197: pytest-cov должен ставиться в CI, иначе --cov упадёт.
    ci = _load_ci()
    steps = ci["jobs"]["lint-test"]["steps"]
    install_step = next(
        s for s in steps if "pip install" in s.get("run", "")
    )
    assert "pytest-cov" in install_step["run"]
