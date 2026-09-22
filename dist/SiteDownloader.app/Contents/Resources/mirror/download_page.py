# -*- coding: utf-8 -*-
"""
Скачать одну страницу www.pavlovo-school.ru со всеми картинками и CSS.
Результат — в папке archive/, открывается локально через index.html.
"""
import os
import re
import sys
import time
import urllib.parse

import requests
import truststore

truststore.inject_into_ssl()

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

TARGET_URL = "https://www.pavlovo-school.ru/"
OUT_DIR = os.path.join(os.path.dirname(__file__), "archive")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ASSET_DELAY = 0.3

session = requests.Session()
session.headers["User-Agent"] = UA

downloaded = set()
failed = []

ALLOWED_HOSTS = {"www.pavlovo-school.ru", "pavlovo-school.ru"}

ASSET_RE = re.compile(
    r"""(?:src|href|content|data-src|data-background|poster)=["']([^"'#]+\.(?:css|js|png|jpe?g|gif|svg|webp|woff2?|ttf|eot|ico|mp4|webm|pdf))(?:[?#][^"']*)?["']""",
    re.IGNORECASE,
)
CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""")


def url_to_path(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    if not path or path == "/":
        path = "/index.html"
    elif path.endswith("/"):
        path += "index.html"
    elif "." not in os.path.basename(path):
        path += "/index.html"
    return os.path.join(OUT_DIR, path.lstrip("/"))


def fetch(url: str):
    try:
        r = session.get(url, timeout=20)
        if r.status_code == 200:
            return r
        print(f"  [!] {r.status_code}  {url}")
        failed.append((url, r.status_code))
    except requests.RequestException as e:
        print(f"  [!] ERR  {url}: {e}")
        failed.append((url, str(e)))
    return None


def save(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def extract_assets(text: str, base: str):
    urls = set()
    for m in ASSET_RE.finditer(text):
        urls.add(urllib.parse.urljoin(base, m.group(1)))
    for m in CSS_URL_RE.finditer(text):
        u = m.group(1).split("#")[0]
        if not u.startswith("data:"):
            urls.add(urllib.parse.urljoin(base, u))
    return urls


def download_asset(url: str):
    if url in downloaded:
        return
    downloaded.add(url)
    host = urllib.parse.urlparse(url).netloc
    if host and host not in ALLOWED_HOSTS:
        return  # внешние CDN пропускаем
    local = url_to_path(url)
    if os.path.exists(local) and os.path.getsize(local) > 0:
        print(f"  [=] cached  {url}")
        return
    resp = fetch(url)
    if resp is None:
        return
    save(local, resp.content)
    print(f"  [+] {url}")
    time.sleep(ASSET_DELAY)

    # Если CSS — рекурсивно тянем вложенные ассеты
    if url.lower().split("?")[0].endswith(".css"):
        try:
            css_text = resp.text
            for nested in extract_assets(css_text, url):
                download_asset(nested)
        except Exception:
            pass


def main():
    print(f"Скачиваем: {TARGET_URL}")
    resp = fetch(TARGET_URL)
    if resp is None:
        print("Не удалось получить страницу.")
        return

    local_index = url_to_path(TARGET_URL)
    save(local_index, resp.content)
    print(f"[HTML] сохранён -> {local_index}")

    assets = extract_assets(resp.text, TARGET_URL)
    print(f"Найдено ассетов: {len(assets)}")
    for asset_url in sorted(assets):
        download_asset(asset_url)

    print(f"\nГотово. Ассетов скачано: {len(downloaded)}, ошибок: {len(failed)}")
    if failed:
        for u, e in failed:
            print(f"  FAIL {u} -> {e}")


if __name__ == "__main__":
    main()
