<!--
Licensed to the Apache Software Foundation (ASF) under one or more
contributor license agreements. See the NOTICE file distributed with
this work for additional information regarding copyright ownership.
The ASF licenses this file to You under the Apache License, Version 2.0
(the "License"); you may not use this file except in compliance with
the License. You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# APISIX CSRF Concurrency Reproduction

This directory provides a standalone browser experiment for
intermittent CSRF cookie/header mismatches during concurrent requests. It is
independent of the application where the behavior was originally observed.
The setup runs the unmodified APISIX plugin, a small HTTP server and a
Python runner controlling two browser tabs that share cookies.

**Current status:** isolated browser reproduction confirmed on 2026-11-01.
The UI probe recorded **198 successful initial POSTs and two native CSRF mismatch
401s out of 200**, with zero upstream writes for both rejected operations.
The included evidence is sanitized. Outcomes depend on browser/network scheduling;
a later run with no mismatch does not invalidate the recorded occurrence.

## Quick Start

From the APISIX checkout root:

```bash
cd repro/csrf-concurrency
./scripts/bootstrap.sh
.venv/bin/python runner/reproduce_ui.py
```

The UI probe prints verified mismatches and saves a fresh JSON report under
`reports/`. It uses the default gateway port `19088`; keep that port in `.env`.
A completed probe exits normally even when no mismatch is observed, so inspect
its `mismatches` count. Unexpected responses or inconsistent counters fail.
The experiment performs writes only to the fixture's in-memory operation counter.

## UI Reproduction

`runner/reproduce_ui.py` opens two headless Chromium tabs in one browser context.
A temporary DOM button calls the existing `window.csrfRepro.write(id)` function;
the client and native `fetch` are unchanged. Python starts one real button click
and one browser GET together per tab using `asyncio.gather`. The GET can already
be in flight when the click reaches the write handler. There is no pause between
reading the token and invoking fetch, transport interception, cookie/header
override, continuous GET burst, or automatic retry.

Two sequential POSTs check the positive path first. Then each nominal inter-tab
delay (10 and 0 ms) has 50 trials, with one POST and one GET per tab: 200 initial
concurrent POSTs and 200 concurrent GETs in total. Initial cookie issuance and
positive controls are additional requests. A mismatch is accepted as evidence
only for the exact native HTTP 401 error, nonempty but different outgoing
cookie/header values, and zero upstream writes for that operation ID.

Recorded observations are versioned in `evidence/observed-ui-run.json`: 198 POSTs
returned 201, two returned the native mismatch 401, and all 200 concurrent GETs
returned 200. Both failures occurred at nominal 10 ms; this is not a universal
threshold or a measured production failure rate. No recovery was attempted by
this probe. It reuses the wire/counter checks in `runner/reproduce.py`.

No OIDC provider, application database, session plugin, Redis, etcd, CORS, or
additional proxy participates. The bootstrap generates the local signing key;
no key, raw token, cookie, or browser storage is included in the repository.

## Environment

The original observations were made on Linux x86_64 with the official
`apache/apisix:3.18.0-ubuntu` Docker image and OpenResty `1.29.2.4`.
The Docker stack uses this APISIX image and builds the fixture server from
`python:3.12.16-slim-bookworm`. Both services have been started and checked at
runtime. The server's Python patch version is pinned in its Dockerfile.

The Python project declares **Python 3.12** and **Playwright 1.64.1** as its only
direct Python dependency. The runner uses a real Chromium browser;
Python coordinates the browser rather than manually constructing HTTP cookie
headers. Ruff **0.16.10** is the development dependency for linting and formatting;
no mypy or other static type checker is configured. Python functions have explicit
type annotations, and Ruff checks their presence. Dependencies are recorded in
`pyproject.toml` and `uv.lock`; bootstrap synchronizes them into the local `.venv`.

## Current Prerequisites

- Bash and standard Unix utilities (`sh`, `dirname`, `mktemp`, `rm`, `grep`,
  `od`, `tr`, `sed` and `ln`), with `/dev/urandom` available.
- Docker with a running local daemon, accessible to the current user, and Docker
  Compose supporting `up --wait --wait-timeout`. Install these manually using the
  [Docker installation guide](https://docs.docker.com/get-started/get-docker/).
- uv in `PATH` or its installation directory; otherwise the bootstrap installs
  it. First-time installation requires `curl` or `wget`, network access to the
  official installer and release downloads, and a writable installation directory.

First-time setup needs access to Python/package/browser downloads and public
image registries. Bootstrap checks Docker/Compose and daemon access, prepares
Python through uv and installs matching Chromium binaries. If native browser
libraries are missing, it reports the error and the manual installation command;
it does not run an administrator-level package installation automatically.

## Bootstrap

From this directory:

```bash
./scripts/bootstrap.sh
```

The command finds or installs uv, checks Docker/Compose and daemon access, then
creates `.env` with a generated signing key only if absent. It validates Compose,
synchronizes the local `.venv` from `uv.lock`, installs/reuses Chromium and checks
headless startup before building or starting containers. It then builds the HTTP
server image and runs `up --detach --build --wait --wait-timeout 120`.
Repeated runs reuse the prepared environment, preserve `.env` and reuse matching
running containers. Docker is never installed automatically. Details and failure
behavior are documented in [scripts/README.md](scripts/README.md).

After successful startup, open `http://127.0.0.1:19088/` (or the port chosen in
`.env`). Fetch the CSRF cookie, then use the manual request buttons. The stack
stays running after the bootstrap exits.

The host `.venv` is dedicated to browser automation and development tools.
`.python-version` selects Python 3.12; uv downloads a compatible interpreter if
needed. Playwright downloads are kept in its normal user cache and reused on
subsequent runs. Python for the HTTP server runs separately inside its image.
A browser startup or dependency error aborts bootstrap before `compose up`;
existing containers are left running.

## Gateway Configuration

### Configuration Files

| File | Purpose |
| --- | --- |
| `apisix/config.yaml` | Process settings, deployment mode and enabled plugins. |
| `apisix/apisix.yaml` | Routes, the CSRF plugin settings and the shared upstream. |
| `compose.yaml` | Official APISIX image, environment, mounts and local HTTP port. |

APISIX uses `deployment.role: data_plane` and
`role_data_plane.config_provider: yaml` to load routes directly from the local
file. No etcd or Admin API is needed. The route file ends with the required
`#END` marker. See the [APISIX standalone documentation](https://github.com/apache/apisix/blob/3.18.0/docs/en/latest/deployment-modes.md#standalone).

The process listens on HTTP port `9080` inside the container. Admin API, Control
API, TLS and IPv6 listeners are disabled for this local fixture. One Nginx worker
keeps resource usage small while still allowing concurrent HTTP requests. Only
`csrf` is enabled in the plugin list, using the image's original implementation.

### Route Contract

| Browser request | CSRF behavior | Destination |
| --- | --- | --- |
| GET/HEAD `/`, `/index.html`, `/app.js` | Static page/assets; no CSRF plugin. | `server:8080`. |
| GET/HEAD/OPTIONS `/api/*` | Native safe-method behavior; response issues a CSRF cookie. | `server:8080`. |
| POST `/api/*` | Matching cookie/header, valid signature and unexpired token required. | `server:8080`, after CSRF validation. |

The client obtains its first token through GET `/api/read` and submits
POST `/api/write`. Both share the API route and its token settings:

- Cookie and custom header: `apisix-csrf-token`, the native default name.
- Token lifetime: `7200` seconds.
- Issuance and validation: the unmodified native plugin, including its current
  per-response cookie replacement.

Page and API requests use the same browser origin through APISIX. No CORS plugin,
OIDC configuration, Redis session or authorization service is included. Paths
are forwarded unchanged; the small HTTP server implements those endpoints.

The `server` service supplies this upstream on the private Compose network.
APISIX starts only after that service's HTTP healthcheck succeeds.

### Compose Settings and Signing Key

The Compose project is named `apisix-csrf-repro` and contains `server` and
`apisix`. The gateway uses `apache/apisix:3.18.0-ubuntu`. HTTP is published only on
`127.0.0.1:19088`; set `CSRF_REPRO_HTTP_PORT` to choose a different host port.
No Admin, Control or TLS ports are published.

Both configuration files are bind-mounted read-only into
`/usr/local/apisix/conf/`. `create_host_path: false` prevents Compose from creating
an empty directory if a source file is missing. The plugin source is not mounted
or replaced. Both services use the default Compose network; there are no
persistent volumes or external infrastructure dependencies.

The versioned `.env.example` declares the host port and an empty signing-key
entry. Bootstrap creates the ignored local `.env` with permissions `600`,
preserves the template's port, and generates `CSRF_REPRO_SIGNING_KEY`. Existing
files, including an existing key or a customized port, are kept unchanged.

Compose reads `.env` automatically for interpolation, then explicitly passes the
signing key into the container through `environment`. No extra `env_file` entry,
shell `source`, or bind mount of the secret file is needed. Already exported
shell variables override `.env`; unset stale overrides if necessary. Compose
still rejects an absent or empty signing key, including in an existing local
file. APISIX expands `${{CSRF_REPRO_SIGNING_KEY}}` in its route file, and
`nginx_config.envs` preserves that variable for Nginx workers.

After bootstrap, configuration can be checked from this directory:

```bash
docker compose config --quiet
```

This validates Compose configuration without starting containers. The server
healthcheck queries its local API; the APISIX healthcheck makes an HTTP request
through the configured route and verifies the upstream JSON response. Automatic
restart is disabled so a failed experimental run remains visible.

`server/Dockerfile` uses one Python slim stage because the fixture has no external
server dependencies or build step. It copies only `server.py`, `app.js` and
`index.html`, then runs as UID/GID `10001:10001`. The build context is limited to
`server/`; its `.dockerignore` allowlists the source files and build instructions,
excluding unrelated files and credentials. The container filesystem is read-only.
Only container port `8080` is exposed internally, with no host publication.

An unchanged bootstrap reuses both containers and retains write counts. Updating
the server image or Compose settings may recreate containers, which clears the
server's in-memory counts. APISIX hot-reloads route-file changes; changes to its
bind-mounted process configuration require an explicit APISIX restart.

## HTTP Server and Browser Client

`server/server.py` uses Python's `ThreadingHTTPServer`; there is no web framework
or extra server dependency. Direct local execution listens on `127.0.0.1:8080`
by default. The Docker command explicitly binds to `0.0.0.0:8080` on the private
Compose network, while only APISIX is published on the host.

| Endpoint | Behavior |
| --- | --- |
| GET `/api/read` | Returns `200` and `{"ok": true}`; APISIX owns CSRF cookie issuance. |
| POST `/api/write` | Accepts a JSON object containing a nonempty `operation_id` string, up to 128 characters. Returns `201`, that ID and its `accepted_count`. |
| GET `/api/stats` | Returns per-ID write counts and `total_writes`. It does not reset state. |
| GET/HEAD `/`, `/index.html`, `/app.js` | Serves only the three declared static paths. |
| OPTIONS on the three API endpoints | Returns `204` without incrementing counters. |

Writes are counted in memory under a lock because requests run in separate
threads. Restarting the server clears the counts. Repeated POSTs with the same
ID each increment its count: this is an observation tool, not a deduplication
mechanism. Malformed JSON, wrong body types and bodies over 4096 bytes are
rejected without counting a write. The server does not issue or validate CSRF
tokens; that responsibility remains with APISIX.

### Python File and Handler Methods

[server/server.py](server/server.py) contains the fixed asset map, body-size limit,
in-memory write counts, the request-handler class and the server entry point.
`ASSET_DIR` and `ASSETS` select the only static files that can be served;
`MAX_BODY_BYTES` limits POST bodies to 4096 bytes. `WRITE_COUNTS` holds counts by
operation ID, and `WRITE_LOCK` protects updates and snapshots across threads.

`Handler` extends `BaseHTTPRequestHandler`, uses HTTP/1.1 and sets a five-second
socket timeout. The standard library parses requests and calls its `do_*` methods.

| Method | Purpose |
| --- | --- |
| `send_body()` | Sends status, content type, content length and cache/safety headers; omits the body for HEAD. |
| `send_json()` | Encodes a dictionary as JSON and delegates response writing to `send_body()`. |
| `reject()` | Returns a JSON error and closes the connection so unread body bytes cannot become another request. |
| `do_GET()` | Handles reads, counter snapshots and the fixed HTML/JS assets; rejects unknown paths. |
| `do_HEAD()` | Reuses GET routing and headers while `send_body()` suppresses the response body. |
| `do_OPTIONS()` | Returns `204` and allowed methods for the API endpoints without changing counters. |
| `do_POST()` | Validates request framing, JSON and operation ID, increments the count under the lock, then returns `201`. |

`main()` is outside the class: it parses `--host` and `--port`, starts
`ThreadingHTTPServer`, and closes it when interrupted. It runs only when the
file is executed directly, so importing the module does not start a server.

### HTML Page and JavaScript Functions

The two browser files have different roles:

| File | Purpose |
| --- | --- |
| [server/index.html](server/index.html) | Defines instructions, buttons and the results area. Loads `/app.js` with `defer`, after HTML parsing. |
| [server/app.js](server/app.js) | Implements HTTP calls, CSRF-header preparation, batch execution and button handlers. Runs in the browser, without Node.js or a build step. |

| JavaScript function | Purpose |
| --- | --- |
| `currentToken()` | Reads exactly one nonempty CSRF cookie and returns its raw value without caching or decoding it. |
| `read()` | Calls GET `/api/read` and checks its response; APISIX may issue a new cookie. |
| `write(operationId)` | Creates the JSON body, reads the current token, immediately invokes native POST `fetch`, and returns sanitized response metadata. Generates an ID if none is supplied. |
| `stats()` | Calls GET `/api/stats` and returns the upstream write counters. |
| `log(result)` | Appends JSON text to the results area using `textContent`, without printing tokens. |
| `batch(concurrent)` | Obtains the initial cookie, runs five writes in the selected mode, logs each result and summarizes successes and HTTP failures. |
| `show(action)` | Runs one button action and displays its result or error; it does not retry. |

`window.csrfRepro` exposes `read`, `write`, `stats` and `batch`. The automated
runner directly invokes `read` and `write` in each page; it does not use the
manual five-write `batch` function or drive the buttons.

The two batch modes generate the following traffic:

- `batch(false)`: initial GET, followed by five sequential POSTs, for six API
  requests in total.
- `batch(true)`: initial GET, followed by five rounds that each launch one POST
  and one GET together through `Promise.all`, for eleven API requests in total.
  The next round waits for both responses; requests inside a round can overlap.

There is no artificial pause between reading the token and calling `fetch`.
A missing, empty or ambiguous cookie causes a local error before a POST is sent.
Request credentials are same-origin; the browser attaches the Cookie header
automatically.

The page makes no API request just by loading. Click **Fetch CSRF cookie** first,
or run a batch: each batch starts with one GET to obtain the token. Sequential
batches make five POSTs; concurrent batches make five POST/GET pairs. HTTP
mismatches are counted without automatic retries. Other HTTP failures are
reported separately, and transport/JSON errors abort the batch with a visible
error rather than a successful summary. Reuse an operation ID explicitly when
checking recovery, then inspect upstream counts to detect duplicate writes.

Result text contains IDs, status codes and counts, never cookie/header token
values. A native mismatch is classified from the `401` response and exact error
message; browser code does not infer the outgoing Cookie header by reading the
cookie store after a response. The runner checks equality using Playwright's
actual outgoing request headers.

For a standalone HTTP component check, without APISIX:

```bash
uv sync --locked
uv run --no-sync python server/server.py
```

Direct server access does not set a CSRF cookie; use it to inspect the assets and
HTTP endpoints, not as the full CSRF reproduction. Browser writes must eventually
be exercised through the gateway, as done by the Playwright runner below.

## Automated Browser Runner

After bootstrap, run `./scripts/reproduce.sh`. The launcher uses this project's
`.venv` and discovers the gateway's published address through Compose, including
a customized port. It neither starts the stack nor repeats bootstrap.
[Parameters and examples](scripts/README.md#parameters) are documented with the
launcher; `runner/reproduce.py` can also be invoked directly.

### Experiment Flow

1. Open two pages in one fresh, non-persistent Chromium context. Both pages share
   cookies, and loading their HTML/JS makes no API call.
2. Obtain the initial CSRF cookie with one browser GET `/api/read`.
3. Run five sequential POSTs, alternating between the pages. All must have a
   coherent cookie/header pair, HTTP 201 and exactly one upstream write per ID.
4. For each delay, run five trials. Each trial launches one POST and one GET
   together in tab A and the same pair in tab B after the nominal delay. Wait
   for both pairs before the next trial; no continuous GET loop is used.
5. After each trial, inspect the actual sent headers and compare the responses
   with upstream counters. Continue through all delays when a verified mismatch
   occurs. Unexpected responses or inconsistent evidence stop the run as an error.
6. For a verified mismatch, issue one separate, deliberate recovery call with
   the same operation ID after both pairs have settled. Require HTTP 201 and
   exactly one counted write. The client itself never retries automatically.
7. Check that all observed POST IDs and GET counts match the plan, including
   only those separate recovery calls, then write the sanitized report.

Defaults are delays **200, 100, 50, 10 and 0 ms**, five trials per delay and five
baseline POSTs. This produces **50 initial concurrent POSTs, 5 baseline POSTs
and 51 browser GETs**, plus one POST per recovery if needed. Oracle and Docker
healthcheck GETs are separate and excluded from these browser counts.

### Evidence and Interpretation

`inspect_write()` reads the completed request's actual Cookie and custom-header
values through Playwright. It compares them only in memory and stores presence
and equality booleans. A verified mismatch requires all of the following:

- HTTP 401 and the exact native error message `csrf token mismatch`.
- Both values present and different in the actual outgoing request.
- Zero upstream writes for that operation ID before any recovery.

The count oracle queries GET `/api/stats` through the gateway using a separate
Python HTTP connection. It does not share the browser's cookie store, so its
response cannot replace a browser cookie. Only IDs owned by the current run
are checked; counters from previous runs are neither cleared nor reported.

The runner delays action groups before JavaScript starts. It does not intercept
`fetch`, replace cookies/headers/responses or insert a pause after token reading.
It records nominal delays, Python action gaps, browser POST send gaps and actual
cross-tab overlap. The send gap may be negative when browser scheduling reverses
the order. No delay is treated as a universal failure threshold.

Reports are created under ignored `reports/csrf-<run-id>.json` by default and
contain versions, settings, per-write evidence, timing, recovery attempts and
summary counts. No raw tokens, cookies, header dumps, traces or browser storage
state are saved. Existing output files are never overwritten. A failed run can
retain partial evidence; argument or launcher errors can occur before a report
exists. See [exit codes](scripts/README.md#exit-codes).

### Runner Functions

| Function | Responsibility |
| --- | --- |
| `arguments()` | Parses and validates origin, bounded trial counts and unique delays. |
| `make_report()` | Initializes metadata and empty evidence lists before the browser starts. |
| `run_browser()` | Owns Chromium/context lifetime and closes them on failure. |
| `campaign()` | Prepares pages and coordinates the sequential control and timing profiles. |
| `run_baseline()` | Checks sequential writes before concurrency is introduced. |
| `send()` | Schedules a client invocation and collects completed POST/GET responses. |
| `run_trial()` | Coordinates both tabs, collects trial evidence and requests any recovery. |
| `interval()` | Converts completed browser request timings to absolute send/end times. |
| `trial_timing()` | Computes actual action/send gaps and cross-tab overlaps. |
| `read_counts()` | Queries the upstream count oracle outside the browser cookie store. |
| `inspect_write()` | Validates wire coherence, status, response ID and counted side effects. |
| `recover()` | Makes and checks one separately scheduled retry of a rejected operation. |
| `verify_traffic()` | Detects requests beyond the explicit plan, including hidden retries. |
| `summarize()` | Counts initial mismatches separately from recoveries. |
| `main()` | Runs the experiment, handles failures and writes the final report/exit code. |

The nested `observe()` callback records request references only. Header reads and
evidence processing happen after the competing requests have finished.

## Lint and Formatting

```bash
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
node --check server/app.js
```

The first two commands use the environment prepared by bootstrap (or manually
with `uv sync --locked`); JavaScript syntax checking uses Node.js if available.
Node.js is not a runtime dependency of the page or server.

During implementation, Python **3.12.14** passed 24 live HTTP checks, including
rejected malformed requests with zero writes, persistent-connection HEAD framing
and 24 concurrently counted POSTs. JavaScript checks used a simulated DOM/fetch
to verify token rereading, raw cookie preservation, synchronous fetch invocation,
no automatic retry, explicit retry and batch request counts. These development
checks are not the Playwright reproduction and do not establish a standalone
APISIX concurrency failure.

## Docker Bootstrap Verification

The two-service stack was built and started on the local Docker context on
2026-11-01. Both HTTP healthchecks passed. A second bootstrap run preserved both
container IDs and start times, the existing `.env` and a previously accepted
operation's in-memory count. The server runs as UID `10001` with a read-only
root filesystem. Existing containers outside this Compose project were unchanged.

Live HTTP checks through APISIX confirmed the page, JavaScript asset and read
API, rejected a POST with no CSRF header without reaching the write counter,
and accepted a POST with a valid cookie/header pair exactly once. These checks
validate stack wiring and the basic CSRF path; they are not the automated browser
concurrency reproduction.

The Python/browser preparation stage was also run twice: Chromium was installed
on the first run and reused without downloads on the second. Installed packages,
`.venv/pyvenv.cfg`, `uv.lock`, `.env` and both running containers were preserved.
The headless launch check passed before Docker startup in each run. Separate
isolated checks covered sync, browser installation and browser launch failures,
confirming they stop bootstrap before `compose up`.

## Recorded Runner Results

Two separate headless campaigns used delays `200 100 50 10 0`, Playwright
`1.64.1` and Chromium `156.0.8078.4`. Both completed without an experiment error.
All initial writes returned 201 with coherent wire values and one counted
upstream write per operation ID.

| Trials per delay | Sequential POSTs | Initial profile POSTs | Browser GETs | Verified mismatches | Recovery POSTs | Outcome |
| --- | --- | --- | --- | --- | --- | --- |
| 5 | 5 | 50 | 51 | 0 | 0 | `not_observed` |
| 20 | 5 | 200 | 201 | 0 | 0 | `not_observed` |

The default campaign took 4.848 seconds; the larger campaign took 13.318 seconds.
Both returned exit code 1, meaning a complete `not_observed` sample. Their
conditional recovery path was not exercised, because neither observed a mismatch.

Local sanitized artifacts, ignored by Git and absent from a fresh checkout:

- `reports/csrf-b36edb48bc944be69b763ea8771456b5.json`.
- `reports/csrf-cf8cf88ea3014b11bb0bf8ddaf3e1774.json`.

The reports record the actual gaps and overlaps per trial. Nominally concurrent
profiles can contain non-overlapping requests. These observations neither
establish a general failure rate nor disprove the application-level finding.
These historical observations used the bundled `write/read` scheduling. The
UI-driven probe above separately reproduced the mismatch without injected faults.

## Directory Layout

```text
.
├── README.md
├── compose.yaml          # APISIX and private HTTP server with healthchecks
├── .env.example          # Versioned port and empty signing-key template
├── .env                  # Generated locally; ignored by Git
├── pyproject.toml        # Python requirements and direct dependency
├── uv.lock               # Resolved runtime and development dependencies
├── .python-version       # Python 3.12 selected for the local environment
├── .gitignore            # Excludes local environment, secrets and Python caches
├── scripts/              # Bootstrap, runner launcher and script documentation
├── apisix/               # Standalone process configuration and CSRF routes
├── server/               # HTTP fixture, HTML/JS, Dockerfile and .dockerignore
├── evidence/             # Sanitized observed UI mismatch campaign
├── runner/               # UI probe, campaign helpers and JSON report generation
└── reports/              # Generated sanitized reports; ignored by Git
```

The runner reports real request evidence without publishing token values.
The UI-driven probe and versioned evidence above demonstrate the isolated
mismatch; the earlier bundled campaigns remain documented with their original
`not_observed` outcomes.
