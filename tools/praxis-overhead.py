#!/usr/bin/env python3
"""praxis-overhead.py — how many milliseconds does the praxis hop actually add?

The question from the team thread: "Qwen feels slow through pricetag, fast
without it." This measures the same model, the same prompt, the same
Anthropic dialect, on two paths:

  A (direct)  ->  qwen upstream vLLM-Anthropic route (no gateway in front)
  B (praxis)  ->  unified PriceTag route (auth + filters + proxy, metering async)

Fairness design:
  * Same request body on both paths (identical model id — verified by probe).
  * A/B alternated per pair, so load drift hits both sides equally.
  * Per-PAIR overhead (B - A), not just column medians: the distribution of
    the difference is the answer; medians of busy systems are just vibes.
  * Warm-up pairs are discarded (TLS handshakes, auth caches, pooled
    connections are all cold on the first few).
  * Persistent connections by default, like a real harness (Claude Code keeps
    its connections alive). --fresh-conn measures the churn case instead.
  * Cold-idle probe: --idle 300 --rounds 3 sleeps, then fires measured pairs —
    the "sits and spins sometimes" repro window: stale pooled connections,
    expired auth caches, evicted keepalives.
  * Every outlier (|B-A| > 1 s or B total > 3 s) is printed with a wall
    timestamp so you can go correlate praxis/metering logs afterwards.

Keys come ONLY from env: PRICETAG_KEY (or ANTHROPIC_API_KEY). Never argv,
never printed. The direct upstream route is unauthenticated (verified
2026-09-23); if that changes, pass --direct-key-env NAME.

Read-only against production. Tiny request budget on purpose (max_tokens
default 12). --burst N runs N pairs concurrently — be gentle, it is a shared
GPU the whole team is typing into.

Examples:
    python3 praxis-overhead.py                        # 12 warm pairs
    python3 praxis-overhead.py --idle 300 --rounds 3  # + cold-start probes
    python3 praxis-overhead.py --burst 4 -n 8         # herd mode (shared GPU!)
"""

import argparse
import http.client
import json
import math
import os
import socket
import ssl
import statistics
import sys
import threading
import time
import urllib.parse

DIRECT_DEFAULT = ("https://qwen38-flash-next-d57178e0-2037-479e-9a33-"
                  "217f81dd6f5e.apps.emerg.pcbk.p1.openshiftapps.com")
PRAXIS_DEFAULT = os.environ.get("GATEWAY_URL", "https://gateway.example.com")
MODEL_DEFAULT = "Inferact/Qwen3.8-Flash-Next-NVFP4"
PROMPT = "Reply with exactly: overhead-probe-ok"

RECONNECT_ERRS = (http.client.RemoteDisconnected, http.client.BadStatusLine,
                  ConnectionResetError, BrokenPipeError, socket.timeout,
                  TimeoutError, ssl.SSLError, OSError)


class Path:
    """One measurement path with a persistent TLS connection."""

    def __init__(self, name, base, model, maxtok, timeout, key_env, fresh):
        self.name = name
        u = urllib.parse.urlparse(base)
        self.host, self.port = u.hostname, u.port or 443
        self.prefix = (u.path or "").rstrip("/")
        self.model, self.maxtok, self.timeout = model, maxtok, timeout
        self.key = None
        if key_env:
            self.key = os.environ.get(key_env)
            if not self.key:
                sys.exit(f"env var {key_env} is not set (needed for {name} path)")
        self.fresh = fresh
        self.conn = None
        self.reconnects = 0

    def _connect(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = http.client.HTTPSConnection(
            self.host, self.port, timeout=self.timeout,
            context=ssl.create_default_context())

    def fire(self):
        """One streaming request. Returns a result dict, never raises."""
        body = json.dumps({
            "model": self.model, "max_tokens": self.maxtok, "stream": True,
            "messages": [{"role": "user", "content": PROMPT}]}).encode()
        headers = {"content-type": "application/json",
                   "anthropic-version": "2023-06-01"}
        if self.key:
            headers["x-api-key"] = self.key
            headers["authorization"] = "Bearer " + self.key
        rec = {"path": self.name, "wall": time.strftime("%H:%M:%S"),
               "status": 0, "ttfb": None, "ttft": None, "total": None,
               "out": 0, "err": None, "recovered": False}
        t0 = time.monotonic()
        for attempt in (1, 2):
            try:
                if self.conn is None or attempt == 2 or self.fresh:
                    if attempt == 2:
                        self.reconnects += 1
                        rec["recovered"] = True
                    self._connect()
                self.conn.request("POST", self.prefix + "/v1/messages",
                                  body=body, headers=headers)
                resp = self.conn.getresponse()
                rec["status"] = resp.status
                if resp.status != 200:
                    rec["err"] = (resp.read(200) or b"").decode(errors="replace")
                    return rec
                # stream: first data line = ttfb, first text/thinking delta = ttft
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    now = time.monotonic()
                    if rec["ttfb"] is None and line.strip():
                        rec["ttfb"] = now - t0
                    s = line.decode(errors="replace").strip()
                    if s.startswith("data:"):
                        try:
                            ev = json.loads(s[5:])
                        except ValueError:
                            continue
                        t = ev.get("type")
                        if t == "content_block_delta":
                            d = ev.get("delta", {})
                            if d.get("type") in ("text_delta", "thinking_delta") \
                                    and rec["ttft"] is None:
                                rec["ttft"] = now - t0
                        elif t == "message_delta":
                            u = ev.get("usage") or {}
                            rec["out"] = u.get("output_tokens", rec["out"])
                rec["total"] = time.monotonic() - t0
                if rec["ttft"] is None:
                    rec["ttft"] = rec["ttfb"]
                return rec
            except RECONNECT_ERRS as e:
                if attempt == 2:
                    rec["err"] = f"{type(e).__name__}: {e}"
                    rec["total"] = time.monotonic() - t0
                    return rec
                t0 = time.monotonic()   # restart the stopwatch for the retry
        return rec


def pctl(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(math.ceil(q * len(xs))) - 1)]


def fmt(x):
    return f"{x * 1000:7.0f}ms" if x == x else "      —"


def row(label, xs):
    print(f"{label:<28}{fmt(pctl(xs, .5))}{fmt(pctl(xs, .9))}"
          f"{fmt(pctl(xs, .95))}{fmt(max(xs) if xs else float('nan'))}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-n", type=int, default=12, metavar="N",
                    help="measured A/B pairs per round (default 12)")
    ap.add_argument("--warmup", type=int, default=3, metavar="N",
                    help="discarded pairs before each round (default 3)")
    ap.add_argument("--rounds", type=int, default=1,
                    help="rounds; rounds after the first start with an idle gap")
    ap.add_argument("--idle", type=float, default=0, metavar="SEC",
                    help="seconds to sleep before each later round (cold probe)")
    ap.add_argument("--burst", type=int, default=1, metavar="K",
                    help="pairs run concurrently (shared GPU — be kind, <=4)")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--max-tokens", dest="maxtok", type=int, default=12)
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--direct-url", default=DIRECT_DEFAULT)
    ap.add_argument("--praxis-url", default=PRAXIS_DEFAULT)
    ap.add_argument("--direct-key-env", default=None,
                    help="env var holding a direct-path key (route is open today)")
    ap.add_argument("--praxis-key-env", default="PRICETAG_KEY")
    ap.add_argument("--fresh-conn", action="store_true",
                    help="new TLS connection per request instead of keep-alive")
    args = ap.parse_args()

    if args.praxis_key_env == "PRICETAG_KEY" and not os.environ.get("PRICETAG_KEY"):
        args.praxis_key_env = "ANTHROPIC_API_KEY"   # the session's own key works

    print(f"model={args.model}")
    print(f"A direct : {args.direct_url}")
    print(f"B praxis : {args.praxis_url}  (key from ${args.praxis_key_env})")
    print(f"pairs={args.n} warmup={args.warmup} rounds={args.rounds} "
          f"idle={args.idle:.0f}s burst={args.burst} "
          f"conn={'fresh' if args.fresh_conn else 'keep-alive'}\n")

    warm, cold = [], []                    # lists of (a, b) result pairs
    errs, outliers = [], []
    lock = threading.Lock()

    pa = Path("A", args.direct_url, args.model, args.maxtok,
              args.timeout, args.direct_key_env, args.fresh_conn)
    pb = Path("B", args.praxis_url, args.model, args.maxtok,
              args.timeout, args.praxis_key_env, args.fresh_conn)

    for rnd in range(args.rounds):
        if rnd and args.idle:
            print(f"[round {rnd + 1}] idling {args.idle:.0f}s to go cold...",
                  flush=True)
            time.sleep(args.idle)
        for _ in range(args.warmup):       # same alternation, discarded
            pa.fire()
            pb.fire()

        def one_pair():
            a = pa.fire()
            b = pb.fire()
            with lock:
                for r in (a, b):
                    if r["err"] or r["status"] != 200:
                        errs.append(f"  {r['wall']} {r['path']} "
                                    f"HTTP={r['status']} {(r['err'] or '')[:90]}")
                if a["ttft"] is not None and b["ttft"] is not None:
                    (warm if rnd == 0 else cold).append((a, b))
                    d = b["ttft"] - a["ttft"]
                    if abs(d) > 1.0 or b["total"] > 3.0:
                        outliers.append(
                            f"  {b['wall']} pair #{len(warm) + len(cold):>3}  "
                            f"B={b['total']:6.2f}s A={a['total']:6.2f}s "
                            f"Δttft={d:+.2f}s"
                            + ("  [recovered conn]" if a["recovered"]
                               or b["recovered"] else ""))

        waves = math.ceil(args.n / args.burst)
        done = 0
        for _ in range(waves):
            ths = [threading.Thread(target=one_pair) for _ in range(args.burst)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            done += args.burst
            print(f"  round {rnd + 1}: {min(done, args.n)}/{args.n} pairs",
                  end="\r", flush=True)
        print()

    ok = warm
    if errs:
        print("\nERRORS (first 10):")
        print("\n".join(errs[:10]))
    if not ok:
        sys.exit("\nno successful warm pairs — nothing to compare.")

    dttft = [b["ttft"] - a["ttft"] for a, b in ok]
    dtotal = [b["total"] - a["total"] for a, b in ok]
    print(f"\n== WARM ({len(ok)} pairs) — TTFT, first streamed token ==")
    print(f"{'':<28}{'p50':>9}{'p90':>9}{'p95':>9}{'max':>9}")
    row("A direct", [a["ttft"] for a, _ in ok])
    row("B praxis", [b["ttft"] for _, b in ok])
    print("== WARM — pair overhead (B − A) ==")
    row("TTFT delta", dttft)
    row("TOTAL delta", dtotal)
    wins = sum(1 for d in dttft if d > 0)
    print(f"\n  praxis slower in {wins}/{len(ok)} pairs; median Δ "
          f"{statistics.median(dttft) * 1000:+.0f}ms, "
          f"mean Δ {statistics.fmean(dttft) * 1000:+.0f}ms")
    if outliers:
        print("\nOUTLIERS (|Δttft|>1s or B total>3s) — correlate these stamps:")
        print("\n".join(outliers))

    if cold:
        wp = statistics.median(b["ttft"] - a["ttft"] for a, b in warm) * 1000
        print(f"\n== COLD probes ({len(cold)}) — fired after idle gap "
              f"(warm median Δ was {wp:+.0f}ms) ==")
        print(f"{'when':<10}{'A ttft':>9}{'B ttft':>9}{'A total':>9}"
              f"{'B total':>9}{'Δttft':>9}")
        for a, b in cold:
            print(f"{b['wall']:<10}{fmt(a['ttft'])}{fmt(b['ttft'])}"
                  f"{fmt(a['total'])}{fmt(b['total'])}"
                  f"{(b['ttft'] - a['ttft']) * 1000:+8.0f}ms")

    print(f"\nconn reconnects on stale pooled conn: A={pa.reconnects} "
          f"B={pb.reconnects}")
    print("(reconnects inside MEASURED pairs = the stale-connection stall; "
          "warmup absorbs them for free)")


if __name__ == "__main__":
    main()
