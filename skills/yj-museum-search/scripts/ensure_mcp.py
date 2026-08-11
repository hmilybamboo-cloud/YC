#!/usr/bin/env python3
"""Check or safely add the yj-museum MCP entry to Codex config.toml."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import tempfile


SERVER_NAME = "yj-museum"
SERVER_URL = "https://mcp.yuanjie.cc:18093/mcp"
SECTION_RE = re.compile(
    r"(?m)^\s*\[mcp_servers\.(?:yj-museum|\"yj-museum\"|'yj-museum')\]\s*(?:#.*)?$"
)
NEXT_SECTION_RE = re.compile(r"(?m)^\s*\[[^\r\n]+\]\s*(?:#.*)?$")


def default_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return root / "config.toml"


def section_span(text: str) -> tuple[int, int] | None:
    match = SECTION_RE.search(text)
    if not match:
        return None
    following = NEXT_SECTION_RE.search(text, match.end())
    return match.start(), following.start() if following else len(text)


def read_setting(section: str, key: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*([^#\r\n]+)", section)
    return match.group(1).strip() if match else None


def unquote(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def inspect_config(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    span = section_span(text)
    if span is None:
        return {
            "configured": False,
            "reason": "missing_server_entry",
            "config_path": str(path),
            "expected_url": SERVER_URL,
        }
    section = text[span[0] : span[1]]
    url = unquote(read_setting(section, "url"))
    enabled_raw = (read_setting(section, "enabled") or "true").lower()
    enabled = enabled_raw == "true"
    configured = url == SERVER_URL and enabled
    return {
        "configured": configured,
        "reason": "ok" if configured else "url_or_enabled_mismatch",
        "config_path": str(path),
        "url": url,
        "enabled": enabled,
        "expected_url": SERVER_URL,
    }


def replace_or_insert(section: str, key: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^(\s*{re.escape(key)}\s*=\s*)[^#\r\n]*(\s*(?:#.*)?)$")
    if pattern.search(section):
        return pattern.sub(lambda m: f"{m.group(1)}{value}{m.group(2)}", section, count=1)
    header_end = section.find("\n")
    if header_end == -1:
        return section + f"\n{key} = {value}\n"
    return section[: header_end + 1] + f"{key} = {value}\n" + section[header_end + 1 :]


def desired_section(newline: str) -> str:
    lines = [
        "[mcp_servers.yj-museum]",
        f'url = "{SERVER_URL}"',
        "enabled = true",
        "startup_timeout_sec = 20",
        "tool_timeout_sec = 60",
        'default_tools_approval_mode = "approve"',
        "",
    ]
    return newline.join(lines)


def configure(path: Path) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    original = path.read_text(encoding="utf-8-sig") if existed else ""
    newline = "\r\n" if "\r\n" in original else "\n"
    span = section_span(original)

    if span is None:
        prefix = original
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += newline
        if prefix and not prefix.endswith(newline * 2):
            prefix += newline
        updated = prefix + desired_section(newline)
    else:
        section = original[span[0] : span[1]]
        for key, value in (
            ("url", f'"{SERVER_URL}"'),
            ("enabled", "true"),
            ("startup_timeout_sec", "20"),
            ("tool_timeout_sec", "60"),
            ("default_tools_approval_mode", '"approve"'),
        ):
            section = replace_or_insert(section, key, value)
        updated = original[: span[0]] + section + original[span[1] :]

    if updated == original:
        result = inspect_config(path)
        result.update({"changed": False, "restart_required": False, "backup_path": None})
        return result

    backup_path: Path | None = None
    if existed:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup_path)

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        handle.write(updated)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)

    result = inspect_config(path)
    result.update(
        {
            "changed": True,
            "restart_required": True,
            "backup_path": str(backup_path) if backup_path else None,
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="Check configuration (default).")
    action.add_argument("--configure", action="store_true", help="Add or repair the server entry.")
    parser.add_argument("--config", type=Path, default=default_config_path())
    args = parser.parse_args()

    result = configure(args.config) if args.configure else inspect_config(args.config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
