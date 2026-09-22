# -*- coding: utf-8 -*-
"""
Докачивает недостающие изображения для архива 155gymspb.ru.

Стратегия: WordPress при загрузке создаёт resize-версии вида filename-300x200.jpg.
Оригинал лежит по тому же пути без суффикса -WxH.
Для каждого 404 из _failed.txt убираем суффикс и пробуем скачать оригинал.
Если оригинал уже скачан ранее — просто фиксируем, не дёргаем сервер.
"""
import argparse
import os
import re
import sys
import time
import urllib.parse

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

BASE     = "http://www.155gymspb.ru"
OUT_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive")
FAILED   = os.path.join(OUT_DIR, "_failed.txt")
UA       = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DELAY    = 0.4

# -300x200, -1210x642, -214x300, -scaled, -e1234567890123 и т.п.
RESIZE_RE = re.compile(r'-\d{2,4}x\d{2,4}(?=\.\w{2,5}$)')

session = requests.Session()
session.headers["User-Agent"] = UA


def url_to_path(url: str) -> str:
    p = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(p.path).lstrip("/")
    return os.path.join(OUT_DIR, path.replace("/", os.sep))


def original_url(url: str) -> str | None:
    """Убирает суффикс resize из URL. Возвращает None если суффикса нет."""
    p = urllib.parse.urlparse(url)
    new_path = RESIZE_RE.sub("", p.path)
    if new_path == p.path:
        return None
    return urllib.parse.urlunparse(p._replace(path=new_path))


def fetch(url: str):
    try:
        r = session.get(url, timeout=20)
        return r if r.status_code == 200 else None
    except requests.RequestException:
        return None


def save(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def main():
    ap = argparse.ArgumentParser(description="Докачка недостающих файлов (WordPress resize-суффиксы)")
    ap.add_argument("failed_file", nargs="?", default=FAILED, help="Файл _failed.txt")
    ap.add_argument("-o", "--output", help="Папка архива (по умолчанию archive/)")
    ap.add_argument("--base", default=BASE, help=f"Базовый URL (по умолчанию {BASE})")
    args = ap.parse_args()

    global OUT_DIR, BASE
    if args.output:
        OUT_DIR = args.output
    BASE = args.base.rstrip("/")
    failed_path = args.failed_file

    with open(failed_path, encoding="utf-8", errors="replace") as f:
        failed_urls = [line.split("\t")[0].strip() for line in f if line.strip()]

    print(f"Всего 404-ошибок: {len(failed_urls)}")

    no_suffix   = 0   # у URL нет resize-суффикса (нечего попробовать)
    orig_cached = 0   # оригинал уже лежит на диске
    downloaded  = 0   # скачали новый оригинал
    still_404   = 0   # оригинал тоже 404
    errors      = 0   # сетевые ошибки

    remaining_failed = []

    for url in failed_urls:
        orig = original_url(url)
        if orig is None:
            no_suffix += 1
            remaining_failed.append(url)
            continue

        local = url_to_path(orig)
        if os.path.exists(local) and os.path.getsize(local) > 0:
            orig_cached += 1
            continue

        resp = fetch(orig)
        if resp is None:
            errors += 1
            remaining_failed.append(url)
            continue

        if resp.status_code if hasattr(resp, "status_code") else 200:
            # fetch возвращает только 200-ответы
            save(local, resp.content)
            downloaded += 1
            print(f"  [+] {orig.replace(BASE, '')}")
            time.sleep(DELAY)
        else:
            still_404 += 1
            remaining_failed.append(url)

    print(f"\nИтого:")
    print(f"  Без resize-суффикса (пропущено): {no_suffix}")
    print(f"  Оригинал уже на диске:           {orig_cached}")
    print(f"  Скачано оригиналов:              {downloaded}")
    print(f"  Оригинал тоже 404:               {still_404}")
    print(f"  Сетевые ошибки:                  {errors}")

    # Обновляем _failed.txt — оставляем только то, что так и не удалось
    if remaining_failed:
        with open(FAILED, "w", encoding="utf-8") as f:
            for u in remaining_failed:
                f.write(f"{u}\t404\n")
        print(f"\nОстаток в _failed.txt: {len(remaining_failed)}")
    else:
        os.remove(FAILED)
        print("\n_failed.txt удалён — все изображения скачаны!")


if __name__ == "__main__":
    main()
