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

"""Reproduce CSRF mismatch using real UI clicks and concurrent browser reads."""

import asyncio
import json
import runpy
import sys
from pathlib import Path
from uuid import uuid4

from playwright.async_api import Page, Response, async_playwright

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
runner = runpy.run_path(str(ROOT / "runner" / "reproduce.py"))

BASE = "http://127.0.0.1:19088"
REPORT = ROOT / "reports" / ("comparison-" + uuid4().hex + ".json")
REPORT.parent.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    data = {
        "run_id": uuid4().hex,
        "base_url": BASE,
        "modes": [],
        "notes": "Unmodified fixture client and gateway; a UI button invokes write; no cookie/header overrides, fetch interception, or pause after token preparation.",
    }
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        data["browser_version"] = browser.version
        try:
            for mode in ("ui-and-read",):
                ctx = await browser.new_context()
                pages = [await ctx.new_page(), await ctx.new_page()]
                for page in pages:
                    await page.goto(BASE)
                    await page.wait_for_function(
                        "typeof window.csrfRepro?.write === 'function'"
                    )
                    await page.evaluate("""() => {
                      const button = document.createElement('button');
                      button.id = 'diagnostic-submit'; button.textContent = 'Diagnostic submit';
                      button.onclick = () => { window.diagnosticResult = window.csrfRepro.write(window.diagnosticId); };
                      document.body.prepend(button);
                    }""")
                await pages[0].evaluate("() => window.csrfRepro.read()")
                for index, page in enumerate(pages):
                    result = await page.evaluate(
                        "id => window.csrfRepro.write(id)",
                        f"{data['run_id']}-{mode}-baseline-{index}",
                    )
                    assert result["status"] == 201
                rows = []
                for delay in (10, 0):
                    for trial in range(50):
                        ids = [
                            f"{data['run_id']}-{mode}-{delay}-{trial}-{tab}"
                            for tab in ("a", "b")
                        ]
                        for page, op in zip(pages, ids, strict=True):
                            await page.evaluate(
                                "id => { window.diagnosticId = id; }", op
                            )
                        start = asyncio.get_running_loop().time()

                        async def group(
                            page: Page, op: str, wait: float, deadline: float
                        ) -> tuple[str, Response, Response]:
                            await asyncio.sleep(
                                max(
                                    0,
                                    deadline + wait - asyncio.get_running_loop().time(),
                                )
                            )
                            async with page.expect_response(
                                lambda r: (
                                    r.request.method == "POST"
                                    and r.url == BASE + "/api/write"
                                )
                            ) as post_event:
                                async with page.expect_response(
                                    lambda r: (
                                        r.request.method == "GET"
                                        and r.url == BASE + "/api/read"
                                    )
                                ) as read_event:
                                    _, read_result = await asyncio.gather(
                                        page.locator("#diagnostic-submit").click(),
                                        page.evaluate("() => window.csrfRepro.read()"),
                                    )
                                    result = [
                                        await page.evaluate(
                                            "() => window.diagnosticResult"
                                        ),
                                        read_result,
                                    ]
                            post, read = await post_event.value, await read_event.value
                            await post.body()
                            await read.body()
                            assert result[1]["status"] == 200
                            return op, post, read

                        groups = await asyncio.gather(
                            group(pages[0], ids[0], 0, start),
                            group(pages[1], ids[1], delay / 1000, start),
                        )
                        counts = await asyncio.to_thread(runner["read_counts"], BASE)
                        for op, post, read in groups:
                            row = await runner["inspect_write"](post, op, counts)
                            pt, gt = post.request.timing, read.request.timing
                            row.update(
                                {
                                    "delay_ms": delay,
                                    "trial": trial,
                                    "get_start_ms": round(
                                        gt["startTime"] + gt["requestStart"], 3
                                    ),
                                    "get_headers_ms": round(
                                        gt["startTime"] + gt["responseStart"], 3
                                    ),
                                    "post_headers_ms": round(
                                        pt["startTime"] + pt["responseStart"], 3
                                    ),
                                }
                            )
                            rows.append(row)
                            if row["native_csrf_mismatch"]:
                                print(
                                    "Mismatch verified", mode, delay, trial, flush=True
                                )
                item = {
                    "mode": mode,
                    "posts": len(rows),
                    "mismatches": sum(row["native_csrf_mismatch"] for row in rows),
                    "reads": len(rows),
                    "rows": rows,
                }
                data["modes"].append(item)
                REPORT.write_text(json.dumps(data, indent=2) + "\n")
                print(
                    {key: value for key, value in item.items() if key != "rows"},
                    flush=True,
                )
                await ctx.close()
        finally:
            await browser.close()
    print("Report:", REPORT)


if __name__ == "__main__":
    asyncio.run(main())
