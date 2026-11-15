#!/usr/bin/env python3
"""Refresh the shared OCX model catalog, preserving all role assignments."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import URLError
from urllib.request import Request, urlopen

# Explicit sibling loading also works under the workflow's python -I invocation.
_spec = importlib.util.spec_from_file_location("agent_models", Path(__file__).with_name("agent_models.py"))
agent_models = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_models)
CONFIG = agent_models.CONFIG
GATEWAY_MODELS_URL = "http://127.0.0.1:10100/v1/models?limit=1000"


def fetch_models(*, claude: bool = False) -> list[str]:
    # Canonical IDs use the OpenAI surface. Only Claude discovery uses this header.
    headers = {"anthropic-version": "2023-06-01"} if claude else {}
    url = GATEWAY_MODELS_URL + "&ids=cli" if claude else GATEWAY_MODELS_URL
    with urlopen(Request(url, headers=headers), timeout=10) as response:
        if response.status != 200:
            raise ValueError(f"OCX model catalog returned HTTP {response.status}")
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("unexpected OCX response: expected a JSON object with a data list")
    if payload.get("has_more"):
        raise ValueError("OCX returned a partial model catalog; configuration was not changed")
    ids: list[str] = []
    for entry in payload["data"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"].strip():
            raise ValueError("OCX model entry must contain a non-empty string id")
        if not agent_models._valid_model_id(entry["id"]):
            raise ValueError(f"invalid OCX model ID: {entry['id']!r}")
        ids.append(entry["id"])
    if not ids:
        raise ValueError("OCX returned an empty model list; configuration was not changed")
    if len(set(ids)) != len(ids):
        raise ValueError("OCX returned duplicate model IDs; configuration was not changed")
    return ids


def canonical_claude_id(alias: str) -> str:
    """Decode OCX's Claude discovery spelling into its canonical route ID."""
    base = alias.removesuffix("[1m]")
    for prefix in ("ocx-claude2-", "ocx-claude-"):
        if base.startswith(prefix):
            provider, separator, model = base[len(prefix):].partition("--")
            if not separator or not provider or not model:
                raise ValueError(f"invalid OCX Claude alias: {alias!r}")
            if prefix == "ocx-claude2-":
                model = re.sub(r"~([st])", lambda match: "/" if match[1] == "s" else "~", model)
            return model if provider == "native" else f"{provider}/{model}"
    return f"anthropic/{base}" if base.startswith("claude-") else base


def fetch_catalog() -> tuple[list[str], dict[str, str]]:
    available = fetch_models()
    aliases: dict[str, str] = {}
    for alias in fetch_models(claude=True):
        model_id = canonical_claude_id(alias)
        if model_id in available:
            # Preserve the long-context Claude spelling when OCX offers it.
            if model_id not in aliases or alias.endswith("[1m]"):
                aliases[model_id] = alias
    if not aliases:
        raise ValueError("OCX catalogs have no matching Claude aliases; configuration was not changed")
    return available, aliases


def update_catalog(path: Path, available: list[str], aliases: dict[str, str]) -> None:
    original = path.read_bytes().decode("utf-8")
    agent_models.parse_config_text(original, path)
    newline = "\r\n" if "\r\n" in original else "\n"
    updated = original
    sections = {
        "available_models": [f'  - "{model}"' for model in available],
        "claude_aliases": [f'  {model}: "{alias}"' for model, alias in aliases.items()],
    }
    for section, lines in sections.items():
        rendered = section + ":" + newline + newline.join(lines) + newline
        marker = re.search(rf"(?m)^{section}:[ \t]*\r?$", updated)
        if marker:
            following = re.search(r"(?m)^[a-z_]+:[ \t]*\r?$", updated[marker.end():])
            end = marker.end() + following.start() if following else len(updated)
            updated = updated[:marker.start()] + rendered + updated[end:]
        else:
            updated = updated.rstrip("\r\n") + newline + rendered
    agent_models.parse_config_text(updated, path)

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        available, aliases = fetch_catalog()
        update_catalog(CONFIG, available, aliases)
    except (OSError, ValueError, URLError) as error:
        print(error, file=sys.stderr)
        return 2
    print(f"Updated OCX catalog: {len(available)} models, {len(aliases)} Claude aliases; role assignments unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
