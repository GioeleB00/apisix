#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Observe real browser CSRF mismatches without modifying requests or cookies."""

import argparse
import asyncio
import json
import time
from collections import Counter
from datetime import UTC, datetime
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from importlib.metadata import version
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import (
    BrowserContext,
    Page,
    Request,
    Response,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

TOKEN_NAME = "apisix-csrf-token"
ROOT = Path(__file__).resolve().parents[1]


class ProbeError(RuntimeError):
    """A failed consistency check with a message safe to include in reports."""


class Report(TypedDict):
    """Public evidence only: never place raw headers, cookies or tokens here."""

    run_id: str
    outcome: str
    started_at: str
    base_url: str
    settings: dict[str, object]
    playwright_version: str
    browser_version: str
    baseline: list[dict[str, object]]
    trials: list[dict[str, object]]
    recoveries: list[dict[str, object]]
    summary: dict[str, int]
    error: str | None
    elapsed_seconds: float


def arguments() -> argparse.Namespace:
    """Keep workload controls explicit and bounded for a local experiment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:19088")
    parser.add_argument(
        "--trials", type=int, default=5, help="Trials per delay (1..1000)"
    )
    parser.add_argument("--baseline-writes", type=int, default=5)
    parser.add_argument(
        "--delays-ms", type=int, nargs="+", default=[200, 100, 50, 10, 0]
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    origin = urlsplit(args.base_url)
    if (
        origin.scheme != "http"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path not in ("", "/")
    ):
        parser.error("--base-url must be an HTTP origin without credentials or a path")
    if not 1 <= args.trials <= 1000 or not 1 <= args.baseline_writes <= 1000:
        parser.error("--trials and --baseline-writes must be between 1 and 1000")
    if any(not 0 <= delay <= 1000 for delay in args.delays_ms):
        parser.error("--delays-ms values must be between 0 and 1000")
    if len(set(args.delays_ms)) != len(args.delays_ms):
        parser.error("--delays-ms values must be unique")
    args.base_url = args.base_url.rstrip("/")
    return args


def read_counts(base_url: str) -> dict[str, int]:
    """Read the upstream oracle using HTTP outside the browser cookie store."""
    origin = urlsplit(base_url)
    connection = HTTPConnection(origin.hostname, origin.port, timeout=10)
    try:
        connection.request("GET", "/api/stats")
        response = connection.getresponse()
        payload = json.loads(response.read())
        if response.status != 200 or not isinstance(payload, dict):
            raise ProbeError("The upstream count endpoint is unavailable")
        counts = payload.get("writes")
        if not isinstance(counts, dict) or any(
            not isinstance(key, str) or type(value) is not int or value < 0
            for key, value in counts.items()
        ):
            raise ProbeError("The upstream count response has an unexpected shape")
        return counts
    finally:
        connection.close()


def interval(response: Response) -> tuple[float, float]:
    """Use browser network timings, not nominal action scheduling times."""
    timing = response.request.timing
    if timing["requestStart"] < 0 or timing["responseEnd"] < 0:
        raise ProbeError("A completed request lacks browser network timing")
    return (
        timing["startTime"] + timing["requestStart"],
        timing["startTime"] + timing["responseEnd"],
    )


async def send(
    page: Page, operation_id: str, mixed: bool, deadline: float
) -> tuple[Response, Response | None, float]:
    """Schedule before JS starts; never pause a request after its header is ready."""
    loop = asyncio.get_running_loop()
    await asyncio.sleep(max(0, deadline - loop.time()))
    launched = loop.time()
    read_response: Response | None = None
    async with page.expect_response(
        lambda r: r.request.method == "POST" and urlsplit(r.url).path == "/api/write"
    ) as written:
        if mixed:
            async with page.expect_response(
                lambda r: (
                    r.request.method == "GET" and urlsplit(r.url).path == "/api/read"
                )
            ) as read:
                await page.evaluate(
                    "id => Promise.all([window.csrfRepro.write(id), window.csrfRepro.read()])",
                    operation_id,
                )
            read_response = await read.value
            await read_response.body()
        else:
            await page.evaluate("id => window.csrfRepro.write(id)", operation_id)
    response = await written.value
    await response.body()
    return response, read_response, launched


async def inspect_write(
    response: Response, operation_id: str, counts: dict[str, int]
) -> dict[str, object]:
    """Compare actual wire values in memory and serialize only presence/equality."""
    headers = await response.request.all_headers()
    cookies = SimpleCookie()
    cookies.load(headers.get("cookie", ""))
    cookie = cookies.get(TOKEN_NAME)
    header = headers.get(TOKEN_NAME)
    present = bool(cookie and cookie.value and header)
    coherent = bool(present and cookie.value == header)
    payload = await response.json()
    if not isinstance(payload, dict):
        raise ProbeError("A write response is not a JSON object")
    mismatch = (
        response.status == 401 and payload.get("error_msg") == "csrf token mismatch"
    )
    count = counts.get(operation_id, 0)
    if response.status == 201:
        if (
            not coherent
            or count != 1
            or payload.get("operation_id") != operation_id
            or payload.get("accepted_count") != 1
        ):
            raise ProbeError(
                "A successful write lacks coherent headers or exactly one upstream write"
            )
    elif not (mismatch and present and not coherent and count == 0):
        raise ProbeError(
            f"Unexpected write result: HTTP {response.status}, upstream count {count}"
        )
    start, end = interval(response)
    return {
        "operation_id": operation_id,
        "status": response.status,
        "cookie_present": bool(cookie and cookie.value),
        "header_present": bool(header),
        "cookie_header_match": coherent,
        "native_csrf_mismatch": mismatch,
        "upstream_count_before_recovery": count,
        "request_start_ms": round(start, 3),
        "response_end_ms": round(end, 3),
    }


async def recover(page: Page, operation_id: str, base_url: str) -> dict[str, object]:
    """Make one explicit retry after both tabs settle, keeping the same operation ID."""
    response, _, _ = await send(
        page, operation_id, False, asyncio.get_running_loop().time()
    )
    counts = await asyncio.to_thread(read_counts, base_url)
    row = await inspect_write(response, operation_id, counts)
    if row["status"] != 201:
        raise ProbeError("The explicit recovery attempt did not succeed")
    return row


def trial_timing(
    first: tuple[Response, Response | None, float],
    second: tuple[Response, Response | None, float],
) -> dict[str, object]:
    """Describe actual overlap separately from the requested delay."""
    windows: list[list[tuple[float, float]]] = []
    for capture in (first, second):
        if capture[1] is None or capture[1].status != 200:
            raise ProbeError("A concurrent read did not complete successfully")
        windows.append([interval(capture[0]), interval(capture[1])])
    overlaps = sum(
        max(left[0], right[0]) < min(left[1], right[1])
        for i, left in enumerate(windows[0])
        for j, right in enumerate(windows[1])
        if i == 0 or j == 0
    )
    return {
        "action_gap_ms": round((second[2] - first[2]) * 1000, 3),
        "post_send_gap_ms": round(interval(second[0])[0] - interval(first[0])[0], 3),
        "cross_tab_overlaps_involving_post": overlaps,
    }


async def run_trial(
    pages: tuple[Page, Page],
    delay: int,
    trial: int,
    report: Report,
    expected_posts: Counter[str],
) -> None:
    """Run exactly two POST/GET pairs, inspect them, then recover any safe rejection."""
    ids = tuple(f"{report['run_id']}-d{delay}-r{trial}-{tab}" for tab in ("a", "b"))
    expected_posts.update(ids)
    start = asyncio.get_running_loop().time()
    first, second = await asyncio.wait_for(
        asyncio.gather(
            send(pages[0], ids[0], True, start),
            send(pages[1], ids[1], True, start + delay / 1000),
        ),
        timeout=35,
    )
    counts = await asyncio.to_thread(read_counts, report["base_url"])
    rows = [
        await inspect_write(capture[0], op_id, counts)
        for capture, op_id in zip((first, second), ids, strict=True)
    ]
    report["trials"].append(
        {
            "delay_ms": delay,
            "trial": trial,
            "writes": rows,
            **trial_timing(first, second),
        }
    )
    for page, operation_id, row in zip(pages, ids, rows, strict=True):
        if row["native_csrf_mismatch"]:
            expected_posts.update([operation_id])
            report["recoveries"].append(
                await recover(page, operation_id, report["base_url"])
            )


async def run_baseline(
    pages: tuple[Page, Page], count: int, report: Report, expected: Counter[str]
) -> None:
    """Verify sequential writes from both tabs before introducing concurrency."""
    for index in range(count):
        op_id = f"{report['run_id']}-baseline-{index}"
        expected.update([op_id])
        response, _, _ = await send(
            pages[index % 2], op_id, False, asyncio.get_running_loop().time()
        )
        counts = await asyncio.to_thread(read_counts, report["base_url"])
        row = await inspect_write(response, op_id, counts)
        report["baseline"].append(row)
        if row["status"] != 201:
            raise ProbeError("The sequential positive control failed")


def verify_traffic(seen: list[Request], expected: Counter[str], reads: int) -> None:
    """Detect extra requests, including retries hidden from the response collector."""
    actual: Counter[str] = Counter()
    for request in seen:
        if request.method == "POST":
            payload = request.post_data_json
            if not isinstance(payload, dict) or not isinstance(
                payload.get("operation_id"), str
            ):
                raise ProbeError("An observed POST has an unexpected body")
            actual.update([payload["operation_id"]])
    if actual != expected or sum(r.method == "GET" for r in seen) != reads:
        raise ProbeError(
            "Browser traffic differs from planned requests and explicit recoveries"
        )


def summarize(report: Report, expected: Counter[str], reads: int) -> dict[str, int]:
    """Count initial mismatches separately from successful explicit recoveries."""
    return {
        "baseline_posts": len(report["baseline"]),
        "initial_concurrent_posts": 2 * len(report["trials"]),
        "verified_mismatches": sum(
            bool(row["native_csrf_mismatch"])
            for item in report["trials"]
            for row in item["writes"]
        ),
        "explicit_recoveries": len(report["recoveries"]),
        "observed_browser_gets": reads,
        "observed_browser_posts": sum(expected.values()),
    }


async def campaign(
    context: BrowserContext, args: argparse.Namespace, report: Report
) -> None:
    """Prepare shared-cookie tabs and orchestrate controls and all timing profiles."""
    seen: list[Request] = []

    def observe(request: Request) -> None:
        # Metadata only; headers are read after the competing requests finish.
        if urlsplit(request.url).path in ("/api/read", "/api/write"):
            seen.append(request)

    context.on("request", observe)
    pages = (await context.new_page(), await context.new_page())
    for page in pages:
        page.set_default_timeout(10000)
        await page.goto(report["base_url"])
        await page.wait_for_function("typeof window.csrfRepro?.write === 'function'")
    await pages[0].evaluate("() => window.csrfRepro.read()")
    expected_posts: Counter[str] = Counter()
    await run_baseline(pages, args.baseline_writes, report, expected_posts)
    for delay in args.delays_ms:
        before = len(report["trials"])
        for trial in range(args.trials):
            await run_trial(pages, delay, trial, report, expected_posts)
        rows = [row for item in report["trials"][before:] for row in item["writes"]]
        mismatches = sum(bool(row["native_csrf_mismatch"]) for row in rows)
        print(
            f"delay={delay}ms: {len(rows)} initial POSTs, {mismatches} verified mismatches",
            flush=True,
        )
    expected_reads = 1 + 2 * args.trials * len(args.delays_ms)
    verify_traffic(seen, expected_posts, expected_reads)
    report["summary"] = summarize(report, expected_posts, expected_reads)


async def run_browser(args: argparse.Namespace, report: Report) -> None:
    """Own browser lifetime and close the isolated context even when a check fails."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        context: BrowserContext | None = None
        try:
            report["browser_version"] = browser.version
            context = await browser.new_context()
            await campaign(context, args, report)
        finally:
            if context is not None:
                await context.close()
            await browser.close()


def make_report(args: argparse.Namespace, run_id: str) -> Report:
    """Initialize a public report before opening the browser so errors retain evidence."""
    return {
        "run_id": run_id,
        "outcome": "error",
        "started_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "settings": {
            "trials_per_delay": args.trials,
            "delays_ms": args.delays_ms,
            "baseline_writes": args.baseline_writes,
            "headed": args.headed,
        },
        "playwright_version": version("playwright"),
        "browser_version": "",
        "baseline": [],
        "trials": [],
        "recoveries": [],
        "summary": {},
        "error": None,
        "elapsed_seconds": 0,
    }


def main() -> int:
    """Write a sanitized report for reproduced, not-observed and error outcomes."""
    args = arguments()
    started = time.monotonic()
    run_id = uuid4().hex
    report = make_report(args, run_id)
    code = 2
    try:
        asyncio.run(run_browser(args, report))
        found = report["summary"]["verified_mismatches"] > 0
        report["outcome"] = "reproduced" if found else "not_observed"
        code = 0 if found else 1
    except ProbeError as error:
        report["error"] = str(error)
    except (PlaywrightError, OSError, ValueError, TimeoutError) as error:
        # Library exceptions can carry response data; keep their type only.
        report["error"] = (
            f"{type(error).__name__}: browser, transport or report prerequisite failed"
        )
    except KeyboardInterrupt:
        report["error"] = "Interrupted by the user"
        code = 130
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    output = args.output or ROOT / "reports" / f"csrf-{run_id}.json"
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
    except OSError:
        print("ERROR: report could not be written; choose a new writable --output path")
        return 2
    print(f"{report['outcome'].upper()}: {report['summary']}")
    if report["error"]:
        print(report["error"])
    print(f"Report: {output}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
