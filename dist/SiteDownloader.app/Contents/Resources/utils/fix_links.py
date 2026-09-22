# -*- coding: utf-8 -*-
"""
Переписывает корневые пути /... -> ./ в index.html,
чтобы ассеты (CSS, картинки) загружались при открытии файла локально.
"""
import re
import os

HTML_PATH = os.path.join(os.path.dirname(__file__), "archive", "index.html")

with open(HTML_PATH, encoding="utf-8", errors="replace") as f:
    html = f.read()

before = len(re.findall(r'(?:href|src|content|data-src|data-background|poster)="/', html))

html = re.sub(r'((?:href|src|content|data-src|data-background|poster)=")/', r'\1./', html)
html = re.sub(r"((?:href|src|content|data-src|data-background|poster)=')/", r"\1./", html)
html = re.sub(r"url\('/", "url('./", html)
html = re.sub(r'url\("/', 'url("./', html)
html = re.sub(r'url\(/', 'url(./', html)

after = len(re.findall(r'(?:href|src|content|data-src|data-background|poster)="\./', html))

with open(HTML_PATH, "wb") as f:
    f.write(html.encode("utf-8"))

print(f"Готово. Заменено {before} -> {after} относительных ссылок.")
