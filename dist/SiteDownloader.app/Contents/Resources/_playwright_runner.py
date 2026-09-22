#!/usr/bin/env python3
"""
Запускается системным python3 (с установленным playwright).
Передаёт параметры через аргументы командной строки.
Вызывается из GUI, когда Playwright не виден из bundled Python.
"""
import json
import os
import re
import sys
import time
import urllib.parse
from collections import deque
from pathlib import Path

# Принудительно UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Добавляем корень проекта в путь, чтобы импортировать site_downloader
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

import requests
from site_downloader import (
    url_to_local_path, save, fix_html, fix_css,
    _stats, UA,
)


def main():
    url = sys.argv[1]
    out_dir = Path(sys.argv[2])
    allowed_hosts = set(sys.argv[3].split(","))
    max_pages = int(sys.argv[4])
    depth_limit = int(sys.argv[5])
    exclude_pattern = sys.argv[6] if sys.argv[6] != "None" else None
    exclude_re = re.compile(exclude_pattern) if exclude_pattern else None

    from playwright.sync_api import sync_playwright

    visited = set()
    queue: deque[str] = deque([url])
    count = 0
    _stats.pages = 0
    _stats.assets = 0
    _stats.skipped = 0
    _stats.failed.clear()

    print(f"PW_START")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            ignore_https_errors=True,
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
            window.chrome = { runtime: {} };
        """)
        page = context.new_page()
        page.set_default_timeout(30000)

        while queue and count < max_pages:
            current = queue.popleft()
            norm = current.rstrip("/")
            if norm in visited:
                continue
            if exclude_re and exclude_re.search(current):
                continue
            visited.add(norm)
            count += 1

            try:
                print(f"PW_PAGE|{count}|{current}", flush=True)

                page.goto(current, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(2000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(500)

                try:
                    page.wait_for_function(
                        """
                        () => {
                            const root = document.querySelector('#root, #app, #__next, #__nuxt');
                            if (!root) return true;
                            return root.textContent.length > 100;
                        }
                        """,
                        timeout=5000,
                    )
                except Exception:
                    pass

                html = page.content()
                local = url_to_local_path(current, url, out_dir)
                save(local, html.encode("utf-8"))
                _stats.pages += 1

                # Ассеты через requests
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "html.parser")
                for img in soup.find_all("img", src=True):
                    _dl(img["src"], url, out_dir, allowed_hosts)
                for link in soup.find_all("link", href=True):
                    h = link["href"]
                    if any(h.endswith(e) for e in [".css", ".js", ".ico"]):
                        _dl(h, url, out_dir, allowed_hosts)
                for m in re.finditer(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", html):
                    u = m.group(1).strip().split("?")[0]
                    if not u.startswith("data:") and u:
                        _dl(u, url, out_dir, allowed_hosts)

                # Ссылки
                links = page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(el => el.href).filter(h => h.startsWith('http'))",
                )
                base_host = urllib.parse.urlparse(url).netloc.lower()
                for link in links:
                    parsed = urllib.parse.urlparse(link)
                    if parsed.netloc.lower() in allowed_hosts:
                        ln = link.rstrip("/")
                        if ln not in visited:
                            queue.append(link)

            except Exception as e:
                print(f"PW_FAIL|{current}|{e}", flush=True)
                _stats.failed.append((current, str(e)))

        browser.close()

    # Фикс путей
    fixed = 0
    for f in out_dir.rglob("*.html"):
        try:
            fix_html(f, out_dir)
            fixed += 1
        except Exception:
            pass
    for f in out_dir.rglob("*.css"):
        try:
            fix_css(f, out_dir)
            fixed += 1
        except Exception:
            pass

    # Выводим результат
    print(f"PW_DONE|{_stats.pages}|{_stats.assets}|{_stats.skipped}|{len(_stats.failed)}", flush=True)
    for url, err in _stats.failed:
        print(f"PW_ERR|{url}|{err}", flush=True)


def _dl(asset_url: str, base_url: str, out_dir: Path, allowed_hosts: set[str]):
    abs_url = urllib.parse.urljoin(base_url, asset_url)
    parsed = urllib.parse.urlparse(abs_url)
    if parsed.netloc and parsed.netloc.lower() not in allowed_hosts:
        return
    if not parsed.netloc:
        return
    local = url_to_local_path(abs_url, base_url, out_dir)
    if local.exists() and local.stat().st_size > 0:
        _stats.skipped += 1
        return
    try:
        r = requests.get(abs_url, headers={"User-Agent": UA}, timeout=15, verify=False)
        if r.status_code == 200 and len(r.content) > 100:
            save(local, r.content)
            _stats.assets += 1
    except Exception:
        pass


if __name__ == "__main__":
    main()
