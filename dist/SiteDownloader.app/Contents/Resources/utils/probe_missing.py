# -*- coding: utf-8 -*-
"""
Проверяет на живом сайте, доступны ли отсутствующие медиа и документы.
Берёт выборку из site_review/_missing_media.txt и делает HEAD-запросы.

Цель — оценить, сколько файлов реально можно докачать, а сколько
придётся заменить заглушками.

Запуск:  python site_review/probe_missing.py [размер_выборки]
"""
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = Path(__file__).resolve().parent.parent
MISSING_FILE = ROOT / "site_review" / "_missing_media.txt"
BASE = "http://www.155gymspb.ru"
RESIZE_RE = re.compile(r"-\d+x\d+(?=\.\w+$)")
TIMEOUT = 15
UA = "Mozilla/5.0 (compatible; GymSite155-archive/1.0)"


def probe(site_path: str) -> int:
    """HEAD-запрос. Возвращает HTTP-код (0 — сетевая ошибка)."""
    url = BASE + "/" + urllib.parse.quote(site_path)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def main() -> None:
    sample_size = int(sys.argv[1]) if len(sys.argv) > 1 else 40

    if not MISSING_FILE.exists():
        print(f"Нет файла {MISSING_FILE}. Сначала запусти audit_result.py")
        return

    rows = []
    for line in MISSING_FILE.read_text(encoding="utf-8").splitlines():
        if "\t" in line:
            count, path = line.split("\t", 1)
            rows.append(path)

    by_ext: dict[str, list[str]] = {}
    for path in rows:
        by_ext.setdefault(Path(path).suffix.lower(), []).append(path)

    print(f"Всего отсутствует: {len(rows)} файлов")
    print(f"По расширениям: { {k: len(v) for k, v in sorted(by_ext.items(), key=lambda x: -len(x[1]))} }\n")

    # Пропорциональная выборка по расширениям, минимум 3 на тип
    random.seed(155)
    sample: list[str] = []
    for ext, paths in by_ext.items():
        take = max(3, round(sample_size * len(paths) / len(rows)))
        sample.extend(random.sample(paths, min(take, len(paths))))

    print(f"Проверяем выборку: {len(sample)} файлов\n")

    results = Counter()
    by_ext_ok = Counter()
    by_ext_total = Counter()
    resize_recovered = 0

    for i, site_path in enumerate(sample, 1):
        ext = Path(site_path).suffix.lower()
        by_ext_total[ext] += 1
        code = probe(site_path)

        note = ""
        if code == 200:
            results["200 доступен"] += 1
            by_ext_ok[ext] += 1
        elif code == 404 and RESIZE_RE.search(site_path):
            # пробуем оригинал без суффикса размера
            original = RESIZE_RE.sub("", site_path)
            code2 = probe(original)
            if code2 == 200:
                results["404, но оригинал есть"] += 1
                by_ext_ok[ext] += 1
                resize_recovered += 1
                note = f"→ оригинал OK: {original.rsplit('/', 1)[-1]}"
            else:
                results[f"404 (и оригинал {code2})"] += 1
        else:
            results[f"{code}"] += 1

        print(f"  [{i:>3}/{len(sample)}] {code} {site_path[-70:]} {note}")

    print("\n--- Итог выборки ---")
    for key, val in results.most_common():
        print(f"  {key:26} {val:>4}")

    ok = sum(by_ext_ok.values())
    total = sum(by_ext_total.values())
    print(f"\nДоступно на живом сайте: {ok}/{total} ({100 * ok / total:.0f}%)")
    print(f"  из них восстановлено через оригинал без -WxH: {resize_recovered}")

    print("\n--- Доступность по расширениям ---")
    for ext in sorted(by_ext_total, key=lambda x: -by_ext_total[x]):
        got, tot = by_ext_ok[ext], by_ext_total[ext]
        print(f"  {ext:8} {got:>3}/{tot:<3} ({100 * got / tot:>3.0f}%)  всего таких файлов: {len(by_ext[ext])}")

    print("\nОценка: из", len(rows), "отсутствующих реально докачать ≈",
          int(len(rows) * ok / total), "файлов")


if __name__ == "__main__":
    main()
