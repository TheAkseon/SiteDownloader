# -*- coding: utf-8 -*-
"""
Исправляет пути во ВСЕХ HTML и CSS в archive/ :
  - корневые /path/to/asset  →  относительный путь от текущего файла
  - работает и с query-string (?v=xxx) — убирает её при поиске на диске
  - обрабатывает и одинарные, и двойные кавычки
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive")


def rel(from_file: str, abs_asset: str) -> str:
    return os.path.relpath(abs_asset, os.path.dirname(from_file)).replace("\\", "/")


def local_path(abs_path_with_qs: str) -> str:
    """Убирает query-string и возвращает абсолютный путь в archive/."""
    clean = abs_path_with_qs.split("?")[0].split("#")[0]
    return os.path.join(OUT_DIR, clean.lstrip("/").replace("/", os.sep))


# Атрибуты href/src/... с корневым путём (оба вида кавычек)
ATTR_RE = re.compile(
    r"""((?:href|src|content|data-src|data-background|poster)=)(['"])(\/[^'"]*)\2""",
    re.IGNORECASE,
)
# url(/...) в CSS и инлайн-стилях
CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)(\/[^'")\s]+)\1\s*\)""", re.IGNORECASE)


def fix_file(filepath: str):
    with open(filepath, encoding="utf-8", errors="replace") as f:
        original = f.read()

    def repl_attr(m):
        attr_eq, quote, path = m.group(1), m.group(2), m.group(3)
        lp = local_path(path)
        if os.path.exists(lp):
            return f'{attr_eq}{quote}{rel(filepath, lp)}{quote}'
        return m.group(0)

    def repl_url(m):
        quote, path = m.group(1), m.group(2)
        lp = local_path(path)
        if os.path.exists(lp):
            return f"url({quote}{rel(filepath, lp)}{quote})"
        return m.group(0)

    result = ATTR_RE.sub(repl_attr, original)
    result = CSS_URL_RE.sub(repl_url, result)

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
            ext = fname.lower().rsplit(".", 1)[-1]
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
