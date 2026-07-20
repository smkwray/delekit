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

REQUIRED = [
    "DELEGATE_ALIAS_DEFAULT",
    "DELEGATE_ALIAS_FAST",
    "DELEGATE_ALIAS_DEEP",
    "DELEGATE_MODEL_DEFAULT",
    "DELEGATE_MODEL_FAST",
    "DELEGATE_MODEL_DEEP",
    "DELEGATE_NAME_DEFAULT",
    "DELEGATE_NAME_FAST",
    "DELEGATE_NAME_DEEP",
    "AGENT_PREFIX",
]


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
    aliases = [values[f"DELEGATE_ALIAS_{role}"] for role in ("DEFAULT", "FAST", "DEEP")]
    if len(set(aliases)) != len(aliases):
        raise ValueError("Delegate aliases must be unique")
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
    for role in ("DEFAULT", "FAST", "DEEP"):
        profile = values[f"DELEGATE_NAME_{role}"]
        alias = values[f"DELEGATE_ALIAS_{role}"]
        for stem, suffix in capabilities:
            template = TEMPLATE_DIR / f"{stem}.md.tmpl"
            agent_name = f"{prefix}-{profile}{suffix}"
            local = dict(values)
            local["AGENT_NAME"] = agent_name
            local["AGENT_MODEL"] = alias
            local["AGENT_PROFILE"] = profile
            destination = GENERATED_AGENT_DIR / f"{agent_name}.md"
            outputs[destination] = render_template(template.read_text(encoding="utf-8"), local)

    for template in sorted(SKILL_TEMPLATE_DIR.glob("**/*.tmpl")):
        relative = template.relative_to(SKILL_TEMPLATE_DIR)
        destination = GENERATED_SKILL_DIR / relative.with_name(relative.name.removesuffix(".tmpl"))
        outputs[destination] = render_template(template.read_text(encoding="utf-8"), values)

    roles = ("DEFAULT", "FAST", "DEEP")
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
            path.write_text(content, encoding="utf-8", newline="\n")
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
