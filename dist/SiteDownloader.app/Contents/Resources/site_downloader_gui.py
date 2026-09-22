#!/usr/bin/env python3
"""
SiteDownloader GUI — macOS native app for downloading websites.
Built with tkinter, wraps site_downloader.py.
"""

import io
import os
import re
import sys
import json
import queue
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path

# ── Определяем путь к корню приложения ──
if getattr(sys, 'frozen', False):
    # В собранном .app ресурсы лежат в Contents/Resources/
    APP_ROOT = Path(sys.executable).parent.parent / "Resources"
else:
    APP_ROOT = Path(__file__).parent

sys.path.insert(0, str(APP_ROOT))

from site_downloader import (
    fetch, analyze_site, print_analysis, SiteAnalysis,
    try_playwright, try_api_extract,
    process_page, fix_html, fix_css,
    url_to_local_path, is_internal, save,
    _stats, _visited_pages, _downloaded_assets,
    DEFAULT_WORKERS, PAGE_DELAY, ASSET_DELAY, TIMEOUT,
    UA,
)
from collections import deque


class TextRedirector(io.StringIO):
    """Перенаправляет print в текстовое поле tkinter."""
    def __init__(self, widget):
        super().__init__()
        self.widget = widget

    def write(self, s):
        self.widget.insert(tk.END, s)
        self.widget.see(tk.END)

    def flush(self):
        pass


class SiteDownloaderGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("SiteDownloader")
        self.root.geometry("800x700")
        self.root.minsize(600, 500)

        # Флаг для переключения на системный Python
        self._use_system_python_for_fetch = False
        self._system_python_path = None

        # Иконка (если есть)
        icon_path = APP_ROOT / "icon.icns"
        if icon_path.exists():
            try:
                self.root.iconbitmap(str(icon_path))
            except Exception:
                pass

        self.setup_ui()

        # Очередь для связи с потоком
        self.task_queue = queue.Queue()
        self.worker_thread = None
        self.running = False

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def setup_ui(self):
        # ── Заголовок ──
        header = ttk.Frame(self.root, padding=10)
        header.pack(fill=tk.X)
        ttk.Label(header, text="SiteDownloader", font=("Helvetica", 18, "bold")).pack(side=tk.LEFT)
        ttk.Label(header, text="Скачивание сайтов целиком", font=("Helvetica", 10)).pack(side=tk.LEFT, padx=10)

        # ── Основные параметры ──
        main_frame = ttk.LabelFrame(self.root, text="Сайт", padding=10)
        main_frame.pack(fill=tk.X, padx=10, pady=5)

        # URL
        url_frame = ttk.Frame(main_frame)
        url_frame.pack(fill=tk.X, pady=3)
        ttk.Label(url_frame, text="URL:").pack(side=tk.LEFT)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(url_frame, textvariable=self.url_var, font=("Helvetica", 12))
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.url_entry.insert(0, "https://")
        self.url_entry.bind("<Return>", lambda e: self.analyze())

        # Папка вывода
        out_frame = ttk.Frame(main_frame)
        out_frame.pack(fill=tk.X, pady=3)
        ttk.Label(out_frame, text="Сохранить в:").pack(side=tk.LEFT)
        self.out_var = tk.StringVar(value=str(APP_ROOT / "mirror"))
        self.out_entry = ttk.Entry(out_frame, textvariable=self.out_var)
        self.out_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(out_frame, text="Обзор...", command=self.browse_output).pack(side=tk.RIGHT)

        # ── Настройки ──
        opts_frame = ttk.LabelFrame(self.root, text="Настройки", padding=10)
        opts_frame.pack(fill=tk.X, padx=10, pady=5)

        opts_grid = ttk.Frame(opts_frame)
        opts_grid.pack(fill=tk.X)

        # Ряд 1: глубина, потоки
        row1 = ttk.Frame(opts_grid)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="Глубина:").pack(side=tk.LEFT)
        self.depth_var = tk.StringVar(value="0")
        ttk.Entry(row1, textvariable=self.depth_var, width=6).pack(side=tk.LEFT, padx=3)
        ttk.Label(row1, text="(0 = без лимита)").pack(side=tk.LEFT, padx=5)

        ttk.Label(row1, text="Потоков:").pack(side=tk.LEFT, padx=(15, 0))
        self.workers_var = tk.StringVar(value=str(DEFAULT_WORKERS))
        ttk.Spinbox(row1, from_=1, to=20, textvariable=self.workers_var, width=4).pack(side=tk.LEFT, padx=3)

        ttk.Label(row1, text="Макс. страниц:").pack(side=tk.LEFT, padx=(15, 0))
        self.max_pages_var = tk.StringVar(value="200")
        ttk.Spinbox(row1, from_=10, to=5000, textvariable=self.max_pages_var, width=6).pack(side=tk.LEFT, padx=3)

        # Ряд 2: задержки
        row2 = ttk.Frame(opts_grid)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="Задержка страниц (сек):").pack(side=tk.LEFT)
        self.delay_var = tk.StringVar(value=str(PAGE_DELAY))
        ttk.Entry(row2, textvariable=self.delay_var, width=6).pack(side=tk.LEFT, padx=3)

        ttk.Label(row2, text="Задержка ассетов:").pack(side=tk.LEFT, padx=(15, 0))
        self.asset_delay_var = tk.StringVar(value=str(ASSET_DELAY))
        ttk.Entry(row2, textvariable=self.asset_delay_var, width=6).pack(side=tk.LEFT, padx=3)

        ttk.Label(row2, text="Таймаут:").pack(side=tk.LEFT, padx=(15, 0))
        self.timeout_var = tk.StringVar(value=str(TIMEOUT))
        ttk.Entry(row2, textvariable=self.timeout_var, width=6).pack(side=tk.LEFT, padx=3)

        # Ряд 3: доп. хосты и исключения
        row3 = ttk.Frame(opts_grid)
        row3.pack(fill=tk.X, pady=2)
        ttk.Label(row3, text="Доп. хосты:").pack(side=tk.LEFT)
        self.include_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.include_var, width=25).pack(side=tk.LEFT, padx=3)
        ttk.Label(row3, text="Исключить (regex):").pack(side=tk.LEFT, padx=(10, 0))
        self.exclude_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.exclude_var, width=20).pack(side=tk.LEFT, padx=3)

        # ── Режимы ──
        mode_frame = ttk.LabelFrame(self.root, text="Режим загрузки", padding=10)
        mode_frame.pack(fill=tk.X, padx=10, pady=5)

        mode_grid = ttk.Frame(mode_frame)
        mode_grid.pack(fill=tk.X)

        self.mode_var = tk.StringVar(value="auto")
        ttk.Radiobutton(mode_grid, text="Авто (requests)", variable=self.mode_var, value="auto").pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(mode_grid, text="Браузер (Playwright)", variable=self.mode_var, value="browser").pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(mode_grid, text="Данные (API/SSR)", variable=self.mode_var, value="api").pack(side=tk.LEFT, padx=5)

        self.force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(mode_grid, text="Force (скачать даже если не рекомендуется)", variable=self.force_var).pack(side=tk.LEFT, padx=15)

        self.no_fix_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(mode_grid, text="Не исправлять пути", variable=self.no_fix_var).pack(side=tk.LEFT, padx=5)

        # ── Playwright статус ──
        pw_frame = ttk.Frame(mode_frame)
        pw_frame.pack(fill=tk.X, pady=(5, 0))
        self.pw_status_label = ttk.Label(pw_frame, text="", font=("Helvetica", 9))
        self.pw_status_label.pack(side=tk.LEFT, padx=5)
        self.pw_install_btn = ttk.Button(pw_frame, text="Установить Playwright", command=self.install_playwright, width=25)
        self.update_pw_status()

        # ── Кнопки ──
        btn_frame = ttk.Frame(self.root, padding=5)
        btn_frame.pack(fill=tk.X, padx=10)

        self.analyze_btn = ttk.Button(btn_frame, text="Анализировать сайт", command=self.analyze, width=20)
        self.analyze_btn.pack(side=tk.LEFT, padx=3)

        self.start_btn = ttk.Button(btn_frame, text="▶ Скачать", command=self.start_download, width=15)
        self.start_btn.pack(side=tk.LEFT, padx=3)

        self.stop_btn = ttk.Button(btn_frame, text="■ Стоп", command=self.stop_download, width=10, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=3)

        self.clear_btn = ttk.Button(btn_frame, text="Очистить лог", command=self.clear_log, width=15)
        self.clear_btn.pack(side=tk.RIGHT, padx=3)

        # ── Статусная строка ──
        self.status_var = tk.StringVar(value="Готов к работе")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=3)
        status_bar.pack(fill=tk.X, padx=10, pady=(0, 5))

        # ── Лог ──
        log_frame = ttk.LabelFrame(self.root, text="Лог", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, font=("Menlo", 10),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Перенаправляем stdout/stderr
        self.redirector = TextRedirector(self.log_text)
        self.old_stdout = sys.stdout
        self.old_stderr = sys.stderr
        sys.stdout = self.redirector
        sys.stderr = self.redirector

    def browse_output(self):
        path = filedialog.askdirectory(initialdir=str(APP_ROOT))
        if path:
            self.out_var.set(path)

    def log(self, msg):
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    def clear_log(self):
        self.log_text.delete(1.0, tk.END)

    def set_status(self, text):
        self.status_var.set(text)
        self.root.update_idletasks()

    def update_pw_status(self):
        """Проверяет, установлен ли Playwright, обновляет UI."""

        def check():
            import subprocess
            python = self._system_python()
            clean_env = self._clean_env()

            has_playwright = False
            has_chromium = False

            # Проверяем playwright
            try:
                proc = subprocess.run(
                    [python, "-c", "import playwright; print('ok')"],
                    capture_output=True, text=True, timeout=10,
                    env=clean_env,
                )
                has_playwright = proc.returncode == 0
            except Exception:
                pass

            # Проверяем chromium
            if has_playwright:
                try:
                    proc2 = subprocess.run(
                        [python, "-c", """
from playwright.sync_api import sync_playwright
try:
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, timeout=5000)
        b.close()
        print('ok')
except Exception:
    print('no')
"""],
                        capture_output=True, text=True, timeout=15,
                        env=clean_env,
                    )
                    has_chromium = 'ok' in proc2.stdout
                except Exception:
                    pass

            def update_ui():
                if has_chromium:
                    self.pw_status_label.config(text="✓ Playwright + Chromium готовы", foreground="green")
                    self.pw_install_btn.pack_forget()
                elif has_playwright:
                    self.pw_status_label.config(text="⚠ Playwright есть, нет Chromium", foreground="orange")
                    self.pw_install_btn.config(text="Установить Chromium")
                    self.pw_install_btn.pack()
                else:
                    self.pw_status_label.config(text="✗ Playwright не установлен", foreground="red")
                    self.pw_install_btn.config(text="Установить Playwright")
                    self.pw_install_btn.pack()

            self.root.after(0, update_ui)

        threading.Thread(target=check, daemon=True).start()

    def _system_python(self) -> str:
        """Возвращает путь к системному python3 (с pip).
        Очищает PYTHONPATH, чтобы не подхватить библиотеки bundled Python.
        """
        candidates = [
            "/usr/bin/python3",
            "/opt/homebrew/bin/python3",
            "/opt/homebrew/bin/python3.14",
            "/usr/local/bin/python3",
            "python3",
        ]

        clean_env = self._clean_env()

        for candidate in candidates:
            try:
                import subprocess
                proc = subprocess.run(
                    [candidate, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                    capture_output=True, text=True, timeout=5,
                    env=clean_env,
                )
                if proc.returncode == 0:
                    # Проверяем pip
                    proc2 = subprocess.run(
                        [candidate, "-m", "pip", "--version"],
                        capture_output=True, text=True, timeout=10,
                        env=clean_env,
                    )
                    if proc2.returncode == 0:
                        return candidate
            except Exception:
                continue

        # Fallback
        return "/usr/bin/python3"

    def _clean_env(self) -> dict:
        """Очищенное окружение без Python-специфичных переменных py2app."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("PYTHON") and not k.startswith("_PYTHON")}
        for var in ["RESOURCEPACKAGEROOT", "EXECUTABLEPATH"]:
            env.pop(var, None)
        # Принудительно ставим UTF-8 (иначе /usr/bin/python3 падает с ASCII)
        env["LC_ALL"] = "en_US.UTF-8"
        env["LANG"] = "en_US.UTF-8"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def install_playwright(self):
        """Устанавливает Playwright + Chromium."""
        result = messagebox.askyesno(
            "Установка Playwright",
            "Будет установлено:\n"
            "  • playwright (Python пакет)\n"
            "  • Chromium (~200 МБ)\n\n"
            "Это может занять несколько минут.\n"
            "Продолжить?",
        )
        if not result:
            return

        self.log("Установка Playwright...")
        self.set_status("Установка Playwright...")
        self.pw_install_btn.config(state=tk.DISABLED)

        def _run(cmd, timeout, label):
            """Запускает процесс, читает вывод в байтах, логит."""
            import subprocess
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self._clean_env(),
            )
            stdout, stderr = proc.communicate(timeout=timeout)
            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            # Логируем вывод (не весь, а последние строки)
            lines = (out + err).strip().split("\n")
            for line in lines[-5:]:
                if line.strip():
                    self.log(f"    {line.strip()}")
            return proc.returncode, err

        def install():
            python = self._system_python()

            try:
                # Шаг 1: установка playwright
                self.log(f"  {python} -m pip install playwright...")
                rc, err = _run(
                    [python, "-m", "pip", "install", "playwright"],
                    timeout=120, label="pip",
                )
                if rc != 0:
                    self.log(f"  ✗ Ошибка: {err[-300:]}")
                    self.set_status("Ошибка установки Playwright")
                    self.pw_install_btn.config(state=tk.NORMAL)
                    return
                self.log("  ✓ playwright установлен")

                # Шаг 2: установка Chromium
                self.log("  playwright install chromium...")
                rc, err = _run(
                    [python, "-m", "playwright", "install", "chromium"],
                    timeout=300, label="chromium",
                )
                if rc != 0:
                    self.log(f"  ✗ Ошибка: {err[-300:]}")
                    self.set_status("Ошибка установки Chromium")
                    self.pw_install_btn.config(state=tk.NORMAL)
                    return
                self.log("  ✓ Chromium установлен")

                self.set_status("Playwright готов")
                self.log("  ✓ Playwright + Chromium готовы к использованию")
                self.update_pw_status()

            except subprocess.TimeoutExpired:
                self.log("  ✗ Таймаут установки")
                self.set_status("Таймаут установки")
                self.pw_install_btn.config(state=tk.NORMAL)
            except Exception as e:
                self.log(f"  ✗ {e}")
                self.set_status("Ошибка установки")
                self.pw_install_btn.config(state=tk.NORMAL)

        threading.Thread(target=install, daemon=True).start()

    def analyze(self):
        """Анализирует сайт. Сначала пробует bundled, при ошибке — системный Python."""
        url = self.url_var.get().strip()
        if not url or url == "https://":
            messagebox.showwarning("Ошибка", "Введите URL сайта")
            return

        self.log(f"Анализирую {url}...")

        def task():
            # Очищаем ошибки от предыдущих вызовов
            _stats.failed.clear()

            try:
                resp = fetch(url.rstrip("/") + "/")
                if resp is not None:
                    self._show_analysis(resp.text, url)
                    return

                # Bundled не работает — через системный
                errors = _stats.failed[-2:] if _stats.failed else []
                for u, e in errors:
                    self.log(f"  (bundled) {u}: {e}")

                self.log("  Переключаюсь на системный python3...")
                self._analyze_via_system(url)

            except Exception as e:
                self.log(f"✗ Ошибка: {e}")

        threading.Thread(target=task, daemon=True).start()

    def _analyze_via_system(self, url):
        """Анализ через системный python3."""
        system_python = self._system_python()
        import subprocess

        # Скрипт анализа на системном Python
        script = rf'''
import sys, urllib.request, urllib.error, re, http.cookiejar

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Создаём opener с поддержкой кук и редиректов
cookie_jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(cookie_jar),
    urllib.request.HTTPRedirectHandler(),
)
opener.addheaders = [
    ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    ("Accept-Language", "ru,en;q=0.9"),
]

try:
    resp = opener.open("{url}/", timeout=15)
    html = resp.read().decode("utf-8", errors="replace")
    print("HTML_SIZE", len(html))
    print("STATUS", resp.status)
    print("FINAL_URL", resp.geturl()[:100])

    # Диагностика: что вернул сервер (первые 200 символов)
    head = html[:200].replace("\\n", " ").replace("\\r", "").strip()
    print("HEAD", head[:150])

    has_cf = "cf-browser-verification" in html.lower() or "checking your browser before accessing" in html.lower() or "just a moment..." in html.lower()
    has_next = "__NEXT_DATA__" in html
    has_nuxt = "__NUXT__" in html
    has_root = 'id="__next"' in html or 'id="root"' in html or 'id="app"' in html
    has_wp = "wp-content" in html or "/wp-" in html
    has_api = "/api/" in html or "/graphql" in html

    print("CLOUDFLARE", has_cf)
    print("SPA", has_root or has_next or has_nuxt)
    print("NEXTJS", has_next)
    print("NUXT", has_nuxt)
    print("WORDPRESS", has_wp)
    print("HAS_API", has_api)

    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    print("TITLE", m.group(1).strip()[:100] if m else "?")

    apis = re.findall(r'["\'](/api/[^"\']+)["\']', html)
    print("API_COUNT", len(apis))
    for a in apis[:5]:
        print("API", a)

except Exception as e:
    print("ERROR", str(e).replace("\\n", " | ")[:200])
'''
        # Читаем в байтах, декодируем в UTF-8
        proc = subprocess.run(
            [system_python, "-c", script],
            capture_output=True, timeout=30,
            env=self._clean_env(),
        )
        output = proc.stdout.decode("utf-8", errors="replace").strip().split("\n")
        stderr_text = proc.stderr.decode("utf-8", errors="replace").strip()

        info = {}
        for line in output:
            if " " in line:
                key, val = line.split(" ", 1)
                info[key] = val

        if "ERROR" in info:
            self.log(f"  ✗ {info['ERROR']}")
            self.log("  Попробуйте режим 'Браузер (Playwright)'")
            return

        self.log("═" * 60)
        self.log(" АНАЛИЗ САЙТА")
        self.log("═" * 60)
        self.log(f"  ℹ Размер: {info.get('HTML_SIZE', '?')} байт")
        self.log(f"  ℹ Заголовок: {info.get('TITLE', '?')}")

        if info.get("CLOUDFLARE") == "True":
            self.log("  ✗ Cloudflare-защита")
        else:
            self.log("  ✓ Cloudflare: нет")

        if info.get("WORDPRESS") == "True":
            self.log("  ✓ WordPress")

        if info.get("SPA") == "True":
            fw = "Next.js" if info.get("NEXTJS") == "True" else "Nuxt" if info.get("NUXT") == "True" else "React/Vue"
            self.log(f"  ⚠ SPA ({fw}) — контент через JS")
            if info.get("NEXTJS") == "True":
                self.log("    → Можно извлечь данные через --api")
            self.log("    → Используйте режим 'Браузер (Playwright)'")
        else:
            self.log("  ✓ Статический HTML — подходит для скачивания")

        if info.get("HAS_API") == "True":
            count = info.get("API_COUNT", "0")
            self.log(f"  ⚠ API: {count} эндпоинтов")
            for line in output:
                if line.startswith("API "):
                    self.log(f"    • {line[4:]}")

        self.log("─" * 60)
        if info.get("CLOUDFLARE") == "True":
            self.log("  ✗ НЕВОЗМОЖНО скачать (Cloudflare)")
        elif info.get("SPA") == "True":
            self.log("  ✓ Скачать можно (нужен --browser или --api)")
        else:
            self.log("  ✓ Отлично подходит для скачивания")
        self.log("═" * 60)
        self.set_status("Анализ завершён")

    def _show_analysis(self, html, url):
        """Выводит анализ из bundled Python."""
        report = analyze_site(html, url)
        self.log("═" * 60)
        self.log(" АНАЛИЗ САЙТА")
        self.log("═" * 60)
        if report.cloudflare:
            self.log("  ✗ Cloudflare-защита")
        else:
            self.log("  ✓ Cloudflare: нет")
        self.log(f"  ℹ Размер: {report.html_size} байт")
        if report.framework:
            self.log(f"  ℹ Фреймворк: {report.framework}")
        if report.generator:
            self.log(f"  ℹ Генератор: {report.generator}")
        if report.spa:
            if report.has_data_in_html:
                self.log(f"  ⚠ SPA ({report.framework}), но данные в HTML")
            else:
                self.log(f"  ⚠ SPA ({report.framework}) — контент через JS")
            if report.content_empty:
                self.log("  ✗ Контентный div пуст")
        else:
            self.log("  ✓ Не SPA")
        if report.data_endpoints:
            self.log(f"  ⚠ API: {len(report.data_endpoints)} эндпоинтов")
            for ep in report.data_endpoints[:3]:
                self.log(f"    • {ep}")
        self.log("─" * 60)
        if not report.can_scrape:
            self.log("  ✗ НЕВОЗМОЖНО скачать")
        elif not report.will_be_complete:
            self.log("  ✓ Скачать можно (нужен --browser или --api)")
        elif report.can_extract_data:
            self.log("  ✓ Контент в SSR-данных (--api)")
        else:
            self.log("  ✓ Отлично подходит для скачивания")
        self.log("═" * 60)
        self.set_status("Анализ завершён")

    def start_download(self):
        url = self.url_var.get().strip()
        if not url or url == "https://":
            messagebox.showwarning("Ошибка", "Введите URL сайта")
            return

        if self.running:
            return

        self.running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.analyze_btn.config(state=tk.DISABLED)
        self.set_status("Загрузка...")

        self.log(f"\n{'='*60}")
        self.log(f" НАЧАЛО ЗАГРУЗКИ: {url}")
        self.log(f"{'='*60}")

        self.worker_thread = threading.Thread(target=self._download_worker, args=(url,), daemon=True)
        self.worker_thread.start()

    def _download_worker(self, url):
        try:
            base = url.rstrip("/")
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
            if self.include_var.get().strip():
                for h in self.include_var.get().split(","):
                    allowed_hosts.add(h.strip().lower())

            exclude_re = re.compile(self.exclude_var.get()) if self.exclude_var.get().strip() else None

            out_dir = Path(self.out_var.get().strip() or str(APP_ROOT / "mirror"))

            depth_limit = int(self.depth_var.get() or "0")
            workers = int(self.workers_var.get() or str(DEFAULT_WORKERS))
            delay = float(self.delay_var.get() or str(PAGE_DELAY))
            asset_delay = float(self.asset_delay_var.get() or str(ASSET_DELAY))
            timeout = int(self.timeout_var.get() or str(TIMEOUT))
            max_pages = int(self.max_pages_var.get() or "200")

            mode = self.mode_var.get()
            force = self.force_var.get()
            no_fix = self.no_fix_var.get()

            # ── Анализ ──
            resp = fetch(base + "/")
            if resp is None:
                self.log("✗ Не удалось подключиться к сайту")
                return

            html = resp.text
            report = analyze_site(html, base)

            if not report.can_scrape and not force:
                self.log("✗ Сайт защищён (Cloudflare). Используйте Force или режим Браузер.")
                return

            # ── Выбор режима ──
            if mode == "browser" or (mode == "auto" and (report.spa or report.data_endpoints)):
                # Проверяем Playwright через системный Python
                system_python = self._system_python()
                clean_env = self._clean_env()
                pw_ok = False
                try:
                    import subprocess
                    proc = subprocess.run(
                        [system_python, "-c", "from playwright.sync_api import sync_playwright; print('ok')"],
                        capture_output=True, text=True, timeout=10,
                        env=clean_env,
                    )
                    pw_ok = proc.returncode == 0
                except Exception:
                    pass

                if not pw_ok:
                    self.log(f"\n  ✗ Playwright не установлен в {system_python}")
                    self.log("  Для работы режима Браузер:")
                    self.log("    1. Нажмите кнопку «Установить Playwright» выше")
                    self.log("    2. Или выполните в терминале:")
                    self.log(f"       {system_python} -m pip install playwright && {system_python} -m playwright install chromium")
                    self.log("  Скачивание отменено.")
                    return

                self.log(f"\n→ Режим: Браузер (Playwright через {system_python})")

                # Запускаем через _playwright_runner.py
                runner = APP_ROOT / "_playwright_runner.py"
                if not runner.exists():
                    runner = APP_ROOT.parent / "_playwright_runner.py"

                cmd = [
                    system_python, str(runner),
                    base, str(out_dir),
                    ",".join(allowed_hosts),
                    str(max_pages), str(depth_limit),
                    str(self.exclude_var.get().strip() or None),
                ]
                self.log(f"  Запуск: {system_python} {runner.name}")
                self.log(f"  Макс. страниц: {max_pages}")

                import subprocess
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, env=clean_env,
                )

                for line in iter(proc.stdout.readline, ""):
                    line = line.strip()
                    if not line:
                        continue
                    if line == "PW_START":
                        self.log("  Playwright запущен...")
                    elif line.startswith("PW_PAGE|"):
                        parts = line.split("|", 2)
                        self.log(f"  [{parts[1]}] {parts[2]}")
                    elif line.startswith("PW_FAIL|"):
                        parts = line.split("|", 2)
                        self.log(f"  ✗ {parts[1]}: {parts[2]}")
                    elif line.startswith("PW_DONE|"):
                        parts = line.split("|")
                        self.log(f"\n  Страниц: {parts[1]}, ассетов: {parts[2]}, пропущено: {parts[3]}, ошибок: {parts[4]}")
                    elif line.startswith("PW_ERR|"):
                        parts = line.split("|", 2)
                        self.log(f"  ✗ {parts[1]}: {parts[2]}")
                    elif line == "PW_CANCELLED":
                        self.log("  ■ Остановлено пользователем")
                    else:
                        self.log(f"  {line}")

                proc.wait()
                self.log(f"\nГотово. Открывай: {out_dir / 'index.html'}")
                return

            if mode == "api":
                self.log("\n→ Режим: Данные (API/SSR)")
                try_api_extract(html, base, out_dir, report)
                self.log(f"\nГотово. Данные в {out_dir / '_api_data'}")
                return

            # ── Обычный режим (requests) ──
            self.log(f"\n→ Режим: Авто (requests)")
            self.log(f"Потоков: {workers}, глубина: {depth_limit or '∞'}, папка: {out_dir}\n")

            # Сбрасываем глобальное состояние
            _stats.pages = 0
            _stats.assets = 0
            _stats.skipped = 0
            _stats.failed.clear()
            _visited_pages.clear()
            _downloaded_assets.clear()

            # Меняем задержки
            import site_downloader as sd
            sd.PAGE_DELAY = delay
            sd.ASSET_DELAY = asset_delay
            sd.TIMEOUT = timeout

            start_url = base + "/"
            _visited_pages.add(start_url)
            _visited_pages.add(base)
            queue = deque([(start_url, 0)])
            page_count = 0

            while queue and self.running:
                current_url, depth = queue.popleft()
                if depth_limit and depth >= depth_limit:
                    continue
                if exclude_re and exclude_re.search(current_url):
                    continue

                page_count += 1
                self.log(f"[{page_count}] глубина {depth}: {current_url}")

                new_links = process_page(current_url, base, out_dir, allowed_hosts) or set()

                if not self.running:
                    break

                for link in new_links:
                    if exclude_re and exclude_re.search(link):
                        continue
                    norm = link.rstrip("/")
                    with __import__("threading").Lock():
                        if norm not in _visited_pages and norm + "/" not in _visited_pages:
                            _visited_pages.add(norm)
                            queue.append((link, depth + 1))

            # ── Итог ──
            self.log(f"\n{'='*50}")
            self.log(f"Страниц:   {_stats.pages}")
            self.log(f"Ассетов:   {_stats.assets}")
            self.log(f"Пропущено: {_stats.skipped}")
            self.log(f"Ошибок:    {len(_stats.failed)}")

            if _stats.failed:
                fail_path = out_dir / "_failed.txt"
                fail_path.write_text(
                    "\n".join(f"{u}\t{e}" for u, e in _stats.failed),
                    encoding="utf-8",
                )
                self.log(f"Ошибки: {fail_path}")

            if not no_fix:
                self.log("\nИсправление путей...")
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
                self.log(f"Исправлено: {fixed} файлов")

            self.log(f"\nГотово. Открывай: {out_dir / 'index.html'}")

        except Exception as e:
            self.log(f"\n✗ Ошибка: {e}")
            import traceback
            self.log(traceback.format_exc())
        finally:
            self.running = False
            self.root.after(0, self._download_finished)

    def _download_finished(self):
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.analyze_btn.config(state=tk.NORMAL)
        self.set_status("Готов")

    def stop_download(self):
        self.running = False
        self.set_status("Останавливаю...")
        self.log("\n■ Остановка по запросу...")

    def on_close(self):
        self.running = False
        sys.stdout = self.old_stdout
        sys.stderr = self.old_stderr
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = SiteDownloaderGUI()
    app.run()
