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
repro_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_command="$repro_dir/.venv/bin/python"
if [[ ! -x "$python_command" ]]; then
    printf '[ERROR] Run %s/scripts/bootstrap.sh first.\n' "$repro_dir" >&2
    exit 2
fi

# Ask Compose for the actual published port instead of sourcing .env as shell code.
address="$(docker compose --project-name apisix-csrf-repro \
    --project-directory "$repro_dir" --env-file "$repro_dir/.env" \
    --file "$repro_dir/compose.yaml" port apisix 9080)" || {
    printf '[ERROR] Cannot find the gateway. Run bootstrap.sh first.\n' >&2
    exit 2
}
[[ -n "$address" ]] || { printf '[ERROR] The gateway is not published.\n' >&2; exit 2; }
exec "$python_command" "$repro_dir/runner/reproduce.py" \
    --base-url "http://$address" "$@"
