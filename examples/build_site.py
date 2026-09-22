# -*- coding: utf-8 -*-
"""
Автоматический перенос всех страниц гимназии 155 в новый дизайн.

Для каждой HTML-страницы из site_review/archive/:
  1. Извлекает заголовок и основной контент
  2. Оборачивает в base.html с навигацией 155
  3. Сохраняет в result/ с сохранением URL-структуры

Использует BeautifulSoup для парсинга HTML.
"""
import os
import re
import sys
from pathlib import Path
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

# Пути
SCRIPT_DIR = Path(__file__).parent
ARCHIVE_DIR = SCRIPT_DIR / "site_review" / "archive"
TEMPLATE_DIR = SCRIPT_DIR / "design" / "template"
RESULT_DIR = SCRIPT_DIR / "result"
BASE_HTML = TEMPLATE_DIR / "base.html"

# Счётчики
processed = 0
skipped = 0
errors = []


def extract_content(html: str, source_path: str) -> dict:
    """
    Извлекает title и основной контент из WordPress-страницы.

    Returns:
        {
            'title': str,
            'content': str (HTML),
            'meta_description': str | None
        }
    """
    soup = BeautifulSoup(html, 'html.parser')

    # Title
    title_tag = soup.find('title')
    title = title_tag.get_text(strip=True) if title_tag else "ГБОУ гимназия № 155"
    # Убираем дубликаты названия сайта
    title = re.sub(r'\s*[—|]\s*ГБОУ гимназия № 155.*$', '', title)

    # Meta description
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    description = meta_desc.get('content') if meta_desc and meta_desc.get('content') else None

    # Основной контент — пробуем несколько WordPress-селекторов
    content_html = None

    # 1. Попытка: .entry-content (основной контент WordPress)
    entry = soup.find(class_='entry-content')
    if entry:
        content_html = str(entry)

    # 2. Попытка: article .post
    if not content_html:
        article = soup.find('article')
        if article:
            # Убираем header/footer статьи, оставляем только контент
            for unwanted in article.find_all(['header', 'footer', '.entry-meta']):
                unwanted.decompose()
            content_html = str(article)

    # 3. Попытка: #primary .site-main
    if not content_html:
        primary = soup.find(id='primary')
        if primary:
            content_html = str(primary)

    # 4. Fallback: <main> или <body>
    if not content_html:
        main = soup.find('main') or soup.find('body')
        if main:
            # Убираем header, footer, sidebar
            for unwanted in main.find_all(['header', 'footer', 'aside', 'nav', '.header', '.footer', '.sidebar']):
                unwanted.decompose()
            content_html = str(main)

    if not content_html:
        content_html = f"<p><em>Контент не извлечён из {source_path}</em></p>"

    return {
        'title': title,
        'content': content_html,
        'meta_description': description
    }


def build_page(title: str, content: str, meta_desc: str | None = None, depth: int = 0) -> str:
    """
    Оборачивает контент в base.html.

    Args:
        depth: вложенность страницы (0 = корень, 1 = /папка/, 2 = /папка/подпапка/)

    Заменяет:
      - <title>...</title> → новый title
      - <meta name="description" .../> → новый description
      - <div id="page-content"></div> → content
      - href="css/... → href="../css/... (с учётом depth)
    """
    with open(BASE_HTML, encoding="utf-8", errors="replace") as f:
        base = f.read()

    # Замена title
    base = re.sub(
        r'<title>.*?</title>',
        f'<title>{title}</title>',
        base,
        flags=re.DOTALL
    )

    # Замена или добавление meta description
    if meta_desc:
        if '<meta name="description"' in base:
            base = re.sub(
                r'<meta name="description"[^>]*/>',
                f'<meta name="description" content="{meta_desc}" />',
                base
            )
        else:
            base = base.replace('</head>', f'    <meta name="description" content="{meta_desc}" />\n</head>')

    # Исправляем пути к статическим файлам с учётом вложенности
    if depth > 0:
        prefix = "../" * depth
        base = base.replace('href="css/', f'href="{prefix}css/')
        base = base.replace('src="js/', f'src="{prefix}js/')
        base = base.replace('href="images/', f'href="{prefix}images/')
        base = base.replace('src="images/', f'src="{prefix}images/')
        base = base.replace('xlink:href="images/', f'xlink:href="{prefix}images/')
        # Исправляем навигационные ссылки
        base = base.replace('href="/', f'href="{prefix}')

    # Вставка контента
    base = base.replace(
        '<div id="page-content"></div>',
        f'<div id="page-content">\n{content}\n        </div>'
    )

    return base


def process_file(source: Path):
    """Обрабатывает один HTML-файл."""
    global processed, skipped

    # Определяем относительный путь для сохранения структуры
    rel_path = source.relative_to(ARCHIVE_DIR)
    dest = RESULT_DIR / rel_path

    # Пропускаем служебные файлы
    if source.name.startswith('_'):
        return

    # Вычисляем глубину вложенности (количество папок от корня)
    depth = len(rel_path.parts) - 1  # -1 потому что сам файл не считается

    try:
        # Специальная обработка для главной страницы
        if str(rel_path) == "index.html":
            # Копируем index-demo.html как главную
            import shutil
            demo_src = TEMPLATE_DIR / "index-demo.html"
            if demo_src.exists():
                shutil.copy(demo_src, dest)
                processed += 1
                print(f"  [ГЛАВНАЯ] Использован index-demo.html")
                return

        with open(source, encoding="utf-8", errors="replace") as f:
            html = f.read()

        # Извлекаем контент
        data = extract_content(html, str(rel_path))

        # Оборачиваем в шаблон с учётом вложенности
        result_html = build_page(data['title'], data['content'], data['meta_description'], depth)

        # Сохраняем
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(result_html.encode("utf-8"))

        processed += 1
        if processed % 50 == 0:
            print(f"  Обработано: {processed} файлов...")

    except Exception as e:
        errors.append((str(rel_path), str(e)))
        print(f"  [!] Ошибка: {rel_path} — {e}")


def main():
    print("Автоматический перенос сайта гимназии 155 в новый дизайн\n")
    print(f"Источник:  {ARCHIVE_DIR}")
    print(f"Шаблон:    {BASE_HTML}")
    print(f"Результат: {RESULT_DIR}\n")

    # Проверка существования базового шаблона
    if not BASE_HTML.exists():
        print(f"ОШИБКА: Базовый шаблон не найден: {BASE_HTML}")
        return

    # Создаём папку результата
    RESULT_DIR.mkdir(exist_ok=True)

    # Копируем статические файлы (CSS, JS, fonts, images)
    print("Копирование статических файлов...")
    import shutil
    for item in ['css', 'js', 'images']:
        src = TEMPLATE_DIR / item
        dst = RESULT_DIR / item
        if src.exists():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            print(f"  ✓ {item}/")

    # Обрабатываем все HTML-файлы
    html_files = list(ARCHIVE_DIR.rglob("*.html"))
    print(f"\nНайдено HTML-файлов: {len(html_files)}")
    print("Обработка страниц...\n")

    for html_file in html_files:
        process_file(html_file)

    print(f"\n{'='*60}")
    print(f"Готово!")
    print(f"  Обработано: {processed}")
    print(f"  Ошибок: {len(errors)}")

    if errors:
        err_file = RESULT_DIR / "_errors.txt"
        with open(err_file, "w", encoding="utf-8") as f:
            for path, err in errors:
                f.write(f"{path}\t{err}\n")
        print(f"  Лог ошибок: {err_file}")

    print(f"\nРезультат: {RESULT_DIR / 'index.html'}")
    print("Открой index.html в браузере для проверки.")


if __name__ == "__main__":
    main()
