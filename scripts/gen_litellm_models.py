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

Reasoning effort is not always a flag. llama.cpp exposes it via
--chat-template-kwargs, but vllm has no such flag: the knob lives inside the
served chat template, reached through --chat-template (a container path when
the model runs under docker). So the template itself is read — local file or
over ssh, container path resolved back through the -v mounts — to see whether
it takes reasoning_effort and which values it accepts.

Strata is neither of those: serve/server.py is launched as `--engine strata
--config <json>`, and the real flags (--max-context, --vision) live in that
JSON. Those configs are read the same way, and the chat template is taken from
the pack's tokenizer dir.

When an entry's cmd is only a launcher script (`run-model.sh ${PORT}`), the
script is read on the serving machine first: that is where the flags are.

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


def fetch_litellm_models(server: str, api_key: str) -> tuple[list[str], dict, set[str]]:
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
    hosts_by_name: dict[str, set[str]] = {}
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
        hosts_by_name.setdefault(name, set()).add(
            urlparse(lp.get("api_base") or "").hostname or ""
        )
        # /model/info can list the same name multiple times (several teams);
        # keep the richest entry.
        prev = by_name.get(name)
        if prev and prev.get("api_base") and len(prev.get("model_info", {})) > len(combined):
            continue
        by_name[name] = entry
    # One name served from two boxes (`k2-horizon-7b` on desktop2 and ai.home):
    # litellm load-balances between them, so neither host's llama-swap flags can
    # be called correct for the name. Reported instead of guessed at.
    ambiguous = {n for n, hosts in hosts_by_name.items() if len(hosts) > 1}
    return ids, by_name, ambiguous


def load_machines_config(path: str = MACHINES_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read machines config {path}: {exc}", file=sys.stderr)
        return {}


def read_remote_text(machine: dict, path: str) -> str | None:
    """Read a file on a machine from the config: local path, or `ssh cat`."""
    if not path:
        return None
    if machine.get("local"):
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None
    ssh = machine.get("ssh")
    if not ssh:
        return None
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh, f"cat {path}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.stdout if proc.returncode == 0 else None
    except (subprocess.SubprocessError, OSError):
        return None


def fetch_llamaswap_yaml(machine: dict, api_base: str) -> str | None:
    return read_remote_text(machine, machine.get("config") or "")


MACRO_RE = re.compile(r"\$\{([^}]+)\}")


def expand_macros(cmd: str, macros: dict, depth: int = 0) -> str:
    if depth > 10:
        return cmd
    m = MACRO_RE.search(cmd)
    if not m:
        return cmd
    value = str(macros.get(m.group(1), ""))
    return expand_macros(cmd.replace(m.group(0), value), macros, depth + 1)


LAUNCHER_RE = re.compile(r"^\s*(\S+\.(?:sh|bash|py))(?:\s|$)")


def launcher_body(machine: dict | None, cmd: str, seen: dict[str, str | None]) -> str:
    """The command inside a launcher script, added to what the cmd already says.

    A cmd can be nothing but `/path/run-server.sh ${PORT}` or `${server}
    --mmproj ...`; the script is then read on the serving machine, because the
    flags each half contributes are only complete together. The script body is
    put first: a wrapper runs `llama-server <its own flags> "$@"`, so the cmd's
    flags come last and win the way llama.cpp's last-flag-wins scan reads them.
    Returns cmd unchanged when the entry is not a script, or when it cannot be
    read.
    """
    if machine is None:
        return cmd
    m = LAUNCHER_RE.match(cmd)
    if not m:
        return cmd
    path = m.group(1)
    if path not in seen:
        seen[path] = read_remote_text(machine, path)
    body = seen[path]
    return f"{body}\n{cmd}" if body else cmd


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
    # vllm (bare or behind docker) uses --max-model-len instead of -c.
    # strata uses --max-context.
    ctx_patterns = [
        re.compile(r"(?:^|\s)--ctx-size\s+(\d+)"),
        re.compile(r"(?:^|\s)--context-size\s+(\d+)"),
        re.compile(r"(?:^|\s)-c\s+(\d+)"),
        re.compile(r"(?:^|\s)--max-model-len\s+(\d+)"),
        re.compile(r"(?:^|\s)--max-context\s+(\d+)"),
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
    # vllm speaks a different grammar than llama.cpp: --language-model-only
    # is an explicit text-only toggle, while multimodal limit/token flags
    # mark a vision-capable server.
    if "--language-model-only" in cmd:
        caps["vision"] = False
    elif re.search(
        r"(?:^|\s)(?:--limit-mm-per-prompt|--image-token-id|--image-input-type|--image-feature-size)\b",
        cmd,
    ):
        caps["vision"] = True

    reasoning = None
    m = re.search(r"(?:^|\s)--reasoning\s+(\S+)", cmd)
    if m:
        reasoning = m.group(1).strip().lower() not in ("off", "0", "false", "no")
    if re.search(r"(?:^|\s)--no-reasoning\b", cmd):
        reasoning = False
    # vllm enables reasoning-content parsing via --reasoning-parser <name>
    m = re.search(r"(?:^|\s)--reasoning-parser\s+(\S+)", cmd)
    if m:
        parser = m.group(1).strip().lower()
        if reasoning is None and parser not in ("none", "null", "off", "0", "false", "no"):
            reasoning = True
    if reasoning is None and "--reasoning-preserve" in cmd:
        reasoning = True
    caps["reasoning"] = reasoning

    caps["effort"] = '"reasoning_effort"' in cmd
    return caps


# Used when the template says nothing about which values it takes.
DEFAULT_EFFORTS = ("low", "medium", "xhigh")


def order_efforts(values: list[str]) -> list[str]:
    """Least to most effort, so every model lists its variants the same way.

    Templates list them in their own order (`xhigh` first, being the default);
    unknown values keep their relative order and sort last.
    """
    def key(value: str) -> tuple[int, int]:
        rank = DEFAULT_EFFORTS.index(value) if value in DEFAULT_EFFORTS else len(DEFAULT_EFFORTS)
        return rank, values.index(value)

    return sorted(values, key=key)


def parse_docker_mounts(cmd: str) -> list[tuple[str, str]]:
    """`-v <host>:<container>` pairs of a docker run cmd, for path translation."""
    out = []
    for m in re.finditer(r"(?:^|\s)-v\s+(\S+)", cmd):
        parts = m.group(1).split(":")
        if len(parts) >= 2 and parts[0] and parts[1]:
            out.append((parts[0], parts[1]))
    return out


def container_to_host(path: str, mounts: list[tuple[str, str]]) -> str:
    """Map a path inside a container back to the host path that serves it."""
    best: tuple[str, str] | None = None
    for host, container in mounts:
        if path == container or path.startswith(container.rstrip("/") + "/"):
            if best is None or len(container) > len(best[1]):
                best = (host, container)
    if not best:
        return path
    host, container = best
    return host + path[len(container) :]


def template_effort(template: str) -> tuple[bool, list[str] | None, bool]:
    """What a chat template reveals: effort support, its values, and thinking.

    Returns (takes reasoning_effort, which values, emits thinking at all).
    The third is separate because a template can gate thinking on
    enable_thinking while taking no effort knob at all -- which is exactly
    what strata serves, having no --reasoning flag to read.

    Swift-1.5 Qwen3.8, for instance:
        reasoning_effort = reasoning_effort|default('xhigh')
        reasoning_effort not in ('xhigh', 'medium', 'low')
    """
    thinks = "enable_thinking" in template or "reasoning_effort" in template
    if "reasoning_effort" not in template:
        return False, None, thinks
    m = re.search(r"reasoning_effort.{0,200}?not in \(([^)]*)\)", template, re.S)
    if not m:
        return True, None, True
    values = re.findall(r"['\"]([a-z]+)['\"]", m.group(1))
    return True, values or None, True


def strata_config_path(cmd: str) -> str | None:
    """Path of the JSON config a Strata entry runs on, None if not Strata.

    Strata's llama-swap cmd is just `serve/server.py --engine strata
    --config <json>` (or a launcher script that runs exactly that); every
    capability flag lives in that JSON, not in the cmd.
    """
    if not re.search(r"(?:^|\s)--engine\s+strata\b", cmd):
        return None
    m = re.search(r"(?:^|\s)--config\s+(\S+)", cmd)
    return m.group(1).strip("\"'") if m else None


def parse_strata(machine: dict, path: str) -> tuple[dict, list[str]]:
    """Capabilities from a Strata server JSON, plus its chat template path.

    Reads the engine's real `args` with the same flag grammar as a cmd, and
    treats the `vision` block as the --mmproj equivalent it is. Returns
    ({}, []) when the config is unreadable, leaving the model name-only.
    """
    body = read_remote_text(machine, path)
    try:
        cfg = json.loads(body or "")
    except json.JSONDecodeError:
        return {}, []
    if not isinstance(cfg, dict):
        return {}, []

    args = cfg.get("args")
    args = [str(a) for a in args] if isinstance(args, list) else []
    caps = parse_flags(" ".join(args))

    vision = cfg.get("vision")
    sees_images = (
        "--vision" in args
        or (isinstance(vision, dict) and bool(vision.get("mmproj")))
        or "--mmproj" in args
    )
    # strata has no --language-model-only; guard anyway so an explicit
    # text-only toggle keeps winning the way it does for vllm.
    caps["vision"] = sees_images and "--language-model-only" not in args

    templates: list[str] = []
    tokenizer = cfg.get("tokenizer")
    if isinstance(tokenizer, str) and tokenizer:
        templates.append(f"{tokenizer.rstrip('/')}/chat_template.jinja")
    return caps, templates


def machine_for_host(machines: dict, host: str) -> dict | None:
    """The machine an api_base host refers to, or None.

    A host can be spelled two ways: the tailscale name the proxy dials, or the
    ssh alias the config records. Both mean the same box, so match either —
    otherwise every model on that host silently falls back to litellm's own
    metadata instead of the real llama-swap flags.
    """
    if not host:
        return None
    found = machines.get(host)
    if found:
        return found
    return next(
        (m for m in machines.values() if isinstance(m, dict) and m.get("ssh") == host),
        None,
    )


def parse_llamaswap(text: str, machine: dict | None = None) -> dict:
    cfg = yaml.safe_load(text) or {}
    macros = cfg.get("macros") or {}
    models = cfg.get("models") or {}

    # several models share one chat template; read each at most once per run
    seen: dict[str, tuple[bool, list[str] | None, bool] | None] = {}
    # same for launcher scripts
    launchers: dict[str, str | None] = {}

    def template_caps(
        cmd: str, extra_paths: list[str] | None = None
    ) -> tuple[bool, list[str] | None, bool] | None:
        """Effort support from the served chat template, None when unknowable.

        Only ever adds capability: an unreadable template leaves the cmd-derived
        answer alone rather than stripping variants off a model that does
        accept --chat-template-kwargs.
        """
        if machine is None:
            return None
        paths = re.findall(r"(?:^|\s)--chat-template(?:-file)?\s+(\S+)", cmd)
        paths += extra_paths or []
        if not paths:
            return None
        mounts = parse_docker_mounts(cmd)
        for raw in paths:
            host_path = container_to_host(raw, mounts)
            if host_path not in seen:
                body = read_remote_text(machine, host_path)
                seen[host_path] = template_effort(body) if body else None
            if seen[host_path]:
                return seen[host_path]
        return None

    # served name (entry name or alias) -> capabilities
    table: dict[str, dict] = {}
    norm: dict[str, dict] = {}
    for entry_name, entry in models.items():
        if not isinstance(entry, dict) or "cmd" not in entry:
            continue
        expanded = expand_macros(str(entry["cmd"]), macros)
        # the flags may be inside a launcher script rather than the cmd
        cmd = launcher_body(machine, expanded, launchers)
        caps = parse_flags(cmd)

        # strata keeps its flags in a JSON file, so the cmd-derived answer
        # above is all defaults; the config holds the real values.
        strata_templates: list[str] = []
        if machine is not None:
            cfg_path = strata_config_path(cmd)
            if cfg_path:
                s_caps, strata_templates = parse_strata(machine, cfg_path)
                if s_caps.get("context") is not None:
                    caps["context"] = s_caps["context"]
                caps["vision"] = s_caps["vision"]
                if s_caps["reasoning"] is not None:
                    caps["reasoning"] = s_caps["reasoning"]

        found = template_caps(cmd, strata_templates)
        if found:
            supports, values, thinks = found
            caps["effort"] = caps["effort"] or supports
            if values:
                caps["effort_values"] = values
            # a template that emits thinking is proof the model reasons,
            # for engines that carry no --reasoning flag to read.
            if thinks and caps["reasoning"] is None:
                caps["reasoning"] = True
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
    override_keys = set(overrides.get(model_id) or {})
    final.update(overrides.get(model_id) or {})

    # llama-swap cmd flags are ground truth for the served instance
    if caps:
        final.setdefault("context", caps.get("context"))
        # guarded on override_keys, not truthiness: an override of
        # `vision: false` must still win over a llama-swap cmd that says
        # text-only, while the normal precedence is kept for other models.
        if "vision" not in override_keys:
            final["vision"] = caps["vision"]
        if caps["reasoning"] is not None:
            final["reasoning"] = caps["reasoning"]
        if "effort" not in override_keys:
            final["effort"] = caps["effort"]
        if "effort_values" not in override_keys and caps.get("effort_values"):
            final["effort_values"] = caps["effort_values"]
        final["tool_call"] = not caps["embedding"]
        if caps["embedding"]:
            final["embedding"] = True

    # litellm model_info as fallback when llama-swap gives nothing
    mi = meta.get("model_info") or {}
    if final.get("context") is None:
        ctx = mi.get("max_input_tokens") or mi.get("max_tokens") or mi.get("context_window")
        if ctx:
            final["context"] = int(ctx)
    if "reasoning" not in final and mi.get("supports_reasoning") is not None:
        final["reasoning"] = bool(mi["supports_reasoning"])
    if "vision" not in override_keys and mi.get("supports_vision") is not None:
        final["vision"] = bool(mi["supports_vision"])
    if "tool_call" not in final and mi.get("supports_function_calling") is not None:
        final["tool_call"] = bool(mi["supports_function_calling"])
    if "output" not in override_keys and mi.get("max_output_tokens"):
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
    if context:
        # opencode's schema requires all three limit keys. llama.cpp gives no
        # hard output cap, so reserve `max_output` from the context for
        # generation (defaults.max_output in scripts/config.json).
        output = caps.get("output") or int(defaults.get("max_output") or 8192)
        output = min(output, int(context))
        entry["limit"] = {
            "context": int(context),
            "input": max(int(context) - output, 1),
            "output": output,
        }

    if caps.get("reasoning") and caps.get("effort"):
        # the chat template is the source of truth for which values it takes
        efforts = order_efforts(caps.get("effort_values") or list(DEFAULT_EFFORTS))
        entry["options"] = {"reasoningEffort": "medium" if "medium" in efforts else efforts[0]}
        entry["variants"] = {name: {"reasoningEffort": name} for name in efforts}
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
    model_ids, by_name, ambiguous = fetch_litellm_models(server, api_key)
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
        machine = machine_for_host(machines, host)
        if not machine:
            print(f"  (no machine in scripts/config.json for host {host})")
            continue
        text = fetch_llamaswap_yaml(machine, host)
        if text is None:
            print(f"  (no llama-swap config for host {host})")
            continue
        entries_by_host[host] = parse_llamaswap(text, machine)

    models: dict[str, dict] = {}
    skipped: list[str] = []
    no_caps: list[str] = []
    multi_host: list[str] = []
    for mid in sorted(model_ids):
        meta = by_name.get(mid, {})
        if mid in ambiguous:
            multi_host.append(mid)
            caps = None
        else:
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
        if caps.get("effort"):
            values = order_efforts(caps.get("effort_values") or list(DEFAULT_EFFORTS))
            detail += f" effort={'/'.join(values)}"
        print(f"  - {mid:36s} {detail}")

    if skipped:
        print(f"\nSkipped non-chat models (embeddings/rerankers): {', '.join(skipped)}")
    if multi_host:
        print("\nServed by more than one machine (no flags attributed):")
        for mid in multi_host:
            print(f"  - {mid}")
        print("  Give one of these a real value in scripts/config.json 'overrides'.")
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