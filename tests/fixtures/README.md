# Stream-JSON test fixtures

Raw `claude -p ... --output-format stream-json --verbose --include-partial-messages`
output, used to test the trigger interpreter and the subprocess transport without
live CLI calls.

The injected command name in every fixture is **`pdfextract-cand-fix01`**.

| Fixture                     | Origin                            | Current-code verdict | Why                                                                                                                                                                                                                                              |
|-----------------------------|-----------------------------------|----------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `real_no_trigger.jsonl`     | live `claude -p` (haiku)          | **False**            | model answers directly; `message_stop` with no pending tool                                                                                                                                                                                      |
| `real_trigger.jsonl`        | live `claude -p` (haiku)          | **False**            | model runs **Bash before Skill**; parser bails on the first non-Skill/Read tool_use, never reaching the later Skill call                                                                                                                         |
| `skill_first_trigger.jsonl` | synthetic (modeled on real shape) | **True**             | `Skill` tool_use is the first tool; the command name arrives contiguously in one `input_json_delta` so a replay harness can rewrite it to the run's randomized name. (Split-across-chunk accumulation is covered by the interpreter unit tests.) |

Key behaviors these pin:

- `thinking` / `text` `content_block_start` events are ignored - only `tool_use`
  blocks drive the decision.
- The **first** tool_use wins: any non-Skill/Read tool (e.g. `Bash`) ⇒ `False`,
  even if the skill is invoked later (`real_trigger`).
- Tool input streams as `input_json_delta` chunks that may split the command name,
  so detection must accumulate partial JSON, not match per-chunk.

`real_trigger.jsonl`'s `False` is a faithful characterization of current behavior,
not necessarily the *desired* behavior - see the bug-fix task.
