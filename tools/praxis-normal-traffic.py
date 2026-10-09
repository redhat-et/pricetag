#!/usr/bin/env python3
"""Operations-grade traffic watch for zero-impact Praxis upgrades.

The probe sends no retries: every failed request remains visible.  In an
interactive terminal it renders a fixed-screen dashboard with one row per
synthetic user plus the real-model canary.  JSONL evidence is always written
for post-run analysis and automation.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import curses
import hashlib
import json
import math
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class UserSpec:
    display: str
    user_id: str
    key: str
    url: str
    lane: str
    provider: str
    model: str


class PreflightError(RuntimeError):
    """A user-actionable OpenShift or input preflight failure."""


def oc_run(arguments: list[str]) -> str:
    if shutil.which("oc") is None:
        raise PreflightError("oc was not found in PATH; install the OpenShift CLI first")
    try:
        completed = subprocess.run(
            ["oc", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        raise PreflightError(f"oc {' '.join(arguments)} timed out") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().replace("\n", " ")
        raise PreflightError(f"oc {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()


def deployment_info(namespace: str, name: str) -> dict[str, Any]:
    raw = json.loads(oc_run(["-n", namespace, "get", f"deployment/{name}", "-o", "json"]))
    containers = raw.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    return {
        "name": name,
        "images": [container.get("image", "") for container in containers],
        "replicas": raw.get("spec", {}).get("replicas", 0),
        "ready": raw.get("status", {}).get("readyReplicas", 0),
        "available": raw.get("status", {}).get("availableReplicas", 0),
        "updated": raw.get("status", {}).get("updatedReplicas", 0),
    }


def resolve_users_file(argument: str | None) -> Path:
    candidates: list[Path] = []
    if argument:
        candidates.append(Path(argument).expanduser())
    if os.environ.get("PRICETAG_USERS_FILE"):
        candidates.append(Path(os.environ["PRICETAG_USERS_FILE"]).expanduser())
    candidates.append(Path.cwd() / "load-test" / "users.json")
    # OpenCode's standard workspace keeps the sensitive inventory next to the
    # repository checkout, not inside it. This remains a convenience fallback;
    # operators can always supply --users-file explicitly.
    script_root = Path(__file__).resolve().parents[3]
    candidates.append(script_root / "load-test" / "users.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise PreflightError(
        "No load-test user inventory was found. Pass --users-file PATH "
        f"(searched: {searched}). The inventory is intentionally not fetched from the cluster."
    )


def preflight(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, str], Path]:
    if args.kubeconfig:
        os.environ["KUBECONFIG"] = str(Path(args.kubeconfig).expanduser())
    elif not os.environ.get("KUBECONFIG"):
        script_root = Path(__file__).resolve().parents[3]
        for candidate in (Path.cwd() / "pricetag-kubeconfig", script_root / "pricetag-kubeconfig"):
            if candidate.is_file():
                os.environ["KUBECONFIG"] = str(candidate)
                break
    try:
        identity = oc_run(["whoami"])
        server = oc_run(["whoami", "--show-server"])
    except PreflightError as exc:
        raise PreflightError(
            "OpenShift is not authenticated. Run `oc login ...` and retry. "
            f"Details: {exc}"
        ) from exc

    namespace = args.namespace
    try:
        oc_run(["get", "namespace", namespace, "-o", "name"])
    except PreflightError as exc:
        raise PreflightError(
            f"OpenShift namespace {namespace!r} was not found or is not accessible. "
            "Override it with --namespace NAMESPACE."
        ) from exc

    try:
        deployments = {
            name: deployment_info(namespace, name)
            for name in ("praxis", "metering-service")
        }
    except (PreflightError, json.JSONDecodeError) as exc:
        raise PreflightError(
            f"Namespace {namespace!r} is reachable, but the Praxis/metering deployment "
            f"inventory could not be collected: {exc}"
        ) from exc

    route_names = {
        "openai-echo": args.openai_echo_route,
        "anthropic-echo": args.anthropic_echo_route,
        "real-glm": args.real_glm_route,
    }
    urls: dict[str, str] = {}
    routes: dict[str, str] = {}
    explicit_urls = {
        "openai-echo": args.openai_echo_url,
        "anthropic-echo": args.anthropic_echo_url,
        "real-glm": args.real_glm_url,
    }
    for lane, route_name in route_names.items():
        if explicit_urls[lane]:
            urls[lane] = explicit_urls[lane]
            routes[lane] = "explicit URL"
            continue
        try:
            host = oc_run(["-n", namespace, "get", f"route/{route_name}", "-o", "jsonpath={.spec.host}"])
        except PreflightError as exc:
            raise PreflightError(
                f"Required route {route_name!r} is missing in namespace {namespace!r}. "
                f"Override it with the corresponding URL option. Details: {exc}"
            ) from exc
        if not host:
            raise PreflightError(f"Route {route_name!r} has no host in namespace {namespace!r}")
        routes[lane] = route_name
        urls[lane] = f"https://{host}"

    users_file = resolve_users_file(args.users_file)
    info = {
        "identity": identity,
        "server": server,
        "namespace": namespace,
        "routes": routes,
        "urls": urls,
        "deployments": deployments,
    }
    return info, urls, users_file


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


class UserMetrics:
    def __init__(self, spec: UserSpec) -> None:
        self.spec = spec
        self.sent = 0
        self.responses = 0
        self.ok = 0
        self.fail = 0
        self.in_flight = False
        self.latencies: deque[tuple[float, float]] = deque(maxlen=4000)
        self.last_success: float | None = None
        self.max_gap = 0.0
        self.last_status: int | None = None
        self.last_error = ""
        self.last_response_at: float | None = None

    def mark_sent(self) -> bool:
        if self.in_flight:
            return False
        self.sent += 1
        self.in_flight = True
        return True

    def record(self, result: dict[str, Any], now: float) -> None:
        self.in_flight = False
        self.responses += 1
        status = int(result.get("status", 0))
        self.last_status = status
        self.last_response_at = now
        if 200 <= status < 300:
            self.ok += 1
            latency = float(result.get("latency_ms", 0.0))
            self.latencies.append((now, latency))
            if self.last_success is not None:
                self.max_gap = max(self.max_gap, now - self.last_success)
            self.last_success = now
        else:
            self.fail += 1
            self.last_error = str(result.get("error", f"http_{status}"))

    def snapshot(self, now: float, window_seconds: float) -> dict[str, Any]:
        current_gap = now - self.last_success if self.last_success is not None else 0.0
        cutoff = now - window_seconds
        while self.latencies and self.latencies[0][0] < cutoff:
            self.latencies.popleft()
        values = [latency for _, latency in self.latencies]
        state = "OK"
        if self.in_flight:
            state = "WAIT"
        if self.fail:
            state = "FAIL"
        return {
            "user": self.spec.display,
            "user_id": self.spec.user_id,
            "lane": self.spec.lane,
            "provider": self.spec.provider,
            "model": self.spec.model,
            "sent": self.sent,
            "responses": self.responses,
            "ok": self.ok,
            "fail": self.fail,
            "in_flight": self.in_flight,
            "p95_ms": percentile(values, 0.95),
            "max_gap_s": max(self.max_gap, current_gap),
            "state": state,
            "last_status": self.last_status,
            "last_error": self.last_error,
        }


class Metrics:
    def __init__(self, specs: list[UserSpec], window_seconds: float) -> None:
        self.lock = threading.Lock()
        self.users = {spec.user_id: UserMetrics(spec) for spec in specs}
        self.order = [spec.user_id for spec in specs]
        self.window_seconds = window_seconds
        self.started = time.monotonic()

    def mark_sent(self, user_id: str) -> bool:
        with self.lock:
            return self.users[user_id].mark_sent()

    def record(self, user_id: str, result: dict[str, Any]) -> None:
        with self.lock:
            self.users[user_id].record(result, time.monotonic())

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self.lock:
            return [self.users[user_id].snapshot(now, self.window_seconds) for user_id in self.order]

    def overall(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        sent = sum(row["sent"] for row in rows)
        responses = sum(row["responses"] for row in rows)
        ok = sum(row["ok"] for row in rows)
        fail = sum(row["fail"] for row in rows)
        inflight = sum(1 for row in rows if row["in_flight"])
        with self.lock:
            cutoff = time.monotonic() - self.window_seconds
            latencies = [
                latency
                for user in self.users.values()
                for sample_time, latency in user.latencies
                if sample_time >= cutoff
            ]
        return {
            "sent": sent,
            "responses": responses,
            "ok": ok,
            "fail": fail,
            "in_flight": inflight,
            "availability": (ok / responses * 100.0) if responses else 0.0,
            "p95_ms": percentile(latencies, 0.95),
            "p99_ms": percentile(latencies, 0.99),
            "max_gap_s": max((row["max_gap_s"] for row in rows), default=0.0),
        }


def request_once(spec: UserSpec, timeout: float, insecure: bool) -> dict[str, Any]:
    if spec.lane == "openai-echo":
        path = "/v1/chat/completions"
        headers = {"Authorization": f"Bearer {spec.key}"}
        body = {
            "model": spec.model,
            "messages": [{"role": "user", "content": "reply with OK"}],
            "max_tokens": 1,
        }
    else:
        path = "/v1/messages"
        headers = {"x-api-key": spec.key, "anthropic-version": "2023-06-01"}
        body = {
            "model": spec.model,
            "messages": [{"role": "user", "content": "reply with OK"}],
            "max_tokens": 1,
        }

    request = urllib.request.Request(
        spec.url.rstrip("/") + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "pricetag-praxis-rollout-watch/2",
            **headers,
        },
    )
    context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    started = time.monotonic()
    result: dict[str, Any] = {
        "time": datetime.now(timezone.utc).isoformat(),
        "user": spec.display,
        "user_id": spec.user_id,
        "lane": spec.lane,
        "provider": spec.provider,
        "model": spec.model,
    }
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            response.read()
            result["status"] = response.status
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["error"] = f"http_{exc.code}"
        exc.read()
    except Exception as exc:  # noqa: BLE001 - transport failures are evidence
        reason = getattr(exc, "reason", None)
        detail = type(exc).__name__
        if reason:
            detail += f": {reason}"
        result["status"] = 0
        result["error"] = detail[:180]
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


def load_specs(data: dict[str, Any], urls: dict[str, str]) -> list[UserSpec]:
    users: list[UserSpec] = []
    for index, user in enumerate(data["users"], start=1):
        user_id = str(user["user_id"])
        digest = hashlib.sha256(user_id.encode()).digest()
        lane = "openai-echo" if digest[0] % 2 else "anthropic-echo"
        users.append(
            UserSpec(
                display=f"user-{index:03d}",
                user_id=user_id,
                key=user["key"],
                url=urls[lane],
                lane=lane,
                provider="OpenAI" if lane == "openai-echo" else "Anthropic",
                model="benchmark-echo",
            )
        )
    real = data["real_glm"]
    users.append(
        UserSpec(
            display="real-glm",
            user_id=str(real["user_id"]),
            key=real["key"],
            url=urls["real-glm"],
            lane="real-glm",
            provider="Z.ai",
            model="rits/zai-org/glm-5-3",
        )
    )
    return users


def compact_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def truncate(value: str, width: int) -> str:
    if width <= 1:
        return ""[:width]
    return value if len(value) <= width else value[: width - 1] + "…"


def draw(stdscr: Any, metrics: Metrics, rows: list[dict[str, Any]], page: int, page_size: int, args: argparse.Namespace, started: float, page_paused: bool, cluster: dict[str, Any]) -> int:
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
    except curses.error:
        pass

    overall = metrics.overall(rows)
    if overall["responses"] == 0:
        health, health_attr = "STARTING", curses.color_pair(2)
    elif overall["fail"] > args.max_failures:
        health, health_attr = "NO-GO", curses.color_pair(3) | curses.A_BOLD
    elif overall["p95_ms"] > args.max_p95_ms or overall["max_gap_s"] > args.max_gap_s:
        health, health_attr = "WATCH", curses.color_pair(2) | curses.A_BOLD
    else:
        health, health_attr = "GO", curses.color_pair(1) | curses.A_BOLD

    def put(y: int, text: str, attr: int = 0) -> None:
        if 0 <= y < height and width > 0:
            try:
                stdscr.addnstr(y, 0, text.ljust(width), max(0, width - 1), attr)
            except curses.error:
                pass

    cluster_label = f"{cluster['namespace']} @ {cluster['identity']}"
    title = "PRAXIS ROLLOUT WATCH"
    if args.phase.upper() != title:
        title += f"  •  {args.phase.upper()}"
    put(0, f" {title}  •  {cluster_label}", curses.color_pair(4) | curses.A_BOLD)
    put(1, f" {health:>8}  elapsed {compact_duration(time.monotonic() - started)}  •  fixed-screen operational evidence", health_attr)
    put(2, f" sent {overall['sent']:,}  responses {overall['responses']:,}  ok {overall['ok']:,}  fail {overall['fail']:,}  in-flight {overall['in_flight']}  availability {overall['availability']:.2f}%")
    put(3, f" latency p95/{int(args.window_seconds)}s {overall['p95_ms']:.0f} ms  p99 {overall['p99_ms']:.0f} ms  max success gap {overall['max_gap_s']:.1f}s  budgets: fail≤{args.max_failures}, p95≤{args.max_p95_ms:.0f}ms, gap≤{args.max_gap_s:.0f}s")
    put(4, "─" * max(1, width - 1), curses.color_pair(4))

    header_y = 5
    row_start = header_y + 2
    available_rows = max(1, height - row_start - 3)
    requested_rows = page_size if page_size > 0 else available_rows
    visible = max(1, min(requested_rows, available_rows))
    total_pages = max(1, math.ceil(len(rows) / visible))
    if page >= total_pages:
        page = 0
    elif page < 0:
        page = total_pages - 1
    first = page * visible
    shown = rows[first : first + visible]
    columns = [
        ("USER", 10), ("PROVIDER", 11), ("MODEL", 25), ("SENT", 7),
        ("RESP", 7), ("FAIL", 6), ("P95 ms", 8), ("GAP s", 8), ("STATE", 10), ("LAST", 24),
    ]
    header = ""
    right_aligned = {"SENT", "RESP", "FAIL", "P95 ms", "GAP s", "STATE", "LAST"}
    for name, size in columns:
        header += f"{name:>{size}}" if name in right_aligned else f"{name:<{size}}"
    put(header_y, truncate(header, width), curses.A_BOLD)
    put(header_y + 1, truncate("─" * len(header), width), curses.color_pair(4))
    for offset, row in enumerate(shown):
        state_attr = curses.color_pair(3) if row["state"] == "FAIL" else curses.color_pair(2) if row["state"] == "WAIT" else curses.color_pair(1)
        line = (
            f"{truncate(row['user'], 10):<10}"
            f"{truncate(row['provider'], 11):<11}"
            f"{truncate(row['model'], 25):<25}"
            f"{row['sent']:>7,}{row['responses']:>7,}{row['fail']:>6,}"
            f"{row['p95_ms']:>8.0f}{row['max_gap_s']:>8.1f}"
            f"{row['state']:>10}"
            f"{truncate(row['last_error'] or (str(row['last_status']) if row['last_status'] else '—'), 24):>24}"
        )
        put(row_start + offset, truncate(line, width), state_attr)

    footer = f" page {page + 1}/{total_pages}  users {first + 1}-{min(len(rows), first + visible)} of {len(rows)}  •  ←/→ page  p pause rotation  q quit"
    put(height - 2, truncate(footer, width), curses.color_pair(4))
    put(height - 1, truncate(" evidence: " + args.output, width), curses.A_DIM)
    stdscr.refresh()
    return page


def print_fallback(metrics: Metrics, rows: list[dict[str, Any]], args: argparse.Namespace, started: float) -> None:
    overall = metrics.overall(rows)
    print(json.dumps({
        "phase": args.phase,
        "elapsed_s": round(time.monotonic() - started, 1),
        **overall,
    }, sort_keys=True), flush=True)


def print_final_report(summary: dict[str, Any], verdict: str) -> None:
    overall = summary
    print()
    print("=" * 72)
    print("PRAXIS ROLLOUT WATCH COMPLETE")
    print("=" * 72)
    print(f"Verdict       : {verdict}")
    print(f"Phase         : {summary['phase']}")
    print(f"Cluster       : {summary['cluster']['namespace']} @ {summary['cluster']['identity']}")
    print(f"Duration      : {compact_duration(summary['duration_seconds'])}")
    print()
    print(
        f"Traffic       : {overall['sent']:,} sent  |  {overall['responses']:,} responses  |  "
        f"{overall['fail']:,} failures"
    )
    print(f"Availability  : {overall['availability']:.2f}%")
    print(f"Latency       : p95 {overall['p95_ms']:.0f} ms  |  p99 {overall['p99_ms']:.0f} ms")
    print(f"Max gap       : {overall['max_gap_s']:.1f}s")
    print()
    print(f"Evidence      : {summary['output']}")
    print(f"Summary       : {summary['summary_path']}")
    print("=" * 72)


def run_loop(stdscr: Any, specs: list[UserSpec], metrics: Metrics, stream: Any, executor: concurrent.futures.ThreadPoolExecutor, args: argparse.Namespace, started: float, cluster: dict[str, Any]) -> None:
    if stdscr is not None:
        stdscr.keypad(True)
        stdscr.nodelay(True)
        stdscr.timeout(200)

    deadline = started + args.duration if args.duration > 0 else float("inf")
    next_due = {
        spec.user_id: started + (int(hashlib.sha256(spec.user_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF) * args.user_interval
        for spec in specs
    }
    next_glm = started
    next_report = started
    page = 0
    page_paused = False
    next_page = started + args.page_seconds
    pending: dict[concurrent.futures.Future[dict[str, Any]], str] = {}

    while time.monotonic() < deadline or pending:
        now = time.monotonic()
        if now < deadline:
            for spec in specs:
                if spec.lane == "real-glm":
                    continue
                if now >= next_due[spec.user_id] and metrics.mark_sent(spec.user_id):
                    pending[executor.submit(request_once, spec, args.timeout, args.insecure)] = spec.user_id
                    next_due[spec.user_id] = now + args.user_interval
            if now >= next_glm:
                spec = next(spec for spec in specs if spec.lane == "real-glm")
                if metrics.mark_sent(spec.user_id):
                    pending[executor.submit(request_once, spec, args.timeout, args.insecure)] = spec.user_id
                next_glm = now + args.real_glm_interval

        for future in [future for future in pending if future.done()]:
            user_id = pending.pop(future)
            result = future.result()
            metrics.record(user_id, result)
            stream.write(json.dumps(result, sort_keys=True) + "\n")
            stream.flush()

        rows = metrics.snapshot()
        if stdscr is not None:
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                deadline = time.monotonic()
            elif key in (curses.KEY_RIGHT, curses.KEY_NPAGE, ord("n"), ord("l")):
                page += 1
                next_page = now + args.page_seconds
            elif key in (curses.KEY_LEFT, curses.KEY_PPAGE, ord("b"), ord("h")):
                page -= 1
                next_page = now + args.page_seconds
            elif key in (ord("p"), ord("P")):
                page_paused = not page_paused
            if not page_paused and now >= next_page:
                page += 1
                next_page = now + args.page_seconds
            page = draw(stdscr, metrics, rows, page, args.page_size, args, started, page_paused, cluster)
        elif now >= next_report:
            print_fallback(metrics, rows, args, started)
            next_report = now + args.stats_interval
        time.sleep(0.02)

    for future, user_id in list(pending.items()):
        result = future.result()
        metrics.record(user_id, result)
        stream.write(json.dumps(result, sort_keys=True) + "\n")
        stream.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fixed-screen Praxis rollout traffic watch with JSONL evidence")
    parser.add_argument("--kubeconfig", help="kubeconfig to use; otherwise use the current oc context")
    parser.add_argument("--namespace", default=os.getenv("PRICETAG_NAMESPACE", "ai-gateway-dogfood"))
    parser.add_argument("--users-file", help="synthetic user inventory; also read from PRICETAG_USERS_FILE")
    parser.add_argument("--openai-echo-route", default="ai-gateway-openai-echo")
    parser.add_argument("--anthropic-echo-route", default="ai-gateway-benchmark")
    parser.add_argument("--real-glm-route", default="ai-gateway-glm-echo")
    parser.add_argument("--openai-echo-url", help="explicit URL override; normally discovered with oc")
    parser.add_argument("--anthropic-echo-url", help="explicit URL override; normally discovered with oc")
    parser.add_argument("--real-glm-url", help="explicit URL override; normally discovered with oc")
    parser.add_argument("--duration", type=float, default=0, help="seconds; 0 runs until q/Ctrl-C")
    parser.add_argument("--user-interval", type=float, default=5)
    parser.add_argument("--real-glm-interval", type=float, default=5)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--stats-interval", type=float, default=5)
    parser.add_argument("--output", help="JSONL evidence path; defaults to the current directory")
    parser.add_argument("--phase", default="PRAXIS ROLLOUT WATCH")
    parser.add_argument("--ui", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--dry-run", action="store_true", help="validate oc, namespace, routes, deployments, and users without sending traffic")
    parser.add_argument("--page-size", type=int, default=0, help="rows per page; 0 fills the terminal")
    parser.add_argument("--page-seconds", type=float, default=8)
    parser.add_argument("--max-failures", type=int, default=0)
    parser.add_argument("--max-p95-ms", type=float, default=5000)
    parser.add_argument("--max-gap-s", type=float, default=30)
    parser.add_argument("--window-seconds", type=float, default=60, help="rolling latency window used by the dashboard")
    parser.add_argument("--insecure", action="store_true", help="disable TLS verification for staging certificates")
    args = parser.parse_args()

    try:
        cluster, urls, users_file = preflight(args)
    except PreflightError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps({"status": "ready", "cluster": cluster, "users_file": str(users_file)}, indent=2, sort_keys=True))
        return 0

    data = json.loads(users_file.read_text())
    specs = load_specs(data, urls)
    if not specs:
        raise SystemExit("users-file has no users")

    if args.window_seconds <= 0:
        raise SystemExit("--window-seconds must be greater than zero")
    metrics = Metrics(specs, args.window_seconds)
    output = Path(args.output) if args.output else Path.cwd() / f"praxis-rollout-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    args.output = str(output)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(128, len(specs)))
    started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    use_ui = args.ui == "always" or (args.ui == "auto" and sys.stdout.isatty() and sys.stdin.isatty())
    try:
        with output.open("w", encoding="utf-8") as stream:
            if use_ui:
                curses.wrapper(lambda stdscr: run_loop(stdscr, specs, metrics, stream, executor, args, started, cluster))
            else:
                run_loop(None, specs, metrics, stream, executor, args, started, cluster)
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown(wait=True)

    rows = metrics.snapshot()
    summary = {
        "phase": args.phase,
        "window_seconds": args.window_seconds,
        "started_at": started_at.isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": time.monotonic() - started,
        "output": str(output),
        "cluster": cluster,
        "users_file": str(users_file),
        **metrics.overall(rows),
        "users": rows,
    }
    summary_path = output.with_suffix(".summary.json")
    summary["summary_path"] = str(summary_path)
    overall = metrics.overall(rows)
    verdict = "NO-GO" if overall["fail"] > args.max_failures else "GO"
    summary.update(overall)
    summary["verdict"] = verdict
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print_final_report(summary | {**overall}, verdict)
    return 0 if verdict == "GO" else 2


if __name__ == "__main__":
    raise SystemExit(main())
