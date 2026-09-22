# -*- coding: utf-8 -*-
"""
Полное статическое зеркалирование www.155gymspb.ru для бэкапа/референса перед редизайном.

Осторожно с нагрузкой на сервер:
- запросы идут строго последовательно (без параллелизма)
- пауза ~1 сек между HTML-страницами, ~0.3 сек между ассетами
- один HTTP-клиент с keep-alive, стандартный User-Agent браузера
- ассеты (css/js/img/fonts) скачиваются один раз и кэшируются по URL
"""
import argparse
import os
import re
import sys
import time
import urllib.parse
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

import argparse

BASE = "http://www.155gymspb.ru"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
OUT_DIR = os.path.join(os.path.dirname(__file__), "archive")

HTML_DELAY = 1.0
ASSET_DELAY = 0.3

session = requests.Session()
session.headers.update({"User-Agent": UA})

downloaded_assets = set()
failed = []


def url_to_local_path(url: str) -> str:
    """Преобразует URL в локальный путь файла внутри OUT_DIR."""
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    if not path or path == "/":
        path = "/index.html"
    elif path.endswith("/"):
        path = path + "index.html"
    elif "." not in os.path.basename(path):
        path = path + "/index.html"
    path = path.lstrip("/")
    return os.path.join(OUT_DIR, path)


def fetch(url: str, timeout=20):
    try:
        r = session.get(url, timeout=timeout)
        if r.status_code == 200:
            return r
        print(f"  [!] {r.status_code} {url}")
        failed.append((url, r.status_code))
        return None
    except requests.RequestException as e:
        print(f"  [!] ERROR {url}: {e}")
        failed.append((url, str(e)))
        return None


def save_bytes(local_path: str, data: bytes):
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(data)


def download_asset(asset_url: str):
    if asset_url in downloaded_assets:
        return
    downloaded_assets.add(asset_url)
    parsed = urllib.parse.urlparse(asset_url)
    if parsed.netloc and parsed.netloc not in (
        "www.155gymspb.ru", "155gymspb.ru"
    ):
        return  # не скачиваем внешние ресурсы (CDN шрифтов, счетчики и т.п.)
    local_path = url_to_local_path(asset_url)
    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        return  # уже скачано в прошлом запуске — не дёргаем сервер повторно
    resp = fetch(asset_url)
    if resp is None:
        return
    save_bytes(local_path, resp.content)
    time.sleep(ASSET_DELAY)


ASSET_PATTERN = re.compile(
    r"""(?:src|href)=["']([^"']+\.(?:css|js|png|jpe?g|gif|svg|webp|woff2?|ttf|eot|ico))(?:\?[^"']*)?["']""",
    re.IGNORECASE,
)
CSS_URL_PATTERN = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""")


def extract_asset_urls(html_or_css: str, base_url: str):
    urls = set()
    for m in ASSET_PATTERN.finditer(html_or_css):
        urls.add(urllib.parse.urljoin(base_url, m.group(1)))
    for m in CSS_URL_PATTERN.finditer(html_or_css):
        u = m.group(1)
        if u.startswith("data:"):
            continue
        urls.add(urllib.parse.urljoin(base_url, u))
    return urls


def main():
    ap = argparse.ArgumentParser(description="Последовательное зеркалирование сайта по списку URL")
    ap.add_argument("url_list", help="Файл со списком URL страниц (по одной на строку)")
    ap.add_argument("-o", "--output", help="Папка для сохранения (по умолчанию archive/)")
    ap.add_argument("--base", default=BASE, help=f"Базовый URL сайта (по умолчанию {BASE})")
    args = ap.parse_args()

    global OUT_DIR, BASE
    if args.output:
        OUT_DIR = args.output
    BASE = args.base.rstrip("/")

    with open(args.url_list, encoding="utf-8") as f:
        page_urls = [l.strip() for l in f if l.strip()]

    print(f"Всего страниц для зеркалирования: {len(page_urls)}")

    for i, page_url in enumerate(page_urls, 1):
        print(f"[{i}/{len(page_urls)}] {page_url}")
        local_path = url_to_local_path(page_url)

        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            with open(local_path, encoding="utf-8", errors="ignore") as f:
                html = f.read()
        else:
            resp = fetch(page_url)
            if resp is None:
                continue
            html = resp.text
            save_bytes(local_path, resp.content)
            time.sleep(HTML_DELAY)

        asset_urls = extract_asset_urls(html, page_url)
        for asset_url in asset_urls:
            download_asset(asset_url)
            if asset_url.lower().endswith(".css"):
                css_resp = fetch(asset_url) if asset_url not in downloaded_assets else None
                # CSS уже скачан выше; читаем сохранённый файл, чтобы найти вложенные url()
                css_local = url_to_local_path(asset_url)
                if os.path.exists(css_local):
                    try:
                        with open(css_local, encoding="utf-8", errors="ignore") as cf:
                            css_text = cf.read()
                        nested = extract_asset_urls(css_text, asset_url)
                        for n_url in nested:
                            download_asset(n_url)
                    except Exception:
                        pass

        time.sleep(HTML_DELAY)

    print("\nГотово.")
    print(f"Страниц обработано: {len(page_urls)}")
    print(f"Ассетов скачано: {len(downloaded_assets)}")
    print(f"Ошибок: {len(failed)}")
    if failed:
        with open(os.path.join(OUT_DIR, "_failed.txt"), "w", encoding="utf-8") as f:
            for url, err in failed:
                f.write(f"{url}\t{err}\n")


if __name__ == "__main__":
    main()
