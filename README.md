# hookforge-scripts

Small Python scripts for the data chores that are too fiddly to do by hand and
too small to justify a library. **No dependencies** — Python 3.9+ and the
standard library, nothing else. One file each. Public domain.

```bash
curl -O https://raw.githubusercontent.com/JDGj/hookforge-scripts/main/csvclean.py
python3 csvclean.py messy.csv --report
```

---

## csvclean.py

Turns a messy CSV or Excel export into a file a program can read: duplicates
gone, whitespace collapsed, dates in ISO, numbers with a decimal point,
headers you can use as variable names.

**Look before you leap.** `--report` changes nothing and tells you what is
wrong:

```
$ python3 csvclean.py customers.csv --report
customers.csv
  codificacao detectada : cp1252
  delimitador           : ';'
  linhas x colunas      : 4812 x 7
  ! linhas com largura errada: 3
  ! linhas em branco        : 41
  ! linhas duplicadas       : 128
  ! cabecalhos repetidos    : 1
  ! cabecalhos por normalizar: 7
    espacos a mais em: Nome Completo, Morada
    parecem datas: Data de Registo
    parecem numeros: Valor Pago
```

**Then fix it.**

```bash
# everything at once
python3 csvclean.py customers.csv -o clean.csv --all

# or pick your battles
python3 csvclean.py customers.csv -o clean.csv \
    --trim --drop-blank --clean-headers \
    --dedupe-on email \
    --dates --numbers --decimals 2 \
    --rename "E-Mail=email" --keep "email,nome_completo,valor_pago"
```

```
  3 linha(s) com largura errada ajustadas para 7 colunas
  41 linha(s) em branco removidas
  1 valor(es) nao reconhecidos como data, deixados como estavam
  128 duplicado(s) removidos (por email)
  4643 linha(s) x 3 coluna(s) -> clean.csv
```

### The parts that are easy to get wrong

Most twenty-line CSV cleaners are the same twenty lines. These are the bits
that make the difference on a real export:

**`1.234,56` and `1,234.56` are both read correctly — and `1.234` is not
guessed.** On its own, `1.234` is 1234 in Lisbon and 1.234 in Chicago, and no
amount of staring at it settles which. So the decimal mark is decided per
*column*, not per value: a column almost always tells on itself somewhere —
one value with both separators, or one with the same separator twice — and
that governs the rest of the column. A cleaner that gets this wrong turns a
€1.234 line item into €1.23 in a total, silently.

**Nothing is converted quietly.** A value that does not parse as a date is
left exactly as it arrived, and counted in the summary. You find the three bad
rows now, not in the invoice run.

**`--dates` only touches columns that are mostly dates.** Pointing a date
parser at a column of names corrupts nothing, but it reports every name as a
failure, and a warning that counts 19 problems when one value is wrong is a
warning nobody reads twice.

**Encoding is tried, not assumed.** An export from Excel on a Portuguese
Windows is usually cp1252 and says so nowhere. Opening it as UTF-8 either
crashes or — worse — silently mojibakes every accented name.

**The delimiter is sniffed outside quotes.** Excel in most of Europe writes
semicolons, and a comma inside `"Lisboa, Portugal"` must not get a vote.

**Ragged rows are repaired, not dropped.** A row missing one trailing field is
almost always still the row you wanted.

### Everything it does

| Flag | What it does |
|---|---|
| `--report` | Say what is wrong, change nothing |
| `--all` | `--trim --drop-blank --dedupe --clean-headers --dates --numbers` |
| `--trim` | Collapse whitespace, including non-breaking spaces |
| `--drop-blank` | Remove empty rows |
| `--dedupe` | Remove identical rows |
| `--dedupe-on COLS` | Remove rows matching on these columns only (case/space-insensitive) |
| `--clean-headers` | `E-Mail Address` → `e_mail_address`; duplicates get a suffix |
| `--rename DE=PARA` | Rename a column; repeatable |
| `--keep COLS` | Keep only these columns, in this order |
| `--dates` | Dates to `YYYY-MM-DD` (day-first; `--month-first` for US) |
| `--numbers` | Numbers to a plain decimal point (`--decimals N`) |
| `--delimiter` | Force the output delimiter |

`python3 csvclean.py --help` for the rest. `python3 csvclean.py --selftest`
runs the test suite — no network, no files, about a second.

### What it deliberately does not do

- **Excel files directly.** `.xlsx` is a zip of XML and reading it properly
  needs a library, which would break the no-dependencies promise. Save as CSV
  first.
- **Fuzzy matching.** "Ana Silva" and "Ana Sliva" stay two people. Guessing
  which near-duplicates are the same person is a judgement call, and a script
  that makes it for you will one day merge two real customers.
- **Anything in place.** It never writes over your input.

---

## Coming next

- `listscrape.py` — pull a repeated list (prices, listings, contacts) off a
  page into CSV, standard library only.
- `hookbridge.py` — receive a webhook and forward it somewhere else, with
  field mapping.

Both exist as working sketches today; they are not here yet because they are
not yet as careful as the one above. Watch the repo, or open an issue if you
want one sooner.

---

## Licence

CC0 1.0 — public domain. Copy it, change it, ship it in something you sell, no
attribution needed. See [LICENSE](LICENSE).

## Who makes this

[HookForge](https://hookforge.dev) — fixed-price automation scripts, written
to order, delivered in 48 hours. These free scripts are the generic half of
that: they solve the common shape of the problem, and they always will,
without us.

If your file is the awkward one — the one where the dates are in three
different formats and one column is a JSON blob — that is the paid half, at a
fixed €49 with no quoting and no meetings. But try this first. It may be all
you needed.

There is no tracking in these scripts, no telemetry, and no call home. They
work offline and will keep working if this repository disappears.
