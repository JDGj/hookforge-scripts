# hookforge-scripts

Small Python scripts for the data chores that are too fiddly to do by hand and
too small to justify a library. **No dependencies** — Python 3.9+ and the
standard library, nothing else. One file each. Public domain.

```bash
curl -O https://raw.githubusercontent.com/JDGj/hookforge-scripts/main/csvclean.py
python3 csvclean.py messy.csv --report
```

| | |
|---|---|
| [`csvclean.py`](#csvcleanpy) | Clean a messy CSV or Excel export |
| [`listscrape.py`](#listscrapepy) | Pull a repeated list off a page into CSV |

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

## listscrape.py

Pulls a repeated list — products, listings, contacts, search results — off a
page into CSV.

```bash
# tell it what an item is, and what you want from each one
python3 listscrape.py https://example.com/shop \
    --select ".product" \
    --field "name=h3" --field "price=.price" --field "url=a@href"
```

```
  https://example.com/shop: 3 item(s), 3 coluna(s)
name,price,url
Data Cleanup Script,49€ one-time,https://example.com/p/clean
Website Scraper,79€ one-time,https://example.com/p/scrape
API / Webhook Bridge,99€ one-time,https://example.com/p/bridge
```

`--auto` guesses the repeating block and the columns, which is useful for a
first look at an unfamiliar page:

```bash
python3 listscrape.py https://example.com/shop --auto --explain
```

**`--auto` is a guess, and says so.** It picks the largest, most uniform
repeating block — on a page whose FAQ has seven entries and whose shop has
three products, it will pick the FAQ, and it is not wrong to. When you know
what you want, `--select` is the answer; `--auto` is for finding out what is
there.

### The parts that are easy to get wrong

**It is a DOM, not a regex.** `<a class="x" href="y">` and
`<a href="y" class="x">` are the same element, an unmatched `</div>` does not
unwind the document, and `<br>` does not swallow the rest of the page. Every
regex-based scraper works until it meets the first page that disagrees with it
about attribute order.

**`robots.txt` is fetched with the scraper's own user agent.** This sounds
like a detail and is not. `urllib.robotparser` fetches with
`Python-urllib/3.x`; a Cloudflare-fronted site answers *that* with 403; and
the parser reads 403 as "everything is forbidden". An earlier version of this
script therefore refused, politely and wrongly, to read about half the web —
including sites whose robots.txt said `Allow: /`. Use `--ignore-robots` to
override, deliberately.

**Items are matched on tag and class, not on their children.** One card in ten
carries a "sale" badge, one has no thumbnail, one has a second line. Matching
on child structure splits those into groups of one and finds no list at all.

**Blocks are scored on sameness, not size.** A container always holds more
text than the things inside it — by construction, not by being more list-like
— so scoring on volume reliably picks the wrapper. Uniformity of item size and
depth of position are what actually distinguish a list.

**One request at a time, with a delay** (`--delay`, default 1s).

### What it deliberately does not do

- **JavaScript.** A list drawn client-side is invisible to anything built on
  the standard library. It says so on stderr instead of writing an empty CSV
  and letting you think the page was empty.
- **Full CSS selectors.** `tag`, `.class`, `#id`, and descendants
  (`.list .item`). `>`, `:nth-child` and friends raise an error rather than
  silently matching nothing.
- **Logins, sessions, or anything a site put behind a wall.**

---

## Coming next

- `hookbridge.py` — receive a webhook and forward it somewhere else, with
  field mapping.

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
