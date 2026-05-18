"""Progressive-size base64 upload test for the OTB Drive MCP.

Runs the same sweep as the Claude-driven test but speaks JSON-RPC directly
to the deployed FastMCP streamable-HTTP endpoint, so payloads never travel
through a Claude/LLM context window. That removes the harness's read-and-
echo overhead and exposes only the server-side cliff.

Usage:
    export MCP_URL="https://<your-render-host>/mcp/"
    export MCP_TOKEN="<bearer token from a completed OAuth session>"
    python scripts/base64_upload_test.py

Optional overrides:
    MCP_SIZES_KB="1,5,10,25,500"           # comma list, default below
    MCP_TIMEOUT_S=120                      # per-call hard timeout
    MCP_PARENT_ID=""                       # blank -> My Drive root
    MCP_WAIT_S=3                           # gap between calls

How to get MCP_TOKEN:
    Complete the normal OAuth flow once via your usual client (LibreChat,
    Claude Desktop, MCP Inspector, …). Then either:
      a) grab the access_token from the FastMCP session store on disk, or
      b) intercept the Authorization header your client sends (browser
         devtools / mitmproxy / proxy logs).
    For a local instance with auth disabled, leave MCP_TOKEN unset.

The script only depends on the stdlib + httpx, which is already pinned in
pyproject.toml.
"""

from __future__ import annotations

import base64
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

DEFAULT_SIZES_KB = [1, 5, 10, 25, 35, 50, 75, 100, 150, 200, 500]


@dataclass
class CallResult:
    size_kb: int
    start: str
    duration_s: float
    status: str  # success / error / timeout
    file_id: str = ""
    error: str = ""
    raw: str = field(default="", repr=False)


def make_payload_b64(size_kb: int) -> str:
    rng = random.Random(42 + size_kb)
    return base64.b64encode(rng.randbytes(size_kb * 1024)).decode("ascii")


def parse_sse_or_json(response: httpx.Response) -> dict:
    """FastMCP streamable-HTTP may return either application/json or
    text/event-stream. Normalise to the parsed JSON-RPC envelope."""
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        return response.json()
    # text/event-stream: one or more `data: {...}` lines, find the last one
    last_data = None
    for line in response.text.splitlines():
        if line.startswith("data:"):
            last_data = line[5:].strip()
    if last_data is None:
        raise RuntimeError(f"no SSE data frame in response: {response.text[:200]!r}")
    return json.loads(last_data)


def extract_file_id(text_blob: str) -> str:
    marker = "(ID:"
    if marker not in text_blob:
        return ""
    return text_blob.split(marker, 1)[1].split(")", 1)[0].strip()


def initialise(client: httpx.Client, mcp_url: str) -> str:
    """Run the MCP handshake. Returns the session id."""
    init_payload = {
        "jsonrpc": "2.0",
        "id": "init-1",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "base64_upload_test", "version": "1.0"},
        },
    }
    resp = client.post(mcp_url, json=init_payload)
    resp.raise_for_status()
    session_id = resp.headers.get("mcp-session-id", "")
    _ = parse_sse_or_json(resp)
    if not session_id:
        # Stateless mode — fine, just return empty.
        return ""
    notify_headers = {"mcp-session-id": session_id}
    notify_payload = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    client.post(mcp_url, json=notify_payload, headers=notify_headers).raise_for_status()
    return session_id


def call_create_drive_file(
    client: httpx.Client,
    mcp_url: str,
    session_id: str,
    rpc_id: str,
    file_name: str,
    base64_content: str,
    parent_id: str,
    timeout_s: float,
) -> dict:
    arguments = {
        "file_name": file_name,
        "base64_content": base64_content,
        "mime_type": "application/octet-stream",
    }
    if parent_id:
        arguments["folder_id"] = parent_id
    payload = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {"name": "create_drive_file", "arguments": arguments},
    }
    headers = {"mcp-session-id": session_id} if session_id else {}
    resp = client.post(mcp_url, json=payload, headers=headers, timeout=timeout_s)
    resp.raise_for_status()
    return parse_sse_or_json(resp)


def main() -> int:
    mcp_url = os.environ.get("MCP_URL")
    if not mcp_url:
        print("MCP_URL must be set, e.g. https://<host>/mcp/", file=sys.stderr)
        return 2
    token = os.environ.get("MCP_TOKEN", "")
    parent_id = os.environ.get("MCP_PARENT_ID", "")
    timeout_s = float(os.environ.get("MCP_TIMEOUT_S", "120"))
    wait_s = float(os.environ.get("MCP_WAIT_S", "3"))
    sizes_env = os.environ.get("MCP_SIZES_KB")
    sizes = (
        [int(s) for s in sizes_env.split(",") if s.strip()] if sizes_env else DEFAULT_SIZES_KB
    )

    base_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token:
        base_headers["Authorization"] = f"Bearer {token}"

    batch_ts = int(time.time())
    results: list[CallResult] = []

    with httpx.Client(headers=base_headers, timeout=timeout_s) as client:
        session_id = initialise(client, mcp_url)
        if session_id:
            print(f"MCP session established: {session_id}", flush=True)
        else:
            print("MCP session: stateless mode (no session id returned)", flush=True)

        for idx, size_kb in enumerate(sizes):
            file_name = f"BASE64TEST_{size_kb:04d}KB_{batch_ts}.bin"
            b64 = make_payload_b64(size_kb)
            start_dt = datetime.now(timezone.utc)
            print(
                f"[{start_dt.isoformat()}] Starting upload: {size_kb} KB "
                f"(b64 chars={len(b64)})",
                flush=True,
            )
            t0 = time.monotonic()
            result = CallResult(
                size_kb=size_kb, start=start_dt.isoformat(), duration_s=0.0, status="error"
            )
            try:
                envelope = call_create_drive_file(
                    client,
                    mcp_url,
                    session_id,
                    rpc_id=f"call-{idx}-{size_kb}",
                    file_name=file_name,
                    base64_content=b64,
                    parent_id=parent_id,
                    timeout_s=timeout_s,
                )
                result.duration_s = time.monotonic() - t0
                if "error" in envelope:
                    result.status = "error"
                    result.error = json.dumps(envelope["error"])[:300]
                else:
                    content = envelope.get("result", {}).get("content", [])
                    text_blob = " ".join(
                        c.get("text", "") for c in content if c.get("type") == "text"
                    )
                    result.raw = text_blob
                    file_id = extract_file_id(text_blob)
                    if file_id:
                        result.status = "success"
                        result.file_id = file_id
                    else:
                        result.status = "error"
                        result.error = text_blob[:300] or "no file id in response"
            except httpx.TimeoutException:
                result.duration_s = time.monotonic() - t0
                result.status = "timeout"
                result.error = f"client timeout after {timeout_s}s"
            except Exception as e:
                result.duration_s = time.monotonic() - t0
                result.status = "error"
                result.error = f"{type(e).__name__}: {e}"[:300]

            print(
                f"  -> {result.status} in {result.duration_s:.2f}s"
                + (f" id={result.file_id}" if result.file_id else "")
                + (f" err={result.error}" if result.error else ""),
                flush=True,
            )
            results.append(result)

            if result.status == "timeout":
                print("Timeout hit — stopping sweep at the cliff.", flush=True)
                break
            if idx + 1 < len(sizes):
                time.sleep(wait_s)

    print()
    print("| Size (KB) | Duration (s) | Status   | File ID | Notes |")
    print("|----------:|-------------:|----------|---------|-------|")
    for r in results:
        notes = (r.error or r.start).replace("|", "\\|").replace("\n", " ")
        print(
            f"| {r.size_kb} | {r.duration_s:.2f} | {r.status} | "
            f"{r.file_id} | {notes} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
