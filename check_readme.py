#!/usr/bin/env python3
"""Check that the README still describes the scripts. Run in CI.

A README is the only part of a project nothing executes, so it is the only
part that can be wrong for months without anyone noticing. This ran for the
first time on 2026-09-14 against the sibling repository and found four claims
that were false -- including a `TypeError` in the very first code block, the
most-read four lines in the project.

What it checks, all of it mechanical:

  Every flag the README mentions exists in that script's --help. Catches the
  rename and the removal, which are the two ways documentation goes stale
  without anybody editing it.

  Every in-page link (#anchor) resolves to a heading that exists.

  Every raw.githubusercontent.com URL points at a file that is actually in
  this repository -- the install line is the first thing a visitor runs, and
  a 404 there is the whole first impression.

  Every ```python block that stands on its own compiles. Not runs: a block
  may need a file that is not here. Compiling still catches the typo, the
  unbalanced bracket and the stray prose.

What it deliberately does not check: claimed OUTPUT. A sample --report block
is illustrative, its input is not in the repo, and pinning it would make every
cosmetic change a failing build. Those claims are checked by hand, which is
exactly why the mechanical ones should not be.
"""
import ast
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
README = HERE / "README.md"
SCRIPTS = ["csvclean.py", "listscrape.py", "hookbridge.py"]


def flags_of(script):
    """Every long flag the script's own --help advertises."""
    out = subprocess.run([sys.executable, str(HERE / script), "--help"],
                         capture_output=True, text=True, timeout=60).stdout
    return set(re.findall(r"(--[a-z][a-z0-9-]*)", out)) | {"--selftest", "--help"}


def mentioned_flags(text):
    """Flags the README names, keyed by the script section they appear under."""
    section, found = None, {s: set() for s in SCRIPTS}
    for line in text.splitlines():
        head = re.match(r"^#+\s+`?([\w-]+\.py)`?", line)
        if head and head.group(1) in found:
            section = head.group(1)
        if section:
            found[section] |= set(re.findall(r"(--[a-z][a-z0-9-]*)", line))
    return found


def slugify(heading):
    """GitHub's heading -> anchor rule, near enough for our own headings."""
    t = heading.strip().lower()
    t = re.sub(r"`|\.|\(|\)|,|:|—|–|/", "", t)
    return re.sub(r"[^a-z0-9\s-]", "", t).strip().replace(" ", "-")


def check(text=None, online=True):
    """Every complaint, as a list. Empty means the README still tells the truth."""
    text = README.read_text(encoding="utf-8") if text is None else text
    bad = []

    # 1. Flags that no longer exist.
    said = mentioned_flags(text)
    for script in SCRIPTS:
        if not (HERE / script).exists():
            bad.append(f"{script}: mencionado no README e nao existe")
            continue
        for flag in sorted(said[script] - flags_of(script)):
            bad.append(f"{script}: o README fala de {flag}, que o --help nao tem")

    # 2. Anchors.
    slugs = {slugify(m.group(1)) for m in re.finditer(r"^#+\s+(.*)$", text, re.M)}
    for anchor in re.findall(r"\]\(#([^)]+)\)", text):
        if anchor not in slugs:
            bad.append(f"ancora #{anchor} nao corresponde a nenhum titulo")

    # 3. Raw file URLs must be files we actually ship.
    for url in re.findall(r"https://raw\.githubusercontent\.com/\S+?/main/([\w./-]+)", text):
        if not (HERE / url).exists():
            bad.append(f"o README manda descarregar {url}, que nao esta no repo")

    # 4. Python blocks compile.
    for i, block in enumerate(re.findall(r"```python\n(.*?)```", text, re.S), 1):
        try:
            ast.parse(block)
        except SyntaxError as exc:
            bad.append(f"bloco python #{i} nao compila: {exc.msg} (linha {exc.lineno})")

    # 5. Links out, once, and only when asked -- CI has a network, a laptop on
    #    a train does not, and a checker that fails offline stops being run.
    if online:
        for url in sorted(set(re.findall(r"\((https://[^)\s]+)\)", text))):
            if "raw.githubusercontent" in url:
                continue
            try:
                req = urllib.request.Request(
                    url.split("#")[0], method="HEAD",
                    headers={"User-Agent": "check-readme/1.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    if r.status >= 400:
                        bad.append(f"{url} -> HTTP {r.status}")
            except urllib.error.HTTPError as e:
                # 403 is nearly always a CDN refusing an unknown agent, not a
                # dead link. Reporting it trains people to ignore this output.
                if e.code not in (403, 405, 429):
                    bad.append(f"{url} -> HTTP {e.code}")
            except Exception as exc:
                bad.append(f"{url} -> {type(exc).__name__}")
    return bad


def selftest():
    fake = """# t

## `csvclean.py`

Use `--report` and `--naoexiste`.

See [the section](#t) and [nowhere](#nao-existe).

```python
def ok():
    return 1
```

```python
def partido(
```

Download https://raw.githubusercontent.com/x/y/main/naoexiste.py
"""
    bad = check(fake, online=False)
    joined = " | ".join(bad)
    assert "--naoexiste" in joined, joined
    assert "--report" not in joined, "uma flag que existe nao pode ser queixa"
    assert "#nao-existe" in joined, joined
    assert "naoexiste.py" in joined, joined
    assert "bloco python #2" in joined, joined
    assert "bloco python #1" not in joined, "um bloco valido nao e queixa"
    # E o README a serio, sem rede, tem de estar limpo.
    real = check(online=False)
    assert real == [], real
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        raise SystemExit(0)
    problems = check(online="--offline" not in sys.argv)
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    print("  o README ainda descreve o que existe" if not problems
          else f"  {len(problems)} problema(s)")
    raise SystemExit(1 if problems else 0)
