# -*- coding: utf-8 -*-
"""
Аудит папки result/: считает битые ссылки, изображения, документы.
Проверяет, можно ли восстановить недостающие медиа из site_review/archive/.

Запуск:  python site_review/audit_result.py
"""
import os
import re
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = Path(__file__).resolve().parent.parent
# Папку можно передать аргументом: python site_review/audit_result.py site/dist
RESULT = (Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "result")
ARCHIVE = ROOT / "site_review" / "archive"

# (?<![\w-]) обязателен: без него `src=` находится внутри `data-original-src=`,
# которым заглушки помечают путь к утраченному файлу, и все заглушки
# считаются битыми изображениями.
ATTR = re.compile(
    r"""(?<![\w-])(?:href|src|poster|data-src|data-background)\s*=\s*(["'])(.*?)\1""",
    re.I | re.S,
)
SRCSET = re.compile(r"""(?<![\w-])srcset\s*=\s*(["'])(.*?)\1""", re.I | re.S)

IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".ico"}
DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".zip", ".rar"}
RESIZE_RE = re.compile(r"-\d+x\d+(?=\.\w+$)")


def kind(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in IMG_EXT:
        return "image"
    if ext in DOC_EXT:
        return "document"
    if ext == ".css":
        return "css"
    if ext == ".js":
        return "js"
    if ext in (".html", ".htm", ""):
        return "page"
    return "other"


def collect_refs(text: str) -> list[str]:
    refs = [m.group(2).strip() for m in ATTR.finditer(text)]
    for m in SRCSET.finditer(text):
        for part in m.group(2).split(","):
            piece = part.strip().split()
            if piece:
                refs.append(piece[0])
    return refs


def main() -> None:
    stats = Counter()
    broken = Counter()
    broken_kind = Counter()
    abs_own_kind = Counter()
    ext_hosts = Counter()
    pages_with_abs: set[str] = set()
    wanted_media: Counter = Counter()

    html_files = sorted(RESULT.rglob("*.html"))
    for f in html_files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for raw in collect_refs(text):
            if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "data:", "//")):
                stats["служебные/пропущено"] += 1
                continue

            if raw.lower().startswith(("http://", "https://")):
                parsed = urllib.parse.urlparse(raw)
                host = parsed.netloc.lower()
                if "155gymspb.ru" in host:
                    stats["абсолютные на 155gymspb.ru"] += 1
                    abs_own_kind[kind(parsed.path)] += 1
                    pages_with_abs.add(str(f.relative_to(RESULT)))
                    site_path = urllib.parse.unquote(parsed.path).lstrip("/")
                    if kind(site_path) in ("image", "document"):
                        wanted_media[site_path] += 1
                else:
                    stats["внешние"] += 1
                    ext_hosts[host] += 1
                continue

            stats["локальные"] += 1
            clean = urllib.parse.unquote(raw.split("#")[0].split("?")[0])
            if not clean:
                continue

            if clean.startswith("/"):
                target = RESULT / clean.lstrip("/")
            else:
                target = f.parent / clean
            target = Path(os.path.normpath(str(target)))
            if target.is_dir() or clean.endswith("/"):
                target = target / "index.html"

            if target.exists():
                stats["локальные OK"] += 1
                continue

            stats["локальные БИТЫЕ"] += 1
            broken[clean] += 1
            k = kind(clean)
            broken_kind[k] += 1
            if k in ("image", "document"):
                try:
                    site_path = str(target.relative_to(RESULT)).replace(os.sep, "/")
                except ValueError:
                    site_path = clean.lstrip("./")
                wanted_media[site_path] += 1

    print(f"HTML-страниц в result/: {len(html_files)}\n")

    print("--- Ссылки по категориям ---")
    for key, val in stats.most_common():
        print(f"  {key:28} {val:>7}")

    print("\n--- Битые локальные ссылки по типу файла ---")
    for key, val in broken_kind.most_common():
        print(f"  {key:12} {val:>7}")

    print("\n--- Абсолютные ссылки на 155gymspb.ru по типу ---")
    for key, val in abs_own_kind.most_common():
        print(f"  {key:12} {val:>7}")

    print(f"\nСтраниц с абсолютными ссылками: {len(pages_with_abs)} из {len(html_files)}")
    print(f"Уникальных битых локальных путей: {len(broken)}")

    # Проверяем, что из недостающих медиа лежит в архиве
    have_exact: list[str] = []
    have_original: list[str] = []
    missing: list[str] = []
    missing_ext = Counter()
    for site_path in sorted(wanted_media):
        if (ARCHIVE / site_path).exists():
            have_exact.append(site_path)
        elif (ARCHIVE / RESIZE_RE.sub("", site_path)).exists():
            have_original.append(site_path)
        else:
            missing.append(site_path)
            missing_ext[Path(site_path).suffix.lower() or "(без расширения)"] += 1

    print("\n--- Медиа и документы, на которые ссылаются страницы ---")
    print(f"  уникальных файлов упомянуто:        {len(wanted_media)}")
    print(f"  есть в архиве как есть:             {len(have_exact)}")
    print(f"  есть оригинал без -WxH (ресайз):    {len(have_original)}")
    print(f"  НЕТ в архиве (нужна заглушка):      {len(missing)}")
    print(f"  отсутствующие по расширениям:       {dict(missing_ext.most_common(10))}")

    # Выгружаем списки для следующих шагов (докачка / заглушки)
    missing_file = ROOT / "site_review" / "_missing_media.txt"
    missing_file.write_text(
        "".join(f"{wanted_media[p]}\t{p}\n" for p in sorted(missing, key=lambda x: -wanted_media[x])),
        encoding="utf-8",
    )
    recoverable_file = ROOT / "site_review" / "_recoverable_media.txt"
    recoverable_file.write_text(
        "".join(f"{p}\n" for p in have_exact + have_original),
        encoding="utf-8",
    )
    print(f"  → список отсутствующих:  {missing_file.relative_to(ROOT)}")
    print(f"  → список в архиве:       {recoverable_file.relative_to(ROOT)}")

    print("\n--- Топ-10 внешних хостов ---")
    for host, val in ext_hosts.most_common(10):
        print(f"  {host:40} {val:>6}")

    print("\n--- Топ-15 битых локальных ссылок ---")
    for ref, val in broken.most_common(15):
        print(f"  {val:>4}  {ref[:100]}")

    fails = [
        str(f.relative_to(RESULT))
        for f in html_files
        if "Контент не извлечён" in f.read_text(encoding="utf-8", errors="replace")
    ]
    print(f"\nСтраниц без извлечённого контента: {len(fails)}")
    for name in fails[:10]:
        print(f"  {name}")


if __name__ == "__main__":
    main()
