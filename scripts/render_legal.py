#!/usr/bin/env python3
"""Рендер docs/legal/*.md → HTML для публикации на сайте (nginx camo-root/legal/).

Без внешних зависимостей: заголовки, абзацы, списки, **жирный**, ссылки.
Запуск из корня репо: `python3 scripts/render_legal.py`. Результат коммитится
в infra/ansible/roles/deploy_web_frontend/files/legal/ — роль копирует его
на хост как есть.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "legal"
DST = ROOT / "infra" / "ansible" / "roles" / "deploy_web_frontend" / "files" / "legal"
PAGES = {
    "user_agreement.md": ("terms.html", "Пользовательское соглашение"),
    "refund_policy.md": ("refund.html", "Политика возвратов"),
    "privacy_policy.md": ("privacy.html", "Политика обработки персональных данных"),
}
NAV = [("terms.html", "Соглашение"), ("refund.html", "Возвраты"), ("privacy.html", "Персональные данные")]

TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title} — V8 VPN</title>
<style>
:root{{color-scheme:light dark}}
body{{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:1.5rem;line-height:1.55;background:#f5f6f8;color:#16181d}}
main{{max-width:44rem;margin:0 auto;background:#fff;border-radius:14px;padding:1.5rem 1.75rem;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
nav{{max-width:44rem;margin:0 auto 1rem;font-size:.9rem}}
nav a{{margin-right:1rem}}
h1{{font-size:1.4rem}} h2{{font-size:1.1rem;margin-top:1.6rem}}
a{{color:#2563eb}}
@media (prefers-color-scheme:dark){{body{{background:#0f1115;color:#e5e7eb}}main{{background:#181b21;box-shadow:none}}}}
</style>
</head>
<body>
<nav>{nav}</nav>
<main>
{body}
</main>
</body>
</html>
"""


def inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    text = re.sub(r"(https?://[^\s<)]+)", r'<a href="\1">\1</a>', text)
    text = re.sub(r"(?<![\w/])@([A-Za-z0-9_]{5,})", r'<a href="https://t.me/\1">@\1</a>', text)
    return text


def render(md: str) -> str:
    out: list[str] = []
    in_list = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line:
            continue
        if line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        else:
            out.append(f"<p>{inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def main() -> None:
    DST.mkdir(parents=True, exist_ok=True)
    nav = " ".join(f'<a href="{href}">{label}</a>' for href, label in NAV)
    for src_name, (dst_name, title) in PAGES.items():
        body = render((SRC / src_name).read_text(encoding="utf-8"))
        (DST / dst_name).write_text(
            TEMPLATE.format(title=title, nav=nav, body=body), encoding="utf-8"
        )
        print(f"{src_name} -> {DST / dst_name}")


if __name__ == "__main__":
    main()
