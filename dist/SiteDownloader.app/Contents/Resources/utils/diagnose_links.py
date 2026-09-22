# -*- coding: utf-8 -*-
"""
Разбирает битые ссылки на страницы: сколько из них — баг шаблона (навигация),
а сколько реально отсутствующие страницы контента.

Ключевая проверка: если битую ссылку трактовать как корневую (от result/),
она находится? Значит это ошибка вычисления пути, а не пропавшая страница.

Запуск:  python site_review/diagnose_links.py
"""
import os
import re
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = Path(__file__).resolve().parent.parent
RESULT = ROOT / "result"
ARCHIVE = ROOT / "site_review" / "archive"
BASE_HTML = ROOT / "design" / "template" / "base.html"

ATTR = re.compile(r"""(?:href|src)\s*=\s*(["'])(.*?)\1""", re.I | re.S)
PAGE_EXT = {".html", ".htm", ""}


def nav_hrefs() -> set[str]:
    """Ссылки, зашитые в base.html (шапка, меню, футер)."""
    if not BASE_HTML.exists():
        return set()
    text = BASE_HTML.read_text(encoding="utf-8", errors="replace")
    out = set()
    for m in ATTR.finditer(text):
        href = m.group(2).strip()
        if href and not href.startswith(("#", "http", "mailto:", "tel:", "javascript:", "data:")):
            out.add(href.lstrip("/"))
    return out


def main() -> None:
    nav = nav_hrefs()
    print(f"Ссылок зашито в base.html: {len(nav)}\n")

    fixable_root = Counter()      #找 находится, если считать от корня result/
    truly_missing = Counter()     # нет ни от корня, ни в архиве
    in_archive_only = Counter()   # нет в result, но есть в архиве
    from_nav = Counter()
    from_content = Counter()

    for f in sorted(RESULT.rglob("*.html")):
        for m in ATTR.finditer(f.read_text(encoding="utf-8", errors="replace")):
            raw = m.group(2).strip()
            if not raw or raw.startswith(("#", "http", "https", "mailto:", "tel:", "javascript:", "data:", "//")):
                continue

            clean = urllib.parse.unquote(raw.split("#")[0].split("?")[0])
            if not clean or Path(clean).suffix.lower() not in PAGE_EXT:
                continue

            # как сейчас резолвится
            if clean.startswith("/"):
                cur = RESULT / clean.lstrip("/")
            else:
                cur = f.parent / clean
            cur = Path(os.path.normpath(str(cur)))
            if cur.is_dir() or clean.endswith("/"):
                cur = cur / "index.html"
            if cur.exists():
                continue  # не битая

            key = clean.lstrip("./")
            (from_nav if key in nav else from_content)[key] += 1

            # а если считать от корня result/?
            as_root = RESULT / key
            if as_root.is_dir() or key.endswith("/"):
                as_root = as_root / "index.html"
            if as_root.exists():
                fixable_root[key] += 1
                continue

            arch = ARCHIVE / key
            if arch.is_dir() or key.endswith("/"):
                arch = arch / "index.html"
            if arch.exists():
                in_archive_only[key] += 1
            else:
                truly_missing[key] += 1

    total = sum(fixable_root.values()) + sum(in_archive_only.values()) + sum(truly_missing.values())
    print("--- Битые ссылки на страницы ---")
    print(f"  всего вхождений:                          {total:>6}")
    print(f"  чинится корневым путём (баг вычисления):  {sum(fixable_root.values()):>6}"
          f"   уникальных: {len(fixable_root)}")
    print(f"  нет в result, но ЕСТЬ в архиве:           {sum(in_archive_only.values()):>6}"
          f"   уникальных: {len(in_archive_only)}")
    print(f"  нет нигде (реально мёртвые):              {sum(truly_missing.values()):>6}"
          f"   уникальных: {len(truly_missing)}")

    print(f"\n  из шаблона base.html:  {sum(from_nav.values()):>6}   уникальных: {len(from_nav)}")
    print(f"  из контента страниц:   {sum(from_content.values()):>6}   уникальных: {len(from_content)}")

    print("\n--- Уникальные ссылки, которых нет нигде (топ-25) ---")
    for ref, cnt in truly_missing.most_common(25):
        print(f"  {cnt:>4}  {ref[:95]}")

    print("\n--- Есть в архиве, но не перенесено в result (топ-15) ---")
    for ref, cnt in in_archive_only.most_common(15):
        print(f"  {cnt:>4}  {ref[:95]}")

    out = ROOT / "site_review" / "_dead_links.txt"
    out.write_text(
        "".join(f"{c}\t{r}\n" for r, c in truly_missing.most_common()), encoding="utf-8"
    )
    print(f"\n→ мёртвые ссылки выгружены: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
