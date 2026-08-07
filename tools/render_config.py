#!/usr/bin/env python3
"""Render Claude agents and CLIProxyAPI snippets from config/models.env."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / "config" / "models.env"
TEMPLATE_DIR = ROOT / "templates" / "claude" / "agents"
SKILL_TEMPLATE_DIR = ROOT / "templates" / "claude" / "skills"
GENERATED_AGENT_DIR = ROOT / "generated" / "claude" / "agents"
GENERATED_SKILL_DIR = ROOT / "generated" / "claude" / "skills"
GENERATED_PROXY_DIR = ROOT / "generated" / "cliproxy"

# Profile == env-suffix (lower-cased) == tandy agent name, so ROLES is the whole
# vocabulary; the profile no longer needs its own DELEGATE_NAME_* mapping.
ROLES = ("TERRA", "LUNA", "SOL")

REQUIRED = [
    "DELEGATE_ALIAS_TERRA",
    "DELEGATE_ALIAS_LUNA",
    "DELEGATE_ALIAS_SOL",
    "DELEGATE_MODEL_TERRA",
    "DELEGATE_MODEL_LUNA",
    "DELEGATE_MODEL_SOL",
    "DELEGATE_HINT_TERRA",
    "DELEGATE_HINT_LUNA",
    "DELEGATE_HINT_SOL",
    "DELEGATE_EFFORT_TERRA",
    "DELEGATE_EFFORT_LUNA",
    "DELEGATE_EFFORT_SOL",
    "AGENT_PREFIX",
]

# DELEGATE_EFFORT_* is shared with the dairy/herd runners, which pass it to a
# backend CLI where the vocabulary differs, so those keys stay restricted.
EFFORTS = ("low", "medium", "high", "max")
# Native profiles are subagent-only, and the clamp that silently rewrote xhigh
# to high on that path is fixed as of Claude Code 2.1.223 (measured on the wire
# with the parent pinned low). See docs/known-issues.md.
NATIVE_EFFORTS = EFFORTS + ("xhigh",)

# Native profiles run Anthropic models straight through the gateway, so they take
# no oauth-model-alias entry and never appear in the proxy fragments. They carry
# no AGENT_PREFIX: `tandy` means Codex-backed, and dairy/herd share that word.
NATIVE_KEY = "DELEGATE_NATIVE_PROFILES"


def native_key(profile: str) -> str:
    """opus5-1m -> OPUS5_1M, the env-key spelling of a native profile name."""
    return profile.upper().replace("-", "_")


def parse_native(values: dict[str, str]) -> list[dict[str, str]]:
    """Resolve DELEGATE_NATIVE_PROFILES into rendered-agent inputs."""
    names = [item.strip() for item in values.get(NATIVE_KEY, "").split(",") if item.strip()]
    if len(set(names)) != len(names):
        raise ValueError(f"{NATIVE_KEY} lists a duplicate profile")
    profiles: list[dict[str, str]] = []
    for name in names:
        # The agent name is the profile name, so it must be a legal one, and it
        # must not collide with the alias namespace Claude Code filters on.
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
            raise ValueError(f"{NATIVE_KEY}: {name!r} is not a valid agent name")
        if name.startswith("claude"):
            raise ValueError(f"{NATIVE_KEY}: {name!r} must not start with 'claude'")
        suffix = native_key(name)
        entry = {"profile": name}
        for field in ("ALIAS", "HINT", "EFFORT"):
            key = f"DELEGATE_NATIVE_{field}_{suffix}"
            if not values.get(key):
                raise ValueError(f"{name} is listed in {NATIVE_KEY} but {key} is missing")
            entry[field.lower()] = values[key]
        if entry["effort"] not in NATIVE_EFFORTS:
            raise ValueError(
                f"DELEGATE_NATIVE_EFFORT_{suffix}={entry['effort']!r} is not one of "
                + ", ".join(NATIVE_EFFORTS)
            )
        profiles.append(entry)
    return profiles


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"{path}:{number}: invalid key {key!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        raise ValueError(f"Missing required keys: {', '.join(missing)}")
    aliases = [values[f"DELEGATE_ALIAS_{role}"] for role in ROLES]
    if len(set(aliases)) != len(aliases):
        raise ValueError("Delegate aliases must be unique")
    for role in ROLES:
        effort = values[f"DELEGATE_EFFORT_{role}"]
        if effort not in EFFORTS:
            raise ValueError(
                f"DELEGATE_EFFORT_{role}={effort!r} is not one of {', '.join(EFFORTS)}"
                + (" (Claude Code clamps xhigh to high for subagents)"
                   if effort == "xhigh" else "")
            )
    return values


def yaml_quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def render_template(text: str, values: dict[str, str]) -> str:
    def replacement(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"Unknown template key: {key}")
        return values[key]

    rendered = re.sub(r"\{\{([A-Z][A-Z0-9_]*)\}\}", replacement, text)
    unresolved = re.findall(r"\{\{[^}]+\}\}", rendered)
    if unresolved:
        raise ValueError(f"Unresolved placeholders: {unresolved}")
    return rendered


def output_files(values: dict[str, str]) -> dict[Path, str]:
    outputs: dict[Path, str] = {}
    # One agent per (model profile x capability). Bodies come from three shared
    # templates so a wording change does not have to be repeated per profile.
    capabilities = (("write", ""), ("worktree", "-worktree"), ("readonly", "-readonly"))
    prefix = values["AGENT_PREFIX"]

    # (agent-name stem, alias, hint, effort, noun) for every rendered family.
    # Tandy agents are prefixed and describe themselves as a "profile"; native
    # ones are unprefixed and say "delegate", because their name is the profile.
    families = [
        (f"{prefix}-{role.lower()}", values[f"DELEGATE_ALIAS_{role}"],
         values[f"DELEGATE_HINT_{role}"], values[f"DELEGATE_EFFORT_{role}"],
         role.lower(), "profile")
        for role in ROLES
    ]
    families += [
        (native["profile"], native["alias"], native["hint"], native["effort"],
         native["profile"], "native model")
        for native in parse_native(values)
    ]

    for stem_name, alias, hint, effort, shown, noun in families:
        for stem, suffix in capabilities:
            template = TEMPLATE_DIR / f"{stem}.md.tmpl"
            agent_name = f"{stem_name}{suffix}"
            local = dict(values)
            local["AGENT_NAME"] = agent_name
            local["AGENT_MODEL"] = alias
            local["AGENT_PROFILE"] = shown
            local["PROFILE_NOUN"] = noun
            local["PROFILE_HINT"] = hint
            local["PROFILE_EFFORT"] = effort
            destination = GENERATED_AGENT_DIR / f"{agent_name}.md"
            outputs[destination] = render_template(template.read_text(encoding="utf-8"), local)

    for template in sorted(SKILL_TEMPLATE_DIR.glob("**/*.tmpl")):
        relative = template.relative_to(SKILL_TEMPLATE_DIR)
        destination = GENERATED_SKILL_DIR / relative.with_name(relative.name.removesuffix(".tmpl"))
        outputs[destination] = render_template(template.read_text(encoding="utf-8"), values)

    roles = ROLES
    oauth_lines = [
        "# Generated from config/models.env. Merge under the top-level config.",
        "# Applies to Codex OAuth/login credentials.",
        "oauth-model-alias:",
        "  codex:",
    ]
    for role in roles:
        oauth_lines.extend(
            [
                f"    - name: {yaml_quote(values[f'DELEGATE_MODEL_{role}'])}",
                f"      alias: {yaml_quote(values[f'DELEGATE_ALIAS_{role}'])}",
                "      force-mapping: true",
            ]
        )
    outputs[GENERATED_PROXY_DIR / "oauth-model-alias.yaml"] = "\n".join(oauth_lines) + "\n"

    api_lines = [
        "# Generated from config/models.env.",
        "# Paste the entries below into the `models:` list of each relevant",
        "# `codex-api-key` credential. This is a fragment, not a full config.",
        "models:",
    ]
    for role in roles:
        api_lines.extend(
            [
                f"  - name: {yaml_quote(values[f'DELEGATE_MODEL_{role}'])}",
                f"    alias: {yaml_quote(values[f'DELEGATE_ALIAS_{role}'])}",
            ]
        )
    outputs[GENERATED_PROXY_DIR / "codex-api-key-models.yaml"] = "\n".join(api_lines) + "\n"

    config_lines = [
        "# Generated from config/models.env by tools/render_config.py.",
        "# Copy to this device's CLIProxyAPI config path, then replace <AUTH_DIR> and",
        "# <CLIENT_KEY>. Never commit or sync a filled copy: it carries a local key.",
        f'host: "{values.get("CLIPROXY_HOST", "127.0.0.1")}"',
        f'port: {values.get("CLIPROXY_PORT", "8317")}',
        "",
        'auth-dir: "<AUTH_DIR>"',
        "",
        "remote-management:",
        "  allow-remote: false",
        '  secret-key: ""',
        "",
        "api-keys:",
        '  - "<CLIENT_KEY>"',
        "",
        "# Alias IDs stay stable because the generated agent files reference them.",
        "oauth-model-alias:",
        "  codex:",
    ]
    for role in roles:
        config_lines.append(f"    - name: {yaml_quote(values[f'DELEGATE_MODEL_{role}'])}")
        config_lines.append(f"      alias: {yaml_quote(values[f'DELEGATE_ALIAS_{role}'])}")
        config_lines.append("      force-mapping: true")
        shown = values.get(f"CLIPROXY_DISPLAY_{role}", "")
        if shown:
            config_lines.append(f"      display-name: {yaml_quote(shown)}")

    excluded = [("claude", "CLIPROXY_EXCLUDE_CLAUDE"), ("codex", "CLIPROXY_EXCLUDE_CODEX")]
    sections = [
        (channel, [item.strip() for item in values.get(key, "").split(",") if item.strip()])
        for channel, key in excluded
    ]
    if any(items for _, items in sections):
        config_lines.extend(["", "oauth-excluded-models:"])
        for channel, items in sections:
            if not items:
                continue
            config_lines.append(f"  {channel}:")
            config_lines.extend(f"    - {yaml_quote(item)}" for item in items)
    outputs[GENERATED_PROXY_DIR / "config.template.yaml"] = "\n".join(config_lines) + "\n"

    map_lines = ["profile\talias\tprovider-model"]
    for role in roles:
        map_lines.append(
            f"{role.lower()}\t{values[f'DELEGATE_ALIAS_{role}']}\t{values[f'DELEGATE_MODEL_{role}']}"
        )
    outputs[ROOT / "generated" / "model-map.tsv"] = "\n".join(map_lines) + "\n"
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if generated files are stale")
    args = parser.parse_args()

    values = parse_env(ENV_PATH)
    outputs = output_files(values)
    stale: list[str] = []
    for path, content in outputs.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            print(path.relative_to(ROOT))
    if stale:
        print("Stale generated files:")
        for path in stale:
            print(f"  {path}")
        print("Run: python tools/render_config.py")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
