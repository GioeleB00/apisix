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

# Reproduction Scripts

## bootstrap.sh

Run from the reproduction directory:

```bash
./scripts/bootstrap.sh
```

The script also works from another directory when called with its full path.
It currently implements the following five steps.

### 1. Find or Install uv

The script first looks for `uv` in `PATH`, then in the installation directory:
`UV_INSTALL_DIR`, if set, or `$HOME/.local/bin` otherwise. An existing executable
is reused and checked with `--version`; it is not upgraded or replaced.

If neither location contains uv, the script downloads the official Astral
installer for **uv 0.13.6** and runs it with `sh`. Downloading requires `curl` or,
as a fallback, `wget`. If neither is available, the script stops with an error.
The download must complete successfully before the installer is executed.

Installation uses the directory above and `UV_NO_MODIFY_PATH=1`. The bootstrap
does not invoke `sudo` or change shell startup files. It uses the installed
executable directly, so reopening a terminal is unnecessary for this script.
To use a custom destination:

```bash
UV_INSTALL_DIR="$PWD/.tools/bin" ./scripts/bootstrap.sh
```

Use the same destination on subsequent runs. The script reuses that installation
even if the directory has not been added to `PATH`. To invoke uv manually from
such a directory, use its full path or add the directory to your shell's `PATH`.
If uv was installed during this run and a later step fails, rerunning the script
reuses the installation.

See the official [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/)
and [installer options](https://docs.astral.sh/uv/reference/installer/).

### 2. Check Docker, Compose and Daemon Access

The script requires `docker` in `PATH` and verifies that `docker --version`
succeeds. If it is missing, the script prints the
[Docker installation instructions](https://docs.docker.com/get-started/get-docker/)
and exits with status 1. It does not install Docker.

It then checks `docker compose version` and `docker info`. Missing Compose,
a stopped daemon or insufficient access causes an explicit error before stack
startup. Docker and Compose are never installed automatically. Use a local
Docker context with access to the repository's bind-mounted configuration files.

### 3. Create the Local Environment File

If `.env` is absent in the reproduction root, the script copies the contents of
`.env.example` and replaces its empty signing-key entry with 32 random bytes
encoded as 64 hexadecimal characters. It reads `/dev/urandom` using `od` and
`tr`; Python does not need to be installed for this step. The template's default
HTTP port is `19088`.

The new file has permissions `600`. A temporary file in the same directory is
fully prepared before an atomic hard link publishes it as `.env`. An existing
file is never overwritten, including if another bootstrap creates it first.
Temporary files are removed when the script exits.

Existing `.env` files are kept byte-for-byte unchanged, including their key,
port, permissions and any manual edits. An existing empty file is also kept;
required-value checks remain the responsibility of Compose. The script reports
an error if `.env` exists as a directory or the template is unavailable or does
not contain the expected empty signing-key entry.

Compose automatically reads `.env` for `${...}` interpolation and the service's
`environment` block passes only the signing key into APISIX. The file is not
mounted in the container or executed as a shell script. Variables already
exported in the shell take precedence over `.env`; unset an old override if the
local file should supply the value. See the [Compose interpolation documentation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/).

`.env.example` is the versioned template; `.env` and its temporary files are ignored by Git.
The bootstrap does not print the signing key. This step also uses standard Unix
utilities `dirname`, `grep`, `sed` and `ln`.

### 4. Validate Configuration and Prepare Python/Chromium

The bootstrap pins the Compose project name to `apisix-csrf-repro` and explicitly
selects the reproduction directory, `.env` and `compose.yaml`. This works from
any caller directory and keeps the stack separate from other Compose projects.
It validates configuration with `config --quiet`, without printing the resolved
signing key. An invalid existing `.env` stops bootstrap before startup.

Before starting containers, it prepares the browser automation environment:

```bash
uv sync --locked
./.venv/bin/python -m playwright install chromium
```

`.python-version` requests Python **3.12**. uv reuses a compatible interpreter or
downloads a managed one when needed. The bootstrap explicitly selects this
project with `--project` and sets `UV_PROJECT_ENVIRONMENT` to its `.venv`, so an
active environment belonging to another project is not modified.

`uv sync --locked` creates or synchronizes that environment with the provided
lockfile, including Playwright and the default Ruff development group. It does
not regenerate `uv.lock` or select newer package versions. A missing/stale
lockfile or failed sync stops bootstrap before `compose up`.

Playwright installs the Chromium binaries required by its locked version,
including the headless shell and supporting artifacts. Existing matching
downloads in the Playwright user cache are reused; the bootstrap never requests
a forced browser download. It then launches and closes an empty headless browser
to check executable availability and native libraries. This check makes no web
requests and does not run the concurrency experiment.

If browser installation or startup fails, bootstrap stops before Docker startup.
Missing operating system libraries are reported through Playwright's error.
Install them manually when required, then rerun bootstrap:

```bash
./.venv/bin/python -m playwright install-deps chromium
./scripts/bootstrap.sh
```

The dependency installer may require administrator privileges. Bootstrap never
runs it or invokes `sudo` automatically. See the [Playwright browser documentation](https://playwright.dev/python/docs/browsers).

### 5. Build and Start the Docker Stack

Once Python and Chromium are ready, the bootstrap performs the equivalent of:

```bash
docker compose up --detach --build --wait --wait-timeout 120
```

Compose builds the HTTP server image using its layer cache, creates missing
containers, starts stopped services and reuses matching running containers.
There is no separate `docker ps` shortcut that could skip a required image or
Compose configuration update, and no forced recreation or cleanup command.

The server starts first. Its healthcheck queries `/api/read` directly with the
Python standard library. APISIX depends on that healthy service; its OpenResty
healthcheck sends an actual GET through `/api/read` and verifies the expected
JSON response, exercising the gateway route and upstream. Neither healthcheck
increments write counters or updates cookies in a user's browser.

The bootstrap returns success only after both services are healthy. The
120-second timeout bounds readiness waiting; pulling/building images may take
additional time. A failed build, startup or healthcheck returns nonzero and leaves
the containers available for inspection. From the reproduction directory:

```bash
docker compose ps
docker compose logs --tail=50 apisix server
```

After success, open `http://127.0.0.1:19088/`, or use the port set in `.env`.
The server has no published host port; page and API requests enter via APISIX.

### Idempotence and Current Scope

Repeated runs reuse existing uv, preserve `.env`, synchronize the existing
`.venv`, reuse matching cached browser binaries and image layers, and keep
matching healthy containers running. The headless startup check runs each time.
With unchanged sources and settings, container IDs and the server's in-memory
counts are preserved. Changes to the server image or Compose settings can require
container recreation; recreating or restarting the server clears its volatile
counters. This is expected for an experiment fixture, not a persistent database.

The route file can be hot-reloaded by APISIX. Editing the bind-mounted process
configuration `apisix/config.yaml` requires restarting APISIX; `compose up` does
not detect changes to bind-mounted file contents as service-definition changes.
The bootstrap stops at the first failure. It does not reset data or rotate an
existing local signing key.

The script does not regenerate `uv.lock` or execute the browser reproduction.
Run the launcher below as a separate step once bootstrap succeeds.

## reproduce.sh

Runs the experiment using the already prepared `.venv` and Docker stack:

```bash
./scripts/reproduce.sh
```

The launcher resolves paths relative to its own location, checks that the local
Python executable exists, then asks this Compose project for the published
address of `apisix:9080`. It passes that origin to `runner/reproduce.py` and
forwards all additional arguments. It does not source `.env`, install packages,
start containers or alter the gateway configuration. A missing environment or
gateway produces an error asking you to run bootstrap first.

The Compose port lookup uses the fixed `apisix-csrf-repro` project and explicit
project, environment and Compose-file paths. The wrapper therefore requires that
local stack even when forwarding an explicit `--base-url`. To use an independently
started HTTP gateway without port discovery, invoke the Python runner directly:

```bash
./.venv/bin/python runner/reproduce.py --base-url http://127.0.0.1:19088
```

### Parameters

| Option | Default | Meaning |
| --- | --- | --- |
| `--trials N` | `5` | Trials per delay; integer from 1 to 1000. Each trial sends two POST/GET pairs across two tabs. |
| `--baseline-writes N` | `5` | Sequential positive-control POSTs, alternating tabs; integer from 1 to 1000. |
| `--delays-ms N [N ...]` | `200 100 50 10 0` | Unique nominal delays between tab action groups, each from 0 to 1000 ms. |
| `--headed` | Disabled | Displays Chromium windows; results remain in the terminal and JSON report. Requires a graphical session. |
| `--output PATH` | New UUID-named file under the reproduction's `reports/` | Custom JSON report path. Relative paths use the caller's working directory. Existing files are refused. |
| `--base-url ORIGIN` | Compose-discovered address in the wrapper; `http://127.0.0.1:19088` in direct Python use | HTTP origin without credentials, query, fragment or a non-root path. An explicit argument overrides the injected value. |
| `-h`, `--help` | — | Shows runner usage. Through the wrapper, environment and port discovery still happen first. |

Examples, from the reproduction directory:

```bash
# Default small campaign.
./scripts/reproduce.sh

# Larger sample, same request pattern.
./scripts/reproduce.sh --trials 20

# Select timing profiles and a new report filename.
./scripts/reproduce.sh --trials 5 --delays-ms 50 10 0 --output reports/timing-review.json

# Observe the windows without changing the tested request logic.
./scripts/reproduce.sh --headed
```

With `D` delays, `T` trials per delay and `B` baseline writes, planned browser
traffic is `2 × D × T + B` initial POSTs and `1 + 2 × D × T` GETs. Each verified
mismatch adds one deliberately scheduled recovery POST. The application never
retries automatically. Requests for counter inspection use a separate Python
HTTP client and do not update the browser cookie store.

### Exit Codes

| Code | Outcome | Interpretation |
| --- | --- | --- |
| `0` | `reproduced` | The campaign completed with at least one wire-verified mismatch and its zero-write check; required recoveries and traffic checks passed. |
| `1` | `not_observed` | The complete campaign passed its consistency checks but did not observe a mismatch. This does not establish absence of the race. |
| `2` | `error` | A prerequisite, argument, transport, consistency or report-writing check failed. Launcher/argument errors may precede report creation. |
| `130` | Interrupted | The user interrupted the Python runner; it attempts to write a partial error report. |

Exit code 1 is intentional for a completed negative observation. A shell or CI
job must interpret it using the table, rather than classify it as an environment
failure. A failed campaign is not reported as a valid `not_observed` sample.

### Reports and Scope

Each successful report write prints its path. `reports/` is ignored by Git;
attach a selected sanitized JSON file explicitly when sharing evidence. Reports
contain run IDs, versions, parameters, status codes, cookie/header presence and
equality, upstream counts and timing. Token values are held only in memory and
are not written to reports. Partial reports use `error` when a run stops early.

The two recorded headless campaigns used 5 and 20 trials per delay and both
returned `not_observed` (exit 1), with zero recovery attempts. The conditional
recovery branch has therefore not been exercised by these standalone campaigns.
See the [root README](../README.md#recorded-runner-results) for counts and artifact
names. Investigating why this differs from the original application remains
pending; no cause has been established.

## UI Mismatch Probe

After bootstrap, run `.venv/bin/python runner/reproduce_ui.py` from the
reproduction directory. It uses the default gateway port 19088, two shared-cookie
tabs, 50 trials at each nominal delay of 10 and 0 ms, and real button clicks
alongside browser GETs. It has no CLI arguments or automatic write retries.

The probe prints verified mismatch occurrences and saves sanitized evidence in
`reports/comparison-<run-id>.json`. It exits normally for a completed run even
when zero mismatches occur; inspect the printed count. Unexpected responses or
inconsistent upstream counts raise an error. Reports contain no raw cookies,
header token values, signing keys or browser storage. Recorded successful
reproduction evidence is in `evidence/observed-ui-run.json`.
