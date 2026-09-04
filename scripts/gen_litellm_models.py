#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "pyyaml",
# ]
# ///
"""
Generate the `models` list for the litellm provider in opencode.jsonc.

Reality-based: it cross-checks the LiteLLM model list against each model's
downstream llama-swap config (local file or over ssh) and parses the real
llama.cpp flags (-c/--ctx-size, --reasoning on/off, --mmproj, --embedding)
so capabilities come from the actual server, never guessed.

Requires env vars:
    LITELLM_SERVER   e.g. https://server.tailc63ae8.ts.net:4444
    LITELLM_API_KEY  the litellm API key

Companion config (machines -> llama-swap config paths):
    scripts/config.json   (edit to point at each machine's llama-swap config)

Usage:
    uv run scripts/gen_litellm_models.py [path/to/opencode.jsonc]
"""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request
from urllib.parse import urlparse

import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(os.path.dirname(SCRIPT_DIR), "opencode.jsonc")
MACHINES_PATH = os.path.join(SCRIPT_DIR, "config.json")

# Top-level sections carried over from an existing config so a full rewrite
# does not silently drop unrelated settings (mcp, plugin, instructions...).
CARRIED_FIELDS = ("instructions", "plugin", "mcp", "disabled_providers")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def http_json(url: str, api_key: str | None) -> dict | list:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_litellm_models(server: str, api_key: str) -> tuple[list[str], dict]:
    base = server.rstrip("/")
    try:
        info = http_json(f"{base}/model/info", api_key)
    except Exception:
        info = {"data": []}
    try:
        listed = http_json(f"{base}/v1/models", api_key)
    except Exception:
        listed = {"data": []}

    ids = []
    for item in listed.get("data", []):
        mid = item.get("id") if isinstance(item, dict) else item
        if mid and str(mid) not in ids:
            ids.append(str(mid))

    by_name: dict[str, dict] = {}
    for item in info.get("data", []):
        name = item.get("model_name")
        if not name or not isinstance(item, dict):
            continue
        lp = item.get("litellm_params") or {}
        mi = item.get("model_info") or {}
        lpmi = lp.get("model_info") or {}
        downstream = str(lp.get("model") or name).split("/", 1)[-1]
        # merge litellm_params.model_info under model_info, keeping non-null
        combined = {**(lpmi or {})}
        for k, v in (mi or {}).items():
            if v is not None:
                combined[k] = v
        entry = {
            "api_base": lp.get("api_base"),
            "downstream_id": downstream,
            "model_info": combined,
        }
        # /model/info can list the same name multiple times (several teams);
        # keep the richest entry.
        prev = by_name.get(name)
        if prev and prev.get("api_base") and len(prev.get("model_info", {})) > len(combined):
            continue
        by_name[name] = entry
    return ids, by_name


def load_machines_config(path: str = MACHINES_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read machines config {path}: {exc}", file=sys.stderr)
        return {}


def fetch_llamaswap_yaml(machine: dict, api_base: str) -> str | None:
    config = machine.get("config")
    if not config:
        return None
    if machine.get("local"):
        try:
            with open(os.path.expanduser(config), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None
    ssh = machine.get("ssh")
    if not ssh:
        return None
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh, f"cat {config}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.stdout if proc.returncode == 0 else None
    except (subprocess.SubprocessError, OSError):
        return None


MACRO_RE = re.compile(r"\$\{([^}]+)\}")


def expand_macros(cmd: str, macros: dict, depth: int = 0) -> str:
    if depth > 10:
        return cmd
    m = MACRO_RE.search(cmd)
    if not m:
        return cmd
    value = str(macros.get(m.group(1), ""))
    return expand_macros(cmd.replace(m.group(0), value), macros, depth + 1)


def parse_flags(cmd: str) -> dict:
    caps: dict = {}

    def last_match(pattern: re.Pattern) -> re.Match | None:
        found = None
        for m in pattern.finditer(cmd):
            if found is None or m.end() >= found.end():
                found = m
        return found

    # llama.cpp takes the LAST context flag; macros may set one that the
    # model cmd overrides with -c/--ctx-size later in the string.
    ctx_patterns = [
        re.compile(r"(?:^|\s)--ctx-size\s+(\d+)"),
        re.compile(r"(?:^|\s)--context-size\s+(\d+)"),
        re.compile(r"(?:^|\s)-c\s+(\d+)"),
    ]
    ctx = None
    for pattern in ctx_patterns:
        m = last_match(pattern)
        if m is not None and (ctx is None or m.end() >= ctx.end()):
            ctx = m
    if ctx:
        caps["context"] = int(ctx.group(1))

    emb = re.search(r"(?:^|\s)--embedding\b", cmd)
    rerank = re.search(r"(?:^|\s)--reranking\b", cmd)
    caps["embedding"] = bool(emb or rerank)

    caps["vision"] = "--mmproj" in cmd

    reasoning = None
    m = re.search(r"(?:^|\s)--reasoning\s+(\S+)", cmd)
    if m:
        reasoning = m.group(1).strip().lower() not in ("off", "0", "false", "no")
    if re.search(r"(?:^|\s)--no-reasoning\b", cmd):
        reasoning = False
    if reasoning is None and "--reasoning-preserve" in cmd:
        reasoning = True
    caps["reasoning"] = reasoning

    caps["effort"] = '"reasoning_effort"' in cmd
    return caps


def parse_llamaswap(text: str) -> dict:
    cfg = yaml.safe_load(text) or {}
    macros = cfg.get("macros") or {}
    models = cfg.get("models") or {}

    # served name (entry name or alias) -> capabilities
    table: dict[str, dict] = {}
    norm: dict[str, dict] = {}
    for entry_name, entry in models.items():
        if not isinstance(entry, dict) or "cmd" not in entry:
            continue
        expanded = expand_macros(str(entry["cmd"]), macros)
        caps = parse_flags(expanded)
        served = [entry_name] + list(entry.get("aliases") or [])
        for name in served:
            table[name] = caps
            norm[normalize(name)] = caps
    return {"table": table, "norm": norm}


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def resolve_caps(model_id: str, meta: dict, entries_by_host: dict[str, dict]) -> dict | None:
    down_id = meta.get("downstream_id") or model_id
    host = ""
    if meta.get("api_base"):
        host = urlparse(meta["api_base"]).hostname or ""

    candidates = []
    for entry in (model_id, down_id):
        candidates.append(entry)
        candidates.append(normalize(entry))

    for hostname, parsed in entries_by_host.items():
        table = parsed["table"]
        norm = parsed["norm"]
        if host and hostname != host:
            continue
        for cand in candidates:
            if cand in table:
                return dict(table[cand])
            if normalize(cand) in norm:
                return dict(norm[normalize(cand)])
    return None


def merge_caps(model_id: str, meta: dict, caps: dict | None, mach, overrides: dict) -> dict:
    final: dict = {}
    lp = {"host": "", "config": None}
    if meta.get("api_base"):
        lp["host"] = urlparse(meta["api_base"]).hostname or ""

    # reality: explicit overrides (user-provided data) win
    if model_id in overrides:
        final.update(overrides[model_id])

    # llama-swap cmd flags are ground truth for the served instance
    if caps:
        final.setdefault("context", caps.get("context"))
        final["vision"] = caps["vision"]
        if caps["reasoning"] is not None:
            final["reasoning"] = caps["reasoning"]
        final["effort"] = caps["effort"]
        final["tool_call"] = not caps["embedding"]
        if caps["embedding"]:
            final["embedding"] = True

    # litellm model_info as fallback when llama-swap gives nothing
    mi = meta.get("model_info") or {}
    if final.get("context") is None:
        ctx = mi.get("max_input_tokens") or mi.get("max_tokens")
        if ctx:
            final["context"] = int(ctx)
    if "reasoning" not in final and mi.get("supports_reasoning") is not None:
        final["reasoning"] = bool(mi["supports_reasoning"])
    if mi.get("supports_vision") is not None:
        final["vision"] = bool(mi["supports_vision"])
    if "tool_call" not in final and mi.get("supports_function_calling") is not None:
        final["tool_call"] = bool(mi["supports_function_calling"])
    if mi.get("max_output_tokens"):
        final["output"] = int(mi["max_output_tokens"])

    return final


def build_model_entry(model_id: str, meta: dict, caps: dict, defaults: dict) -> dict | None:
    if caps.get("embedding"):
        return None

    entry: dict = {"name": model_id}

    tool_call = caps.get("tool_call", bool(defaults.get("tool_call", True)))
    entry["tool_call"] = bool(tool_call)
    if isinstance(caps.get("tool_call"), bool):
        entry["tool_call"] = caps["tool_call"]

    if "reasoning" in caps and caps["reasoning"] is not None:
        entry["reasoning"] = bool(caps["reasoning"])

    modalities_in = ["text"]
    if caps.get("vision"):
        modalities_in.append("image")
    entry["modalities"] = {"input": modalities_in, "output": ["text"]}

    context = caps.get("context")
    output = caps.get("output")
    if context:
        limit = {"context": int(context)}
        if output:
            limit["output"] = int(output)
            limit["input"] = int(context) - int(output)
        entry["limit"] = limit

    if caps.get("reasoning") and caps.get("effort"):
        entry["options"] = {"reasoningEffort": "medium"}
        entry["variants"] = {
            "low": {"reasoningEffort": "low"},
            "medium": {"reasoningEffort": "medium"},
            "xhigh": {"reasoningEffort": "xhigh"},
        }
    return entry


def load_existing_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def build_config(models: dict[str, dict], existing: dict | None = None) -> dict:
    config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "litellm": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "litellm",
                "options": {
                    "baseURL": "{env:LITELLM_SERVER}/v1",
                    "apiKey": "{env:LITELLM_API_KEY}",
                },
                "models": models,
            }
        },
        "disabled_providers": [],
    }
    existing = existing or {}
    for field in CARRIED_FIELDS:
        if field in existing:
            config[field] = existing[field]
    return config


def _git(*args: str, cwd: str) -> bool:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"git {' '.join(args)} failed:\n{result.stderr.strip()}", file=sys.stderr)
    return result.returncode == 0


def commit_and_push(target: str) -> bool:
    repo = os.path.dirname(os.path.abspath(target))
    if not _git("rev-parse", "--is-inside-work-tree", cwd=repo):
        print("Not a git repo, skipping commit/push.", file=sys.stderr)
        return False
    rel = os.path.relpath(target, repo)
    if not _git("add", "--", rel, cwd=repo):
        return False
    if not _git("diff", "--cached", "--quiet", cwd=repo):
        if not _git("commit", "-m", "update litellm models from server", cwd=repo):
            return False
    return _git("push", cwd=repo)


def main() -> int:
    server = os.environ.get("LITELLM_SERVER")
    api_key = os.environ.get("LITELLM_API_KEY")
    if not server or not api_key:
        print("Missing env vars. Need LITELLM_SERVER and LITELLM_API_KEY.", file=sys.stderr)
        return 1

    path = sys.argv[1] if len(sys.argv) > 1 else CONFIG_PATH
    mach_cfg = load_machines_config()
    machines = mach_cfg.get("llama_swap", {}).get("machines", {})
    defaults = mach_cfg.get("defaults", {})
    overrides = mach_cfg.get("overrides", {})

    print(f"Fetching models from {server} ...")
    model_ids, by_name = fetch_litellm_models(server, api_key)
    if not model_ids:
        print("No models returned by server.", file=sys.stderr)
        return 1

    # group models by downstream host
    hosts: dict[str, list[str]] = {}
    for mid in model_ids:
        host = urlparse(by_name.get(mid, {}).get("api_base") or "").hostname or ""
        hosts.setdefault(host, []).append(mid)

    entries_by_host: dict[str, dict] = {}
    for host in hosts:
        machine = machines.get(host)
        if not machine:
            continue
        text = fetch_llamaswap_yaml(machine, host)
        if text is None:
            print(f"  (no llama-swap config for host {host})")
            continue
        entries_by_host[host] = parse_llamaswap(text)

    models: dict[str, dict] = {}
    skipped: list[str] = []
    no_caps: list[str] = []
    for mid in sorted(model_ids):
        meta = by_name.get(mid, {})
        caps = resolve_caps(mid, meta, entries_by_host)
        caps = merge_caps(mid, meta, caps, machines, overrides)
        entry = build_model_entry(mid, meta, caps, defaults)
        if entry is None:
            skipped.append(mid)
            continue
        if not caps:
            no_caps.append(mid)
        models[mid] = entry
        detail = (
            f"ctx={caps.get('context') or '?'}"
            f" vision={caps.get('vision', '?')}"
            f" reason={caps.get('reasoning', '?')}"
            f" tool={caps.get('tool_call', '?')}"
        )
        print(f"  - {mid:36s} {detail}")

    if skipped:
        print(f"\nSkipped non-chat models (embeddings/rerankers): {', '.join(skipped)}")
    if no_caps:
        print("\nNo capability data found for (name-only output):")
        for mid in no_caps:
            print(f"  - {mid}")
    print("\nAdd real values for those in scripts/config.json under 'overrides'.")

    config = build_config(models, load_existing_config(path))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")

    print(f"\nWrote {len(models)} models to {path}")

    if not commit_and_push(path):
        return 1
    print("Committed and pushed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())