#!/usr/bin/env python3
"""Receive a webhook, reshape it, forward it somewhere else. No dependencies.

Python 3.9+, standard library only, one file. Public domain (CC0).

    python3 hookbridge.py --listen 8080 \
        --forward https://hooks.example.com/inbox \
        --map "user.email=email" --map "data.id=external_id" \
        --secret-env HOOK_SECRET

The twenty-line version of this is a handler that parses JSON and calls
urlopen. It works on your laptop and loses data in production, for one reason:
it forwards while the sender is still waiting. When the target is slow the
sender times out and retries; when the target is down the event is gone; and
when the process restarts, whatever was in flight is gone with it.

So this one is built the other way round, which is the whole point of the file:

  ACCEPT FAST, DELIVER SLOWLY. The incoming request is written to a queue on
  disk and answered 200 immediately -- usually in a millisecond. Stripe, GitHub
  and everyone else stop retrying, which is what you want, because the event is
  now safe.

  THE QUEUE IS ON DISK, not in memory. Kill -9 the process mid-delivery and
  every undelivered event is still there when it starts again. A queue in a
  list is a queue that a deploy empties.

  RETRIES BACK OFF, and give up loudly. 1s, 2s, 4s... up to --max-tries, then
  the event moves to a `failed/` directory and stays there. It is never
  silently dropped, and it can be replayed with --replay once the target is
  fixed.

  THE SENDER IS VERIFIED, if you give it a secret. HMAC-SHA256 over the raw
  body, compared in constant time, before anything is parsed. An open forwarder
  on the public internet is somebody else's free mail relay.

Written and maintained by HookForge (https://hookforge.dev). It is the free,
generic half of a paid service: if the two ends do not line up this neatly --
an API that needs auth, pagination, or a reply posted back -- we build that
bridge for a fixed 99 EUR. No tracking, no telemetry, no call home.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 2 * 1024 * 1024          # 2 MB: a webhook is an event, not a file


# ------------------------------------------------------------------ mapping --

def dig(obj, path):
    """Value at a dotted path, or None. "a.b.0.c" walks dicts and lists alike."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            i = int(part)
            cur = cur[i] if -len(cur) <= i < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def put(obj, path, value):
    """Set a dotted path, creating dicts on the way. Returns obj."""
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
        if not isinstance(cur, dict):            # a conflicting scalar loses
            return obj
    cur[parts[-1]] = value
    return obj


def reshape(payload, mapping, keep_unmapped=False):
    """Apply "source.path=target.path" rules to a parsed payload.

    A source path that is missing produces no key at all, rather than a null.
    A target that receives `"email": null` for an event that simply did not
    carry one usually treats it as "clear the email", and that is a data-loss
    bug you find weeks later.
    """
    if not mapping:
        return payload
    out = dict(payload) if keep_unmapped else {}
    for src, dst in mapping:
        value = dig(payload, src)
        if value is not None:
            put(out, dst, value)
        if keep_unmapped and src in out and src != dst:
            out.pop(src, None)
    return out


def parse_map(items):
    rules = []
    for item in items or []:
        src, sep, dst = item.partition("=")
        if not sep or not src.strip() or not dst.strip():
            raise SystemExit(f"--map invalido: {item!r} (usa origem.campo=destino.campo)")
        rules.append((src.strip(), dst.strip()))
    return rules


# ------------------------------------------------------------------- queue --

class Queue:
    """A directory of JSON files, oldest first. That is the whole design.

    Not sqlite, not a lock file: one file per event, named by timestamp, moved
    between directories to change state. Crash-safe because a rename inside one
    filesystem is atomic, and inspectable because it is just files -- when
    something goes wrong at 3am you can `cat` the event that broke it.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.pending = self.root / "pending"
        self.failed = self.root / "failed"
        for d in (self.pending, self.failed):
            d.mkdir(parents=True, exist_ok=True)

    def add(self, event):
        name = f"{time.time():.6f}-{uuid.uuid4().hex[:8]}.json"
        tmp = self.pending / (name + ".tmp")
        # Written to .tmp then renamed: a reader must never see half a file.
        tmp.write_text(json.dumps(event), encoding="utf-8")
        tmp.rename(self.pending / name)
        return name

    def take(self):
        files = sorted(p for p in self.pending.glob("*.json"))
        for p in files:
            try:
                return p, json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                p.rename(self.failed / p.name)
        return None, None

    def done(self, path):
        try:
            path.unlink()
        except OSError:
            pass

    def give_up(self, path):
        try:
            path.rename(self.failed / path.name)
        except OSError:
            pass

    def counts(self):
        return (len(list(self.pending.glob("*.json"))),
                len(list(self.failed.glob("*.json"))))


# ---------------------------------------------------------------- delivery --

def deliver(event, target, timeout=30, opener=None):
    """POST one event. Returns (ok, detail). 2xx is success, everything else is not."""
    body = json.dumps(event["payload"]).encode()
    headers = {"Content-Type": "application/json",
               "User-Agent": "hookbridge/1.0 (+https://github.com/JDGj/hookforge-scripts)"}
    for k, v in (event.get("headers") or {}).items():
        headers[k] = v
    req = urllib.request.Request(target, data=body, headers=headers, method="POST")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
            return 200 <= r.status < 300, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        # 4xx that is not 408/429 will not get better by trying again.
        permanent = 400 <= e.code < 500 and e.code not in (408, 429)
        return False, f"HTTP {e.code}{' (permanente)' if permanent else ''}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


class Worker(threading.Thread):
    """Drains the queue in the background, so accepting never waits on delivery."""

    daemon = True

    def __init__(self, q, target, max_tries=6, base_delay=1.0, opener=None, log=print):
        super().__init__()
        self.q, self.target = q, target
        self.max_tries, self.base_delay = max_tries, base_delay
        self.opener, self.log = opener, log
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.delivered = self.dropped = 0

    def run(self):
        while not self.stopping.is_set():
            path, event = self.q.take()
            if path is None:
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            self.attempt(path, event)

    def attempt(self, path, event):
        tries = event.get("tries", 0)
        ok, detail = deliver(event, self.target, opener=self.opener)
        if ok:
            self.q.done(path)
            self.delivered += 1
            self.log(f"  entregue {path.name} ({detail})")
            return
        tries += 1
        if tries >= self.max_tries or "permanente" in detail:
            self.q.give_up(path)
            self.dropped += 1
            self.log(f"  DESISTIU de {path.name} apos {tries} tentativa(s): {detail}"
                     f" — guardado em failed/, reenviavel com --replay")
            return
        event["tries"] = tries
        path.write_text(json.dumps(event), encoding="utf-8")
        delay = self.base_delay * (2 ** (tries - 1))
        self.log(f"  {path.name}: {detail}; nova tentativa em {delay:.0f}s "
                 f"({tries}/{self.max_tries})")
        self.stopping.wait(delay)


# ----------------------------------------------------------------- receiving --

def verify(secret, raw, signature):
    """Whether `signature` is a valid HMAC-SHA256 of `raw`.

    Accepts a bare hex digest or the "sha256=..." form GitHub and Stripe use.
    Compared with compare_digest: a byte-by-byte == on a signature leaks, over
    enough requests, exactly the information needed to forge one.
    """
    if not secret:
        return True
    if not signature:
        return False
    sent = signature.strip()
    if "=" in sent:
        sent = sent.split("=", 1)[1].strip()
    want = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent.lower(), want)


def make_handler(q, worker, args, secret):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, code, text=""):
            body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") in ("/health", ""):
                pending, failed = q.counts()
                return self._reply(200, json.dumps(
                    {"ok": True, "pending": pending, "failed": failed,
                     "delivered": worker.delivered, "dropped": worker.dropped}))
            self._reply(404, "not found")

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return self._reply(400, "bad length")
            if n <= 0 or n > MAX_BODY:
                return self._reply(413, "empty or too large")
            raw = self.rfile.read(n)

            # Verified against the RAW bytes, before parsing. Re-serialising
            # JSON changes key order and whitespace, and the signature is over
            # what was sent, not over what it means.
            if not verify(secret, raw, self.headers.get(args.signature_header, "")):
                return self._reply(401, "bad signature")

            try:
                payload = json.loads(raw)
            except ValueError:
                if not args.allow_non_json:
                    return self._reply(400, "not json")
                payload = {"raw": raw.decode("utf-8", "replace")}

            event = {"payload": reshape(payload, args._rules, args.keep_unmapped),
                     "received": time.time(), "tries": 0}
            if args.forward_header:
                event["headers"] = {h: self.headers.get(h, "") for h in args.forward_header
                                    if self.headers.get(h)}
            name = q.add(event)
            worker.wake.set()
            # 202, not 200: it is accepted, not yet delivered, and saying so is
            # the honest answer to a sender that may care.
            self._reply(202, name)

        def log_message(self, *a):
            pass

    return Handler


# ---------------------------------------------------------------------- cli --

def build_parser():
    p = argparse.ArgumentParser(
        prog="hookbridge.py",
        description="Recebe um webhook, remapeia e reenvia, sem perder eventos.",
        epilog="HookForge — https://hookforge.dev — dominio publico (CC0)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--listen", type=int, default=8080, metavar="PORTA")
    p.add_argument("--bind", default="127.0.0.1",
                   help="interface (por omissao 127.0.0.1; usa 0.0.0.0 atras de um proxy)")
    p.add_argument("--forward", metavar="URL", help="para onde reenviar")
    p.add_argument("--map", action="append", default=[], metavar="ORIGEM=DESTINO",
                   help="remapeia um campo; caminhos com pontos; repetivel")
    p.add_argument("--keep-unmapped", action="store_true",
                   help="mantem os campos que nenhum --map menciona")
    p.add_argument("--queue", default="./hookbridge-queue", metavar="DIR")
    p.add_argument("--max-tries", type=int, default=6)
    p.add_argument("--secret-env", metavar="VAR",
                   help="nome da variavel de ambiente com o segredo HMAC")
    p.add_argument("--signature-header", default="X-Hub-Signature-256", metavar="H")
    p.add_argument("--forward-header", action="append", default=[], metavar="H",
                   help="cabecalho a repassar ao destino; repetivel")
    p.add_argument("--allow-non-json", action="store_true",
                   help='aceita corpos que nao sao JSON, embrulhados em {"raw": "..."}')
    p.add_argument("--replay", action="store_true",
                   help="devolve tudo o que esta em failed/ para pending/ e sai")
    p.add_argument("--status", action="store_true", help="conta a fila e sai")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args._rules = parse_map(args.map)
    q = Queue(args.queue)

    if args.status:
        pending, failed = q.counts()
        print(f"  pendentes: {pending}\n  falhados : {failed}  ({q.failed})")
        return 0

    if args.replay:
        moved = 0
        for p in sorted(q.failed.glob("*.json")):
            try:
                event = json.loads(p.read_text(encoding="utf-8"))
                event["tries"] = 0
                p.write_text(json.dumps(event), encoding="utf-8")
                shutil.move(str(p), str(q.pending / p.name))
                moved += 1
            except (OSError, ValueError):
                continue
        print(f"  {moved} evento(s) devolvidos a fila")
        return 0

    if not args.forward:
        raise SystemExit("indica --forward URL (ou usa --status / --replay)")

    secret = os.environ.get(args.secret_env, "") if args.secret_env else ""
    if args.secret_env and not secret:
        raise SystemExit(f"{args.secret_env} nao esta no ambiente — "
                         "sem segredo isto e um reenviador aberto")
    if not secret and args.bind not in ("127.0.0.1", "localhost", "::1"):
        print("  AVISO: a escutar fora do localhost sem --secret-env. "
              "Qualquer pessoa pode injectar eventos no teu destino.", file=sys.stderr)

    worker = Worker(q, args.forward, args.max_tries,
                    log=lambda m: print(m, file=sys.stderr))
    worker.start()

    pending, failed = q.counts()
    if pending:
        print(f"  {pending} evento(s) da sessao anterior por entregar — a retomar",
              file=sys.stderr)
    if failed:
        print(f"  {failed} em failed/ (--replay devolve-os a fila)", file=sys.stderr)

    srv = ThreadingHTTPServer((args.bind, args.listen),
                              make_handler(q, worker, args, secret))
    print(f"  a escutar em http://{args.bind}:{args.listen}  ->  {args.forward}",
          file=sys.stderr)
    print(f"  fila em {q.root}  |  GET /health para o estado", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  a parar; o que nao foi entregue fica na fila", file=sys.stderr)
        worker.stopping.set()
    return 0


# -------------------------------------------------------------------- tests --

def selftest():
    import tempfile

    # Dotted paths, into dicts and lists, with missing branches.
    obj = {"a": {"b": [{"c": 1}, {"c": 2}]}, "n": None}
    assert dig(obj, "a.b.0.c") == 1
    assert dig(obj, "a.b.1.c") == 2
    assert dig(obj, "a.b.9.c") is None
    assert dig(obj, "a.x.y") is None
    assert dig(obj, "n") is None
    assert dig(obj, "a") == obj["a"]
    assert put({}, "x.y.z", 5) == {"x": {"y": {"z": 5}}}

    # A missing source produces NO key, never a null -- a null usually means
    # "clear this field" at the far end.
    rules = parse_map(["user.email=email", "data.id=external_id",
                       "nao.existe=nada"])
    out = reshape({"user": {"email": "a@b.pt"}, "data": {"id": 7}}, rules)
    assert out == {"email": "a@b.pt", "external_id": 7}, out
    assert "nada" not in out

    # Nested targets, and --keep-unmapped.
    out = reshape({"user": {"email": "a@b.pt"}, "extra": 1},
                  parse_map(["user.email=contact.email"]), keep_unmapped=True)
    assert out["contact"]["email"] == "a@b.pt" and out["extra"] == 1, out
    # No rules at all is a pass-through, not an empty object.
    assert reshape({"a": 1}, []) == {"a": 1}
    try:
        parse_map(["sem-igual"])
    except SystemExit as e:
        assert "invalido" in str(e)
    else:
        raise AssertionError("um --map sem = tem de falhar em voz alta")

    # Signatures.
    raw = b'{"hello":"world"}'
    good = hmac.new(b"s3cr3t", raw, hashlib.sha256).hexdigest()
    assert verify("s3cr3t", raw, good)
    assert verify("s3cr3t", raw, "sha256=" + good), "a forma do GitHub/Stripe"
    assert verify("s3cr3t", raw, good.upper()), "hex e insensivel a maiusculas"
    assert not verify("s3cr3t", raw, good[:-1] + "0")
    assert not verify("s3cr3t", raw, "")
    assert not verify("s3cr3t", b'{"hello":"mundo"}', good), "corpo alterado"
    assert verify("", raw, ""), "sem segredo configurado, nao se verifica nada"

    # The queue survives a process that never comes back.
    with tempfile.TemporaryDirectory() as d:
        q = Queue(d)
        q.add({"payload": {"n": 1}, "tries": 0})
        q.add({"payload": {"n": 2}, "tries": 0})
        assert q.counts() == (2, 0)
        q2 = Queue(d)                       # a fresh process, same directory
        assert q2.counts() == (2, 0), "a fila esta no disco, nao na memoria"
        p, e = q2.take()
        assert e["payload"]["n"] == 1, "o mais antigo primeiro"
        q2.done(p)
        p, e = q2.take()
        q2.give_up(p)
        assert q2.counts() == (0, 1), "desistir nao apaga, arquiva"
        # A corrupt file is quarantined, not a crash loop.
        (Path(d) / "pending" / "9999.json").write_text("{nao e json")
        p, e = Queue(d).take()
        assert p is None and Queue(d).counts() == (0, 2)

    # Delivery: 2xx succeeds, 5xx retries, 4xx gives up at once.
    class R:
        def __init__(self, status): self.status = status
        def __enter__(self): return self
        def __exit__(self, *a): return False

    assert deliver({"payload": {}}, "http://x", opener=lambda *a, **k: R(204))[0]
    ok, detail = deliver({"payload": {}}, "http://x",
                         opener=lambda *a, **k: R(500))
    assert not ok and "500" in detail

    def raise_http(code):
        def _o(req, timeout=None):
            raise urllib.error.HTTPError("http://x", code, "no", {}, None)
        return _o
    assert "permanente" in deliver({"payload": {}}, "http://x", opener=raise_http(422))[1]
    assert "permanente" not in deliver({"payload": {}}, "http://x", opener=raise_http(429))[1]
    assert "permanente" not in deliver({"payload": {}}, "http://x", opener=raise_http(503))[1]

    # A worker gives up on a permanent failure without burning its retries.
    with tempfile.TemporaryDirectory() as d:
        q = Queue(d)
        q.add({"payload": {"n": 1}, "tries": 0})
        w = Worker(q, "http://x", max_tries=6, base_delay=0,
                   opener=raise_http(422), log=lambda m: None)
        path, event = q.take()
        w.attempt(path, event)
        assert q.counts() == (0, 1) and w.dropped == 1, q.counts()

    # ...and retries a temporary one, keeping the count.
    with tempfile.TemporaryDirectory() as d:
        q = Queue(d)
        q.add({"payload": {"n": 1}, "tries": 0})
        w = Worker(q, "http://x", max_tries=3, base_delay=0,
                   opener=raise_http(503), log=lambda m: None)
        for expected in (1, 2):
            path, event = q.take()
            w.attempt(path, event)
            path, event = q.take()
            assert event["tries"] == expected, event
        path, event = q.take()
        w.attempt(path, event)
        assert q.counts() == (0, 1), "ao fim das tentativas, arquiva"

    print("selftest ok")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
