#!/usr/bin/env python3
"""Authenticated JSON-RPC client for the yj-museum MCP server."""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import getpass
import json
import math
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.request


DEFAULT_URL = "https://mcp.yuanjie.cc:18093/mcp"
PROTOCOL_VERSION = "2025-03-26"
SERVICE_NAME = "codex-yj-museum"
ALLOWED_QUERY_KEYS = {
    "title",
    "creator",
    "dynasty",
    "kiln",
    "material",
    "technique",
    "glaze",
    "motif",
    "descriptors",
    "inscription",
    "detailed_form",
}


class ClientError(RuntimeError):
    pass


class AuthorizationRequired(ClientError):
    pass


class MCPClient:
    def __init__(self, url: str = DEFAULT_URL, timeout: int = 60):
        self.url = url
        self.timeout = timeout
        self.request_id = 0

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "User-Agent": "yj-museum-search-skill/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ClientError(f"MCP HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise ClientError(f"Cannot reach yj-museum MCP: {exc.reason}") from exc

        payload = self._parse_transport(raw)
        if payload.get("error"):
            error = payload["error"]
            raise ClientError(f"MCP error {error.get('code')}: {error.get('message')}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ClientError("MCP response did not contain an object result")
        return result

    @staticmethod
    def _parse_transport(raw: str) -> dict[str, Any]:
        stripped = raw.strip()
        if stripped.startswith("{"):
            return json.loads(stripped)
        data_lines = []
        for line in stripped.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            raise ClientError("Unsupported MCP response transport")
        return json.loads("\n".join(data_lines))

    def initialize(self) -> dict[str, Any]:
        return self.rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "yj-museum-search-skill", "version": "1.0.0"},
            },
        )

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.rpc("tools/list", {})
        tools = result.get("tools", [])
        if not isinstance(tools, list):
            raise ClientError("tools/list returned a non-list tools value")
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise ClientError(self._content_message(result) or f"Tool {name} failed")
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        content = result.get("content", [])
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                try:
                    return json.loads(text)
                except (TypeError, json.JSONDecodeError):
                    return {"text": text}
        return result

    @staticmethod
    def _content_message(result: dict[str, Any]) -> str:
        for item in result.get("content", []):
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = str(item.get("text", ""))
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return str(parsed.get("message") or parsed)
            except json.JSONDecodeError:
                pass
            return text
        return ""


class CredentialStore:
    def __init__(self, state_dir: Path | None = None):
        self.state_dir = state_dir or self.default_state_dir()
        self.file_path = self.state_dir / "credential.json"
        self.system = platform.system().lower()

    @staticmethod
    def default_state_dir() -> Path:
        codex_home = os.environ.get("CODEX_HOME")
        root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
        return root / "yj-museum"

    def backend(self) -> str:
        if self.system == "windows":
            return "windows-dpapi"
        if self.system == "darwin" and shutil.which("security"):
            return "macos-keychain"
        if self.system == "linux" and shutil.which("secret-tool"):
            return "linux-secret-service"
        return "file-0600"

    def set(self, code: str) -> str:
        code = code.strip()
        if not code:
            raise ClientError("Authorization code is empty")
        backend = self.backend()
        if backend == "windows-dpapi":
            protected = self._dpapi_protect(code.encode("utf-8"))
            self._write_file({"version": 1, "backend": backend, "blob": base64.b64encode(protected).decode("ascii")})
        elif backend == "macos-keychain":
            subprocess.run(
                [
                    "security",
                    "add-generic-password",
                    "-a",
                    getpass.getuser(),
                    "-s",
                    SERVICE_NAME,
                    "-w",
                    code,
                    "-U",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        elif backend == "linux-secret-service":
            subprocess.run(
                [
                    "secret-tool",
                    "store",
                    "--label=Codex yj-museum authorization",
                    "service",
                    SERVICE_NAME,
                    "account",
                    getpass.getuser(),
                ],
                input=code,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        else:
            self._write_file(
                {"version": 1, "backend": backend, "code_b64": base64.b64encode(code.encode("utf-8")).decode("ascii")}
            )
        return backend

    def get(self) -> str | None:
        env_code = os.environ.get("YJ_MUSEUM_AUTH_CODE")
        if env_code:
            return env_code.strip()
        backend = self.backend()
        if backend == "windows-dpapi":
            if not self.file_path.exists():
                return None
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
            protected = base64.b64decode(payload["blob"])
            return self._dpapi_unprotect(protected).decode("utf-8")
        if backend == "macos-keychain":
            result = subprocess.run(
                ["security", "find-generic-password", "-a", getpass.getuser(), "-s", SERVICE_NAME, "-w"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        if backend == "linux-secret-service":
            result = subprocess.run(
                ["secret-tool", "lookup", "service", SERVICE_NAME, "account", getpass.getuser()],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        if not self.file_path.exists():
            return None
        payload = json.loads(self.file_path.read_text(encoding="utf-8"))
        return base64.b64decode(payload["code_b64"]).decode("utf-8")

    def delete(self) -> None:
        backend = self.backend()
        if backend == "macos-keychain":
            subprocess.run(
                ["security", "delete-generic-password", "-a", getpass.getuser(), "-s", SERVICE_NAME],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif backend == "linux-secret-service":
            subprocess.run(
                ["secret-tool", "clear", "service", SERVICE_NAME, "account", getpass.getuser()],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if self.file_path.exists():
            self.file_path.unlink()

    def _write_file(self, payload: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.system != "windows":
            os.chmod(self.state_dir, stat.S_IRWXU)
        temp_path = self.file_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        if self.system != "windows":
            os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp_path, self.file_path)

    @staticmethod
    def _dpapi_protect(data: bytes) -> bytes:
        return CredentialStore._dpapi(data, protect=True)

    @staticmethod
    def _dpapi_unprotect(data: bytes) -> bytes:
        return CredentialStore._dpapi(data, protect=False)

    @staticmethod
    def _dpapi(data: bytes, protect: bool) -> bytes:
        if platform.system().lower() != "windows":
            raise ClientError("DPAPI is only available on Windows")

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        buffer = ctypes.create_string_buffer(data)
        input_blob = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        output_blob = DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if protect:
            ok = crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                "yj-museum authorization",
                None,
                None,
                None,
                0,
                ctypes.byref(output_blob),
            )
        else:
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)
            )
        if not ok:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)


def require_auth(store: CredentialStore) -> str:
    code = store.get()
    if not code:
        raise AuthorizationRequired("No stored yj-museum authorization code")
    return code


def clean_query_params(params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ClientError("Query params must be a JSON object")
    unknown = set(params) - ALLOWED_QUERY_KEYS
    if unknown:
        raise ClientError(f"Unsupported query fields: {', '.join(sorted(unknown))}")
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if not isinstance(value, str):
            raise ClientError(f"Query field {key} must be a string")
        value = value.strip()
        if value:
            cleaned[key] = value
    return cleaned


def query_page(client: MCPClient, code: str, params: dict[str, Any], page: int, page_size: int) -> dict[str, Any]:
    arguments = clean_query_params(params)
    arguments.update({"authCode": code, "pageNum": page, "pageSize": page_size})
    payload = client.call_tool("query_coll", arguments)
    if not isinstance(payload, dict):
        raise ClientError("query_coll returned a non-object payload")
    return payload


def fetch_all(
    client: MCPClient,
    code: str,
    params: dict[str, Any],
    max_records: int,
) -> dict[str, Any]:
    page_size = 50
    first = query_page(client, code, params, 1, page_size)
    total = int(first.get("total", 0) or 0)
    items = list(first.get("items") or [])
    raw_count = len(items)
    pages_needed = math.ceil(total / page_size) if total else 1
    max_pages = max(1, math.ceil(max_records / page_size))
    pages_to_fetch = min(pages_needed, max_pages)
    for page in range(2, pages_to_fetch + 1):
        payload = query_page(client, code, params, page, page_size)
        page_items = list(payload.get("items") or [])
        items.extend(page_items)
        raw_count += len(page_items)
        if not page_items:
            break
    return {
        "server_total": total,
        "items": items,
        "complete": total <= raw_count,
        "retrieved": raw_count,
    }


def item_key(item: dict[str, Any]) -> str:
    code = str(item.get("coll_code") or "").strip()
    if code:
        return f"code:{code}"
    fallback = [
        str(item.get("title") or ""),
        str(item.get("holding_institution") or ""),
        str(item.get("accession_no") or ""),
    ]
    return "fallback:" + "\u241f".join(fallback)


def run_group(
    client: MCPClient,
    code: str,
    queries: list[dict[str, Any]],
    max_records: int,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], bool]:
    merged: dict[str, dict[str, Any]] = {}
    query_meta: list[dict[str, Any]] = []
    all_complete = True
    for index, query in enumerate(queries, start=1):
        if not isinstance(query, dict):
            raise ClientError("Each query plan entry must be an object")
        label = str(query.get("label") or f"query-{index}")
        params = clean_query_params(query.get("params") or {})
        result = fetch_all(client, code, params, max_records)
        all_complete = all_complete and bool(result["complete"])
        query_meta.append(
            {
                "label": label,
                "params": params,
                "server_total": result["server_total"],
                "retrieved": result["retrieved"],
                "complete": result["complete"],
            }
        )
        for raw_item in result["items"]:
            if not isinstance(raw_item, dict):
                continue
            key = item_key(raw_item)
            if key not in merged:
                merged[key] = {"record": raw_item, "matched_by": [label]}
            elif label not in merged[key]["matched_by"]:
                merged[key]["matched_by"].append(label)
    return merged, query_meta, all_complete


def parse_json_input(value: str | None, from_stdin: bool) -> Any:
    if from_stdin:
        value = sys.stdin.read()
    if value is None or not value.strip():
        raise ClientError("JSON input is empty")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ClientError(f"Invalid JSON input: {exc}") from exc


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("YJ_MUSEUM_MCP_URL", DEFAULT_URL))
    parser.add_argument("--state-dir", type=Path, default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="Check the MCP endpoint and list tool names.")

    auth = sub.add_parser("auth", help="Manage the locally stored authorization code.")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_sub.add_parser("status")
    set_parser = auth_sub.add_parser("set")
    set_parser.add_argument("--stdin", action="store_true", required=True)
    auth_sub.add_parser("delete")

    search = sub.add_parser("search", help="Run one query_coll request.")
    search.add_argument("--params-json")
    search.add_argument("--params-stdin", action="store_true")
    search.add_argument("--page", type=int, default=1)
    search.add_argument("--page-size", type=int, default=10)
    search.add_argument("--all-pages", action="store_true")
    search.add_argument("--max-records", type=int, default=5000)

    detail = sub.add_parser("detail", help="Run info_coll for a returned coll_code.")
    detail.add_argument("--coll-code", required=True)

    batch = sub.add_parser("batch-search", help="Run strict and related plans with exact de-duplication.")
    batch.add_argument("--plan-json")
    batch.add_argument("--plan-stdin", action="store_true")
    batch.add_argument("--max-records-per-query", type=int, default=5000)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    client = MCPClient(args.url)
    store = CredentialStore(args.state_dir)

    if args.command == "probe":
        initialized = client.initialize()
        tools = client.list_tools()
        print_json(
            {
                "ok": True,
                "serverInfo": initialized.get("serverInfo"),
                "protocolVersion": initialized.get("protocolVersion"),
                "tools": [tool.get("name") for tool in tools],
            }
        )
        return 0

    if args.command == "auth":
        if args.auth_command == "status":
            print_json({"configured": bool(store.get()), "backend": store.backend()})
            return 0
        if args.auth_command == "delete":
            store.delete()
            print_json({"configured": False, "deleted": True, "backend": store.backend()})
            return 0
        code = sys.stdin.read().strip()
        if not code:
            raise ClientError("No authorization code was received on stdin")
        query_page(client, code, {}, 1, 1)
        backend = store.set(code)
        print_json({"configured": True, "validated": True, "backend": backend})
        return 0

    code = require_auth(store)

    if args.command == "search":
        params = parse_json_input(args.params_json, args.params_stdin)
        if args.all_pages:
            print_json(fetch_all(client, code, params, args.max_records))
        else:
            if args.page < 1 or not 1 <= args.page_size <= 50:
                raise ClientError("page must be >= 1 and page-size must be between 1 and 50")
            print_json(query_page(client, code, params, args.page, args.page_size))
        return 0

    if args.command == "detail":
        coll_code = args.coll_code.strip()
        if not coll_code:
            raise ClientError("coll_code is empty")
        payload = client.call_tool("info_coll", {"authCode": code, "coll_code": coll_code})
        print_json(payload)
        return 0

    plan = parse_json_input(args.plan_json, args.plan_stdin)
    if not isinstance(plan, dict):
        raise ClientError("Search plan must be a JSON object")
    strict_queries = plan.get("strict") or []
    related_queries = plan.get("related") or []
    if not isinstance(strict_queries, list) or not isinstance(related_queries, list):
        raise ClientError("strict and related must be arrays")
    strict, strict_meta, strict_complete = run_group(
        client, code, strict_queries, args.max_records_per_query
    )
    related, related_meta, related_complete = run_group(
        client, code, related_queries, args.max_records_per_query
    )
    for key in strict:
        related.pop(key, None)
    counts_exact = strict_complete and related_complete
    print_json(
        {
            "summary": {
                "strict_count": len(strict),
                "related_count": len(related),
                "counts_exact": counts_exact,
                "count_qualifier": "exact" if counts_exact else "at_least",
            },
            "strict": list(strict.values()),
            "related": list(related.values()),
            "queries": {"strict": strict_meta, "related": related_meta},
        }
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuthorizationRequired as exc:
        print_json({"error": "authorization_required", "message": str(exc)})
        raise SystemExit(4)
    except (ClientError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        print_json({"error": "yj_museum_client_error", "message": str(exc)})
        raise SystemExit(2)
