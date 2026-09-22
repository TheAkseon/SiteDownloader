# -*- coding: utf-8 -*-
"""
Полное зеркалирование www.pavlovo-school.ru:
  - BFS по всем внутренним HTML-страницам
  - скачивание всех ассетов (CSS, JS, картинки, шрифты)
  - исправление путей во всех HTML и CSS для офлайн-просмотра
"""
import argparse
import os
import re
import sys
import time
import urllib.parse
from collections import deque
import threading
import requests
import truststore

truststore.inject_into_ssl()
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

BASE    = "https://www.pavlovo-school.ru"
HOSTS   = {"www.pavlovo-school.ru", "pavlovo-school.ru"}
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive")
UA      = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PAGE_DELAY  = 1.0
ASSET_DELAY = 0.2

_tls          = threading.local()
_lock         = threading.Lock()
pages_visited = set()
assets_done   = set()
failed        = []

ASSET_RE = re.compile(
    r'(?:src|href|content|data-src|data-background|poster)=["\']'
    r'([^"\'#]+\.(?:css|js|png|jpe?g|gif|svg|webp|woff2?|ttf|eot|ico|mp4|webm|pdf))'
    r'(?:[?#][^"\']*)?["\']',
    re.IGNORECASE,
)
CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")
LINK_RE    = re.compile(r'<a\b[^>]+\bhref=["\']([^"\'#?][^"\']*)["\']', re.IGNORECASE)

def get_session():
    if not hasattr(_tls, "s"):
        s = requests.Session()
        s.headers["User-Agent"] = UA
        _tls.s = s
    return _tls.s


def url_to_path(url):
    p = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(p.path)
    if not path or path == "/":
        path = "/index.html"
    elif path.endswith("/"):
        path += "index.html"
    elif "." not in os.path.basename(path):
        path += "/index.html"
    return os.path.join(OUT_DIR, path.lstrip("/"))


def fetch(url, timeout=25):
    try:
        r = get_session().get(url, timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return r
        print(f"  [!] {r.status_code}  {url}")
        with _lock:
            failed.append((url, r.status_code))
    except Exception as e:
        print(f"  [!] ERR  {url}: {e}")
        with _lock:
            failed.append((url, str(e)))
    return None


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def is_internal(url):
    h = urllib.parse.urlparse(url).netloc
    return not h or h in HOSTS


def extract_assets(text, base):
    urls = set()
    for m in ASSET_RE.finditer(text):
        urls.add(urllib.parse.urljoin(base, m.group(1)))
    for m in CSS_URL_RE.finditer(text):
        u = m.group(1).split("#")[0].strip()
        if not u.startswith("data:") and u:
            urls.add(urllib.parse.urljoin(base, u))
    return {u for u in urls if is_internal(u)}


def extract_links(html, base):
    links = set()
    for m in LINK_RE.finditer(html):
        raw = m.group(1).split("?")[0].split("#")[0]
        u = urllib.parse.urljoin(base, raw)
        p = urllib.parse.urlparse(u)
        if p.scheme not in ("http", "https") or p.netloc not in HOSTS:
            continue
        ext = os.path.splitext(p.path)[1].lower()
        if ext in ("", ".html", ".htm"):
            links.add(u)
    return links


def download_asset(url):
    with _lock:
        if url in assets_done:
            return
        assets_done.add(url)
    local = url_to_path(url)
    if os.path.exists(local) and os.path.getsize(local) > 0:
        return
    resp = fetch(url)
    if not resp:
        return
    save(local, resp.content)
    print(f"  [asset] {url.replace(BASE, '')}")
    time.sleep(ASSET_DELAY)
    if url.lower().split("?")[0].endswith(".css"):
        for nested in extract_assets(resp.text, url):
            download_asset(nested)


def process_page(url):
    local = url_to_path(url)
    if os.path.exists(local) and os.path.getsize(local) > 0:
        with open(local, encoding="utf-8", errors="replace") as f:
            html = f.read()
    else:
        resp = fetch(url)
        if not resp:
            return set()
        html = resp.text
        save(local, resp.content)
        print(f"  [page] {url.replace(BASE, '') or '/'}")
        time.sleep(PAGE_DELAY)

    for asset_url in extract_assets(html, url):
        download_asset(asset_url)

    return extract_links(html, url)


def rel_path(from_file, asset_abs):
    """Relative path from from_file's directory to asset_abs."""
    return os.path.relpath(asset_abs, os.path.dirname(from_file)).replace("\\", "/")


def fix_html(html_file):
    with open(html_file, encoding="utf-8", errors="replace") as f:
        text = f.read()

    def repl_attr(m):
        attr, path = m.group(1), m.group(2)
        local = os.path.join(OUT_DIR, path.lstrip("/"))
        if os.path.exists(local):
            return f'{attr}="{rel_path(html_file, local)}"'
        return m.group(0)

    def repl_url(m):
        path = m.group(1).split("#")[0]
        if path.startswith("data:") or not path.startswith("/"):
            return m.group(0)
        local = os.path.join(OUT_DIR, path.lstrip("/"))
        if os.path.exists(local):
            return f"url('{rel_path(html_file, local)}')"
        return m.group(0)

    text = re.sub(
        r'((?:href|src|content|data-src|data-background|poster))="(/[^"]*)"',
        repl_attr, text,
    )
    text = re.sub(r"url\(\s*['\"]?(/[^'\")]+)['\"]?\s*\)", repl_url, text)
    with open(html_file, "wb") as f:
        f.write(text.encode("utf-8"))


def fix_css(css_file):
    with open(css_file, encoding="utf-8", errors="replace") as f:
        text = f.read()

    def repl_url(m):
        path = m.group(1).split("#")[0]
        if path.startswith("data:") or not path.startswith("/"):
            return m.group(0)
        local = os.path.join(OUT_DIR, path.lstrip("/"))
        if os.path.exists(local):
            return f"url('{rel_path(css_file, local)}')"
        return m.group(0)

    text = re.sub(r"url\(\s*['\"]?(/[^'\")]+)['\"]?\s*\)", repl_url, text)
    with open(css_file, "wb") as f:
        f.write(text.encode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="Полное BFS-зеркалирование сайта с авто-фиксом путей")
    ap.add_argument("-o", "--output", help="Папка для сохранения (по умолчанию archive/)")
    ap.add_argument("--base", default=BASE, help=f"Базовый URL (по умолчанию {BASE})")
    ap.add_argument("--workers", type=int, default=1, help="Количество потоков (по умолч. 1)")
    ap.add_argument("--delay", type=float, default=PAGE_DELAY, help="Задержка между страницами")
    args = ap.parse_args()

    global OUT_DIR, BASE, PAGE_DELAY
    if args.output:
        OUT_DIR = args.output
    BASE = args.base.rstrip("/")
    if args.delay != PAGE_DELAY:
        PAGE_DELAY = args.delay

    start = BASE + "/"
    with _lock:
        pages_visited.add(start)
        pages_visited.add(BASE)

    queue = deque([start])
    page_count = 0

    while queue:
        url = queue.popleft()
        page_count += 1
        print(f"[{page_count}] {url}")
        new_links = process_page(url) or set()
        for link in new_links:
            norm = link.rstrip("/")
            with _lock:
                if norm not in pages_visited and norm + "/" not in pages_visited:
                    pages_visited.add(norm)
                    queue.append(link)

    print(f"\nСтраниц: {page_count}, ассетов: {len(assets_done)}, ошибок: {len(failed)}")

    print("\nИсправляем пути в HTML...")
    fixed_html = 0
    for root, _, files in os.walk(OUT_DIR):
        for fname in files:
            if fname.endswith((".html", ".htm")):
                fix_html(os.path.join(root, fname))
                fixed_html += 1
    print(f"  HTML: {fixed_html} файлов")

    print("Исправляем пути в CSS...")
    fixed_css = 0
    for root, _, files in os.walk(OUT_DIR):
        for fname in files:
            if fname.endswith(".css"):
                fix_css(os.path.join(root, fname))
                fixed_css += 1
    print(f"  CSS: {fixed_css} файлов")

    if failed:
        fail_path = os.path.join(OUT_DIR, "_failed.txt")
        with open(fail_path, "w", encoding="utf-8") as f:
            for url, err in failed:
                f.write(f"{url}\t{err}\n")
        print(f"\nОшибки сохранены: {fail_path}")

    print("\nГотово. Открывай archive/index.html.")


if __name__ == "__main__":
    main()
