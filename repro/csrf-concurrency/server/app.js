/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements. See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License. You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

"use strict";

const csrfName = "apisix-csrf-token";

/** Read the raw value without decoding it or keeping a cached copy. */
function currentToken() {
  const prefix = `${csrfName}=`;
  const matches = document.cookie
    .split(";")
    .map((part) => part.trim())
    .filter((part) => part.startsWith(prefix));
  if (matches.length !== 1 || matches[0].length === prefix.length) {
    throw new Error("One nonempty CSRF cookie is required. Fetch /api/read first.");
  }
  return matches[0].slice(prefix.length);
}

/** Safe requests also pass through APISIX and may replace its CSRF cookie. */
async function read() {
  const response = await fetch("/api/read", {
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
  });
  const body = await response.json();
  if (response.status !== 200 || body.ok !== true) {
    throw new Error(`Read failed: HTTP ${response.status}`);
  }
  return { action: "read", status: response.status };
}

/** @param {string} operationId Reuse this ID for an explicit recovery attempt. */
function write(operationId = crypto.randomUUID()) {
  const body = JSON.stringify({ operation_id: operationId });
  const token = currentToken();
  // No await, timer, interception or retry between this read and native fetch.
  return fetch("/api/write", {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    headers: { "Content-Type": "application/json", [csrfName]: token },
    body,
  }).then(async (response) => {
    const payload = await response.json();
    return {
      action: "write",
      operation_id: operationId,
      status: response.status,
      csrf_mismatch:
        response.status === 401 && payload.error_msg === "csrf token mismatch",
      accepted_count: payload.accepted_count ?? null,
      error: payload.error_msg ?? payload.error ?? null,
    };
  });
}

async function stats() {
  const response = await fetch("/api/stats", {
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
  });
  if (response.status !== 200) {
    throw new Error(`Stats failed: HTTP ${response.status}`);
  }
  return response.json();
}

/** @param {object} result Sanitized response metadata. */
function log(result) {
  // Render text only. Never display cookies or custom-header token values.
  const output = document.getElementById("output");
  output.textContent += `${JSON.stringify(result)}\n`;
}

/** @param {boolean} concurrent Launch one GET alongside each POST when true. */
async function batch(concurrent) {
  await read();
  const writes = [];
  for (let index = 0; index < 5; index += 1) {
    let result;
    if (concurrent) {
      [result] = await Promise.all([write(), read()]);
    } else {
      result = await write();
    }
    writes.push(result);
    log(result);
  }
  return {
    action: concurrent ? "concurrent batch" : "sequential batch",
    posts: writes.length,
    created: writes.filter((result) => result.status === 201).length,
    csrf_mismatches: writes.filter((result) => result.csrf_mismatch).length,
    other_http_failures: writes.filter(
      (result) => result.status !== 201 && !result.csrf_mismatch,
    ).length,
  };
}

/** @param {() => Promise<object>} action One manual action, without retries. */
async function show(action) {
  try {
    log(await action());
  } catch (error) {
    log({ error: error.message });
  }
}

// The future Playwright runner can call the same small functions as the page.
window.csrfRepro = { read, write, stats, batch };
document.getElementById("read").addEventListener("click", () => show(read));
document.getElementById("write").addEventListener("click", () => show(write));
document.getElementById("stats").addEventListener("click", () => show(stats));
document.getElementById("sequential").addEventListener("click", () => {
  show(() => batch(false));
});
document.getElementById("concurrent").addEventListener("click", () => {
  show(() => batch(true));
});
