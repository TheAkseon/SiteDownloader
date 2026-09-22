# -*- coding: utf-8 -*-
"""
Собирает медиа и документы в site/public/, сохраняя пути wp-content/uploads/...

  1. Копирует то, что уже есть в site_review/archive/
  2. Докачивает отсутствующее с живого сайта 155gymspb.ru
  3. Пишет отчёты: что не удалось скачать, какие документы недоступны

Запуск:  python scripts/fetch_media.py [--workers N] [--limit N]
Повторный запуск дешёвый — уже загруженные файлы пропускаются.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "site_review" / "archive"
PUBLIC = ROOT / "site" / "public"
MISSING_FILE = ROOT / "site_review" / "_missing_media.txt"
RECOVERABLE_FILE = ROOT / "site_review" / "_recoverable_media.txt"

BASE = "http://www.155gymspb.ru"
UA = "Mozilla/5.0 (compatible; GymSite155-archive/1.0)"
TIMEOUT = 25
RETRIES = 2
DELAY = 0.15  # пауза между запросами внутри воркера

DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".zip", ".rar"}
RESIZE_RE = re.compile(r"-\d+x\d+(?=\.\w+$)")

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def safe_dest(site_path: str) -> Path | None:
    """Путь внутри PUBLIC. Отсекает traversal и абсолютные пути."""
    clean = site_path.replace("\\", "/").lstrip("/")
    if not clean or ".." in clean.split("/"):
        return None
    dest = (PUBLIC / clean).resolve()
    try:
        dest.relative_to(PUBLIC.resolve())
    except ValueError:
        return None
    return dest


def read_paths(path: Path, tabbed: bool) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        out.append(line.split("\t", 1)[1] if tabbed and "\t" in line else line)
    return out


def copy_from_archive() -> Counter:
    """Переносит медиа, уже лежащее в архиве."""
    stats = Counter()
    for site_path in read_paths(RECOVERABLE_FILE, tabbed=False):
        dest = safe_dest(site_path)
        if dest is None:
            stats["небезопасный путь"] += 1
            continue
        if dest.exists():
            stats["уже на месте"] += 1
            continue

        src = ARCHIVE / site_path
        if not src.exists():
            # ресайз-вариант: берём оригинал под нужным именем
            src = ARCHIVE / RESIZE_RE.sub("", site_path)
        if not src.exists():
            stats["не найдено в архиве"] += 1
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        stats["скопировано"] += 1
    return stats


def fetch(site_path: str) -> tuple[str, str, int]:
    """
    Качает один файл. Возвращает (site_path, статус, размер).
    Статусы: ok / skip / 404 / html / error
    """
    dest = safe_dest(site_path)
    if dest is None:
        return site_path, "error", 0
    if dest.exists() and dest.stat().st_size > 0:
        return site_path, "skip", dest.stat().st_size

    url = BASE + "/" + urllib.parse.quote(site_path)
    req = urllib.request.Request(url, headers={"User-Agent": UA})

    for attempt in range(RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                # WordPress иногда отдаёт 200 с HTML-страницей ошибки вместо файла
                if "text/html" in ctype and Path(site_path).suffix.lower() != ".html":
                    return site_path, "html", 0
                data = resp.read()
            if not data:
                return site_path, "404", 0
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            time.sleep(DELAY)
            return site_path, "ok", len(data)
        except urllib.error.HTTPError as e:
            return site_path, "404" if e.code == 404 else f"http{e.code}", 0
        except Exception:
            if attempt == RETRIES:
                return site_path, "error", 0
            time.sleep(0.6 * (attempt + 1))
    return site_path, "error", 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="ограничить число загрузок (для теста)")
    args = ap.parse_args()

    PUBLIC.mkdir(parents=True, exist_ok=True)

    print("=== Шаг 1: копирование из архива ===")
    copied = copy_from_archive()
    for key, val in copied.most_common():
        print(f"  {key:24} {val:>5}")

    targets = read_paths(MISSING_FILE, tabbed=True)
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print(f"\nНет {MISSING_FILE.name}. Сначала запусти site_review/audit_result.py")
        return

    print(f"\n=== Шаг 2: докачка с живого сайта ({len(targets)} файлов, {args.workers} потоков) ===")
    stats = Counter()
    total_bytes = 0
    failed: list[str] = []
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch, p): p for p in targets}
        for fut in as_completed(futures):
            site_path, status, size = fut.result()
            stats[status] += 1
            total_bytes += size
            done += 1
            if status not in ("ok", "skip"):
                failed.append(site_path)
            if status == "ok":
                log(f"  [{done:>4}/{len(targets)}] OK  {size/1024:>7.0f} КБ  {site_path[-64:]}")
            elif done % 100 == 0:
                log(f"  [{done:>4}/{len(targets)}] ...")

    print("\n--- Итог докачки ---")
    for key, val in stats.most_common():
        print(f"  {key:10} {val:>5}")
    print(f"  объём: {total_bytes/1024/1024:.1f} МБ")

    # Отчёты
    reports = ROOT / "site_review"
    (reports / "_still_missing.txt").write_text(
        "".join(f"{p}\n" for p in sorted(failed)), encoding="utf-8"
    )

    missing_docs = sorted(p for p in failed if Path(p).suffix.lower() in DOC_EXT)
    lines = [
        "# Недоступные документы\n",
        "\nФайлы, на которые ссылаются страницы сайта, но которых нет ни в архиве,\n",
        "ни на живом сервере. Их нужно перезалить — раздел «Документы» обязателен\n",
        "по Приказу №1493.\n",
        f"\n**Всего недоступно: {len(missing_docs)}**\n\n",
    ]
    by_ext: dict[str, list[str]] = {}
    for p in missing_docs:
        by_ext.setdefault(Path(p).suffix.lower(), []).append(p)
    for ext in sorted(by_ext, key=lambda x: -len(by_ext[x])):
        lines.append(f"\n## {ext} — {len(by_ext[ext])}\n\n")
        lines.extend(f"- `{p}`\n" for p in by_ext[ext])
    (ROOT / "MISSING_DOCUMENTS.md").write_text("".join(lines), encoding="utf-8")

    got = stats["ok"] + stats["skip"]
    print(f"\nПолучено: {got}/{len(targets)}   не удалось: {len(failed)}")
    print(f"  → отчёт по документам: MISSING_DOCUMENTS.md ({len(missing_docs)} шт.)")
    print(f"  → полный список недостающего: site_review/_still_missing.txt")


if __name__ == "__main__":
    main()
