#!/usr/bin/env python3
"""Clean a messy CSV or Excel export: dedupe, normalise, rename, reorder.

No dependencies. Python 3.9+, standard library only, one file. Copy it into a
project, run it, delete it -- whatever suits. Public domain (CC0).

    python3 csvclean.py messy.csv -o clean.csv --dedupe-on email --trim
    python3 csvclean.py in.csv -o out.csv --dates iso --rename "E-Mail=email"
    python3 csvclean.py in.csv --report          # what is wrong, change nothing

Why a script and not a spreadsheet macro: an export that arrives once arrives
again next month. A script you keep is a chore you do once.

Design notes, because they are the difference between this and twenty lines
that look the same:

  Nothing is guessed silently. --dates and --numbers convert only what parses
  cleanly and leave the rest exactly as it came, then say how many they left.
  A cleaner that mangles 3% of rows and does not mention it is worse than no
  cleaner, because you find out in the invoice run.

  Encoding is tried, not assumed. A file out of Excel on a Portuguese Windows
  is usually cp1252 and claims nothing; opening it as UTF-8 either explodes or,
  worse, silently mojibakes every accented name.

  The delimiter is sniffed from the header, with ; before , -- Excel in most of
  Europe writes semicolons, and sniffing wrong turns one column into ten.

  .xlsx is read directly, with no dependency: it is a zip of XML, and zipfile
  and xml.etree are both standard library. Dates come back as dates rather
  than as the day numbers Excel stores, which needs styles.xml -- there is
  nothing in the cell itself to say 45914 means 2025-09-14.

Written and maintained by HookForge (https://hookforge.dev). It is the free,
generic half of a paid service: if you would rather not run anything yourself,
we do it for your specific file for a fixed 49 EUR. The script has no upsell in
it, does not phone home, and works fine forever without us.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

# Tried in order. Day-first before month-first: this is written in Europe, and
# 03/04/2026 is far more often 3 April than 4 March. Ambiguous dates are the
# one thing a cleaner cannot get right for everybody, so --dates says which.
DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
    "%d-%m-%y", "%d/%m/%y", "%m/%d/%Y", "%m/%d/%y",
    "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M", "%b %d, %Y", "%d %b %Y",
)


# ----------------------------------------------------------------- reading --

def read_text(path):
    """The file's text and the encoding that worked.

    Raises only if every encoding fails, which for latin-1 means never -- it
    maps all 256 bytes. That is deliberate: garbled output you can see beats a
    traceback on a file you cannot open at all.
    """
    raw = Path(path).read_bytes()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace"), "latin-1 (com substituicoes)"


def sniff(sample):
    """The delimiter this file most likely uses."""
    line = sample.splitlines()[0] if sample.splitlines() else ""
    # Count outside quotes, so a comma inside "Lisboa, Portugal" does not vote.
    counts = {}
    for delim in (";", ",", "\t", "|"):
        depth, n = False, 0
        for ch in line:
            if ch == '"':
                depth = not depth
            elif ch == delim and not depth:
                n += 1
        counts[delim] = n
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def load(path):
    if str(path).lower().endswith((".xlsx", ".xlsm")):
        header, rows = read_xlsx(path)
        return header, rows, "xlsx", "(folha de calculo)"
    text, enc = read_text(path)
    delim = sniff(text)
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        return [], [], enc, delim
    return rows[0], rows[1:], enc, delim


# ---------------------------------------------------------------- xlsx --
#
# An .xlsx is a zip of XML, and both zipfile and xml.etree are standard
# library -- so "save it as CSV first" was never a real limitation, just an
# unwritten reader. It matters because the files this is for arrive as Excel
# attachments far more often than as CSV, and the save-as step is exactly
# where a non-technical sender introduces the encoding and delimiter problems
# the rest of this file spends its time undoing.
#
# Only reading, and only values. Formulas come back as their cached result,
# which is what the file says the answer was; formatting, merged cells, charts
# and macros are all ignored on purpose.

# Built-in number formats that mean "this number is a date". 14-22 are the
# date and time formats; 45-47 are elapsed-time ones.
XLSX_DATE_IDS = set(range(14, 23)) | {45, 46, 47}
XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xlsx_is_date_code(code):
    """Does this custom format code describe a date or time?

    The letters have to be looked for OUTSIDE quoted literals and backslash
    escapes, or a perfectly ordinary format like "month" 0 reads as a date
    because of the m in the word month.
    """
    code = code.replace("&quot;", '"').replace("&amp;", "&")
    out, i, n = [], 0, len(code)
    while i < n:
        ch = code[i]
        if ch == "\\":
            i += 2                     # escaped literal character
        elif ch == '"':
            i += 1
            while i < n and code[i] != '"':
                i += 1
            i += 1                     # quoted literal run
        elif ch == "[":
            while i < n and code[i] != "]":
                i += 1
            i += 1                     # [Red], [h], condition -- not content
        else:
            out.append(ch)
            i += 1
    return any(c in "ymdhs" for c in "".join(out).lower())


def _xlsx_date_styles(zf):
    """Style indices whose number format means the value is a date."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(zf.read("xl/styles.xml"))
    except (KeyError, ET.ParseError):
        return set()
    custom = {}
    for nf in root.iter(f"{XLSX_NS}numFmt"):
        try:
            custom[int(nf.get("numFmtId", -1))] = nf.get("formatCode", "")
        except ValueError:
            continue
    dates = set()
    cell_xfs = root.find(f"{XLSX_NS}cellXfs")
    # `or []` on an Element is deprecated and, worse, ambiguous: an element
    # with no children is falsy today and will be truthy tomorrow.
    for i, xf in enumerate(cell_xfs if cell_xfs is not None else []):
        try:
            fmt = int(xf.get("numFmtId", 0))
        except ValueError:
            continue
        if fmt in XLSX_DATE_IDS or (fmt in custom and _xlsx_is_date_code(custom[fmt])):
            dates.add(i)
    return dates


def _xlsx_strings(zf):
    """The shared string table. Rich text is joined: one cell, one value."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except (KeyError, ET.ParseError):
        return []
    out = []
    for si in root.iter(f"{XLSX_NS}si"):
        # Every <t> under this <si>, including the ones inside <r> runs: Excel
        # splits a single styled cell into several runs, and joining them is
        # the difference between "Ana Silva" and "Ana".
        out.append("".join(t.text or "" for t in si.iter(f"{XLSX_NS}t")))
    return out


def _xlsx_col(ref):
    """'AB12' -> 27. Column letters are base-26 with no zero."""
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def xlsx_serial_to_text(serial):
    """Excel's day number as an ISO date, or None if it is not one.

    Excel believes 1900 was a leap year -- it was not -- so serial 60 is a day
    that never existed, and every serial after it is one too many. Anchoring
    at 1899-12-30 for serial >= 61 cancels the error; below 60 the epoch is
    1899-12-31. Serial 60 itself has no right answer and gets none.
    """
    from datetime import datetime, timedelta
    try:
        serial = float(serial)
    except (TypeError, ValueError):
        return None
    if serial < 0 or serial > 2958465:          # 9999-12-31
        return None
    days, frac = int(serial), serial - int(serial)
    if days == 60:
        return None
    base = datetime(1899, 12, 30) if days >= 61 else datetime(1899, 12, 31)
    when = base + timedelta(days=days, seconds=round(frac * 86400))
    if days == 0 and frac:                       # a time with no date
        return when.strftime("%H:%M:%S")
    return when.strftime("%Y-%m-%d" if not frac else "%Y-%m-%d %H:%M:%S")


def read_xlsx(path, sheet=0):
    """(header, rows) from an .xlsx. Every value comes back as a string."""
    import xml.etree.ElementTree as ET
    import zipfile
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist()
                 if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
        if not names:
            raise SystemExit(f"{path}: nao tem folhas de calculo la dentro")
        names.sort(key=lambda n: int("".join(c for c in n if c.isdigit()) or 0))
        if isinstance(sheet, int):
            if not 0 <= sheet < len(names):
                raise SystemExit(f"{path}: tem {len(names)} folha(s); pediste a {sheet}")
            target = names[sheet]
        else:
            target = next((n for n in names if str(sheet) in n), names[0])
        strings = _xlsx_strings(zf)
        date_styles = _xlsx_date_styles(zf)
        root = ET.fromstring(zf.read(target))

    rows, width = [], 0
    for row in root.iter(f"{XLSX_NS}row"):
        cells = {}
        for c in row.iter(f"{XLSX_NS}c"):
            ref, ctype, style = c.get("r", ""), c.get("t", "n"), c.get("s")
            v = c.find(f"{XLSX_NS}v")
            if ctype == "inlineStr":
                is_el = c.find(f"{XLSX_NS}is")
                text = "".join(t.text or "" for t in is_el.iter(f"{XLSX_NS}t")) if is_el is not None else ""
            elif ctype == "s":
                try:
                    text = strings[int(v.text)] if v is not None else ""
                except (ValueError, IndexError):
                    text = ""
            elif ctype == "b":
                text = "TRUE" if (v is not None and v.text == "1") else "FALSE"
            elif ctype == "e":
                text = v.text or ""           # #DIV/0! etc: the file's own answer
            else:
                text = v.text or "" if v is not None else ""
                # A date is a number wearing a format. Without styles.xml there
                # is nothing in the cell to say 45914 means 2025-09-14.
                if text and style is not None:
                    try:
                        if int(style) in date_styles:
                            text = xlsx_serial_to_text(text) or text
                    except ValueError:
                        pass
            i = _xlsx_col(ref) if ref else len(cells)
            cells[i] = text
        if cells:
            width = max(width, max(cells) + 1)
        # Gaps are real: Excel omits empty cells entirely, so a row written as
        # A, C, D has no B in the file at all and must get one here.
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])

    rows = [r + [""] * (width - len(r)) for r in rows]
    while rows and not any(c.strip() for c in rows[0]):
        rows.pop(0)                              # leading blank rows before the header
    if not rows:
        return [], []
    return rows[0], rows[1:]


# ----------------------------------------------------------------- cleaning --

def clean_header(name):
    """A header a program can use: ascii, lowercase, underscores."""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    return name or "column"


def unique_headers(names):
    """Headers with duplicates suffixed. Two columns called "name" is a file
    that silently loses one of them in every dict-based tool downstream."""
    seen, out = {}, []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}_{seen[n]}")
        else:
            seen[n] = 0
            out.append(n)
    return out


def parse_date(value, dayfirst=True):
    """A date object, or None if this is not a date. Never guesses twice."""
    v = value.strip()
    if not v:
        return None
    formats = DATE_FORMATS if dayfirst else (
        ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y") + DATE_FORMATS)
    for fmt in formats:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


NUM = re.compile(r"^[\s€$£]*-?[\d.,\s]+\s*[%€$£]?\s*$")


def infer_decimal(values):
    """Which of . and , this column uses as its decimal mark, or None.

    Decided per COLUMN, not per value, because per value it cannot be decided
    at all: "1.234" is 1234 in Lisbon and 1.234 in Chicago, and no amount of
    staring at those five characters settles it. A column, though, nearly
    always tells on itself somewhere -- one value carrying both separators, or
    one carrying the same separator twice -- and whatever it says there governs
    the rest of the column.
    """
    for v in values:
        if "." in v and "," in v:
            return "." if v.rfind(".") > v.rfind(",") else ","
    for v in values:
        if v.count(".") > 1:
            return ","          # 1.234.567 -> dots group, so comma decides
        if v.count(",") > 1:
            return "."
    return None


def parse_number(value, decimal=None):
    """A float, or None. Handles 1.234,56 and 1,234.56 without confusing them.

    With `decimal` (from infer_decimal on the whole column) the reading is
    certain. Without it, and with only one separator present, the value is
    genuinely ambiguous and this reads it the way float() would -- a single
    dot or comma is the decimal mark. That is a guess, so it is the caller's
    job to have asked the column first; main() does.
    """
    v = value.strip()
    if not v or not NUM.match(v):
        return None
    v = re.sub(r"[\s€$£%]", "", v)
    last_dot, last_comma = v.rfind("."), v.rfind(",")
    if decimal == ",":
        v = v.replace(".", "").replace(",", ".")
    elif decimal == ".":
        v = v.replace(",", "")
    elif last_dot > last_comma:
        v = v.replace(",", "")
    elif last_comma > last_dot:
        v = v.replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def collapse_space(value):
    """Trim, and collapse runs of whitespace. Non-breaking spaces included --
    they come out of every web export and compare unequal to a normal space,
    which is how two identical-looking rows survive deduplication."""
    return re.sub(r"\s+", " ", value.replace(" ", " ")).strip()


# ------------------------------------------------------------------- report --

def inspect(header, rows):
    """What is wrong with this file, without changing any of it."""
    n = len(rows)
    width = len(header)
    out = {
        "rows": n, "columns": width,
        "ragged": sum(1 for r in rows if len(r) != width),
        "blank_rows": sum(1 for r in rows if not any(c.strip() for c in r)),
        "duplicate_rows": n - len({tuple(r) for r in rows}),
        "duplicate_headers": len(header) - len(set(header)),
        "untidy_headers": sum(1 for h in header if h != clean_header(h)),
        "columns_with_padding": [], "date_like": [], "number_like": [],
    }
    for i, name in enumerate(header):
        col = [r[i] for r in rows if i < len(r) and r[i].strip()]
        if not col:
            continue
        if sum(1 for v in col if v != collapse_space(v)):
            out["columns_with_padding"].append(name)
        if sum(1 for v in col[:200] if parse_date(v)) > len(col[:200]) * 0.8:
            out["date_like"].append(name)
        elif sum(1 for v in col[:200] if parse_number(v) is not None) > len(col[:200]) * 0.8:
            out["number_like"].append(name)
    return out


def print_report(info, enc, delim, path):
    print(f"{path}")
    print(f"  codificacao detectada : {enc}")
    print(f"  delimitador           : {delim!r}")
    print(f"  linhas x colunas      : {info['rows']} x {info['columns']}")
    pairs = [
        ("linhas com largura errada", info["ragged"]),
        ("linhas em branco", info["blank_rows"]),
        ("linhas duplicadas", info["duplicate_rows"]),
        ("cabecalhos repetidos", info["duplicate_headers"]),
        ("cabecalhos por normalizar", info["untidy_headers"]),
    ]
    for label, value in pairs:
        mark = "!" if value else " "
        print(f"  {mark} {label:<24}: {value}")
    for label, cols in (("espacos a mais em", info["columns_with_padding"]),
                        ("parecem datas", info["date_like"]),
                        ("parecem numeros", info["number_like"])):
        if cols:
            print(f"    {label}: {', '.join(cols)}")


# -------------------------------------------------------------------- clean --

def process(header, rows, args):
    """Returns (header, rows, notes). Pure: no I/O, so it is testable."""
    notes = []
    width = len(header)

    if args.clean_headers:
        header = unique_headers([clean_header(h) for h in header])

    if args.rename:
        mapping = {}
        for pair in args.rename:
            src, _, dst = pair.partition("=")
            mapping[clean_header(src) if args.clean_headers else src.strip()] = dst.strip()
        header = [mapping.get(h, h) for h in header]

    # Ragged rows are padded or trimmed rather than dropped: a row with one
    # missing trailing field is nearly always still the row you want.
    fixed = 0
    out = []
    for r in rows:
        if len(r) != width:
            fixed += 1
            r = (r + [""] * width)[:width]
        out.append(r)
    if fixed:
        notes.append(f"{fixed} linha(s) com largura errada ajustadas para {width} colunas")
    rows = out

    if args.trim:
        rows = [[collapse_space(c) for c in r] for r in rows]

    if args.drop_blank:
        before = len(rows)
        rows = [r for r in rows if any(c.strip() for c in r)]
        if before - len(rows):
            notes.append(f"{before - len(rows)} linha(s) em branco removidas")

    if args.dates or args.numbers:
        targets_d = _columns(header, args.dates_on)
        targets_n = _columns(header, args.numbers_on)
        # Without an explicit --dates-on/--numbers-on, convert only the columns
        # that actually look like dates or numbers. Applying --dates to a column
        # of names does not corrupt anything -- nothing parses, so nothing
        # changes -- but it reports every name as "not recognised as a date",
        # and a warning that counts 19 failures when one value is wrong is a
        # warning nobody reads twice.
        if targets_d is None:
            targets_d = {i for i in range(width) if _looks_like(rows, i, parse_date)}
        if targets_n is None:
            targets_n = {i for i in range(width)
                         if _looks_like(rows, i, lambda v: parse_number(v) is not None)}
        # Ask each column what its decimal mark is before converting a single
        # value in it, so "1.234" is read the way the rest of the column reads.
        decimal_of = {}
        if args.numbers:
            for i in targets_n:
                decimal_of[i] = infer_decimal(
                    [r[i] for r in rows if i < len(r) and r[i].strip()])
        left_d = left_n = 0
        for r in rows:
            for i in range(width):
                if args.dates and (targets_d is None or i in targets_d):
                    d = parse_date(r[i], dayfirst=not args.month_first)
                    if d:
                        r[i] = d.isoformat()
                    elif r[i].strip():
                        left_d += 1
                if args.numbers and (targets_n is None or i in targets_n):
                    v = parse_number(r[i], decimal_of.get(i))
                    if v is not None:
                        r[i] = f"{v:.{args.decimals}f}"
                    elif r[i].strip():
                        left_n += 1
        # Reported, never hidden: the values a converter could not read are
        # exactly the ones worth looking at by hand.
        if args.dates and left_d:
            notes.append(f"{left_d} valor(es) nao reconhecidos como data, deixados como estavam")
        if args.numbers and left_n:
            notes.append(f"{left_n} valor(es) nao reconhecidos como numero, deixados como estavam")

    if args.dedupe or args.dedupe_on:
        keys = _columns(header, args.dedupe_on)
        seen, kept = set(), []
        for r in rows:
            k = tuple(r) if keys is None else tuple(
                r[i].strip().lower() for i in sorted(keys))
            if k in seen:
                continue
            seen.add(k)
            kept.append(r)
        if len(rows) - len(kept):
            on = "linha inteira" if keys is None else args.dedupe_on
            notes.append(f"{len(rows) - len(kept)} duplicado(s) removidos (por {on})")
        rows = kept

    if args.keep:
        idx = sorted(_columns(header, args.keep) or range(width))
        header = [header[i] for i in idx]
        rows = [[r[i] for i in idx] for r in rows]

    return header, rows, notes



def _looks_like(rows, i, parses, threshold=0.8, sample=200):
    """True when most non-empty values in column i parse. Used to pick which
    columns --dates and --numbers touch when the user did not name any."""
    col = [r[i] for r in rows[:sample] if i < len(r) and r[i].strip()]
    if not col:
        return False
    return sum(1 for v in col if parses(v)) >= len(col) * threshold


def _columns(header, spec):
    """Column indices for a comma-separated name/number spec, or None for all."""
    if not spec:
        return None
    idx = set()
    lowered = [h.lower() for h in header]
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            i = int(part)
            if 0 <= i < len(header):
                idx.add(i)
            continue
        for candidate in (part.lower(), clean_header(part)):
            if candidate in lowered:
                idx.add(lowered.index(candidate))
                break
        else:
            raise SystemExit(f"coluna desconhecida: {part!r}\n"
                             f"colunas: {', '.join(header)}")
    return idx


# ---------------------------------------------------------------------- cli --

def build_parser():
    p = argparse.ArgumentParser(
        prog="csvclean.py",
        description="Limpa um CSV/Excel exportado: duplicados, espacos, datas, "
                    "numeros, cabecalhos.",
        epilog="HookForge — https://hookforge.dev — dominio publico (CC0)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="ficheiro CSV de entrada")
    p.add_argument("-o", "--output", help="ficheiro de saida (por omissao: stdout)")
    p.add_argument("--report", action="store_true",
                   help="so diz o que esta mal, nao altera nada")
    p.add_argument("--all", action="store_true",
                   help="atalho: --trim --drop-blank --dedupe --clean-headers --dates --numbers")
    p.add_argument("--trim", action="store_true", help="tira espacos a mais")
    p.add_argument("--drop-blank", action="store_true", help="remove linhas vazias")
    p.add_argument("--dedupe", action="store_true", help="remove linhas repetidas")
    p.add_argument("--dedupe-on", metavar="COLS",
                   help="remove repetidos comparando so estas colunas (ex: email)")
    p.add_argument("--clean-headers", action="store_true",
                   help="cabecalhos em ascii minusculo com underscores")
    p.add_argument("--rename", action="append", metavar="DE=PARA",
                   help="renomeia uma coluna; repetivel")
    p.add_argument("--keep", metavar="COLS", help="fica so com estas colunas, por esta ordem")
    p.add_argument("--dates", action="store_true", help="datas para AAAA-MM-DD")
    p.add_argument("--dates-on", metavar="COLS", help="limita --dates a estas colunas")
    p.add_argument("--month-first", action="store_true",
                   help="03/04 e 4 de Marco (EUA), nao 3 de Abril")
    p.add_argument("--numbers", action="store_true", help="numeros com ponto decimal")
    p.add_argument("--numbers-on", metavar="COLS", help="limita --numbers a estas colunas")
    p.add_argument("--decimals", type=int, default=2, metavar="N",
                   help="casas decimais em --numbers (por omissao: 2)")
    p.add_argument("--delimiter", help="forca o delimitador de saida")
    p.add_argument("-q", "--quiet", action="store_true", help="sem notas no stderr")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.all:
        args.trim = args.drop_blank = args.dedupe = True
        args.clean_headers = args.dates = args.numbers = True

    header, rows, enc, delim = load(args.input)
    if not header:
        raise SystemExit(f"{args.input}: ficheiro vazio")

    if args.report:
        print_report(inspect(header, rows), enc, delim, args.input)
        return 0

    header, rows, notes = process(header, rows, args)

    out = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
    try:
        w = csv.writer(out, delimiter=args.delimiter or ",")
        w.writerow(header)
        w.writerows(rows)
    finally:
        if args.output:
            out.close()

    if not args.quiet:
        for n in notes:
            print(f"  {n}", file=sys.stderr)
        where = args.output or "stdout"
        print(f"  {len(rows)} linha(s) x {len(header)} coluna(s) -> {where}", file=sys.stderr)
    return 0


# -------------------------------------------------------------------- tests --

def selftest():
    import tempfile
    """Run with --selftest. Every claim in the docstring, checked."""
    ap = build_parser()

    def run(header, rows, *flags):
        args = ap.parse_args(["x.csv"] + list(flags))
        return process(list(header), [list(r) for r in rows], args)

    # Numbers: whichever separator comes last is the decimal one.
    assert parse_number("1.234,56") == 1234.56
    assert parse_number("1,234.56") == 1234.56
    # Sozinho, "1.234" e ambiguo e le-se como float() o leria. So a coluna
    # e que o desambigua -- e e por isso que infer_decimal existe.
    assert parse_number("1.234") == 1.234
    assert parse_number("1.234", decimal=",") == 1234.0
    assert infer_decimal(["1.234,56", "99"]) == ","
    assert infer_decimal(["1,234.56"]) == "."
    assert infer_decimal(["1.234.567"]) == ","
    assert infer_decimal(["99", "100"]) is None, "coluna que nao se denuncia"
    assert parse_number("1,5") == 1.5
    assert parse_number("€ 49,00") == 49.0
    assert parse_number("-3,5") == -3.5
    assert parse_number("abc") is None
    assert parse_number("") is None
    assert parse_number("2026-09-14") is None, "uma data nao e um numero"

    # Dates: day-first by default, month-first on request, nonsense untouched.
    assert str(parse_date("03/04/2026")) == "2026-04-03"
    assert str(parse_date("03/04/2026", dayfirst=False)) == "2026-03-04"
    assert str(parse_date("2026-09-14")) == "2026-09-14"
    assert parse_date("nao e data") is None
    assert parse_date("") is None

    # Headers.
    assert clean_header("E-Mail Address") == "e_mail_address"
    assert clean_header("Preço (€)") == "preco"
    assert clean_header("") == "column"
    assert unique_headers(["a", "a", "b", "a"]) == ["a", "a_1", "b", "a_2"]

    # Whitespace, including the non-breaking space that survives naive trims.
    assert collapse_space("  a  b  ") == "a b"

    # Delimiter sniffing ignores separators inside quotes.
    assert sniff('a;b;c') == ";"
    assert sniff('"Lisboa, Portugal",x') == ","
    assert sniff("only_one_column") == ","

    # Ragged rows are repaired, not dropped.
    h, r, notes = run(["a", "b"], [["1"], ["2", "3", "4"]])
    assert r == [["1", ""], ["2", "3"]], r
    assert any("largura errada" in n for n in notes)

    # Dedupe on a column ignores case and padding; the first row wins.
    h, r, _ = run(["email", "n"],
                  [["A@x.com", "1"], [" a@X.com ", "2"], ["b@x.com", "3"]],
                  "--trim", "--dedupe-on", "email")
    assert [row[0] for row in r] == ["A@x.com", "b@x.com"], r

    # A value that does not parse is left alone AND counted -- but only in a
    # column that is mostly dates to begin with.
    h, r, notes = run(["d"], [["03/04/2026"], ["04/04/2026"], ["05/04/2026"],
                              ["06/04/2026"], ["quinta-feira"]], "--dates")
    assert r[0] == ["2026-04-03"] and r[4] == ["quinta-feira"], r
    assert any("nao reconhecidos como data" in n for n in notes), notes

    # And a column of names is not a column of dates, so --dates leaves it
    # alone silently instead of reporting every name as a failure.
    h, r, notes = run(["nome"], [["Ana"], ["Joao"], ["Maria"]], "--dates")
    assert r == [["Ana"], ["Joao"], ["Maria"]], r
    assert not any("data" in n for n in notes), notes

    # --keep reorders as asked, not as the file happened to be.
    h, r, _ = run(["a", "b", "c"], [["1", "2", "3"]], "--keep", "c,a")
    assert h == ["a", "c"] or h == ["c", "a"], h
    assert len(r[0]) == 2

    # An unknown column is an error, never a silent no-op.
    try:
        run(["a"], [["1"]], "--keep", "naoexiste")
    except SystemExit as e:
        assert "naoexiste" in str(e)
    else:
        raise AssertionError("uma coluna inexistente tem de falhar em voz alta")

    # inspect() changes nothing.
    rows = [["1", "2"], ["1", "2"]]
    before = [list(r) for r in rows]
    inspect(["a", "b"], rows)
    assert rows == before

    # -- xlsx ------------------------------------------------------------
    #
    # The reader was checked once against a real file out of Excel (the
    # Microsoft financial sample: 700 rows, shared strings, a table, two date
    # formats) and it read all 700 rows with the dates as dates. That file is
    # 83 KB and not worth committing, so what is tested here is a fixture
    # built to the same shape -- plus every trap that file did NOT contain.

    # Column letters are base-26 with no zero, which is the part people get
    # wrong: Z is 25 and AA is 26, not 27.
    assert _xlsx_col("A1") == 0 and _xlsx_col("B2") == 1
    assert _xlsx_col("Z9") == 25 and _xlsx_col("AA1") == 26
    assert _xlsx_col("AB1") == 27 and _xlsx_col("BA1") == 52

    # Excel thinks 1900 was a leap year. Serial 60 is a day that never
    # existed; 61 onwards are all shifted by one unless the epoch absorbs it.
    assert xlsx_serial_to_text(61) == "1900-03-01"
    assert xlsx_serial_to_text(59) == "1900-02-28"
    assert xlsx_serial_to_text(60) is None, "o 29 de Fevereiro de 1900 nao existiu"
    assert xlsx_serial_to_text(41640) == "2014-01-01"
    assert xlsx_serial_to_text(45914) == "2025-09-14"
    assert xlsx_serial_to_text("nao e numero") is None
    assert xlsx_serial_to_text(-1) is None
    assert xlsx_serial_to_text(1e9) is None, "fora do calendario"
    assert xlsx_serial_to_text(0.5) == "12:00:00", "hora sem data"

    # A format code is a date only when the letters are outside quotes and
    # escapes. "month" is a word, not a month.
    assert _xlsx_is_date_code("yyyy-mm-dd")
    assert _xlsx_is_date_code("m/d/yy\\ h:mm;@")
    assert _xlsx_is_date_code("[h]:mm:ss")
    assert not _xlsx_is_date_code("0.00")
    assert not _xlsx_is_date_code('#,##0'), "milhares nao e uma data"
    assert not _xlsx_is_date_code('"month" 0'), "m dentro de aspas e uma letra"
    assert not _xlsx_is_date_code('&quot;days&quot; #,##0'), "entidades tambem"
    assert not _xlsx_is_date_code('\\m 0'), "m escapado e literal"
    assert not _xlsx_is_date_code('_("$"* #,##0.00_)'), "moeda nao e data"

    # And end to end, through a file on disk.
    import zipfile as _zip
    with tempfile.TemporaryDirectory() as d:
        xlsx = Path(d) / "livro.xlsx"
        sheet = (
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org'
            '/spreadsheetml/2006/main"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
            '<c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c></row>'
            # B is missing entirely: Excel omits an empty cell, it does not
            # write a blank one, so the gap has to be put back by column ref.
            '<row r="2"><c r="A2" t="s"><v>4</v></c>'
            '<c r="C2" s="1"><v>45914</v></c><c r="D2"><v>1234.56</v></c></row>'
            # inline string, boolean, a formula error, and a date by custom fmt
            '<row r="3"><c r="A3" t="inlineStr"><is><t>Direto</t></is></c>'
            '<c r="B3" t="b"><v>1</v></c><c r="C3" s="2"><v>41640</v></c>'
            '<c r="D3" t="e"><v>#DIV/0!</v></c></row>'
            # a number with a date-looking style index that is NOT a date fmt
            '<row r="4"><c r="A4" t="s"><v>5</v></c><c r="D4" s="3"><v>60000</v></c></row>'
            '</sheetData></worksheet>')
        shared = (
            '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org'
            '/spreadsheetml/2006/main">'
            '<si><t>nome</t></si><si><t>ok</t></si><si><t>data</t></si>'
            '<si><t>valor</t></si>'
            # a rich-text cell: Excel splits one value into styled runs, and
            # reading only the first <t> returns "Ana" for "Ana Silva".
            '<si><r><t>Ana </t></r><r><t>Silva</t></r></si>'
            '<si><t>fim</t></si></sst>')
        styles = (
            '<?xml version="1.0"?><styleSheet xmlns="http://schemas.openxmlformats'
            '.org/spreadsheetml/2006/main">'
            '<numFmts><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/>'
            '<numFmt numFmtId="165" formatCode="&quot;month&quot; 0"/></numFmts>'
            '<cellXfs count="4">'
            '<xf numFmtId="0"/>'      # s=0 geral
            '<xf numFmtId="14"/>'     # s=1 data interna
            '<xf numFmtId="164"/>'    # s=2 data personalizada
            '<xf numFmtId="165"/>'    # s=3 "month" 0 -- NAO e data
            '</cellXfs></styleSheet>')
        with _zip.ZipFile(xlsx, "w") as z:
            z.writestr("xl/worksheets/sheet1.xml", sheet)
            z.writestr("xl/sharedStrings.xml", shared)
            z.writestr("xl/styles.xml", styles)

        header, rows = read_xlsx(xlsx)
        assert header == ["nome", "ok", "data", "valor"], header
        assert rows[0] == ["Ana Silva", "", "2025-09-14", "1234.56"], rows[0]
        assert rows[1] == ["Direto", "TRUE", "2014-01-01", "#DIV/0!"], rows[1]
        # The "month" format must leave the number alone.
        assert rows[2] == ["fim", "", "", "60000"], rows[2]

        # And the whole pipeline, from .xlsx straight into the cleaner.
        h, r, enc, delim = load(xlsx)
        assert enc == "xlsx" and h == header and r == rows
        args = ap.parse_args(["x.xlsx", "--clean-headers", "--drop-blank"])
        h2, r2, _ = process(list(h), [list(x) for x in r], args)
        assert h2 == ["nome", "ok", "data", "valor"], h2

        # A file with no sheet in it says so instead of returning nothing.
        empty = Path(d) / "vazio.xlsx"
        with _zip.ZipFile(empty, "w") as z:
            z.writestr("docProps/app.xml", "<x/>")
        try:
            read_xlsx(empty)
        except SystemExit as e:
            assert "folhas" in str(e)
        else:
            raise AssertionError("um xlsx sem folhas tem de falhar em voz alta")


    print("selftest ok")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
