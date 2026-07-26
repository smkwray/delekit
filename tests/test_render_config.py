from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / 'generated' / 'claude' / 'agents'
SKILL = ROOT / 'generated' / 'claude' / 'skills' / 'orchestrate-delegates' / 'SKILL.md'
ROLES = ('TERRA', 'LUNA', 'SOL')


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in (ROOT / 'config' / 'models.env').read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            env[key.strip()] = value.strip()
    return env


def agent_names(env: dict[str, str]) -> dict[str, str]:
    """Every generated agent name mapped to the alias it must declare."""
    prefix = env['AGENT_PREFIX']
    expected: dict[str, str] = {}
    for role in ROLES:
        profile = role.lower()
        alias = env[f'DELEGATE_ALIAS_{role}']
        for suffix in ('', '-worktree', '-readonly'):
            expected[f'{prefix}-{profile}{suffix}'] = alias
    return expected


class RenderConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = read_env()

    def test_generated_files_are_current(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / 'tools' / 'render_config.py'), '--check'],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_profile_capability_pair_is_generated(self) -> None:
        # A missing pair means the orchestrator can name an agent that does not
        # exist, and the spawn fails at run time rather than at render time.
        expected = set(agent_names(self.env))
        found = {path.stem for path in AGENTS.glob('*.md')}
        self.assertEqual(expected, found)

    def test_each_agent_declares_its_own_profile_alias(self) -> None:
        # The alias is the only thing that routes a subagent to its intended
        # model, so a copied or defaulted alias silently downgrades a profile.
        for name, alias in agent_names(self.env).items():
            text = (AGENTS / f'{name}.md').read_text(encoding='utf-8')
            self.assertIn(f'model: {alias}\n', text, name)

    def test_agents_never_pin_a_provider_model_id(self) -> None:
        # Provider IDs belong only in config/models.env; a pinned ID in an agent
        # file survives a model change there and routes to a stale model.
        for path in AGENTS.glob('*.md'):
            self.assertNotRegex(path.read_text(encoding='utf-8'), r'\bgpt-[A-Za-z0-9._-]+', str(path))

    def test_tandy_aliases_use_the_native_compaction_family(self) -> None:
        # Claude Code recognizes this family and preflights compaction instead
        # of waiting for an unknown gateway model to fail upstream.
        aliases = [self.env[f'DELEGATE_ALIAS_{role}'] for role in ROLES]
        self.assertEqual(len(set(aliases)), len(ROLES))
        for alias in aliases:
            self.assertTrue(alias.startswith('claude-sonnet-4-6-tandy-'), alias)

    def test_oauth_aliases_preserve_client_model_identity(self) -> None:
        # Keep the compatibility alias stable in responses as well as requests;
        # otherwise transcripts and diagnostics switch to the provider ID.
        for relative in (
            'generated/cliproxy/config.template.yaml',
            'generated/cliproxy/oauth-model-alias.yaml',
        ):
            text = (ROOT / relative).read_text(encoding='utf-8')
            self.assertEqual(text.count('force-mapping: true'), len(ROLES), relative)

    def test_agent_bodies_are_lean(self) -> None:
        # These bodies are re-sent on every delegate invocation.
        for path in AGENTS.glob('*.md'):
            body = path.read_text(encoding='utf-8').split('---', 2)[2]
            words = re.findall(r"\b[\w'-]+\b", body)
            self.assertLessEqual(len(words), 80, f'{path}: {len(words)} words')

    def test_agent_capability_boundaries(self) -> None:
        prefix = self.env['AGENT_PREFIX']
        for role in ROLES:
            profile = role.lower()

            for name in (f'{prefix}-{profile}', f'{prefix}-{profile}-worktree'):
                text = (AGENTS / f'{name}.md').read_text(encoding='utf-8')
                # Writers must not recurse into further delegates or reach MCP
                # servers whose credentials belong to the parent session.
                self.assertIn('permissionMode: acceptEdits', text, name)
                self.assertIn('- Agent', text, name)
                self.assertIn('"mcp__*"', text, name)

            worktree = (AGENTS / f'{prefix}-{profile}-worktree.md').read_text(encoding='utf-8')
            # Without this, parallel writers share one checkout and clobber it.
            self.assertIn('isolation: worktree', worktree)

            readonly = (AGENTS / f'{prefix}-{profile}-readonly.md').read_text(encoding='utf-8')
            self.assertIn('permissionMode: plan', readonly)
            self.assertIn('  - Bash', readonly)
            self.assertIn('  - SendMessage', readonly)
            self.assertNotIn('  - Edit', readonly)
            self.assertNotIn('  - Write', readonly)

    def test_skill_names_agents_the_orchestrator_can_actually_spawn(self) -> None:
        # The Skill is the orchestrator's only guide to what exists; a stale
        # prefix or profile name there produces unspawnable agent names.
        skill = SKILL.read_text(encoding='utf-8')
        prefix = self.env['AGENT_PREFIX']
        for role in ROLES:
            self.assertIn(role.lower(), skill)
        self.assertIn(f'{prefix}-<profile>', skill)
        # Model aliases legitimately start with `claude-`; agent names must not.
        aliases = {self.env[f'DELEGATE_ALIAS_{r}'] for r in ROLES}
        for token in re.findall(r'`([a-z0-9]+-[a-z0-9<>-]+)`', skill):
            if token in aliases:
                continue
            self.assertEqual(token.split('-')[0], prefix,
                             f'skill names a non-{prefix} agent: {token}')
        self.assertNotRegex(skill, r'\bgpt-[A-Za-z0-9._-]+')

    def test_skill_requires_background_watchers_for_every_herd_turn(self) -> None:
        # A detached herd process cannot notify its calling Claude session.
        # The orchestrator must keep a background tool task waiting on the
        # existing result primitive so lane completion wakes it automatically.
        skill = SKILL.read_text(encoding='utf-8')
        self.assertIn('Always watch herd turns', skill)
        self.assertIn('every successful `herd spawn` or', skill)
        self.assertIn('`herd send`', skill)
        self.assertIn('herd result <task> --wait --timeout <seconds>', skill)
        self.assertIn('`run_in_background` mode, not shell `&`', skill)
        self.assertIn('Never leave a working herd turn without one live watcher', skill)

    def test_launchers_preserve_dynamic_model_selection(self) -> None:
        for name in ('claudex.sh', 'claudex.ps1'):
            text = (ROOT / 'bin' / name).read_text(encoding='utf-8')
            self.assertIn('CLAUDE_CODE_SUBAGENT_MODEL', text)
            self.assertIn('CLAUDE_CODE_MAX_CONTEXT_TOKENS', text)
            self.assertIn('CLAUDE_CODE_AUTO_COMPACT_WINDOW', text)
            self.assertIn('DISABLE_COMPACT', text)
            self.assertIn('DELEKIT_TANDY_CONTEXT_MODE', text)
            self.assertIn('seed_claude_context_cache.py', text)
            self.assertIn('CLAUDE_CODE_ALWAYS_ENABLE_EFFORT', text)
            self.assertIn('CLAUDE_CODE_ATTRIBUTION_HEADER', text)
            self.assertIn('ENABLE_TOOL_SEARCH', text)

    def test_launchers_pin_the_parent_model(self) -> None:
        # Claude Code writes a /model choice to the GLOBAL settings file, so a
        # gateway alias picked in a gateway session breaks every direct launch
        # until the pin overrides it. The pin must also yield to an explicit
        # --model, or `claudex --model fable` silently ignores the request.
        for name in ('claudex.sh', 'claudex.ps1'):
            text = (ROOT / 'bin' / name).read_text(encoding='utf-8')
            self.assertIn('DELEGATE_PARENT_MODEL', text, name)
            self.assertIn('--model=*', text, f'{name} must detect a user-supplied --model')
        example = (ROOT / 'config' / 'device.env.example').read_text(encoding='utf-8')
        self.assertIn('DELEGATE_PARENT_MODEL=', example)
        self.assertNotIn('CLAUDE_CODE_MAX_CONTEXT_TOKENS=', example)
        self.assertRegex(example, r'(?m)^DELEKIT_TANDY_CONTEXT_MODE=clientdata-272k$')
        self.assertIn('DELEKIT_TANDY_CONTEXT_MODE:-clientdata-272k', (ROOT / 'bin' / 'claudex.sh').read_text(encoding='utf-8'))
        self.assertIn("else { 'clientdata-272k' }", (ROOT / 'bin' / 'claudex.ps1').read_text(encoding='utf-8'))
        for model in ('claude-opus-4-8[1m]', 'claude-fable-5[1m]', 'claude-sonnet-5[1m]'):
            self.assertIn(model, example)

    def test_known_issues_covers_both_platforms(self) -> None:
        # This file is the only thing a new device inherits from past debugging.
        text = (ROOT / 'docs' / 'known-issues.md').read_text(encoding='utf-8')
        for tag in ('[all]', '[posix]'):
            self.assertIn(tag, text)
        for topic in ('DELEGATE_PARENT_MODEL', 'subshell', 'placeholder'):
            self.assertIn(topic, text)

    def test_ccg_snippets_do_not_leak_gateway_env(self) -> None:
        # ccg must not persist ANTHROPIC_BASE_URL into the calling shell, or a
        # later plain `claude` silently keeps routing through the proxy.
        posix = (ROOT / 'bin' / 'ccg-snippet.sh').read_text(encoding='utf-8')
        self.assertRegex(posix, r'ccg\(\)\s*\(', 'ccg must be a subshell function: ccg() ( ... )')
        launcher = (ROOT / 'bin' / 'ccg-launch.ps1').read_text(encoding='utf-8')
        self.assertIn('finally', launcher)
        self.assertIn("Join-Path $PSScriptRoot 'claudex.ps1'", launcher)
        powershell = (ROOT / 'bin' / 'ccg-snippet.ps1').read_text(encoding='utf-8')
        self.assertIn('DelekitCcgLauncher', powershell)


if __name__ == '__main__':
    unittest.main()
