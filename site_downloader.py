#!/usr/bin/env python3
"""
Универсальный загрузчик сайтов.

Скачивает сайт целиком для офлайн-просмотра.
Перед скачиванием анализирует сайт и предупреждает,
если сайт — веб-приложение (SPA/React/Angular/Vue), которое
нормально не сохранится.

Запуск:
  python site_downloader.py https://example.com
  python site_downloader.py https://example.com --depth 3 --workers 10
  python site_downloader.py https://example.com --output ./my_mirror
  python site_downloader.py https://example.com --browser   # через Playwright
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import json as json_lib

import requests
from bs4 import BeautifulSoup

# Отключаем предупреждения SSL
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ──────────────────────────────────────────────
#  Настройки по умолчанию
# ──────────────────────────────────────────────
DEFAULT_WORKERS = 5
PAGE_DELAY = 1.0
ASSET_DELAY = 0.2
TIMEOUT = 30
RETRIES = 2
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"

# Браузерные заголовки для обхода базовой защиты
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ru,en;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

ASSET_RE = re.compile(
    r'(?:src|href|content|data-src|data-lazy-src|data-original|data-background|poster)'
    r'=["\']([^"\'#]+\.(?:css|js|png|jpe?g|gif|svg|webp|avif|woff2?|ttf|eot|otf|ico|mp4|webm|ogv|mp3|ogg|wav|pdf|docx?|xlsx?|pptx?|zip|rar))'
    r'(?:[?#][^"\']*)?["\']',
    re.IGNORECASE,
)
CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", re.IGNORECASE)
LINK_RE = re.compile(r'<a\b[^>]+\bhref=["\']([^"\'#?][^"\']*)["\']', re.IGNORECASE)

# Признаки SPA / веб-приложения
CLOUDFLARE_SIGNALS = [
    "cf-browser-verification", "cf_challenge", "cf-ray",
    "Attention Required! | Cloudflare", "Just a moment...",
    "_cf_chl_opt", "cdn-cgi/challenge-platform",
    "Checking your browser before accessing",
]
DYNAMIC_KEYWORDS = [
    "api.", "/api/", "/graphql", "/rest/", "/ajax/",
    "fetch(", "XMLHttpRequest", "axios", "$.ajax",
    "application/json",
]

# SPA-фреймворки и их сигнатуры
SPA_FRAMEWORKS = {
    "React": {
        "divs": ["id=\"root\"", "id=\"__next\"", "id=\"__react-root\""],
        "data": [("__NEXT_DATA__", "json"), ("__reactRouterContext", "json")],
        "meta": ["react", "next.js"],
        "generator": ["next.js", "gatsby", "react-static"],
    },
    "Vue": {
        "divs": ["id=\"app\"", "id=\"__nuxt\""],
        "data": [("__NUXT__", "json"), ("window.__INITIAL_STATE__", "json")],
        "meta": ["vue", "nuxt", "vue.js"],
        "generator": ["nuxt", "vuepress", "vitepress"],
    },
    "Angular": {
        "divs": ["ng-version", "ng-app"],
        "data": [],
        "meta": ["angular"],
        "generator": [],
    },
    "Svelte": {
        "divs": ["id=\"svelte\""],
        "data": [("__SVELTEKIT__", "json"), ("__sveltekit", "json")],
        "meta": ["svelte", "sveltekit"],
        "generator": ["sveltekit"],
    },
}


# ──────────────────────────────────────────────
#  Состояние
# ──────────────────────────────────────────────
@dataclass
class Stats:
    pages: int = 0
    assets: int = 0
    failed: list[tuple[str, str | int]] = field(default_factory=list)
    skipped: int = 0


_lock = threading.Lock()
_stats = Stats()
_visited_pages: set[str] = set()
_downloaded_assets: set[str] = set()
_tls = threading.local()


# ──────────────────────────────────────────────
#  HTTP
# ──────────────────────────────────────────────
def get_session() -> requests.Session:
    if not hasattr(_tls, "session"):
        s = requests.Session()
        s.headers.update(BROWSER_HEADERS)

        # Пробуем truststore для системных сертификатов
        try:
            import truststore
            truststore.inject_into_ssl()
        except ImportError:
            pass

        # Явно указываем системные сертификаты macOS
        for cert_path in [
            "/etc/ssl/cert.pem",
            "/System/Library/OpenSSL/cert.pem",
            "/private/etc/ssl/cert.pem",
        ]:
            if os.path.exists(cert_path):
                os.environ.setdefault("SSL_CERT_FILE", cert_path)
                break

        # Отключаем проверку SSL (fallback)
        s.verify = False

        # Принудительно отключаем проверку на уровне адаптера
        from requests.adapters import HTTPAdapter

        class SSLAdapter(HTTPAdapter):
            def init_poolmanager(self, *args, **kwargs):
                kwargs["cert_reqs"] = "CERT_NONE"
                kwargs["assert_hostname"] = False
                return super().init_poolmanager(*args, **kwargs)

            def proxy_manager_for(self, *args, **kwargs):
                kwargs["cert_reqs"] = "CERT_NONE"
                kwargs["assert_hostname"] = False
                return super().proxy_manager_for(*args, **kwargs)

        s.mount("https://", SSLAdapter())
        _tls.session = s
    return _tls.session


def fetch(url: str, timeout: int = TIMEOUT) -> Optional[requests.Response]:
    for attempt in range(RETRIES + 1):
        try:
            s = get_session()
            r = s.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                return r
            if attempt == RETRIES:
                with _lock:
                    _stats.failed.append((url, f"HTTP {r.status_code}"))
            return None
        except requests.exceptions.SSLError as e:
            msg = f"SSL: {e}"
            if attempt == RETRIES:
                with _lock:
                    _stats.failed.append((url, msg))
            time.sleep(0.5 * (attempt + 1))
        except requests.exceptions.ConnectionError as e:
            msg = f"Connection: {e}"
            if attempt == RETRIES:
                with _lock:
                    _stats.failed.append((url, msg))
            time.sleep(0.5 * (attempt + 1))
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if attempt == RETRIES:
                with _lock:
                    _stats.failed.append((url, msg))
            time.sleep(0.5 * (attempt + 1))
    return None


# ──────────────────────────────────────────────
#  Пути
# ──────────────────────────────────────────────
def url_to_local_path(url: str, base: str, out_dir: Path) -> Path:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    if not path or path == "/":
        path = "/index.html"
    elif path.endswith("/"):
        path += "index.html"
    elif "." not in os.path.basename(path):
        path += "/index.html"
    return out_dir / path.lstrip("/")


def is_internal(url: str, base_host: str, allowed_hosts: set[str]) -> bool:
    h = urllib.parse.urlparse(url).netloc.lower()
    if not h:
        return True
    return h in allowed_hosts


# ──────────────────────────────────────────────
#  Анализ сайта (определяем, можно ли его скачать)
# ──────────────────────────────────────────────
@dataclass
class SiteAnalysis:
    framework: Optional[str]             # React / Vue / Angular / Svelte / WordPress / Static / Unknown
    spa: bool                            # SPA-приложение
    has_data_in_html: bool               # данные встроены в HTML (__NEXT_DATA__, __NUXT__)
    data_endpoints: list[str]            # найденные API/JSON endpoints
    cloudflare: bool                     # защита Cloudflare
    api_patterns: list[str]              # что нашли в HTML
    content_empty: bool                  # контентный div пуст
    html_size: int
    generator: Optional[str]             # <meta name="generator">
    explanation: str                     # человекочитаемое объяснение

    @property
    def can_scrape(self) -> bool:
        return not self.cloudflare

    @property
    def will_be_complete(self) -> bool:
        """Будет ли скачанный сайт выглядеть как оригинал."""
        if self.cloudflare:
            return False
        if self.spa and not self.has_data_in_html:
            return False
        return True

    @property
    def can_extract_data(self) -> bool:
        """Можно ли вытащить данные напрямую из HTML (SSR + JSON)."""
        return self.has_data_in_html


def _check_empty_content(html: str, soup: BeautifulSoup) -> bool:
    """Проверяет, пуст ли контентный div (root/app/__next)."""
    for div_id in ["root", "app", "__next", "__nuxt"]:
        div = soup.find("div", id=div_id)
        if div:
            text = div.get_text(strip=True)
            # если внутри только пробелы или скрипты — пусто
            if len(text) < 50:
                return True
    return False


def _extract_ssr_data(html: str, soup: BeautifulSoup) -> dict:
    """
    Извлекает данные, встроенные в HTML серверным рендерингом.
    Возвращает { "__NEXT_DATA__": {...}, ... }
    """
    result = {}
    # Next.js: <script id="__NEXT_DATA__" type="application/json">...</script>
    for script_id in ["__NEXT_DATA__", "__NUXT__"]:
        tag = soup.find("script", id=script_id)
        if tag and tag.string:
            try:
                import json
                result[script_id] = json.loads(tag.string)
            except Exception:
                result[script_id] = str(tag.string)[:200]

    # Vue: window.__INITIAL_STATE__
    m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html, re.DOTALL)
    if m:
        try:
            import json
            result["__INITIAL_STATE__"] = json.loads(m.group(1))
        except Exception:
            result["__INITIAL_STATE__"] = m.group(1)[:200]

    return result


def _detect_api_endpoints(html: str, base_url: str) -> list[str]:
    """Ищет в HTML и скриптах URL API-эндпоинтов."""
    endpoints = set()
    base_parsed = urllib.parse.urlparse(base_url)
    base_netloc = base_parsed.netloc

    # Ищем fetch('/api/...') и подобные
    patterns = [
        r"""["'](/api/[^"']+)["']""",
        r"""["'](/graphql)[^"']*["']""",
        r"""["'](/v[0-9]+/[^"']+)["']""",
        r"""["'](/rest/[^"']+)["']""",
        r"""["'](/wp-json/[^"']+)["']""",
    ]
    for pat in patterns:
        for m in re.finditer(pat, html):
            url = urllib.parse.urljoin(base_url, m.group(1))
            endpoints.add(url)

    # Ищем полные URL с тем же хостом и /api/
    for m in re.finditer(rf"""["'](https?://{re.escape(base_netloc)}/api/[^"']+)["']""", html):
        endpoints.add(m.group(1))

    return sorted(endpoints)


def _check_generator(html: str) -> Optional[str]:
    """Извлекает <meta name="generator" content="...">."""
    m = re.search(r'<meta\s+name=["\']generator["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def _detect_framework(soup: BeautifulSoup, html: str) -> tuple[Optional[str], bool, bool]:
    """
    Определяет фреймворк.
    Возвращает (название, это_spa, есть_данные_в_html).
    """
    html_lower = html.lower()

    for fw_name, fw in SPA_FRAMEWORKS.items():
        # Проверяем div-ы
        has_divs = any(d in html for d in fw["divs"])
        # Проверяем data-blocks
        has_data = any(d[0] in html for d in fw["data"])
        # Проверяем meta-теги
        has_meta = any(m in html_lower for m in fw["meta"])
        # Проверяем generator
        generator = _check_generator(html)
        has_generator = generator and any(g in generator.lower() for g in fw["generator"])

        if has_divs or has_meta or has_generator:
            return fw_name, True, has_data

    # Проверка на WordPress
    wp_signals = ["wp-content", "wp-includes", "/wp-"]
    if any(s in html_lower for s in wp_signals):
        return "WordPress", False, False

    return None, False, False


def analyze_site(html: str, url: str) -> SiteAnalysis:
    """
    Глубокий анализ сайта: фреймворк, SPA, данные, API, Cloudflare.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    cloudflare = any(s.lower() in html.lower() for s in CLOUDFLARE_SIGNALS)

    framework, is_spa, has_data = _detect_framework(soup, html)
    generator = _check_generator(html)

    api_patterns = [kw for kw in DYNAMIC_KEYWORDS if kw.lower() in html.lower()]
    data_endpoints = _detect_api_endpoints(html, url)

    content_empty = _check_empty_content(html, soup) if is_spa else False

    # Собираем человекочитаемое объяснение
    parts = []
    if cloudflare:
        parts.append("Cloudflare-защита")
    if framework:
        parts.append(f"Фреймворк: {framework}")
    if is_spa and not has_data:
        parts.append("SPA без данных в HTML — контент только через JS")
    if is_spa and has_data:
        parts.append(f"SPA с SSR-данными в HTML — контент можно извлечь")
    if content_empty:
        parts.append("контентный div пуст")
    if data_endpoints:
        parts.append(f"найдено API: {len(data_endpoints)} эндпоинтов")
    if not framework and not cloudflare:
        parts.append("статический HTML")

    explanation = ", ".join(parts)

    return SiteAnalysis(
        framework=framework,
        spa=is_spa,
        has_data_in_html=has_data,
        data_endpoints=data_endpoints,
        cloudflare=cloudflare,
        api_patterns=api_patterns,
        content_empty=content_empty,
        html_size=len(html),
        generator=generator,
        explanation=explanation,
    )


def print_analysis(report: SiteAnalysis) -> None:
    """Выводит анализ сайта в консоль."""
    print()
    print("═" * 60)
    print(" АНАЛИЗ САЙТА")
    print("═" * 60)

    if report.cloudflare:
        print(f"  ✗ Cloudflare-защита — скачать не получится")
    else:
        print(f"  ✓ Cloudflare: нет")

    print(f"  ℹ Размер HTML: {report.html_size} байт")

    if report.framework:
        print(f"  ℹ Фреймворк: {report.framework}")
    if report.generator:
        print(f"  ℹ Генератор: {report.generator}")

    if report.spa:
        if report.has_data_in_html:
            print(f"  ⚠ SPA-приложение ({report.framework}), НО данные встроены в HTML (SSR)")
            print(f"    → контент МОЖНО извлечь напрямую из JSON в HTML")
        else:
            print(f"  ✗ SPA-приложение ({report.framework}) — контент только через JavaScript")
            print(f"    → requests скачает пустую оболочку")
        if report.content_empty:
            print(f"  ✗ Контентный div пуст — все данные через JS")
    else:
        print(f"  ✓ Не SPA — статический HTML или SSR")

    if report.data_endpoints:
        print(f"  ⚠ Найдено API-эндпоинтов: {len(report.data_endpoints)}")
        for ep in report.data_endpoints[:5]:
            print(f"    • {ep}")
        if len(report.data_endpoints) > 5:
            print(f"    • ... и ещё {len(report.data_endpoints) - 5}")

    if report.api_patterns:
        print(f"  ⚠ Динамическая загрузка: {', '.join(report.api_patterns[:5])}")

    print("─" * 60)

    if not report.can_scrape:
        print("  ✗ Скачать НЕВОЗМОЖНО (Cloudflare)")
    elif not report.will_be_complete:
        print("  ✗ Скачать МОЖНО, но сайт будет НЕПОЛНЫМ (SPA без данных в HTML)")
        if report.data_endpoints:
            print("    • Попробуйте --api чтобы скачать данные через API")
        print("    • Попробуйте --browser для Playwright")
    elif report.can_extract_data:
        print("  ✓ Скачать МОЖНО, контент БУДЕТ ИЗВЛЕЧЁН из SSR-данных")
    else:
        print("  ✓ Скачать МОЖНО, сайт будет ПОЛНЫМ")

    print("═" * 60)
    print()


def print_analysis(report: dict) -> None:
    """Выводит анализ сайта в консоль."""
    print()
    print("═" * 60)
    print(" АНАЛИЗ САЙТА")
    print("═" * 60)

    for msg in report["info"]:
        print(f"  ✓ {msg}")

    for msg in report["warnings"]:
        print(f"  ⚠ {msg}")

    for msg in report["issues"]:
        print(f"  ✗ {msg}")

    print("─" * 60)
    if not report["can_download"]:
        print("  РЕЗУЛЬТАТ: Сайт защищён — скачать не получится.")
        print("  Попробуйте обходной путь:")
        print("    • httrack (может обходить простую защиту)")
        print("    • Playwright (--browser)")
        print("    • Сохранить страницы вручную через браузер")
    elif report["recommends_browser"]:
        print("  РЕЗУЛЬТАТ: Скачать можно, но КОНТЕНТ БУДЕТ НЕПОЛНЫМ.")
        print("  Лучше использовать --browser для Playwright.")
    else:
        print("  РЕЗУЛЬТАТ: Сайт подходит для скачивания.")
    print("═" * 60)
    print()

    if not report["can_download"]:
        return


# ──────────────────────────────────────────────
#  Извлечение ссылок и ассетов
# ──────────────────────────────────────────────
def extract_assets(text: str, base_url: str, allowed_hosts: set[str]) -> set[str]:
    urls: set[str] = set()
    for m in ASSET_RE.finditer(text):
        raw = m.group(1)
        if raw.startswith("data:"):
            continue
        abs_url = urllib.parse.urljoin(base_url, raw)
        if is_internal(abs_url, "", allowed_hosts):
            urls.add(abs_url.split("#")[0])
    for m in CSS_URL_RE.finditer(text):
        raw = m.group(1).strip("\"' ").split("#")[0]
        if raw.startswith("data:") or not raw:
            continue
        abs_url = urllib.parse.urljoin(base_url, raw)
        if is_internal(abs_url, "", allowed_hosts):
            urls.add(abs_url)
    return urls


def extract_links(html: str, base_url: str, allowed_hosts: set[str]) -> set[str]:
    links: set[str] = set()
    for m in LINK_RE.finditer(html):
        raw = m.group(1).split("?")[0].split("#")[0]
        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        abs_url = urllib.parse.urljoin(base_url, raw)
        parsed = urllib.parse.urlparse(abs_url)
        if parsed.scheme not in ("http", "https"):
            continue
        if not is_internal(abs_url, "", allowed_hosts):
            continue
        ext = os.path.splitext(parsed.path)[1].lower()
        if ext in ("", ".html", ".htm", ".php", ".asp", ".aspx"):
            links.add(abs_url.rstrip("/") or "/")
    return links


# ──────────────────────────────────────────────
#  Сохранение
# ──────────────────────────────────────────────
def save(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# ──────────────────────────────────────────────
#  Загрузка ассета
# ──────────────────────────────────────────────
def download_asset(url: str, base: str, out_dir: Path, allowed_hosts: set[str]) -> None:
    with _lock:
        if url in _downloaded_assets:
            return
        _downloaded_assets.add(url)

    local = url_to_local_path(url, base, out_dir)
    if local.exists() and local.stat().st_size > 0:
        with _lock:
            _stats.skipped += 1
        return

    resp = fetch(url)
    if not resp:
        return
    save(local, resp.content)
    with _lock:
        _stats.assets += 1
    time.sleep(ASSET_DELAY)

    if urllib.parse.urlparse(url).path.lower().endswith(".css"):
        for nested in extract_assets(resp.text, url, allowed_hosts):
            download_asset(nested, base, out_dir, allowed_hosts)


# ──────────────────────────────────────────────
#  Обработка страницы
# ──────────────────────────────────────────────
def process_page(url: str, base: str, out_dir: Path, allowed_hosts: set[str]) -> set[str]:
    local = url_to_local_path(url, base, out_dir)

    if local.exists() and local.stat().st_size > 0:
        html = local.read_text(encoding="utf-8", errors="replace")
        with _lock:
            _stats.skipped += 1
    else:
        resp = fetch(url)
        if not resp:
            return set()
        html = resp.text
        save(local, resp.content)
        with _lock:
            _stats.pages += 1
        time.sleep(PAGE_DELAY)

    for asset_url in extract_assets(html, url, allowed_hosts):
        download_asset(asset_url, base, out_dir, allowed_hosts)

    return extract_links(html, url, allowed_hosts)


# ──────────────────────────────────────────────
#  Исправление путей для офлайн
# ──────────────────────────────────────────────
def _rel_path(from_file: Path, to_abs: Path) -> str:
    return os.path.relpath(to_abs, from_file.parent).replace("\\", "/")


def fix_html(html_file: Path, out_dir: Path) -> None:
    text = html_file.read_text(encoding="utf-8", errors="replace")

    def repl_attr(m: re.Match) -> str:
        attr, path = m.group(1), m.group(2)
        local = out_dir / path.lstrip("/")
        if local.exists():
            return f'{attr}="{_rel_path(html_file, local)}"'
        return m.group(0)

    def repl_url(m: re.Match) -> str:
        path = m.group(1).split("#")[0]
        if path.startswith("data:") or not path.startswith("/"):
            return m.group(0)
        local = out_dir / path.lstrip("/")
        if local.exists():
            return f"url('{_rel_path(html_file, local)}')"
        return m.group(0)

    text = re.sub(
        r'((?:href|src|content|data-src|data-background|poster))="(/[^"]*)"',
        repl_attr, text,
    )
    text = re.sub(r"url\(\s*['\"]?(/[^'\")]+)['\"]?\s*\)", repl_url, text)
    html_file.write_text(text, encoding="utf-8")


def fix_css(css_file: Path, out_dir: Path) -> None:
    text = css_file.read_text(encoding="utf-8", errors="replace")

    def repl_url(m: re.Match) -> str:
        path = m.group(1).split("#")[0]
        if path.startswith("data:") or not path.startswith("/"):
            return m.group(0)
        local = out_dir / path.lstrip("/")
        if local.exists():
            return f"url('{_rel_path(css_file, local)}')"
        return m.group(0)

    text = re.sub(r"url\(\s*['\"]?(/[^'\")]+)['\"]?\s*\)", repl_url, text)
    css_file.write_text(text, encoding="utf-8")


# ──────────────────────────────────────────────
#  Playwright-режим (опционально)
# ──────────────────────────────────────────────
def try_playwright(
    url: str,
    out_dir: Path,
    allowed_hosts: set[str],
    max_pages: int = 200,
    exclude_re: Optional[re.Pattern] = None,
) -> None:
    """
    Загружает сайт через Playwright (headless Chromium).

    Playwright выполняет JavaScript, поэтому:
      - SPA-сайты (React/Vue/Angular) рендерят реальный контент
      - Cloudflare базовый обходится
      - Картинки и ассеты, подгружаемые через JS, сохраняются
      - Ссылки, сгенерированные JS, находятся и обходятся

    НО:
      - Интерактивный функционал (формы, поиск, проверка ответов) НЕ работает
      - Это архив HTML-страниц, а не рабочий сайт
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ✗ Playwright не установлен. Установите:")
        print("    pip install playwright && playwright install chromium")
        sys.exit(1)

    print(f"  Запуск Playwright (headless Chromium)")
    print(f"  User-Agent: {UA[:60]}...")
    print(f"  Макс. страниц: {max_pages}")
    print()

    visited = set()
    queue: deque[str] = deque([url])
    count = 0
    _stats.pages = 0
    _stats.assets = 0
    _stats.skipped = 0
    _stats.failed.clear()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            ignore_https_errors=True,
        )
        # Убираем признаки автоматизации
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
                print(f"  [{count}] {current}", end="", flush=True)

                # Загружаем страницу
                page.goto(current, wait_until="networkidle", timeout=30000)
                # Даём JS время на рендер
                page.wait_for_timeout(2000)

                # Скроллим вниз, чтобы подгрузить ленивые картинки
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(500)

                # Ждём, когда контентный div перестанет быть пустым
                try:
                    page.wait_for_function(
                        """
                        () => {
                            const root = document.querySelector('#root, #app, #__next, #__nuxt');
                            if (!root) return true; // нет такого div — не SPA
                            return root.textContent.length > 100;
                        }
                        """,
                        timeout=5000,
                    )
                except Exception:
                    pass  # если не дождались — сохраняем как есть

                # Сохраняем HTML
                html = page.content()
                local = url_to_local_path(current, url, out_dir)
                save(local, html.encode("utf-8"))
                _stats.pages += 1
                print(f" ✓ ({len(html)} байт)")

                # Скачиваем ассеты (CSS, JS, картинки — всё что нашли в HTML)
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "html.parser")

                # Картинки
                for img in soup.find_all("img", src=True):
                    _download_asset_pw(img["src"], url, out_dir, allowed_hosts, page, context)

                # CSS
                for link in soup.find_all("link", href=True):
                    href = link["href"]
                    if any(href.endswith(ext) for ext in [".css", ".js", ".ico"]):
                        _download_asset_pw(href, url, out_dir, allowed_hosts, page, context)

                # Фоновые картинки в стилях
                for m in re.finditer(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", html):
                    u = m.group(1).strip().split("?")[0]
                    if u.startswith("data:") or not u:
                        continue
                    _download_asset_pw(u, url, out_dir, allowed_hosts, page, context)

                # Извлекаем ссылки через Playwright (они уже с реальными URL)
                links = page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(el => el.href).filter(h => h.startsWith('http'))",
                )
                base_host = urllib.parse.urlparse(url).netloc.lower()
                for link in links:
                    parsed = urllib.parse.urlparse(link)
                    if parsed.netloc.lower() in allowed_hosts:
                        link_norm = link.rstrip("/")
                        if link_norm not in visited:
                            queue.append(link)

            except Exception as e:
                print(f" ✗ {e}")
                _stats.failed.append((current, str(e)))

        browser.close()

    # Фикс путей для офлайн-просмотра
    print(f"\n  Исправление путей...")
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

    print(f"\n{'='*50}")
    print(f" Playwright завершил работу")
    print(f"{'='*50}")
    print(f"  Страниц:    {_stats.pages}")
    print(f"  Ассетов:    {_stats.assets}")
    print(f"  Пропущено:  {_stats.skipped}")
    print(f"  Ошибок:     {len(_stats.failed)}")
    if _stats.failed:
        fail_path = out_dir / "_failed.txt"
        fail_path.write_text(
            "\n".join(f"{url}\t{err}" for url, err in _stats.failed),
            encoding="utf-8",
        )
        print(f"  Ошибки:     {fail_path}")
    print(f"\n  ВАЖНО: Это архив HTML-страниц, а не рабочий сайт.")
    print(f"  JS-функционал (формы, поиск, проверка ответов) НЕ работает.")
    print(f"\n  Открывай: {out_dir / 'index.html'}")


def _download_asset_pw(
    asset_url: str,
    base_url: str,
    out_dir: Path,
    allowed_hosts: set[str],
    page: any,
    context: any,
) -> None:
    """Скачивает ассет через Playwright (или requests как fallback)."""
    # Приводим к абсолютному URL
    abs_url = urllib.parse.urljoin(base_url, asset_url)
    parsed = urllib.parse.urlparse(abs_url)
    if parsed.netloc and parsed.netloc.lower() not in allowed_hosts:
        return
    if not parsed.netloc:
        return

    # Уже скачано?
    local = url_to_local_path(abs_url, base_url, out_dir)
    if local.exists() and local.stat().st_size > 0:
        _stats.skipped += 1
        return

    # Пробуем requests (быстрее)
    try:
        r = requests.get(abs_url, headers={"User-Agent": UA}, timeout=15)
        if r.status_code == 200 and len(r.content) > 100:
            save(local, r.content)
            _stats.assets += 1
            return
    except Exception:
        pass

    # Fallback: Playwright
    try:
        resp = context.request.get(abs_url)
        if resp.ok and len(resp.body()) > 100:
            save(local, resp.body())
            _stats.assets += 1
    except Exception:
        pass


# ──────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
#  API-режим: скачивание через JSON API
# ──────────────────────────────────────────────
def try_api_extract(html: str, base_url: str, out_dir: Path, report: SiteAnalysis) -> None:
    """
    Если сайт — SPA с SSR-данными в HTML (__NEXT_DATA__, __NUXT__),
    извлекает данные и строит из них статический HTML.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    print(f"\n{'='*50}")
    print(" API-РЕЖИМ: извлечение данных из HTML")
    print(f"{'='*50}")

    pages = 0
    data_dir = out_dir / "_api_data"

    # Next.js
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data and next_data.string:
        try:
            data = json_lib.loads(next_data.string)
            (data_dir / "__NEXT_DATA__.json").write_text(
                json_lib.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  ✓ Извлечён __NEXT_DATA__ ({len(json_lib.dumps(data))} байт)")

            # Пробуем собрать страницы из props
            if "props" in data and "pageProps" in data["props"]:
                pages += _build_pages_from_next_data(data, base_url, out_dir)

            pages += _extract_next_routes(data, base_url, out_dir)

        except Exception as e:
            print(f"  ✗ Ошибка парсинга __NEXT_DATA__: {e}")

    # Nuxt/Vue
    nuxt_data = soup.find("script", id="__NUXT__")
    if nuxt_data and nuxt_data.string:
        try:
            data = json_lib.loads(nuxt_data.string)
            (data_dir / "__NUXT__.json").write_text(
                json_lib.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  ✓ Извлечён __NUXT__ ({len(json_lib.dumps(data))} байт)")
            pages += _build_pages_from_nuxt_data(data, base_url, out_dir)
        except Exception as e:
            print(f"  ✗ Ошибка парсинга __NUXT__: {e}")

    # window.__INITIAL_STATE__
    m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html, re.DOTALL)
    if m:
        try:
            data = json_lib.loads(m.group(1))
            (data_dir / "__INITIAL_STATE__.json").write_text(
                json_lib.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  ✓ Извлечён __INITIAL_STATE__ ({len(json_lib.dumps(data))} байт)")
        except Exception as e:
            print(f"  ✗ Ошибка парсинга __INITIAL_STATE__: {e}")

    # Пробуем скачать известные API-эндпоинты
    if report.data_endpoints:
        print(f"\n  Скачивание данных через API ({len(report.data_endpoints)} эндпоинтов)...")
        for ep in report.data_endpoints:
            try:
                resp = fetch(ep)
                if resp and "application/json" in (resp.headers.get("Content-Type") or ""):
                    ep_name = ep.replace(base_url, "").strip("/").replace("/", "_") or "api"
                    ep_path = data_dir / f"api_{ep_name}.json"
                    ep_path.write_text(resp.text, encoding="utf-8")
                    print(f"  ✓ {ep}")
                elif resp:
                    ep_name = ep.replace(base_url, "").strip("/").replace("/", "_") or "api"
                    ep_path = data_dir / f"api_{ep_name}.html"
                    ep_path.write_bytes(resp.content)
                    print(f"  ✓ {ep} (не JSON, сохранён как HTML)")
            except Exception as e:
                print(f"  ✗ {ep}: {e}")

    print(f"\n  Данные сохранены в {data_dir}")
    print(f"  Собрано HTML-страниц: {pages}")


def _build_pages_from_next_data(data: dict, base_url: str, out_dir: Path) -> int:
    """Пробует собрать HTML-страницы из данных Next.js."""
    count = 0
    try:
        props = data.get("props", {}).get("pageProps", {})
        if props:
            html = _dict_to_html(props, "Данные страницы (Next.js)")
            (out_dir / "_ssr_page.html").write_text(html, encoding="utf-8")
            count += 1
    except Exception:
        pass

    # Извлекаем роуты из buildId / routes
    try:
        build_id = data.get("buildId", "")
        if build_id:
            (out_dir / "_next_build_id.txt").write_text(build_id, encoding="utf-8")
    except Exception:
        pass

    return count


def _extract_next_routes(data: dict, base_url: str, out_dir: Path) -> int:
    """Пробует извлечь список роутов из Next.js данных."""
    count = 0
    # Ищем массив путей в props
    try:
        for key in ["routes", "pages", "items", "problems", "tasks", "articles"]:
            items = _deep_get(data, key)
            if isinstance(items, list) and len(items) > 10:
                routes_file = out_dir / "_api_data" / "_routes.txt"
                with open(routes_file, "w", encoding="utf-8") as f:
                    for item in items:
                        if isinstance(item, dict):
                            slug = item.get("slug") or item.get("id") or item.get("path") or ""
                            if slug:
                                f.write(f"{slug}\n")
                                count += 1
                        elif isinstance(item, str):
                            f.write(f"{item}\n")
                            count += 1
                print(f"  ✓ Извлечено {count} роутов в _routes.txt")
                break
    except Exception:
        pass
    return count


def _build_pages_from_nuxt_data(data: dict, base_url: str, out_dir: Path) -> int:
    """Пробует собрать HTML-страницы из данных Nuxt."""
    count = 0
    try:
        if isinstance(data, dict):
            html = _dict_to_html(data, "Данные страницы (Nuxt)")
            (out_dir / "_ssr_nuxt_page.html").write_text(html, encoding="utf-8")
            count += 1
    except Exception:
        pass
    return count


def _deep_get(d: dict, key: str) -> any:
    """Рекурсивно ищет ключ во вложенных словарях."""
    if key in d:
        return d[key]
    for v in d.values():
        if isinstance(v, dict):
            result = _deep_get(v, key)
            if result is not None:
                return result
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    result = _deep_get(item, key)
                    if result is not None:
                        return result
    return None


def _dict_to_html(data: dict, title: str) -> str:
    """Конвертирует словарь в читаемый HTML."""
    lines = [f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{title}</title></head><body>"]
    lines.append(f"<h1>{title}</h1>")
    lines.append("<pre>" + json_lib.dumps(data, ensure_ascii=False, indent=2) + "</pre>")
    lines.append("</body></html>")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Универсальный загрузчик сайтов. Скачивает сайт целиком для офлайн-просмотра.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  %(prog)s https://example.com\n"
            "  %(prog)s https://example.com --depth 2 --workers 10 --delay 0.5\n"
            "  %(prog)s https://example.com --output ./mirror --no-fix\n"
            "  %(prog)s https://example.com --browser       # через Playwright\n"
            "  %(prog)s https://example.com --api           # извлечь SSR-данные\n"
        ),
    )
    ap.add_argument("url", help="URL сайта для скачивания (https://...)")
    ap.add_argument("-o", "--output", help="Папка для сохранения (по умолчанию ./mirror_<домен>)")
    ap.add_argument("--depth", type=int, default=0, help="Максимальная глубина обхода (0 = без ограничений)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"Количество потоков (по умолчанию {DEFAULT_WORKERS})")
    ap.add_argument("--delay", type=float, default=PAGE_DELAY, help=f"Задержка между страницами в сек (по умолчанию {PAGE_DELAY})")
    ap.add_argument("--asset-delay", type=float, default=ASSET_DELAY, help=f"Задержка между ассетами в сек (по умолчанию {ASSET_DELAY})")
    ap.add_argument("--timeout", type=int, default=TIMEOUT, help=f"Таймаут запроса в сек (по умолчанию {TIMEOUT})")
    ap.add_argument("--no-fix", action="store_true", help="Не исправлять пути для офлайн-просмотра")
    ap.add_argument("--include", help="Дополнительные хосты для скачивания (через запятую, например cdn.example.com,fonts.example.com)")
    ap.add_argument("--exclude", help="URL-паттерны для исключения (regex, например /wp-json/|/feed/)")
    ap.add_argument("--browser", action="store_true", help="Использовать Playwright (headless браузер) вместо requests")
    ap.add_argument("--api", action="store_true", help="Извлечь SSR-данные (__NEXT_DATA__, __NUXT__) и скачать API-эндпоинты")
    ap.add_argument("--max-pages", type=int, default=200, help="Максимум страниц для Playwright (по умолч. 200)")
    ap.add_argument("--force", action="store_true", help="Скачать даже если сайт не рекомендуется")
    args = ap.parse_args()

    # ── Определяем базовый URL и хост ──
    base = args.url.rstrip("/")
    parsed_base = urllib.parse.urlparse(base)
    if not parsed_base.scheme:
        base = "https://" + base
        parsed_base = urllib.parse.urlparse(base)

    base_host = parsed_base.netloc.lower()
    allowed_hosts = {base_host}
    if base_host.startswith("www."):
        allowed_hosts.add(base_host[4:])
    else:
        allowed_hosts.add(f"www.{base_host}")
    if args.include:
        for h in args.include.split(","):
            allowed_hosts.add(h.strip().lower())

    exclude_re = re.compile(args.exclude) if args.exclude else None

    out_dir = Path(args.output or f"mirror_{base_host}")

    # ── Анализ сайта ──
    print(f"Подключаюсь к {base}...")
    resp = fetch(base + "/")
    if resp is None:
        print("  ✗ Не удалось подключиться к сайту. Проверьте URL.")
        sys.exit(1)

    html = resp.text
    report = analyze_site(html, base)
    print_analysis(report)

    if not report.can_scrape and not args.force:
        print("Скачивание отменено. Используйте --force чтобы принудительно скачать.")
        sys.exit(1)

    if not report.will_be_complete and not args.browser and not args.api and not args.force:
        print("Рекомендую:")
        if report.can_extract_data:
            print("  • --api  — извлечь данные из HTML (SSR) и API")
        print("  • --browser — загрузить через Playwright")
        print("  • --force — скачать как есть (только HTML-оболочка)\n")

    # ── API-режим ──
    if args.api:
        try_api_extract(html, base, out_dir, report)
        print(f"\nГотово. Данные в {out_dir / '_api_data'}")
        return

    # ── Playwright-режим ──
    if args.browser:
        try_playwright(
            base,
            out_dir,
            allowed_hosts=allowed_hosts,
            max_pages=args.max_pages,
            exclude_re=exclude_re,
        )
        return

    # ── Requests-режим (обычный) ──
    # Меняем модульные переменные задержек
    module_globals = globals()
    module_globals["PAGE_DELAY"] = args.delay
    module_globals["ASSET_DELAY"] = args.asset_delay
    module_globals["TIMEOUT"] = args.timeout

    start_url = base + "/"
    _visited_pages.add(start_url)
    _visited_pages.add(base)
    queue: deque[tuple[str, int]] = deque([(start_url, 0)])

    print(f"Сайт:       {base}")
    print(f"Хосты:      {', '.join(sorted(allowed_hosts))}")
    print(f"Потоков:    {args.workers}")
    print(f"Глубина:    {'без ограничений' if args.depth == 0 else args.depth}")
    print(f"Папка:      {out_dir.resolve()}")
    print(f"Фикс путей: {'нет' if args.no_fix else 'да'}")
    if exclude_re:
        print(f"Исключения: {args.exclude}")
    print()

    page_count = 0
    while queue:
        url, depth = queue.popleft()
        if args.depth and depth >= args.depth:
            continue
        if exclude_re and exclude_re.search(url):
            continue

        page_count += 1
        print(f"[{page_count}] глубина {depth}: {url}")

        new_links = process_page(url, base, out_dir, allowed_hosts) or set()

        for link in new_links:
            if exclude_re and exclude_re.search(link):
                continue
            norm = link.rstrip("/")
            with _lock:
                if norm not in _visited_pages and norm + "/" not in _visited_pages:
                    _visited_pages.add(norm)
                    queue.append((link, depth + 1))

    # ── Статистика ──
    print(f"\n{'='*50}")
    print(f"Страниц:          {_stats.pages}")
    print(f"Ассетов:          {_stats.assets}")
    print(f"Пропущено:        {_stats.skipped}")
    print(f"Ошибок:           {len(_stats.failed)}")

    if _stats.failed:
        fail_path = out_dir / "_failed.txt"
        fail_path.write_text(
            "\n".join(f"{url}\t{err}" for url, err in _stats.failed),
            encoding="utf-8",
        )
        print(f"Ошибки сохранены: {fail_path}")

    # ── Фикс путей ──
    if not args.no_fix:
        print(f"\n{'='*50}")
        print("Исправление путей для офлайн-просмотра...")
        fixed_html = 0
        for f in out_dir.rglob("*"):
            if f.suffix.lower() in (".html", ".htm"):
                fix_html(f, out_dir)
                fixed_html += 1
        print(f"  HTML: {fixed_html} файлов")
        fixed_css = 0
        for f in out_dir.rglob("*.css"):
            fix_css(f, out_dir)
            fixed_css += 1
        print(f"  CSS:  {fixed_css} файлов")

    print(f"\nГотово. Открывай {out_dir / 'index.html'}")


if __name__ == "__main__":
    main()
