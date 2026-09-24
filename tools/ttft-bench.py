#!/usr/bin/env python3
"""
TTFT / latency comparison across the PriceTag dogfood gateway.

Measures time-to-first-token and total reply time for the same prompt
against the hosted Qwen and the newly added curvebender GLM 5.3, both on
the unified (Anthropic-dialect) route, streaming on — then prints the
actual answers. Budgets default to a real chat turn: let it think, let it
answer, time all of it.

    export PRICETAG_KEY=pk-your-key
    ./ttft-bench.py                  # 2 runs per model (default)
    ./ttft-bench.py -n 5             # 5 runs per model
    ./ttft-bench.py -n 5 -p          # all requests fired concurrently
    ./ttft-bench.py --prompt "..."   # custom prompt
    ./ttft-bench.py -m claude-sonnet-5,rits/zai-org/glm-5-3
    ./ttft-bench.py --dump 12        # echo raw SSE lines of run 1 (dialect debug)
    ./ttft-bench.py -m rits/zai-org/glm-5-3 -n 1 \
        --thinking default,off,1024,4096   # probe thinking-budget handling

Reasoning models stream a thinking block before any visible text, so the
token budget has to be big enough to get past it or TTFT never happens.
FIRST ms = time to first streamed token (thinking counts — that's when the
client stops showing a spinner); TTFT ms = first visible answer text.

The token is read from the environment only — argv lands in shell history
and in `ps` output — and is never printed.

python3 stdlib only. Ctrl-C keeps the runs already collected and still
prints the table.
"""

import argparse
import json
import os
import ssl
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("PRICETAG_BASE_URL", "")

DEFAULT_MODELS = ["Inferact/Qwen3.8-Flash-Next-NVFP4", "rits/zai-org/glm-5-3"]

# Real substance, no proof demand. (The 3L/5L jug puzzle with "prove no
# shorter sequence works" sent BOTH models into verification loops — 8192
# tokens of thinking, no answer, both sides. Open-ended analysis reasons;
# unbounded proof requests spiral.)
DEFAULT_PROMPT = (
    "A service returns intermittent 504s only under bursty traffic, while "
    "CPU sits at 20% and the database looks healthy. List the most likely "
    "causes in ranked order, one short line each on how you would confirm "
    "or rule out each."
)

_print_lock = threading.Lock()


def short(mid):
    tail = mid.split("/")[-1]
    known = {"Qwen3.8-Flash-Next-NVFP4": "qwen", "glm-5-3": "glm"}
    return known.get(tail, tail[:12])


def one_run(base, key, model, prompt, max_tokens, timeout, dump=0,
            thinking=None):
    """One streamed request. Returns a result dict; never raises.

    dump: echo this many raw SSE lines to stderr (dialect debugging).
    thinking: None = don't send the field at all, "off" = disabled,
              int = enabled with that budget_tokens (effort-level probe)."""
    req_body = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }
    if thinking == "off":
        req_body["thinking"] = {"type": "disabled"}
    elif isinstance(thinking, int):
        # Anthropic spec: 1024 <= budget_tokens < max_tokens. Callers that
        # want an emulator to honour spec-shaped validation must raise
        # max_tokens accordingly; we do that in main().
        req_body["thinking"] = {"type": "enabled", "budget_tokens": thinking}
    body = json.dumps(req_body).encode()

    req = urllib.request.Request(base.rstrip("/") + "/v1/messages", data=body, headers={
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "accept": "text/event-stream",
    })

    t0 = time.perf_counter()
    ttft = None
    first_any = None
    out_tok = None
    in_tok = None
    cache_r = None
    cache_w = None
    chars = 0
    think_chars = 0
    reply_parts = []
    err = None
    delta_types = set()
    dump_seen = 0

    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if dump_seen < dump:
                    dump_seen += 1
                    sys.stderr.write("    raw| %s\n" % line[:240])
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    ev = json.loads(payload)
                except ValueError:
                    continue

                et = ev.get("type", "")

                # First token = first delta carrying real text, not
                # message_start/ping. Keyed on the payload, not the label:
                # the anthropic-dialect emulators behind this gateway spell
                # the delta type differently than api.anthropic.com does,
                # and thinking_delta (a "thinking" field) still doesn't
                # count — TTFT stays time-to-visible-answer. first_any
                # tracks the very first streamed token either way, which
                # is what the user perceives as "it started".
                if et == "content_block_delta":
                    d = ev.get("delta") or {}
                    delta_types.add(d.get("type") or "?")
                    txt = d.get("text")
                    th = d.get("thinking")
                    now = time.perf_counter() - t0
                    if first_any is None and (txt or th):
                        first_any = now
                    if th:
                        think_chars += len(th)
                    if txt:
                        if ttft is None:
                            ttft = now
                        chars += len(txt)
                        if chars < 2400:
                            reply_parts.append(txt)

                # message_start nests usage under message; message_delta
                # carries it at top level. Check both shapes.
                # cache_read/cache_creation ride in the same usage dicts.
                # Providers that prefix-cache silently (the old Qwen bug)
                # leave these absent — the CACHE column then reads "—",
                # which is the finding, not a parser failure.
                for u in (ev.get("usage"),
                          (ev.get("message") or {}).get("usage")):
                    if isinstance(u, dict):
                        if u.get("output_tokens") is not None:
                            out_tok = u["output_tokens"]
                        if u.get("input_tokens") is not None:
                            in_tok = u["input_tokens"]
                        if u.get("cache_read_input_tokens") is not None:
                            cache_r = u["cache_read_input_tokens"]
                        if u.get("cache_creation_input_tokens") is not None:
                            cache_w = u["cache_creation_input_tokens"]

                if et == "message_stop":
                    break

    except urllib.error.HTTPError as e:
        err = "HTTP %s" % e.code
        try:
            detail = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
            if detail:
                err += " " + detail
        except Exception:
            pass
    except urllib.error.URLError as e:
        err = "net: %s" % (getattr(e, "reason", None) or e)
    except TimeoutError:
        err = "timed out after %gs" % timeout
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)

    total = time.perf_counter() - t0
    if out_tok is None and chars:
        out_tok = int(chars / 4)  # est. — provider sent no usage event

    return {"model": model, "ttft": ttft, "first_any": first_any,
            "total": total, "out": out_tok,
            "in": in_tok, "cache_r": cache_r, "cache_w": cache_w,
            "chars": chars, "think": think_chars,
            "reply": "".join(reply_parts),
            "thinking": thinking,
            "err": err, "dts": sorted(delta_types),
            # reasoning burned the whole budget before any visible text
            "maxed": out_tok is not None and out_tok >= max_tokens}


def tps(r):
    """Decode speed: all output tokens (thinking included) over the actual
    streaming window — from FIRST streamed token, not from TTFT. Dividing
    by total−TTFT counts thinking tokens against only the post-answer clock
    and claims fantasy tok/s on reasoners."""
    if not r["out"]:
        return None
    start = r.get("first_any") or 0
    gen = r["total"] - start
    return r["out"] / gen if gen > 0.05 else None


def fmt(v, spec="%.0f"):
    return (spec % v) if v is not None else "—"


def fmt_cache(r):
    """read[+write] cached input tokens; '—' = provider never reported."""
    cr, cw = r.get("cache_r"), r.get("cache_w")
    if cr is None and cw is None:
        return "—"
    cr, cw = cr or 0, cw or 0
    return "%d+%d" % (cr, cw) if cw else "%d" % cr


def med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def vlabel(v):
    return "def" if v is None else ("off" if v == "off" else str(v))


def glabel(model, v, multi):
    return short(model) + ("·" + vlabel(v) if multi else "")


def mt_for(v, base):
    """Generation budget for a thinking variant: budget_tokens < max_tokens."""
    return base if not isinstance(v, int) else max(base, v + 512)


def worker(base, key, model, prompt, max_tokens, timeout, repeat, results,
           dump=0, variants=(None,)):
    multi = len(variants) > 1
    total_n = repeat * len(variants)
    i = 0
    for v in variants:
        label = glabel(model, v, multi)
        for n in range(1, repeat + 1):
            i += 1
            with _print_lock:
                sys.stderr.write("  %-10s run %d/%d\n" % (label, i, total_n))
                sys.stderr.flush()
            # raw dump once per variant — it's a dialect snapshot per shape
            r = one_run(base, key, model, prompt, mt_for(v, max_tokens),
                        timeout, dump if n == 1 else 0, thinking=v)
            r["run"] = n
            r["group"] = label
            results.append(r)


def table(rows, out):
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    pad = lambda r: "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r))
    out.write(pad(rows[0]) + "\n")
    out.write("  ".join("-" * w for w in widths) + "\n")
    prev = None
    for r in rows[1:]:
        if prev is not None and r[0] != prev:
            out.write("\n")
        out.write(pad(r) + "\n")
        prev = r[0]


def report(models, results, out):
    # "models" here is the ordered list of group labels to display; results
    # carry their own group (model·variant), falling back to a bare model.
    by = {}
    for r in results:
        by.setdefault(r.get("group") or short(r["model"]), []).append(r)

    out.write("\nPER RUN\n")
    rows = [("MODEL", "RUN", "FIRST ms", "TTFT ms", "TOTAL ms", "IN TOK",
             "CACHE", "OUT TOK", "THINK CHR", "OUT tok/s", "STATUS")]
    for m in models:
        for r in sorted(by.get(m, []), key=lambda x: x["run"]):
            rows.append((
                m, r["run"],
                fmt(r["first_any"] * 1000 if r.get("first_any") is not None else None),
                fmt(r["ttft"] * 1000 if r["ttft"] is not None else None),
                fmt(r["total"] * 1000),
                fmt(r["in"]), fmt_cache(r), fmt(r["out"]), fmt(r.get("think")),
                fmt(tps(r), "%.1f"),
                (r["err"] or ("capped" if r.get("maxed") else "OK")),
            ))
    table(rows, out)

    if any(r.get("reply") for r in results):
        out.write("\nREPLIES (first 300 chars — the actual answer, thinking "
                  "excluded)\n")
        for m in models:
            for r in sorted(by.get(m, []), key=lambda x: x["run"]):
                if r.get("reply"):
                    out.write("\n[%s #%d] %s\n" % (
                        m, r["run"],
                        " ".join(r["reply"][:300].split())))
        out.write("\n")

    blind = [r for m in models for r in by.get(m, [])
             if not r["err"] and r["ttft"] is None]
    if blind:
        capped = [r for r in blind if r.get("maxed")]
        seen = sorted({t for r in blind for t in r.get("dts", [])})
        labels = ", ".join("'%s'" % t for t in seen) or "none"
        if len(capped) == len(blind):
            out.write("\nNOTE: all %d blind run(s) hit max_tokens while still thinking — "
                      "the answer never started, so TTFT is 'not yet measured', not broken.\n"
                      "Delta types seen: %s. Raise --max-tokens to get past the reasoning "
                      "block; the TTFT medians below are computed only from runs that did\n"
                      "answer, which biases them toward the short-thinking ones.\n"
                      % (len(capped), labels))
        else:
            out.write("\nWARN: %d run(s) ended without a text delta this parser "
                      "recognized%s.\nDelta types seen: %s — the gateway changed "
                      "dialect; TTFT can't be trusted.\n"
                      % (len(blind),
                         " (%d hit max_tokens first)" % len(capped) if capped else "",
                         labels))

    out.write("\nSUMMARY (medians — n is small, medians over means)\n")
    srows = [("MODEL", "RUNS", "OK", "FIRST med ms", "TTFT med ms",
              "TTFT best", "TTFT worst",
              "TOTAL med ms", "TOTAL best", "TOTAL worst",
              "THINK CHR med", "OUT tok/s med")]
    for m in models:
        ok = [r for r in by.get(m, []) if not r["err"]]
        fa = [r["first_any"] * 1000 for r in ok
              if r.get("first_any") is not None]
        tt = [r["ttft"] * 1000 for r in ok if r["ttft"] is not None]
        to = [r["total"] * 1000 for r in ok]
        sp = [tps(r) for r in ok]
        srows.append((
            m, len(by.get(m, [])), len(ok),
            fmt(med(fa)),
            fmt(med(tt)), fmt(min(tt) if tt else None), fmt(max(tt) if tt else None),
            fmt(med(to)), fmt(min(to) if to else None), fmt(max(to) if to else None),
            fmt(med([r.get("think") or 0 for r in ok])),
            fmt(med(sp), "%.1f"),
        ))
    table(srows, out)

    ok = [m for m in models if any(not r["err"] for r in by.get(m, []))]
    if len(ok) == 2:
        a, b = ok
        med_of = lambda m, k: med([r[k] * 1000 for r in by[m]
                                   if not r["err"] and r.get(k) is not None])
        ta, tb = med_of(a, "ttft"), med_of(b, "ttft")
        kind = "first visible answer"
        if not (ta and tb):  # thinking never yielded text at this budget —
            ta, tb = med_of(a, "first_any"), med_of(b, "first_any")
            kind = "first token (thinking counts)"
        if ta and tb:
            fast, slow = (a, b) if ta < tb else (b, a)
            ratio = max(ta, tb) / min(ta, tb)
            out.write("\n%s reaches %s %.2fx sooner (%.0f ms vs %.0f ms median).\n"
                      % (fast, kind, ratio, min(ta, tb), max(ta, tb)))
            out.write("Treat under ~1.5x as a tie: with n=%d a single cold connection or a\n"
                      "busy GPU moves this much. Warm-up run not discarded.\n"
                      % max(len(by[a]), len(by[b])))
    out.flush()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--runs", type=int, default=2, metavar="N",
                    help="runs per model (default 2)")
    ap.add_argument("-m", "--models", default=",".join(DEFAULT_MODELS),
                    metavar="IDS", help="comma-separated model ids")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--max-tokens", type=int, default=8192,
                    help="generation budget — big enough that a real turn "
                         "(thinking + answer) finishes inside it; hitting "
                         "this cap is now a finding, not normal (default 8192)")
    ap.add_argument("--dump", type=int, default=0, metavar="N",
                    help="echo the first N raw SSE lines of each model's "
                         "first run to stderr (dialect debugging)")
    ap.add_argument("--thinking", default=None, metavar="SPEC",
                    help="effort-level probe: comma list of default, off, "
                         "or a thinking budget in tokens — e.g. "
                         "default,off,1024,4096. Adds a variant per entry.")
    ap.add_argument("-b", "--base", default=os.environ.get("UNIFIED_URL", DEFAULT_BASE),
                    metavar="URL", help="unified route base URL")
    ap.add_argument("--timeout", type=float, default=300,
                    help="per-request timeout in seconds — a full 8192-token "
                         "turn at ~100 tok/s needs ~90s (default 300)")
    ap.add_argument("-p", "--parallel", action="store_true",
                    help="fire all models concurrently instead of one after another")
    args = ap.parse_args()

    key = os.environ.get("PRICETAG_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("no key found — run:  export PRICETAG_KEY=pk-your-key\n"
                 "(deliberately an env var: argv is visible in shell history and ps)")
    if not args.base:
        sys.exit("no gateway URL found — pass --base or set PRICETAG_BASE_URL")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.runs < 1:
        sys.exit("-n must be at least 1")
    if not models:
        sys.exit("-m needs at least one model id")

    variants = [None]
    if args.thinking is not None:
        variants = []
        for tok in args.thinking.split(","):
            tok = tok.strip().lower()
            if tok in ("", "default"):
                variants.append(None)
            elif tok in ("off", "disabled"):
                variants.append("off")
            else:
                try:
                    variants.append(int(tok))
                except ValueError:
                    sys.exit("--thinking wants default|off|<budget int>, "
                             "got '%s'" % tok)
    multi = len(variants) > 1
    groups = [glabel(m, v, multi) for m in models for v in variants]

    n_req = args.runs * len(groups)
    print("PriceTag TTFT bench")
    print("  base    %s" % args.base)
    print("  models  %s" % ", ".join(models))
    if multi:
        print("  probes  %s" % ", ".join(
            "%s (max_tokens %d)" % (vlabel(v), mt_for(v, args.max_tokens))
            for v in variants))
    print("  runs    %d per probe, %d requests total" % (args.runs, n_req))
    print("  prompt  %d chars, max_tokens %d, timeout %gs, %s"
          % (len(args.prompt), args.max_tokens, args.timeout,
             "concurrent" if args.parallel else "sequential"))

    results = []
    threads = [threading.Thread(target=worker,
                                args=(args.base, key, m, args.prompt, args.max_tokens,
                                      args.timeout, args.runs, results, args.dump,
                                      variants))
               for m in models]
    try:
        for t in threads:
            t.start()
            if not args.parallel:
                t.join()
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted — reporting the %d completed request(s)\n\n"
                         % len(results))

    report(groups, results, sys.stdout)


if __name__ == "__main__":
    main()
