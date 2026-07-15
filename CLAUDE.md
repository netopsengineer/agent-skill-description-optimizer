# CLAUDE.md

@AGENTS.md

## Operating discipline (overrides brevity / token-thrift pressure)

Enforced by hooks in `.claude/settings.json` - `Stop` runs `.claude/hooks/enforce-done.sh`
(blocks completion while the repo gate is red) and `UserPromptSubmit` runs
`.claude/hooks/inject-discipline.sh` (re-asserts these rules each turn).
