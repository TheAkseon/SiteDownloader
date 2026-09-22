# -*- coding: utf-8 -*-
"""
Исправляет пути во всех HTML и CSS в site_review/archive/:
  - абсолютные http://www.155gymspb.ru/... → относительный путь
  - корневые /wp-content/..., /wp-includes/... → относительный путь
  - работает с query-string (?ver=xxx)
  - оба вида кавычек
"""
import os
import re
import sys
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

OUT_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive")
ABS_BASE = "http://www.155gymspb.ru"


def rel(from_file: str, abs_asset: str) -> str:
    return os.path.relpath(abs_asset, os.path.dirname(from_file)).replace("\\", "/")


def local_path(raw: str) -> str:
    """
    raw — путь вида /wp-content/... или http://www.155gymspb.ru/...
    Возвращает абсолютный путь в OUT_DIR (без query-string).
    Декодирует процент-кодирование (%d1%81... → кириллица).
    Для ссылок на директории добавляет /index.html.
    """
    if raw.startswith("http"):
        raw = raw[len(ABS_BASE):]        # убираем домен
    clean = raw.split("?")[0].split("#")[0]
    clean = urllib.parse.unquote(clean)  # %d1%81... → кириллица
    clean = clean.lstrip("/")
    p = os.path.join(OUT_DIR, clean.replace("/", os.sep))
    # Если путь — директория, добавляем index.html
    if os.path.isdir(p):
        p = os.path.join(p, "index.html")
    return p


# Атрибуты со значением, начинающимся на http://www.155gymspb.ru/ или /
ATTR_ABS_RE = re.compile(
    r"""((?:href|src|content|data-src|data-background|poster)=)(['"])(http://www\.155gymspb\.ru/[^'"]*)\2""",
    re.IGNORECASE,
)
ATTR_ROOT_RE = re.compile(
    r"""((?:href|src|content|data-src|data-background|poster)=)(['"])(\/[^'"]*)\2""",
    re.IGNORECASE,
)
CSS_URL_ABS_RE = re.compile(
    r"""url\(\s*(['"]?)(http://www\.155gymspb\.ru/[^'"\)\s]+)\1\s*\)""",
    re.IGNORECASE,
)
CSS_URL_ROOT_RE = re.compile(
    r"""url\(\s*(['"]?)(\/[^'"\)\s]+)\1\s*\)""",
    re.IGNORECASE,
)


def fix_file(filepath: str) -> bool:
    with open(filepath, encoding="utf-8", errors="replace") as f:
        original = f.read()

    def repl_attr(m):
        attr_eq, quote, raw = m.group(1), m.group(2), m.group(3)
        lp = local_path(raw)
        if os.path.exists(lp):
            return f'{attr_eq}{quote}{rel(filepath, lp)}{quote}'
        return m.group(0)

    def repl_url(m):
        quote, raw = m.group(1), m.group(2)
        lp = local_path(raw)
        if os.path.exists(lp):
            return f"url({quote}{rel(filepath, lp)}{quote})"
        return m.group(0)

    result = ATTR_ABS_RE.sub(repl_attr, original)
    result = ATTR_ROOT_RE.sub(repl_attr, result)
    result = CSS_URL_ABS_RE.sub(repl_url, result)
    result = CSS_URL_ROOT_RE.sub(repl_url, result)

    if result != original:
        with open(filepath, "wb") as f:
            f.write(result.encode("utf-8"))
        return True
    return False


def main():
    html_fixed = css_fixed = html_total = css_total = 0
    for root, _, files in os.walk(OUT_DIR):
        for fname in files:
            fp = os.path.join(root, fname)
            ext = fname.lower().rsplit(".", 1)[-1] if "." in fname else ""
            if ext in ("html", "htm"):
                html_total += 1
                if fix_file(fp):
                    html_fixed += 1
            elif ext == "css":
                css_total += 1
                if fix_file(fp):
                    css_fixed += 1

    print(f"HTML : {html_total} файлов, исправлено {html_fixed}")
    print(f"CSS  : {css_total} файлов, исправлено {css_fixed}")
    print("Готово.")


if __name__ == "__main__":
    main()
