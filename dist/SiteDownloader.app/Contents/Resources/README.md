# SiteDownloader

Инструменты для скачивания (зеркалирования) сайтов из интернета.

**Ключевая особенность:** перед скачиванием скрипт анализирует сайт и говорит, 
можно ли его нормально сохранить. Если это SPA (React/Vue/Angular) — предупредит.
Если есть SSR-данные (__NEXT_DATA__, __NUXT__) — извлечёт их через `--api`.
Если нужен полный рендеринг — используйте `--browser` (Playwright).

## Быстрый старт

```bash
pip install -r requirements.txt

# Просто ввести URL — скрипт сам всё проанализирует
python site_downloader.py https://example.com
```

## Режимы работы

| Режим | Команда | Что делает |
|---|---|---|
| **Обычный** | `python site_downloader.py https://...` | BFS-обход, requests, подходит для статики |
| **Браузер** | `python site_downloader.py https://... --browser` | Playwright, выполняет JS, обходит Cloudflare |
| **Данные** | `python site_downloader.py https://... --api` | Извлекает SSR-данные (Next.js/Nuxt) + API |
| **Принудительно** | `... --force` | Скачать даже если не рекомендуется |

## Анализ сайта

При запуске скрипт:
1. Определяет фреймворк (React/Vue/Angular/Svelte/WordPress/Static)
2. Проверяет Cloudflare-защиту
3. Ищет SSR-данные в HTML (__NEXT_DATA__, __NUXT__)
4. Находит API-эндпоинты
5. Проверяет, пуст ли контентный div (root/app)
6. Даёт рекомендацию: скачается нормально / нужен браузер / не скачается

### Пример вывода для SPA (sdamgia.ru):
```
════════════════════════════════════════════════════
 АНАЛИЗ САЙТА
════════════════════════════════════════════════════
  ✓ Cloudflare: нет
  ℹ Размер HTML: 8241 байт
  ℹ Фреймворк: React
  ℹ Генератор: Next.js
  ✗ SPA-приложение (React) — контент только через JavaScript
  ✗ Контентный div пуст — все данные через JS
  ⚠ Найдено API-эндпоинтов: 3
    • https://inf-ege.sdamgia.ru/api/...
────────────────────────────────────────────────
  ✗ Скачать МОЖНО, но сайт будет НЕПОЛНЫМ (SPA без данных в HTML)
    • Попробуйте --api чтобы скачать данные через API
    • Попробуйте --browser для Playwright
════════════════════════════════════════════════════
```

## Параметры

| Параметр | Описание |
|---|---|
| `--depth N` | Глубина обхода (0 = без лимита) |
| `--workers N` | Потоков (по умолч. 5) |
| `--delay N` | Задержка между страницами, сек |
| `--asset-delay N` | Задержка между ассетами |
| `--timeout N` | Таймаут запроса, сек |
| `--include cdn.example.com` | Дополнительные хосты |
| `--exclude "/wp-json\|/feed/"` | Исключить по regex |
| `--no-fix` | Не исправлять пути для офлайн |
| `--browser` | Playwright (headless браузер) |
| `--api` | Извлечь SSR-данные + API |
| `--max-pages N` | Максимум страниц для Playwright |
| `--force` | Скачать даже если не рекомендуется |

## Структура

```
site_downloader.py       — главный скрипт (универсальный загрузчик)
mirror/                  — скрипты для зеркалирования
  mirror_site_sequential.py  — последовательное скачивание по списку URL
  mirror_site_parallel.py    — многопоточное скачивание по списку URL
  mirror_full.py             — BFS-обход + авто-фикс путей
  download_page.py           — скачать одну страницу со всеми ассетами
  download_missing.py        — докачка битых файлов (WordPress resize)
utils/                   — утилиты
  fix_paths.py               — исправление путей в HTML/CSS
  fix_paths_absolute.py      — исправление абсолютных URL в относительные
  fix_links.py               — быстрая замена / → ./ в index.html
  audit_result.py            — аудит зеркала (битые ссылки, медиа)
  diagnose_links.py          — диагностика битых ссылок
  probe_missing.py           — проверка доступности файлов на живом сайте
  check_links.py             — подсчёт ссылок в index.html
examples/                — примеры (из проекта GymSite155)
  fetch_media.py             — загрузка медиа с живого сайта
  migrate.py                 — извлечение контента из архива в фрагменты
  build_site.py              — сборка HTML-страниц из архива
```

## Зеркалирование по списку URL

```bash
python mirror/mirror_site_sequential.py urls.txt -o ./archive
python mirror/mirror_site_parallel.py urls.txt -o ./archive --workers 8
```

## Полное зеркалирование (BFS)

```bash
python mirror/mirror_full.py -o ./archive
```

## Исправление путей

```bash
python utils/fix_paths.py              # /path → относительные
python utils/fix_paths_absolute.py     # http://site.com/path → относительные
python utils/fix_links.py              # / → ./ в index.html
```

## Аудит

```bash
python utils/audit_result.py ./mirror
python utils/diagnose_links.py ./mirror
python utils/check_links.py ./mirror/index.html
python utils/probe_missing.py 50       # проверить 50 случайных файлов
```
