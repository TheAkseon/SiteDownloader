# -*- coding: utf-8 -*-
"""
Переносит контент из site_review/archive/ в site/content/.

Для каждой страницы:
  1. Извлекает entry-content, title, description, дату, категории
  2. Вычищает WP-мусор: script, style, noscript, шеринг, «похожие записи»
  3. Абсолютные ссылки на 155gymspb.ru → корневые локальные
  4. Проверяет каждый <img> по факту наличия файла в site/public/
     живой — оставляет, битый — заменяет заглушкой
  5. Срезает srcset/sizes (ведут на несуществующие ресайз-варианты)
  6. Помечает недоступные документы

Результат: HTML-фрагменты + site/content/manifest.json

Запуск:  python scripts/migrate.py
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "site_review" / "archive"
PUBLIC = ROOT / "site" / "public"
# Фрагменты лежат внутри src/, чтобы их разрешал Vite: пути из import.meta.url
# при сборке переписываются на bundled-чанк, и чтение через fs ломается.
# Имя fragments/, а не content/ — src/content/ зарезервировано под коллекции Astro.
CONTENT = ROOT / "site" / "src" / "fragments"
PAGES_DIR = CONTENT / "pages"
NEWS_DIR = CONTENT / "news"

PLACEHOLDER_TEXT = "Тут будет изображение"
VIDEO_PLACEHOLDER_TEXT = "Тут будет видео"
LIVE_BASE = "http://www.155gymspb.ru"

IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".ico"}
DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".zip", ".rar"}
MEDIA_EXT = {".mp4", ".webm", ".ogv", ".mov", ".mp3", ".ogg", ".wav", ".m4a"}

# Мусор из WordPress, который не несёт контента
JUNK_SELECTORS = [
    ".sharedaddy", ".jp-relatedposts", ".sd-sharing", ".addtoany_share_save_container",
    ".post-navigation", ".nav-links", ".comments-area", "#comments", ".entry-footer",
    ".screen-reader-text", ".wp-block-post-navigation", ".sgb-lightbox-wrap",
]

# Ссылки-мусор, вставленные из Word
BAD_LINK_SCHEMES = ("file:///", "file://", "consultantplus:")

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}

stats = Counter()
img_report: list[dict] = []
doc_report: list[dict] = []
# что развернули в текст — для проверки, что не потеряли ничего нужного
unwrap_report: Counter = Counter()


def translit(text: str) -> str:
    """Кириллица → латиница, для коротких безопасных имён файлов."""
    out = []
    for ch in text.lower():
        if ch in TRANSLIT:
            out.append(TRANSLIT[ch])
        elif ch.isalnum() and ch.isascii():
            out.append(ch)
        elif ch in "-_/":
            out.append("-")
    return re.sub(r"-+", "-", "".join(out)).strip("-")


def safe_filename(slug: str) -> str:
    """
    Короткое ASCII-имя файла: транслит (до 40 симв.) + хеш slug.
    Хеш гарантирует уникальность, транслит — читаемость.
    Длинные кириллические пути на Windows упираются в лимит 260 символов.
    """
    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:8]
    base = translit(slug)[:40].strip("-") or "page"
    return f"{base}-{digest}.html"


def to_site_path(raw: str, page_dir: Path) -> str | None:
    """
    Любую ссылку приводит к пути от корня сайта (без ведущего /).
    Возвращает None для внешних ссылок и спецсхем.
    """
    if not raw:
        return None
    low = raw.strip().lower()
    if low.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    if low.startswith(BAD_LINK_SCHEMES):
        return None

    if low.startswith(("http://", "https://", "//")):
        parsed = urllib.parse.urlparse(raw if not raw.startswith("//") else "http:" + raw)
        if "155gymspb.ru" not in parsed.netloc.lower():
            return None  # внешняя ссылка — не трогаем
        path = parsed.path
    elif raw.startswith("/"):
        path = raw
    else:
        # относительная — резолвим от папки страницы
        try:
            resolved = (page_dir / raw.split("#")[0].split("?")[0]).resolve()
            path = "/" + resolved.relative_to(ARCHIVE.resolve()).as_posix()
        except (ValueError, OSError):
            return None

    clean = urllib.parse.unquote(path.split("#")[0].split("?")[0]).lstrip("/")
    # relative_to у корня архива даёт '.', а это главная, а не страница './'
    return "" if clean in (".", "./") else clean


def asset_exists(site_path: str) -> bool:
    if not site_path:
        return False
    candidate = PUBLIC / site_path
    try:
        candidate.relative_to(PUBLIC)
    except ValueError:
        return False
    return candidate.is_file()


def _load_aliases() -> dict[str, str]:
    """
    Карта переименований от optimize_images.py: BMP и крупные PNG
    конвертируются в JPEG, расширение меняется. Без этой карты страница
    ссылалась бы на исходное имя и получала заглушку вместо картинки.
    """
    path = ROOT / "site_review" / "_asset_aliases.json"
    if not path.exists():
        return {}
    try:
        import json as _json

        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


ASSET_ALIASES = _load_aliases()


def resolve_asset(site_path: str | None) -> str | None:
    """
    Фактический путь к файлу с учётом переименований.
    Возвращает None, если файла нет — тогда ставится заглушка.
    """
    if not site_path:
        return None
    if asset_exists(site_path):
        return site_path
    alias = ASSET_ALIASES.get(site_path)
    if alias and asset_exists(alias):
        stats["путь через alias"] += 1
        return alias
    return None


def fix_images(soup: BeautifulSoup, page_dir: Path, slug: str) -> str | None:
    """
    Проверяет каждый <img>. Живой оставляет с корневым путём,
    битый заменяет заглушкой. Возвращает путь первой живой картинки
    (для карточки новости на главной).
    """
    lead: str | None = None

    for img in soup.find_all("img"):
        # srcset ведёт на ресайз-варианты, которых нет на сервере.
        # Убираем у ВСЕХ картинок, включая живые: иначе браузер
        # выберет битый вариант даже при рабочем src.
        for attr in ("srcset", "sizes", "data-srcset", "data-lazy-srcset"):
            if img.has_attr(attr):
                del img[attr]
                stats["srcset убран"] += 1

        # WP-плагины прячут настоящий адрес в data-атрибуты
        raw = None
        for attr in ("src", "data-src", "data-lazy-src", "data-original"):
            val = img.get(attr)
            if val and not val.startswith("data:"):
                raw = val
                break

        site_path = to_site_path(raw, page_dir) if raw else None
        actual = resolve_asset(site_path)

        if actual:
            img["src"] = "/" + urllib.parse.quote(actual)
            img.attrs.pop("data-src", None)
            img.attrs.pop("data-lazy-src", None)
            img.attrs.pop("data-original", None)
            img.attrs.setdefault("loading", "lazy")
            img.attrs.setdefault("decoding", "async")
            if not img.get("alt"):
                img["alt"] = ""
            stats["изображений живых"] += 1
            if lead is None:
                lead = img["src"]
            continue

        # Файла нет — на сайте не показываем заглушку, только пишем в список
        if site_path:
            img_report.append({
                "slug": slug,
                "missing": site_path,
                "alt": (img.get("alt") or "").strip(),
                "kind": "изображение",
            })
        img.decompose()
        stats["изображений пропущено"] += 1

    return lead


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$")


def unwrap(a, reason: str, target: str = "") -> None:
    """Убирает ссылку, оставляя её текст и вложенные теги."""
    if target:
        unwrap_report[target] += 1
    a.unwrap()
    stats[reason] += 1


def fix_media(soup: BeautifulSoup, page_dir: Path, slug: str) -> None:
    """
    <video>, <audio>, <source>: живые файлы → корневой путь, утраченные → заглушка.
    Эти теги не ловятся ни fix_links (там <a>), ни fix_images (там <img>),
    поэтому раньше 29 видео оставались абсолютными ссылками на старый домен.
    """
    for tag in soup.find_all(["video", "audio"]):
        # адрес может быть и на самом теге, и во вложенных <source>
        candidates = []
        if tag.get("src"):
            candidates.append((tag, "src"))
        for src in tag.find_all("source"):
            if src.get("src"):
                candidates.append((src, "src"))

        alive = False
        for node, attr in candidates:
            site_path = to_site_path(node.get(attr), page_dir)
            actual = resolve_asset(site_path)
            if actual:
                node[attr] = "/" + urllib.parse.quote(actual)
                alive = True
                stats["видео живых"] += 1
            elif site_path:
                img_report.append({"slug": slug, "missing": site_path})

        # poster тоже может быть утрачен
        poster = to_site_path(tag.get("poster"), page_dir) if tag.get("poster") else None
        if poster:
            actual_poster = resolve_asset(poster)
            if actual_poster:
                tag["poster"] = "/" + urllib.parse.quote(actual_poster)
            else:
                del tag["poster"]

        if alive:
            tag["controls"] = ""
            tag.attrs.pop("autoplay", None)
            # видео весит десятки МБ — не тянем его, пока не нажали play
            tag["preload"] = "none"
            continue

        first = candidates[0] if candidates else None
        original = to_site_path(first[0].get(first[1]), page_dir) if first else None
        if original:
            img_report.append({"slug": slug, "missing": original, "alt": "", "kind": "видео"})
        tag.decompose()
        stats["видео пропущено"] += 1


def fix_links(soup: BeautifulSoup, page_dir: Path, known_pages: set[str], slug: str) -> None:
    """
    Переписывает ссылки. Внутренние страницы сверяются с known_pages —
    множеством реально существующих адресов. Всё, чего нет, разворачивается
    в текст, поэтому битых внутренних ссылок не остаётся по построению.
    """
    for a in soup.find_all("a"):
        raw = (a.get("href") or "").strip()

        if raw.lower().startswith(BAD_LINK_SCHEMES):
            # мусор из Word: разворачиваем в текст, ссылку убираем
            unwrap(a, "мусорных ссылок убрано")
            continue

        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        # Email без mailto: — иначе резолвится как относительный путь
        if EMAIL_RE.match(raw):
            a["href"] = f"mailto:{raw}"
            stats["email исправлен"] += 1
            continue

        site_path = to_site_path(raw, page_dir)
        if site_path is None:
            # внешняя ссылка — открываем в новой вкладке
            if raw.lower().startswith(("http://", "https://")):
                a["target"] = "_blank"
                a["rel"] = "noopener"
            continue

        ext = Path(site_path).suffix.lower()
        last = Path(site_path).name

        if ext in DOC_EXT and not resolve_asset(site_path):
            title = " ".join(a.get_text(" ", strip=True).split())
            doc_report.append({"slug": slug, "path": site_path, "title": title, "file": Path(site_path).name})
            a.decompose()
            stats["документов недоступно"] += 1
            continue

        if ext in IMG_EXT or ext in DOC_EXT or ext in MEDIA_EXT:
            actual = resolve_asset(site_path)
            if actual:
                a["href"] = "/" + urllib.parse.quote(actual)
                stats["ссылок на файлы"] += 1
            else:
                # обёртка галереи вокруг утраченной картинки — ссылка ни к чему
                unwrap(a, "ссылок на утраченные файлы убрано")
            continue

        # Похоже на файл (есть точка в имени), но расширение не наше:
        # в контенте встречается обрезанное «.pd» вместо «.pdf».
        # Страницей это быть не может — разворачиваем в текст.
        if "." in last and ext not in ("", ".html", ".htm"):
            actual = resolve_asset(site_path)
            if actual:
                a["href"] = "/" + urllib.parse.quote(actual)
                stats["ссылок на файлы"] += 1
            else:
                unwrap(a, "битых файловых ссылок убрано")
            continue

        # ссылка на страницу: приводим к виду /путь/
        page = site_path
        if page.endswith("index.html"):
            page = page[: -len("index.html")]
        page = page.strip("/")

        if page and page not in known_pages:
            # страницы нет на сайте (архивы авторов WP, удалённые разделы)
            unwrap(a, "ссылок на несуществующие страницы убрано", f"/{page}/")
            continue

        a["href"] = "/" + (f"{page}/" if page else "")
        stats["ссылок на страницы"] += 1


def clean(soup: BeautifulSoup) -> None:
    """Удаляет то, что не несёт контента."""
    for tag in soup.find_all(["script", "style", "noscript", "form", "button"]):
        tag.decompose()
        stats["мусорных тегов"] += 1

    # HTML-комментарии WordPress: блоки wp:*, данные галерей simply-gallery-block.
    # Пользователю не видны, но это 274 КБ на 352 страницы и ссылки на старый домен.
    for node in soup.find_all(string=lambda t: isinstance(t, Comment)):
        stats["комментариев убрано"] += 1
        node.extract()

    for selector in JUNK_SELECTORS:
        for tag in soup.select(selector):
            tag.decompose()
            stats["WP-блоков убрано"] += 1

    # инлайновые обработчики
    for tag in soup.find_all(True):
        for attr in [a for a in tag.attrs if a.lower().startswith("on")]:
            del tag[attr]

    # id мешают, если совпадут с нашими
    for tag in soup.find_all(id="page-content"):
        del tag["id"]


def extract_meta(soup: BeautifulSoup, body_class: str) -> dict:
    """Заголовок, описание, дата, категории."""
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    title = re.sub(r"\s*[—|–-]\s*ГБОУ гимназия\s*№?\s*155.*$", "", title)
    title = html.unescape(title).strip() or "Без названия"

    desc_tag = soup.find("meta", attrs={"name": "description"})
    description = (desc_tag.get("content") or "").strip() if desc_tag else ""

    date = None
    time_tag = soup.find("time")
    if time_tag and time_tag.get("datetime"):
        date = time_tag["datetime"]
    if not date:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', str(soup))
        if m:
            date = m.group(1)

    categories = []
    for a in soup.find_all("a", rel=True):
        rel = a.get("rel")
        rel = " ".join(rel) if isinstance(rel, list) else str(rel)
        if "category" not in rel:
            continue
        name = a.get_text(strip=True)
        href = a.get("href") or ""
        # Категории бывают вложенными: /category/кафедры/французский-язык/
        # Берём ВЕСЬ путь после /category/, иначе «Французский язык»
        # схлопнется в slug «кафедры» вместе с восемью другими кафедрами.
        m = re.search(r"/category/(.+?)/?(?:[?#]|$)", urllib.parse.unquote(href))
        if m:
            # fix_paths.py ранее переписал часть ссылок в относительные и дописал
            # index.html — иначе slug становится 'кафедры/французский-язык/index.html'
            slug = re.sub(r"/?index\.html?$", "", m.group(1)).strip("/")
        else:
            slug = translit(name)
        if name and slug and not any(c["slug"] == slug for c in categories):
            categories.append({"title": name, "slug": slug})

    if not categories:
        for cls in body_class.split():
            if cls.startswith("category-"):
                name = cls[len("category-"):]
                categories.append({"title": name, "slug": name})

    return {
        "title": title,
        "description": description,
        "date": date,
        "categories": categories,
    }


def _biggest_container(scope) -> object | None:
    """
    Тема scholarship на части страниц не даёт entry-content: контент лежит
    в #content > .mt-container, рядом с .entry-header (заголовком).
    Берём тот .mt-container, в котором больше всего текста.
    """
    best = None
    best_len = 0
    for node in scope.find_all(class_="mt-container"):
        # заголовок страницы — не контент
        if node.find_parent(class_="entry-header"):
            continue
        length = len(node.get_text(" ", strip=True))
        if length > best_len:
            best, best_len = node, length
    return best if best_len > 200 else None


def find_content(soup: BeautifulSoup):
    """Основной контейнер контента WordPress."""
    for finder in (
        lambda: soup.find(class_="entry-content"),
        lambda: soup.find(class_="post-content"),
        lambda: soup.find("article"),
        lambda: soup.find(id="primary"),
        lambda: soup.find("main"),
    ):
        node = finder()
        if node:
            return node

    content = soup.find(id="content")
    if content:
        node = _biggest_container(content)
        if node:
            return node
    return None


def scan(source: Path) -> dict | None:
    """
    Проход 1: собирает метаданные и запоминает, какой узел содержит контент.
    Ссылки пока не трогает — сначала нужно узнать, какие страницы вообще есть.
    """
    html_text = source.read_text(encoding="utf-8", errors="replace")

    m = re.search(r'<body class="([^"]*)"', html_text)
    body_class = m.group(1) if m else ""

    rel_dir = source.parent.relative_to(ARCHIVE)
    slug = "" if rel_dir.as_posix() == "." else rel_dir.as_posix()

    # Главную собираем вручную в Astro, архивы категорий генерируем сами
    if not slug:
        stats["пропущено: главная"] += 1
        return None
    if slug.startswith("category/") or "category" in body_class or "archive" in body_class:
        stats["пропущено: архив категории"] += 1
        return None

    soup = BeautifulSoup(html_text, "html.parser")
    meta = extract_meta(soup, body_class)

    node = find_content(soup)
    if node is None:
        stats["БЕЗ КОНТЕНТА"] += 1
        print(f"  [!] контент не найден: {slug}")
        return None

    kind = "news" if "single-post" in body_class else "page"
    filename = safe_filename(slug)

    return {
        "slug": slug,
        "file": f"{'news' if kind == 'news' else 'pages'}/{filename}",
        "kind": kind,
        "title": meta["title"],
        "description": meta["description"],
        "date": meta["date"],
        "categories": meta["categories"],
        "image": None,
        # служебные поля, в манифест не попадают
        "_source": source,
        "_html": str(node),
    }


def render(entry: dict, known_pages: set[str]) -> None:
    """Проход 2: чистит фрагмент, правит ссылки и изображения, пишет файл."""
    fragment = BeautifulSoup(entry["_html"], "html.parser")
    source_dir = entry["_source"].parent

    clean(fragment)
    fix_links(fragment, source_dir, known_pages, entry["slug"])
    fix_media(fragment, source_dir, entry["slug"])
    entry["image"] = fix_images(fragment, source_dir, entry["slug"])

    out_dir = NEWS_DIR if entry["kind"] == "news" else PAGES_DIR
    filename = entry["file"].split("/", 1)[1]
    (out_dir / filename).write_text(str(fragment), encoding="utf-8")

    stats[f"перенесено: {entry['kind']}"] += 1


def main() -> None:
    for directory in (PAGES_DIR, NEWS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        for old in directory.glob("*.html"):
            old.unlink()

    sources = sorted(ARCHIVE.rglob("index.html"))
    print(f"Найдено HTML-файлов: {len(sources)}\n")

    # --- Проход 1: метаданные ---
    entries = []
    for source in sources:
        try:
            entry = scan(source)
            if entry:
                entries.append(entry)
        except Exception as exc:
            stats["ОШИБОК"] += 1
            print(f"  [!] {source.parent.name}: {exc}")

    # Множество существующих адресов: страницы, новости, архивы категорий
    # и то, что генерирует Astro. Всё остальное — битая ссылка.
    known_pages = {e["slug"] for e in entries}
    for entry in entries:
        for cat in entry["categories"]:
            known_pages.add(f"category/{cat['slug']}")
    known_pages.update({"новости", "search"})

    # --- Проход 2: контент со сверенными ссылками ---
    for entry in entries:
        try:
            render(entry, known_pages)
        except Exception as exc:
            stats["ОШИБОК"] += 1
            print(f"  [!] {entry['slug']}: {exc}")

    # служебные поля не попадают в манифест
    for entry in entries:
        entry.pop("_source", None)
        entry.pop("_html", None)

    # новости — по убыванию даты, страницы — по slug
    news = sorted(
        [e for e in entries if e["kind"] == "news"],
        key=lambda e: e["date"] or "",
        reverse=True,
    )
    pages = sorted([e for e in entries if e["kind"] == "page"], key=lambda e: e["slug"])

    categories: dict[str, dict] = {}
    for item in news:
        for cat in item["categories"]:
            entry = categories.setdefault(cat["slug"], {"slug": cat["slug"], "title": cat["title"], "count": 0})
            entry["count"] += 1

    manifest = {
        "pages": pages,
        "news": news,
        "categories": sorted(categories.values(), key=lambda c: -c["count"]),
        "titles": {e["slug"]: e["title"] for e in entries},
    }
    (CONTENT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print("\n--- Итог ---")
    for key, val in stats.most_common():
        print(f"  {key:26} {val:>6}")
    print(f"\n  страниц:    {len(pages)}")
    print(f"  новостей:   {len(news)}")
    print(f"  категорий:  {len(categories)}")
    print(f"  → {CONTENT / 'manifest.json'}")

    if unwrap_report:
        print(f"\n--- Развёрнуто в текст: {len(unwrap_report)} уникальных адресов ---")
        for target, count in unwrap_report.most_common(20):
            print(f"  {count:>4}  {target[:78]}")
        (ROOT / "site_review" / "_unwrapped_links.txt").write_text(
            "".join(f"{c}\t{t}\n" for t, c in unwrap_report.most_common()), encoding="utf-8"
        )
        print("  → site_review/_unwrapped_links.txt")

    titles = {e["slug"]: e["title"] for e in entries}

    def heading(slug: str, kind_hint: str = "") -> tuple[str, str, str]:
        title = titles.get(slug) or slug
        kind = "Новость" if kind_hint == "news" else "Страница"
        if any(e["slug"] == slug and e["kind"] == "news" for e in entries):
            kind = "Новость"
        url = f"/{slug}/" if slug else "/"
        return title, kind, url

    if img_report:
        by_slug: dict[str, list[dict]] = {}
        for row in img_report:
            items = by_slug.setdefault(row["slug"], [])
            name = Path(row["missing"]).name
            if any(i["name"] == name for i in items):
                continue
            items.append({
                "name": name,
                "alt": row.get("alt") or "",
                "kind": row.get("kind") or "изображение",
            })
        lines = [
            "# Страницы, на которых не хватает изображений\n\n",
            f"Всего пропало **{sum(len(v) for v in by_slug.values())}** файлов на **{len(by_slug)}** страницах.\n\n",
            "Эти картинки (и видео) были на старом сайте, но файлов больше нет. ",
            "Нужно заново положить их на страницу — ниже название страницы и имя файла.\n\n",
        ]
        for slug in sorted(by_slug, key=lambda s: (titles.get(s) or s).lower()):
            title, kind, url = heading(slug)
            lines.append(f"## {title}\n\n{kind}. Адрес: {url}\n\n")
            for item in by_slug[slug]:
                extra = f", подпись: {item['alt']}" if item["alt"] else ""
                lines.append(f"- `{item['name']}` ({item['kind']}{extra})\n")
            lines.append("\n")
        (ROOT / "нужно-добавить-изображения.md").write_text("".join(lines), encoding="utf-8")
        print(f"  → нужно-добавить-изображения.md ({sum(len(v) for v in by_slug.values())} файлов)")

    if doc_report:
        by_slug: dict[str, list[dict]] = {}
        for row in doc_report:
            items = by_slug.setdefault(row["slug"], [])
            if any(i["file"] == row["file"] for i in items):
                continue
            items.append(row)
        lines = [
            "# Недоступные документы\n\n",
            f"Всего **{sum(len(v) for v in by_slug.values())}** документов на **{len(by_slug)}** страницах.\n\n",
            "Ссылки на сайте оставлены, но самого файла нет. ",
            "Нужно загрузить документ заново — ниже страница и название.\n\n",
        ]
        for slug in sorted(by_slug, key=lambda s: (titles.get(s) or s).lower()):
            title, kind, url = heading(slug)
            lines.append(f"## {title}\n\n{kind}. Адрес: {url}\n\n")
            for item in by_slug[slug]:
                if item.get("title") and item["title"] != item["file"]:
                    lines.append(f"- {item['title']} — файл `{item['file']}`\n")
                else:
                    lines.append(f"- `{item['file']}`\n")
            lines.append("\n")
        (ROOT / "нужно-добавить-документы.md").write_text("".join(lines), encoding="utf-8")
        print(f"  → нужно-добавить-документы.md ({sum(len(v) for v in by_slug.values())} файлов)")


if __name__ == "__main__":
    main()
