#!/usr/bin/env bash
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

set -euo pipefail

UV_BOOTSTRAP_VERSION="0.13.6"
uv_install_dir="${UV_INSTALL_DIR:-${HOME}/.local/bin}"
repro_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
installer_path=""
env_temp_path=""

cleanup() {
    if [[ -n "$installer_path" ]]; then rm -f -- "$installer_path"; fi
    if [[ -n "$env_temp_path" ]]; then rm -f -- "$env_temp_path"; fi
}
trap cleanup EXIT

fail() {
    printf '[ERROR] %s\n' "$1" >&2
    exit 1
}

if uv_command="$(command -v uv 2>/dev/null)"; then
    printf '[INFO] Reusing uv from PATH: %s\n' "$uv_command"
elif [[ -x "$uv_install_dir/uv" ]]; then
    uv_command="$uv_install_dir/uv"
    printf '[INFO] Reusing uv from the installation directory: %s\n' "$uv_command"
else
    if command -v curl >/dev/null 2>&1; then
        downloader="curl"
    elif command -v wget >/dev/null 2>&1; then
        downloader="wget"
    else
        fail "uv is missing. Install curl or wget, then rerun this script."
    fi

    printf '[INFO] Installing uv %s in %s\n' "$UV_BOOTSTRAP_VERSION" "$uv_install_dir"
    installer_url="https://astral.sh/uv/${UV_BOOTSTRAP_VERSION}/install.sh"
    installer_path="$(mktemp "${TMPDIR:-/tmp}/apisix-csrf-uv.XXXXXX")"

    if [[ "$downloader" == "curl" ]]; then
        curl --fail --silent --show-error --location \
            --output "$installer_path" "$installer_url" \
            || fail "Could not download the official uv installer."
    else
        wget --quiet --output-document="$installer_path" "$installer_url" \
            || fail "Could not download the official uv installer."
    fi

    UV_INSTALL_DIR="$uv_install_dir" UV_NO_MODIFY_PATH=1 sh "$installer_path" \
        || fail "The uv installer failed."
    uv_command="$uv_install_dir/uv"
    [[ -x "$uv_command" ]] || fail "The uv installer did not create $uv_command."
fi

uv_version="$("$uv_command" --version)" \
    || fail "The uv executable could not report its version: $uv_command"
printf '[OK] %s\n' "$uv_version"

command -v docker >/dev/null 2>&1 \
    || fail "Docker CLI is missing. Install Docker: https://docs.docker.com/get-started/get-docker/ and rerun this script."
docker_version="$(docker --version)" \
    || fail "Docker CLI was found but could not report its version. Repair the installation and rerun this script."
printf '[OK] %s\n' "$docker_version"
docker compose version >/dev/null 2>&1 \
    || fail "Docker Compose is missing. Install the Compose plugin: https://docs.docker.com/compose/install/"
docker info >/dev/null 2>&1 \
    || fail "Docker daemon is unavailable. Start Docker and check access for the current user/context."
printf '[OK] Docker Compose and daemon are available.\n'

env_file="$repro_dir/.env"
env_template="$repro_dir/.env.example"
if [[ -e "$env_file" || -L "$env_file" ]]; then
    [[ -f "$env_file" ]] || fail "$env_file exists but is not a regular file."
    printf '[INFO] Keeping the existing .env unchanged.\n'
else
    [[ -r "$env_template" ]] || fail "Missing readable template: $env_template"
    grep -qx 'CSRF_REPRO_SIGNING_KEY=' "$env_template" \
        || fail "The .env.example template must contain an empty CSRF_REPRO_SIGNING_KEY entry."
    csrf_signing_key="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')" \
        || fail "Could not generate the local CSRF signing key."
    [[ "$csrf_signing_key" =~ ^[0-9a-f]{64}$ ]] \
        || fail "The random source did not provide a complete 32-byte signing key."
    # mktemp creates a private file; publish it only after all content is ready.
    env_temp_path="$(mktemp "$repro_dir/.env.XXXXXX")"
    sed "s/^CSRF_REPRO_SIGNING_KEY=$/CSRF_REPRO_SIGNING_KEY=$csrf_signing_key/" \
        "$env_template" > "$env_temp_path" \
        || fail "Could not prepare the local .env file."
    # Linking creates .env atomically and never overwrites an existing file.
    if ln "$env_temp_path" "$env_file" 2>/dev/null; then
        printf '[OK] Created .env from .env.example with a generated CSRF key.\n'
    elif [[ -f "$env_file" ]]; then
        printf '[INFO] Another bootstrap created .env; keeping it unchanged.\n'
    else
        fail "Could not create $env_file."
    fi
fi

# Explicit paths and project name keep this stack independent of the caller's cwd.
compose=(docker compose --project-name apisix-csrf-repro
    --project-directory "$repro_dir" --env-file "$env_file"
    --file "$repro_dir/compose.yaml")
"${compose[@]}" config --quiet \
    || fail "Compose configuration is invalid. Check the existing .env and configuration files."

[[ -s "$repro_dir/.python-version" && -s "$repro_dir/uv.lock" ]] \
    || fail "The reproduction requires a populated .python-version and uv.lock."

# uv selects the declared Python, downloading it if needed, and reuses .venv.
printf '[INFO] Synchronizing the local Python environment from uv.lock...\n'
UV_PROJECT_ENVIRONMENT="$repro_dir/.venv" \
    "$uv_command" sync --project "$repro_dir" --locked \
    || fail "Could not prepare the Python environment from uv.lock."
python_command="$repro_dir/.venv/bin/python"

# Playwright installs the matching binaries and skips those already cached.
printf '[INFO] Preparing Playwright Chromium...\n'
"$python_command" -m playwright install chromium \
    || fail "Could not install Chromium. Check the Playwright download error above."

# Validate native libraries and browser startup before touching the Docker stack.
if ! "$python_command" - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    browser.close()
PY
then
    fail "Chromium could not start. Check the error above. If native libraries are missing, run '$python_command -m playwright install-deps chromium' manually; it may require administrator privileges."
fi
printf '[OK] Python environment and headless Chromium are ready.\n'

# Compose reuses matching running containers and starts stopped/missing services.
printf '[INFO] Building and starting the reproduction stack...\n'
"${compose[@]}" up --detach --build --wait --wait-timeout 120 \
    || fail "The reproduction stack did not become healthy. Inspect it with docker compose ps and docker compose logs from $repro_dir."
printf '[OK] APISIX and the HTTP server are running and healthy.\n'
