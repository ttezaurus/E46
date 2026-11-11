#!/usr/bin/env python3
"""
build_dtc_db.py: собирает все JSON-базы кодов неисправностей в ОДИН txt-файл,
который OBDnoPRO (02v30+) загружает кнопкой «Загрузить большую базу».

Понимает форматы:
  1) {"codes": [{"code", "title": {en,ru,ka}, "description": {...}, "solutions": {...}}]}  (codes.json, data.json)
  2) список пар [{"P0100": "P0101", "Описание ячейки": "Описание"}, ...]  (obd-trouble-codes.json,
     где первая строка таблицы попала в ключи: скрипт её восстанавливает)
  3) {"P0100": "описание", ...} и NDJSON / склеенные объекты {"code": ...}{"code": ...}

Дубликаты объединяются: языки и поля дополняют друг друга, более полная запись побеждает.
Выход: по одному компактному JSON-объекту на строку (формат понятен загрузчику приложения).

Запуск:
  python build_dtc_db.py                      # берёт все *.json из текущей папки и подпапок
  python build_dtc_db.py data/ -o dtc-db.txt  # папка или список файлов
  python build_dtc_db.py data/ --langs en,ru  # оставить только эти языки (файл меньше)
Только стандартная библиотека Python 3.8+.
"""
import argparse
import json
import os
import re
import sys
from collections import OrderedDict

CODE_RE = re.compile(r'^[A-Z0-9]{5,8}$')            # как принимает загрузчик приложения
OBD_RE = re.compile(r'^[PCBU][0-9A-F]{4}$')          # стандартный OBD-II код
HEX6_RE = re.compile(r'^[0-9A-F]{6}$')               # сырой 3-байтовый код (BMW и др.)
LANG_RE = re.compile(r'^[a-z]{2}(-[A-Za-z]+)?$')
SKIP_DIRS = {'.git', '.github', 'node_modules', '__pycache__'}
SKIP_FILES = {'package.json', 'package-lock.json', 'tsconfig.json'}
JSON_NAME = re.compile(r'\.json( \d+)?$', re.I)      # включая «codes.json 2» (дубликат из iOS)
APP_LOADER = re.compile(r'\{\s*"code"\s*:\s*"([A-Za-z0-9]{5,8})"')   # та же проверка, что в приложении


def norm_code(s):
    return re.sub(r'[^0-9A-Za-z]', '', str(s)).upper()


def clean(v):
    """Обрезает пробелы, выбрасывает пустое."""
    if isinstance(v, str):
        return v.replace('\u00a0', ' ').strip()
    if isinstance(v, list):
        out = [clean(x) for x in v]
        return [x for x in out if x not in ('', None, [], {})]
    if isinstance(v, dict):
        out = OrderedDict((k, clean(x)) for k, x in v.items())
        return OrderedDict((k, x) for k, x in out.items() if x not in ('', None, [], {}))
    return v


def is_lang_dict(v):
    return isinstance(v, dict) and v and all(isinstance(k, str) and LANG_RE.match(k) for k in v)


class Conflicts:
    def __init__(self):
        self.rows = []

    def add(self, code, field, a, b):
        self.rows.append((code, field, a, b))


def merge(a, b, code, path, conf):
    """Дополняет a данными из b. Строки из a сохраняются, пустое заполняется, длинный список побеждает."""
    for k, v in b.items():
        cur = a.get(k)
        if cur in (None, '', [], {}):
            a[k] = v
        elif isinstance(cur, dict) and isinstance(v, dict):
            merge(cur, v, code, path + [k], conf)
        elif isinstance(cur, list) and isinstance(v, list):
            if len(v) > len(cur):
                a[k] = v
        elif isinstance(cur, str) and isinstance(v, str) and cur != v and path and path[-1] == 'title' and k == 'en':
            conf.add(code, 'title.en', cur, v)
    return a


def rich_entry(o):
    code = norm_code(o.get('code', ''))
    e = OrderedDict([('code', code)])
    for k, v in o.items():
        if k == 'code':
            continue
        v = clean(v)
        if v not in (None, '', [], {}):
            e[k] = v
    return code, e


def plain_entry(code, text):
    text = clean(text)
    return code, OrderedDict([('code', code), ('title', OrderedDict([('en', text)]))])


def weight(v):
    """Сколько текстовых значений в записи: чем больше, тем она полнее."""
    if isinstance(v, str):
        return 1
    if isinstance(v, list):
        return sum(weight(x) for x in v)
    if isinstance(v, dict):
        return sum(weight(x) for x in v.values())
    return 0


def code_like(s):
    s = norm_code(s)
    return bool(OBD_RE.match(s) or HEX6_RE.match(s))


def pair_columns(d):
    """Для словаря из двух строк возвращает (индекс колонки с кодом) или None."""
    items = list(d.items())
    if len(items) != 2 or not all(isinstance(v, str) for _, v in items):
        return None
    c = [code_like(v) for _, v in items]
    if c[0] and not c[1]:
        return 0
    if c[1] and not c[0]:
        return 1
    return None


def extract_pairs(lst, out, stats):
    """Формат «код: описание» из таблицы. Первая строка таблицы могла стать ключами: восстанавливаем её."""
    header_done = False
    for d in lst:
        col = pair_columns(d) if isinstance(d, dict) else None
        if col is None:
            stats['skipped'] += 1
            continue
        keys, vals = list(d.keys()), list(d.values())
        if not header_done:
            header_done = True
            if code_like(keys[col]) and not code_like(keys[1 - col]):
                out.append(plain_entry(norm_code(keys[col]), keys[1 - col]))
                stats['header_rows'] += 1
        out.append(plain_entry(norm_code(vals[col]), vals[1 - col]))


def extract(obj, out, stats):
    if isinstance(obj, list):
        pairs = [x for x in obj if isinstance(x, dict) and pair_columns(x) is not None]
        if obj and len(pairs) >= 0.8 * len(obj):
            extract_pairs(obj, out, stats)
            return
        for x in obj:
            extract(x, out, stats)
    elif isinstance(obj, dict):
        if isinstance(obj.get('code'), (str, int)):
            out.append(rich_entry(obj))
            return
        keys = [k for k in obj if isinstance(k, str)]
        if keys and sum(1 for k in keys if code_like(k)) >= 0.5 * len(obj) and all(isinstance(v, (str, dict)) for v in obj.values()):
            for k, v in obj.items():
                if not code_like(k):
                    continue
                if isinstance(v, str):
                    out.append(plain_entry(norm_code(k), v))
                else:
                    out.append(rich_entry(OrderedDict([('code', k)] + list(v.items()))))
            return
        for v in obj.values():
            extract(v, out, stats)


def load_any(path):
    """Читает JSON; если файл не цельный (NDJSON, склеенные объекты), вытаскивает объекты с «code» по одному."""
    with open(path, encoding='utf-8-sig') as f:
        text = f.read()
    try:
        return json.loads(text, object_pairs_hook=OrderedDict)
    except ValueError:
        dec, found = json.JSONDecoder(object_pairs_hook=OrderedDict), []
        for m in re.finditer(r'\{\s*"code"\s*:', text):
            try:
                found.append(dec.raw_decode(text, m.start())[0])
            except ValueError:
                pass
        return found


def collect_files(inputs, skip):
    files = []
    for p in inputs:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                for n in sorted(names):
                    fp = os.path.join(root, n)
                    if JSON_NAME.search(n) and n.lower() not in SKIP_FILES and os.path.abspath(fp) not in skip:
                        files.append(fp)
        elif os.path.isfile(p):
            files.append(p)
        else:
            print('! нет такого пути: ' + p, file=sys.stderr)
    return files


def filter_langs(e, langs):
    if not langs:
        return e
    out = OrderedDict()
    for k, v in e.items():
        if is_lang_dict(v):
            kept = OrderedDict((l, x) for l, x in v.items() if l.split('-')[0] in langs)
            if not kept and k == 'title':       # заголовок нужен всегда: оставляем en или первый язык
                first = 'en' if 'en' in v else next(iter(v))
                kept = OrderedDict([(first, v[first])])
            if kept:
                out[k] = kept
        else:
            out[k] = v
    return out


def verify(path, expected):
    """Те же шаги, что у загрузчика в приложении: найти {"code":"XXXXX…"} и разобрать объект целиком."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    dec, n, bad = json.JSONDecoder(), 0, 0
    for m in APP_LOADER.finditer(text):
        try:
            dec.raw_decode(text, m.start())
            n += 1
        except ValueError:
            bad += 1
    return n == expected and bad == 0, n, bad


def main():
    ap = argparse.ArgumentParser(description='Собирает базы кодов в один txt для OBDnoPRO.')
    ap.add_argument('inputs', nargs='*', default=['.'], help='файлы или папки (по умолчанию текущая)')
    ap.add_argument('-o', '--output', default='dtc-db.txt', help='итоговый файл (по умолчанию dtc-db.txt)')
    ap.add_argument('--langs', default='', help='оставить только эти языки, например en,ru (по умолчанию все)')
    ap.add_argument('--report', default='', help='файл отчёта (по умолчанию <output>.report.txt)')
    a = ap.parse_args()

    out_path = os.path.abspath(a.output)
    report_path = os.path.abspath(a.report or a.output + '.report.txt')
    langs = {x.strip().lower() for x in a.langs.split(',') if x.strip()}
    files = collect_files(a.inputs, {out_path, report_path})
    if not files:
        sys.exit('Не найдено ни одного .json файла.')

    conf, lines, allrecs = Conflicts(), [], []
    for idx, fp in enumerate(files):
        stats = {'skipped': 0, 'header_rows': 0}
        recs = []
        try:
            extract(load_any(fp), recs, stats)
        except Exception as ex:                     # один битый файл не должен ронять сборку
            lines.append('! %s: ошибка чтения (%s)' % (fp, ex))
            continue
        good = 0
        for code, e in recs:
            if CODE_RE.match(code):
                good += 1
                allrecs.append((-weight(e), idx, code, e))
            else:
                stats['skipped'] += 1
        lines.append('%-40s записей: %6d, пропущено (плохой код): %d%s%s' % (
            os.path.basename(fp), good, stats['skipped'],
            ', восстановлена строка-заголовок: %d' % stats['header_rows'] if stats['header_rows'] else '',
            '  <- кодов не найдено, формат файла не распознан' if good == 0 else ''))

    # сначала самые полные записи (много языков и полей), чтобы они задавали основной текст
    db = OrderedDict()
    for _, _, code, e in sorted(allrecs, key=lambda r: (r[0], r[1])):
        if code in db:
            merge(db[code], e, code, [], conf)
        else:
            db[code] = e

    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
        for code in sorted(db):
            f.write(json.dumps(filter_langs(db[code], langs), ensure_ascii=False, separators=(',', ':')) + '\n')

    ok, n, bad = verify(out_path, len(db))
    nonstd = sum(1 for c in db if not OBD_RE.match(c))
    lines += ['', 'Итого уникальных кодов: %d (стандартных P/C/B/U: %d, прочих: %d)' % (len(db), len(db) - nonstd, nonstd),
              'Проверка по логике загрузчика приложения: %s (найдено %d, ошибок разбора %d)' % ('OK' if ok else 'НЕ ПРОЙДЕНА', n, bad),
              'Расхождений en-заголовков между источниками: %d' % len(conf.rows)]
    if conf.rows:
        lines += ['', 'Расхождения (оставлен текст из более полной записи):']
        lines += ['%s | %s | %s || %s' % r for r in conf.rows[:300]]
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

    print('\n'.join(lines[:len(files) + 4]))
    print('Файл: %s (%.1f МБ)\nОтчёт: %s' % (out_path, os.path.getsize(out_path) / 1048576, report_path))
    if not ok:
        sys.exit(1)


if __name__ == '__main__':
    main()
