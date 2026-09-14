# hookforge-scripts

[![tests](https://github.com/JDGj/hookforge-scripts/actions/workflows/test.yml/badge.svg)](https://github.com/JDGj/hookforge-scripts/actions/workflows/test.yml)

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
| [`hookbridge.py`](#hookbridgepy) | Receive a webhook, reshape it, forward it without losing it |

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

**`.xlsx` is read directly, with no dependency.** It is a zip of XML, and
`zipfile` and `xml.etree` are both standard library — so "save it as CSV
first" was never a real limitation, just an unwritten reader. It matters
because these files arrive as Excel attachments far more often than as CSV,
and the save-as step is exactly where a non-technical sender introduces the
encoding and delimiter problems the rest of this script spends its time
undoing.

```bash
python3 csvclean.py vendas.xlsx -o limpo.csv --all
```

Dates come back as dates, which is the part that needs care: Excel stores
`2025-09-14` as the number `45914` and only `styles.xml` says which numbers
are dates. It also believes 1900 was a leap year, so serial 60 is a day that
never existed and everything after it is shifted by one — handled, and
tested. Shared strings, inline strings, booleans, cached formula errors and
missing cells (Excel omits an empty cell rather than writing a blank one) are
all handled too.

Verified once against a real 700-row workbook out of Excel, and the fixtures
in `--selftest` cover the traps that file did not contain.

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

- **Excel files with formatting, formulas or macros.** `.xlsx` is read (see
  below), but only its values: a formula comes back as its cached result,
  and colours, merged cells, charts and macros are ignored.
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

**`--auto` is a guess, and says so.** It scores candidate blocks on five
things — how many items, how much text (capped), how *alike* those items are
in size, whether they link out, and whether they have fields rather than being
repeated prose. On a page with no list at all it still returns its best
candidate, but it says on stderr that the candidate looks like prose:

```
  https://example.com/about: o melhor candidato parece prosa, nao uma lista
  (itens sem campos). Confirma com --explain, ou usa --select se sabes o que queres.
```

When you know what you want, `--select` is the answer; `--auto` is for finding
out what is there.

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
script therefore refused, politely and wrongly, to read sites whose robots.txt
said `Allow: /` — two of six in a quick sample, including `cloudflare.com`
itself. ([The measurement and the fix, written
up.](https://hookforge.dev/blog/robotparser-403-cloudflare.html)) Use
`--ignore-robots` to override, deliberately.

**Items are matched on tag and class, not on their children.** One card in ten
carries a "sale" badge, one has no thumbnail, one has a second line. Matching
on child structure splits those into groups of one and finds no list at all.

**Blocks are scored on sameness, not size.** A container always holds more
text than the things inside it — by construction, not by being more list-like
— so scoring on volume reliably picks the wrapper.

**And on having fields.** Measured across a real site: the genuine lists scored
316 and 254 with a median of 5 and 2 child elements per item; three prose pages
scored 17, 36 and **264** with a median of zero. Score alone would have let an
article's paragraphs out-rank a product grid — a record has fields, a paragraph
is text, and that one number separates them where nothing else does. It is a
penalty and not a veto, because `<li>Alpha</li><li>Beta</li>` is a list too.

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

## hookbridge.py

Receives a webhook, reshapes the payload, forwards it somewhere else — without
losing events.

```bash
HOOK_SECRET=... python3 hookbridge.py \
    --listen 8080 --forward https://hooks.example.com/inbox \
    --map "user.email=email" --map "data.id=external_id" \
    --secret-env HOOK_SECRET
```

```
  a escutar em http://127.0.0.1:8080  ->  https://hooks.example.com/inbox
  fila em hookbridge-queue  |  GET /health para o estado
```

`{"user":{"email":"a@b.pt"},"data":{"id":7},"noise":"..."}` arrives and
`{"email":"a@b.pt","external_id":7}` is what leaves. Add `--keep-unmapped` to
pass the rest through.

### The part that is the whole point

**It accepts fast and delivers slowly.** The twenty-line version forwards while
the sender is still waiting on the socket — so a slow target makes the sender
time out and retry, a down target loses the event outright, and a restart loses
whatever was in flight. This one writes the event to a queue on disk and
answers `202` in about a millisecond. Stripe and GitHub stop retrying, because
the event is already safe.

**The queue is a directory, not a variable.** One JSON file per event, written
to `.tmp` and renamed (atomic, so a reader never sees half a file), moved
between `pending/` and `failed/` to change state. `kill -9` mid-delivery and
everything undelivered is still there on the next start. It is also just
files — when something breaks at 3am you can `cat` the event that broke it.

Verified, not asserted:

```
  three events accepted while the target returned 503     -> 202, 202, 202
  kill -9 the bridge                                      -> 3 files in pending/
  target back up, bridge restarted from scratch           -> 3 delivered, 0 failed
  {"email": "c1@exemplo.pt", "external_id": 1}
  {"email": "c2@exemplo.pt", "external_id": 2}
  {"email": "c3@exemplo.pt", "external_id": 3}
```

**Retries back off and give up loudly.** 1s, 2s, 4s… to `--max-tries`, then the
event moves to `failed/` and stays. A 4xx that is not 408 or 429 gives up at
once — it will not get better by trying again. `--replay` puts everything in
`failed/` back in the queue once you have fixed the target.

**A missing source field produces no key, never a `null`.** A target that
receives `"email": null` for an event that simply did not carry one usually
reads it as *clear the email*, and that is a data-loss bug you find weeks
later.

**The sender is verified before the body is parsed.** HMAC-SHA256 over the raw
bytes — not over the re-serialised JSON, whose key order and whitespace differ
from what was signed — compared with `compare_digest`. Accepts both the bare
hex digest and the `sha256=…` form. Without `--secret-env` on a public
interface it warns you, loudly, that you have built somebody else's free relay.

`GET /health` returns pending, failed, delivered and dropped counts.

### What it deliberately does not do

- **Authenticate to the target.** `--forward-header` passes headers through,
  but OAuth dances and token refresh are a per-API problem, not a generic one.
- **Transform values.** It moves fields; it does not reformat dates or do
  arithmetic. Pipe to something else, or see below.
- **Run as a service for you.** It is a foreground process. `systemd`,
  `supervisord` or `screen` are better at that than anything this file could
  contain.

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
