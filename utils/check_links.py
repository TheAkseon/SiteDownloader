import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "archive/index.html"
with open(path, encoding="utf-8", errors="replace") as f:
    html = f.read()
refs = re.findall(r'(?:href|src)=["\x27](http://www\.155gymspb\.ru[^"\x27]*)["\x27]', html)
print(f"Файл: {path}")
print(f"Всего ссылок: {len(refs)}")
# Классифицируем
nav = [r for r in refs if not any(x in r for x in ["feed", "xmlrpc", "wp-login", "wp-admin", "page/", "?"])]
ext = [r for r in refs if r not in nav]
print(f"Навигационные (страницы сайта): {len(nav)}")
print(f"Служебные/прочие: {len(ext)}")
print("\nПервые навигационные:")
for r in nav[:10]:
    print(" ", r)
print("\nСлужебные:")
for r in ext[:10]:
    print(" ", r)
