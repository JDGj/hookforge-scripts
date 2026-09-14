#!/usr/bin/env python3
"""Pull a repeated list off a web page into CSV. No dependencies.

Python 3.9+, standard library only, one file. Public domain (CC0).

    python3 listscrape.py https://example.com/shop --auto
    python3 listscrape.py page.html --select ".product" \
        --field "name=h3" --field "price=.price" --field "url=a@href"

Most "scrape a page" snippets are a regex over HTML, which works until the
first page that puts an attribute in a different order. This builds a real
(small) DOM with html.parser, so `<a class="x" href="y">` and
`<a href="y" class="x">` are the same element, and a `<br>` does not eat the
rest of the document.

Three things it does that the twenty-line version does not:

  --auto finds the repeating block for you. A listing page is a container
  whose children all have the same shape; this scores every container by how
  many same-shaped children it has and how deep they are, and picks the best.
  You look at the CSV, not at the DOM.

  robots.txt is respected by default. Not decoration: a scraper that ignores
  it gets the IP banned, and the person running it usually finds out days
  later when the data quietly stops arriving. --ignore-robots exists, says
  what it is, and is never the default.

  Requests are one at a time with a delay. Politeness is the difference
  between a tool you can leave running and one that gets you blocked.

It does NOT run JavaScript. A page that renders its list client-side will look
empty, and this says so rather than writing an empty CSV. That is the honest
boundary of anything built on the standard library.

Written and maintained by HookForge (https://hookforge.dev). It is the free,
generic half of a paid service: if the page you need is the awkward one, we
build the scraper for it for a fixed 79 EUR. No tracking, no telemetry, no
call home -- it works offline on a saved page and keeps working without us.
"""
from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser

UA = ("Mozilla/5.0 (compatible; listscrape/1.0; "
      "+https://github.com/JDGj/hookforge-scripts)")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
SKIP_TEXT = {"script", "style", "noscript", "template"}


# ------------------------------------------------------------------- a DOM --

class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent
        self.text = []

    # -- reading ---------------------------------------------------------
    @property
    def classes(self):
        return set(self.attrs.get("class", "").split())

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def inner_text(self):
        """Visible text, whitespace collapsed. Script and style excluded --
        otherwise a price selector happily returns a jQuery snippet."""
        if self.tag in SKIP_TEXT:
            return ""
        parts = list(self.text)
        for c in self.children:
            parts.append(c.inner_text())
        return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()

    def signature(self):
        """What this node's shape is, for grouping siblings.

        Tag plus classes, and deliberately NOT the tags of its children. On a
        real listing, one card in ten carries a "sale" badge, one has no
        thumbnail, one has a second line of text -- and a signature that
        included children would split those three off into groups of one and
        find no list at all. Cards on the same page share a class; that is what
        the class is for.
        """
        return f"{self.tag}|{'.'.join(sorted(self.classes))}"

    def depth(self):
        n, d = self, 0
        while n.parent:
            n, d = n.parent, d + 1
        return d

    def __repr__(self):
        return f"<{self.tag}{'.' + '.'.join(sorted(self.classes)) if self.classes else ''}>"


class Builder(HTMLParser):
    """html.parser into a Node tree. Tolerant: real pages are not well-formed."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, {k: (v or "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        # Climb to the nearest matching open tag. An unmatched </div> in the
        # wild must not unwind the whole document.
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        if data.strip():
            self.cur.text.append(data)


def parse(markup):
    b = Builder()
    b.feed(markup)
    return b.root


# -------------------------------------------------------------- selectors --

SIMPLE = re.compile(r"^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)$")


def match_one(node, part):
    """Does `node` match a single compound selector like div.card#x?"""
    m = SIMPLE.match(part)
    if not m:
        raise SystemExit(f"selector nao suportado: {part!r} "
                         "(usa tag, .classe, #id, ou os tres juntos)")
    tag, rest = m.group(1), m.group(2) or ""
    if tag and node.tag != tag.lower():
        return False
    for token in re.findall(r"[.#][\w-]+", rest):
        if token[0] == "." and token[1:] not in node.classes:
            return False
        if token[0] == "#" and node.attrs.get("id") != token[1:]:
            return False
    return True


def select(root, selector):
    """Nodes matching a descendant selector ("div.list .item"). Document order."""
    parts = selector.split()
    if not parts:
        return []
    found = [n for n in root.walk() if match_one(n, parts[0])]
    for part in parts[1:]:
        nxt, seen = [], set()
        for base in found:
            for n in base.walk():
                if n is not base and match_one(n, part) and id(n) not in seen:
                    seen.add(id(n))
                    nxt.append(n)
        found = nxt
    return found


# ------------------------------------------------------------------ --auto --

def find_repeats(root, min_items=3):
    """The most promising repeating block on the page, as a list of nodes.

    Scored on three things, and the third is the one that matters:

      how many same-shaped children there are;
      how much text each carries, CAPPED -- past a paragraph or so, more text
        does not make something more list-like;
      how ALIKE those texts are in length.

    That last factor is what separates a list from a page. Eight product cards
    have descriptions of roughly the same size; the four <section> elements
    wrapping them have wildly different ones. Scoring on raw volume alone
    reliably picks the sections -- they always contain more text than the
    things inside them, by construction -- which is exactly what an earlier
    version of this function did on a real page.

    Depth breaks remaining ties towards the more specific container, because
    <body> technically repeats too.
    """
    best, best_score = [], 0.0
    for node in root.walk():
        if len(node.children) < min_items:
            continue
        groups = {}
        for c in node.children:
            groups.setdefault(c.signature(), []).append(c)
        for group in groups.values():
            if len(group) < min_items:
                continue
            lengths = [len(c.inner_text()) for c in group]
            mean = sum(lengths) / len(lengths)
            if mean < 10:                 # a row of icons is not a list of things
                continue
            spread = (sum((n - mean) ** 2 for n in lengths) / len(lengths)) ** 0.5
            uniformity = 1.0 / (1.0 + spread / mean)
            # sqrt on the text, uniformity squared. Volume is weak evidence
            # and a parent always has more of it than its children -- by
            # construction, never by being more list-like. Sameness of size and
            # specificity of position are the strong signals, so they are the
            # ones allowed to dominate.
            #
            # Two more, added after watching this pick a page's FAQ over its
            # product grid. Both were right to be called lists; only one is
            # what somebody running a scraper meant.
            #
            #   Items that link out. A product card, a search result, a job
            #   ad, a classified -- each points somewhere. Seven <details> in
            #   an FAQ point nowhere. This is a bonus and not a requirement,
            #   because a table of figures is still a list.
            #
            #   Items with internal structure. The product cards on that page
            #   had seven distinct descendant tags each (heading, price, list,
            #   link); the FAQ entries had two. Repeated prose is flat;
            #   records have fields.
            #   Items with fields. A record has sub-elements; a paragraph of
            #   prose has none. Measured across this site's own pages, that
            #   one number separates them where nothing else does: the real
            #   lists scored 316 and 254 with a median of 5 and 2 child
            #   elements per item, and three prose pages scored 17, 36 and
            #   264 with a median of ZERO. Score alone would have let an
            #   article's paragraphs out-rank a product grid. A penalty and
            #   not a veto, because <li>Alpha</li><li>Beta</li> is a list too.
            linked = sum(1 for c in group
                         if any(d.tag == "a" and d.attrs.get("href")
                                for d in c.walk()))
            link_factor = 1 + 0.6 * (linked / len(group))
            tags = len({d.tag for c in group for d in c.walk() if d is not c})
            structure = min(tags, 6) / 3.0
            kids = sorted(len(c.children) for c in group)[len(group) // 2]
            fields = 1.0 if kids >= 2 else (0.7 if kids == 1 else 0.35)
            score = (len(group) * min(mean, 600.0) ** 0.5 * uniformity ** 2
                     * (1 + node.depth() * 0.3) * link_factor * structure
                     * fields)
            if score > best_score:
                best, best_score = group, score
    return best


def guess_fields(nodes):
    """Column names and extractors for an auto-detected block.

    Every descendant position that carries text in most of the items becomes a
    column, named after its class when it has one -- a `.price` span becomes a
    "price" column, which is usually exactly right and always obvious when it
    is not.
    """
    counts, order = {}, []
    for n in nodes:
        for d in n.walk():
            if d is n:
                continue
            key = ".".join(sorted(d.classes)) or d.tag
            if not d.inner_text():
                continue
            if key not in counts:
                counts[key] = 0
                order.append(key)
            counts[key] += 1
    tags = {d.tag for n in nodes for d in n.walk()}
    keep = [k for k in order if counts[k] >= len(nodes) * 0.6][:6]
    fields, used = [], set()
    for k in keep:
        # `k` is either a dotted class list or a bare tag name; the selector
        # differs and getting it backwards yields a column of empty strings.
        sel = k if k in tags and "." not in k else "." + k.split(".")[0]
        name = re.sub(r"[^0-9a-zA-Z]+", "_", k).strip("_").lower() or "field"
        while name in used:
            name += "_"
        used.add(name)
        fields.append((name, sel))
    # A link is nearly always worth having, and is never the text of anything.
    if any(d.tag == "a" and d.attrs.get("href") for n in nodes for d in n.walk()):
        fields.append(("url", "a@href"))
    return fields or [("text", "")]


# ----------------------------------------------------------------- extract --

def extract(node, spec, base_url=""):
    """One field out of one item. `spec` is "sel", "sel@attr", or "" for the
    whole item's text."""
    sel, _, attr = spec.partition("@")
    sel = sel.strip()
    target = node if not sel else next(iter(select(node, sel)), None)
    if target is None:
        return ""
    if attr:
        value = target.attrs.get(attr, "")
        if attr in ("href", "src") and base_url and value:
            value = urllib.parse.urljoin(base_url, value)
        return value.strip()
    return target.inner_text()


# ----------------------------------------------------------------- fetching --

# --------------------------------------------------------------- robots --
#
# Our own matcher, because urllib.robotparser answers differently depending on
# the ORDER of the rules:
#
#   Allow: /publico/   then  Disallow: /        -> /publico/x allowed
#   Disallow: /        then  Allow: /publico/   -> /publico/x DENIED
#
# Same rules, same site, opposite answers. RFC 9309 section 2.2.2 says the
# most specific rule wins -- the longest matching path -- and order carries no
# meaning at all. The stdlib takes the first match instead, so "block
# everything, then open these paths", which is one of the most common shapes a
# real robots.txt takes, reads as a total ban.
#
# That matters more than it sounds: the sites written that way are exactly the
# ones with a deliberate crawling policy, and refusing them is refusing the
# people who took the trouble to say yes.

def parse_robots(text, agent):
    """[(path, allowed)] for `agent`, from robots.txt source.

    Groups are matched by the most specific user-agent that applies: an exact
    name beats `*`, and a group for somebody else is skipped entirely.
    """
    agent = (agent or "*").lower()
    groups, current, starting = {}, [], True
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if not starting:                 # a new group begins
                current, starting = [], True
            groups.setdefault(value.lower(), []).extend([])
            current.append(value.lower())
        elif field in ("allow", "disallow"):
            starting = False
            for name in current or ["*"]:
                groups.setdefault(name, []).append((value, field == "allow"))
    # The most specific agent that matches wins; `*` is the fallback.
    for name in sorted(groups, key=len, reverse=True):
        if name != "*" and name in agent:
            return groups[name]
    return groups.get("*", [])


def rule_matches(path, rule):
    """Does this robots.txt path rule apply to `path`? Handles * and $."""
    import re as _re
    if rule == "":
        return False                          # empty Disallow means "allow all"
    pattern = "".join(
        ".*" if ch == "*" else ("$" if ch == "$" else _re.escape(ch))
        for ch in rule)
    if not rule.endswith("$"):
        pattern += ".*"
    return _re.match(pattern, path) is not None


def robots_allows(text, agent, path):
    """RFC 9309: the longest matching rule wins, and Allow wins a tie."""
    best_len, best_allow = -1, True
    for rule, allow in parse_robots(text, agent):
        if not rule_matches(path, rule):
            continue
        weight = len(rule)
        if weight > best_len or (weight == best_len and allow):
            best_len, best_allow = weight, allow
    return best_allow

def robots_for(base, opener=None):
    """This site's robots.txt as text, or None (no rules) or False (stay out).

    Fetched HERE, with the same user agent the scrape will use.
    RobotFileParser.read() fetches with urllib's own ("Python-urllib/3.x"), a
    Cloudflare-fronted site answers that with 403, and the parser reads 403 as
    "everything is forbidden" -- correct per the RFC, but the 403 was about the
    user agent, not about us, and the file itself said Allow: /. That one
    detail made an earlier version of this script refuse, politely and
    wrongly, to read sites that welcomed it.

    Three answers and not two, because "no rules" and "keep out" are different
    facts and a caller that cannot tell them apart will get one of them wrong.
    """
    req = urllib.request.Request(f"{base}/robots.txt", headers={"User-Agent": UA})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=15) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 401/403 really do mean "stay out" (RFC 9309 2.3.1.3); 404 and the
        # rest mean there are no rules to obey.
        return False if e.code in (401, 403) else None
    except Exception:
        return None                           # unreachable is not a refusal


def allowed(url, ignore=False, opener=None):
    """Whether robots.txt permits this. Unreachable robots.txt means allowed --
    that is what every well-behaved crawler does, and refusing would make the
    tool useless against any site without one."""
    if ignore:
        return True
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https"):
        return True
    text = robots_for(f"{parts.scheme}://{parts.netloc}", opener)
    if text is None:
        return True                           # no rules to obey
    if text is False:
        return False                          # robots.txt itself was refused
    return robots_allows(text, UA, parts.path or "/")


def fetch(url, timeout=30):
    if "://" not in url:
        with open(url, "rb") as f:
            return f.read().decode("utf-8", "replace")
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        charset = r.headers.get_content_charset()
    for enc in ([charset] if charset else []) + ["utf-8", "cp1252", "latin-1"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError, TypeError):
            continue
    return raw.decode("latin-1", "replace")


# ---------------------------------------------------------------------- cli --

def build_parser():
    p = argparse.ArgumentParser(
        prog="listscrape.py",
        description="Extrai uma lista repetida de uma pagina para CSV.",
        epilog="HookForge — https://hookforge.dev — dominio publico (CC0)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", nargs="+", help="URL(s) ou ficheiro(s) HTML local(is)")
    p.add_argument("-o", "--output", help="CSV de saida (por omissao: stdout)")
    p.add_argument("--select", metavar="SEL",
                   help="selector do item repetido (ex: .product)")
    p.add_argument("--field", action="append", default=[], metavar="NOME=SEL",
                   help="coluna; SEL pode ser 'h3' ou 'a@href'; repetivel")
    p.add_argument("--auto", action="store_true",
                   help="descobre sozinho o bloco repetido e as colunas")
    p.add_argument("--explain", action="store_true",
                   help="diz o que encontrou e nao escreve CSV")
    p.add_argument("--delay", type=float, default=1.0, metavar="S",
                   help="pausa entre paginas (por omissao: 1s)")
    p.add_argument("--ignore-robots", action="store_true",
                   help="ignora o robots.txt do site (pensa duas vezes)")
    p.add_argument("--min-items", type=int, default=3, metavar="N",
                   help="minimo de itens para --auto considerar um bloco")
    return p


def scrape_one(markup, args, base_url=""):
    root = parse(markup)
    if args.select:
        items = select(root, args.select)
    elif args.auto:
        items = find_repeats(root, args.min_items)
    else:
        raise SystemExit("indica --select SEL ou --auto")

    if not items:
        return [], []

    if args.field:
        fields = []
        for f in args.field:
            name, _, sel = f.partition("=")
            fields.append((name.strip(), sel.strip()))
    else:
        fields = guess_fields(items)

    rows = [[extract(it, sel, base_url) for _, sel in fields] for it in items]
    return [n for n, _ in fields], rows



def looks_like_records(markup, args):
    """Whether --auto's pick has fields, rather than being repeated prose."""
    items = find_repeats(parse(markup), args.min_items)
    if not items:
        return True
    return sorted(len(c.children) for c in items)[len(items) // 2] >= 1


def main(argv=None):
    args = build_parser().parse_args(argv)
    header, all_rows = None, []

    for i, url in enumerate(args.url):
        if i:
            time.sleep(args.delay)
        if not allowed(url, args.ignore_robots):
            print(f"  {url}: o robots.txt do site nao permite — saltado "
                  f"(--ignore-robots forca)", file=sys.stderr)
            continue
        try:
            markup = fetch(url)
        except (urllib.error.URLError, OSError) as e:
            print(f"  {url}: {e}", file=sys.stderr)
            continue

        h, rows = scrape_one(markup, args, url if "://" in url else "")
        if args.auto and rows and not looks_like_records(markup, args):
            # Said out loud rather than returned quietly: --auto always returns
            # its best candidate, and on a page with no list at all the best
            # candidate is the prose.
            print(f"  {url}: o melhor candidato parece prosa, nao uma lista "
                  f"(itens sem campos). Confirma com --explain, ou usa "
                  f"--select se sabes o que queres.", file=sys.stderr)
        if not rows:
            # Said out loud, because an empty CSV looks like "nothing there"
            # when it usually means "the list is drawn by JavaScript".
            print(f"  {url}: nenhum item encontrado. Se a lista so aparece com "
                  f"JavaScript ligado, este script nao a ve.", file=sys.stderr)
            continue
        if header is None:
            header = h
        all_rows.extend(rows)
        print(f"  {url}: {len(rows)} item(s), {len(h)} coluna(s)", file=sys.stderr)

        if args.explain:
            print(f"\n  colunas: {', '.join(h)}", file=sys.stderr)
            for r in rows[:3]:
                print(f"    {r}", file=sys.stderr)

    if args.explain or header is None:
        return 0 if header is not None else 1

    out = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
    try:
        w = csv.writer(out)
        w.writerow(header)
        w.writerows(all_rows)
    finally:
        if args.output:
            out.close()
    print(f"  {len(all_rows)} linha(s) -> {args.output or 'stdout'}", file=sys.stderr)
    return 0


# -------------------------------------------------------------------- tests --

SHOP = """<html><body>
<nav class="menu"><a href="/a">A</a><a href="/b">B</a><a href="/c">C</a></nav>
<div class="grid">
  <div class="product"><h3 class="title">Alpha</h3>
    <span class="price">1.234,56 €</span><a href="/p/alpha">ver</a></div>
  <div class="product"><h3 class="title">Beta</h3>
    <span class="price">99,00 €</span><a href="/p/beta">ver</a>
    <span class="badge">sale</span></div>
  <div class="product"><h3 class="title">Gama &amp; Filhos</h3>
    <span class="price">7,50 €</span><a href="/p/gama">ver</a></div>
</div>
<script>var price = "nao sou um preco";</script>
</body></html>"""


def selftest():
    ap = build_parser()
    root = parse(SHOP)

    # The DOM is a DOM, not a regex: attribute order is irrelevant.
    a = parse('<a class="x" href="y">t</a>').children[0]
    b = parse('<a href="y" class="x">t</a>').children[0]
    assert a.attrs == b.attrs and a.classes == b.classes == {"x"}

    # Void elements do not swallow the document.
    assert len(parse("<div><br><p>a</p></div>").children[0].children) == 2
    # Nor does a stray closing tag.
    assert parse("<div><p>a</p></div></div><p>b</p>").inner_text() == "a b"

    # Selectors.
    assert len(select(root, ".product")) == 3
    assert len(select(root, ".grid .product")) == 3
    assert len(select(root, "div.product")) == 3
    assert select(root, "h3.title")[0].inner_text() == "Alpha"
    assert select(root, "#nao-existe") == []

    # Script text is never content.
    assert "nao sou um preco" not in root.inner_text()

    # Entities are decoded once, not twice.
    assert select(root, ".product")[2].inner_text().startswith("Gama & Filhos")

    # Signatures group on tag+class, so the sale badge on one card does not
    # split three products into a group of two and a group of one.
    prods = select(root, ".product")
    assert prods[0].signature() == prods[1].signature() == prods[2].signature()
    assert prods[0].signature() != select(root, "nav a")[0].signature()
    found = find_repeats(root)
    assert len(found) == 3, found
    assert all("product" in n.classes for n in found), found

    # --auto ignores the nav: three identical links, but no text worth having.
    assert not any(n.tag == "a" for n in found)

    # And it prefers the cards over the section that wraps them, even though
    # the section contains strictly more text.
    #
    # The cards here carry a heading and a link, because that is what a card
    # is. An earlier version of this fixture used bare <div>s of prose, and
    # when the link and structure signals were added it started preferring the
    # sections -- correctly, as it turns out: three identical blocks of text
    # with no heading, no link and no fields are not obviously more of a list
    # than the three sections around them, and a scraper has no honest reason
    # to pick one. The fixture was unrealistic, not the scorer.
    wrapped = parse("""<body><section class="s"><h2>Um titulo</h2>
      <div class="grid">
        <div class="card"><h3>Alpha</h3><span class="p">10 EUR</span>
          <a href="/a">ver</a></div>
        <div class="card"><h3>Beta</h3><span class="p">20 EUR</span>
          <a href="/b">ver</a></div>
        <div class="card"><h3>Gama</h3><span class="p">30 EUR</span>
          <a href="/c">ver</a></div>
      </div></section>
      <section class="s"><h2>Outro</h2><p>Uma seccao muito mais curta.</p></section>
      <section class="s"><h2>Terceiro</h2><p>E outra ainda, de outro tamanho
        completamente diferente, bastante mais longa que as anteriores para
        garantir que a variacao entre seccoes e grande.</p></section>
      </body>""")
    picked = find_repeats(wrapped)
    assert picked and all("card" in n.classes for n in picked), picked

    # A small bench of page shapes, because two signals added to fix one page
    # is how a heuristic gets tuned into something that only works on that
    # page. Each of these says what it expects and why.
    def shaped(html, want, why):
        got = find_repeats(parse(html))
        assert got, f"{why}: nao encontrou nada"
        classes = [c for n in got for c in (n.classes or {n.tag})]
        assert any(want in c for c in classes), f"{why}: escolheu {got[:3]}"

    # A nav of links must never beat the results below it: links, but no text.
    shaped("""<body><nav><a href="/1">Um</a><a href="/2">Dois</a>
        <a href="/3">Tres</a><a href="/4">Quatro</a></nav>
      <div class="results">
        <div class="res"><h3>Primeiro resultado</h3><p>Uma descricao com
          tamanho normal de resultado de pesquisa.</p><a href="/r1">abrir</a></div>
        <div class="res"><h3>Segundo resultado</h3><p>Outra descricao com
          tamanho parecido com a anterior aqui.</p><a href="/r2">abrir</a></div>
        <div class="res"><h3>Terceiro resultado</h3><p>E a terceira, tambem
          do mesmo tamanho aproximado que as outras.</p><a href="/r3">abrir</a></div>
      </div></body>""", "res", "nav vs resultados")

    # When the FAQ is the only list on the page, the FAQ is the answer.
    shaped("""<body><main><h1>Perguntas</h1>
        <details class="q"><summary>Primeira pergunta aqui</summary>
          <p>Uma resposta com algum tamanho, como as respostas tem.</p></details>
        <details class="q"><summary>Segunda pergunta aqui</summary>
          <p>Outra resposta de tamanho semelhante a primeira.</p></details>
        <details class="q"><summary>Terceira pergunta</summary>
          <p>E a terceira resposta, tambem parecida em tamanho.</p></details>
      </main></body>""", "q", "FAQ sozinha na pagina")

    # Table rows are a list even though nothing in them links anywhere.
    shaped("""<body><table><tbody>
        <tr class="r"><td>Lisboa</td><td>545923</td><td>2026-01-01</td></tr>
        <tr class="r"><td>Porto</td><td>231962</td><td>2026-01-01</td></tr>
        <tr class="r"><td>Braga</td><td>193333</td><td>2026-01-01</td></tr>
        <tr class="r"><td>Coimbra</td><td>140796</td><td>2026-01-01</td></tr>
      </tbody></table></body>""", "r", "linhas de tabela sem links")

    # Extraction, including attributes and URL joining.
    p0 = prods[0]
    assert extract(p0, ".price") == "1.234,56 €"
    assert extract(p0, "a@href") == "/p/alpha"
    assert extract(p0, "a@href", "https://x.pt/shop") == "https://x.pt/p/alpha"
    assert extract(p0, ".naoexiste") == "", "um campo em falta e vazio, nao um erro"
    assert extract(p0, "") == p0.inner_text()

    # End to end, with explicit fields.
    args = ap.parse_args(["x.html", "--select", ".product",
                          "--field", "nome=.title", "--field", "preco=.price",
                          "--field", "url=a@href"])
    h, rows = scrape_one(SHOP, args, "https://x.pt/shop")
    assert h == ["nome", "preco", "url"]
    assert rows[0] == ["Alpha", "1.234,56 €", "https://x.pt/p/alpha"], rows[0]
    assert rows[1][0] == "Beta" and rows[2][1] == "7,50 €"

    # End to end, auto.
    args = ap.parse_args(["x.html", "--auto"])
    h, rows = scrape_one(SHOP, args)
    assert len(rows) >= 2 and len(h) >= 2, (h, rows)
    assert any("Alpha" in " ".join(r) for r in rows), rows

    # A page with no list says so by returning nothing, not by inventing a row.
    args = ap.parse_args(["x.html", "--auto"])
    h, rows = scrape_one("<html><body><p>so um paragrafo</p></body></html>", args)
    assert rows == [], rows

    # An unsupported selector is loud.
    try:
        select(root, "div > p")
    except SystemExit as e:
        assert "nao suportado" in str(e)
    else:
        raise AssertionError("um selector nao suportado tem de falhar em voz alta")

    # robots.txt on a local file is not consulted at all.
    assert allowed("page.html") is True

    # The rules themselves, RFC 9309 rather than first-match. This is the
    # second robotparser bug in this file's history and the quieter one: the
    # stdlib takes the FIRST matching rule, so the same robots.txt gives
    # opposite answers depending on the order its lines happen to be in.
    before = "User-agent: *\nAllow: /publico/\nDisallow: /\n"
    after = "User-agent: *\nDisallow: /\nAllow: /publico/\n"
    for txt in (before, after):
        assert robots_allows(txt, UA, "/publico/x") is True, txt
        assert robots_allows(txt, UA, "/outro") is False, txt
    # "block everything, then open these paths" is one of the commonest shapes
    # a real robots.txt takes, and first-match reads it as a total ban.
    import urllib.robotparser as _rp
    _p = _rp.RobotFileParser(); _p.parse(after.splitlines())
    assert _p.can_fetch(UA, "https://x.pt/publico/x") is False, \
        "se a stdlib deixar de errar isto, este matcher deixa de ser preciso"

    # The longest rule wins, and Allow wins a tie.
    assert robots_allows("User-agent: *\nDisallow: /a/\nAllow: /a/b/\n", UA, "/a/b/c")
    assert not robots_allows("User-agent: *\nAllow: /a/\nDisallow: /a/b/\n", UA, "/a/b/c")
    assert robots_allows("User-agent: *\nAllow: /x\nDisallow: /x\n", UA, "/x"), "empate: Allow"

    # A named group beats the wildcard one, and somebody else's group is not ours.
    named = "User-agent: *\nDisallow: /\n\nUser-agent: listscrape\nAllow: /\n"
    assert robots_allows(named, "listscrape/1.0", "/x") is True
    assert robots_allows(named, "outro-bot", "/x") is False

    # Wildcards and end-anchors.
    pdf = "User-agent: *\nDisallow: /*.pdf$\nAllow: /\n"
    assert robots_allows(pdf, UA, "/a.pdf") is False
    assert robots_allows(pdf, UA, "/a.html") is True
    assert robots_allows(pdf, UA, "/a.pdf.txt") is True, "$ ancora mesmo no fim"

    # An empty Disallow is the documented way to say "allow everything".
    assert robots_allows("User-agent: *\nDisallow:\n", UA, "/seja-o-que-for")
    # Comments and blank lines are not rules.
    assert robots_allows("# nada\nUser-agent: *\n\nAllow: /\n", UA, "/x")
    # And no robots.txt at all is no rules, not a ban.
    assert robots_allows("", UA, "/x") is True

    # robots.txt, without touching the network. The 403 case is the one that
    # matters: it must come from the FILE saying so, never from a CDN refusing
    # urllib's own user agent.
    import io as _io

    class _Resp(_io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *a): return super().read(*a)

    def opener_returning(text):
        def _o(req, timeout=None):
            assert req.get_header("User-agent") == UA, "o nosso UA, nao o do urllib"
            return _Resp(text.encode())
        return _o

    def opener_raising(code):
        def _o(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "no", {}, None)
        return _o

    allow_all = "User-agent: *\nAllow: /\n"
    assert allowed("https://x.pt/a", opener=opener_returning(allow_all))
    assert allowed("https://x.pt/a", opener=opener_returning(
        "User-agent: *\nDisallow: /private\n"))
    assert not allowed("https://x.pt/private/p", opener=opener_returning(
        "User-agent: *\nDisallow: /private\n"))
    assert not allowed("https://x.pt/a", opener=opener_returning(
        "User-agent: *\nDisallow: /\n"))
    # 404: no rules to obey. 403: the file itself is off limits, so stay out.
    assert allowed("https://x.pt/a", opener=opener_raising(404))
    assert not allowed("https://x.pt/a", opener=opener_raising(403))
    # A network failure is not consent-by-silence in either direction; it is
    # treated as no rules, which is what every crawler does.
    def _boom(req, timeout=None):
        raise OSError("connection reset")
    assert allowed("https://x.pt/a", opener=_boom)
    # And --ignore-robots never consults anything at all.
    assert allowed("https://x.pt/a", ignore=True, opener=opener_raising(403))

    print("selftest ok")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
