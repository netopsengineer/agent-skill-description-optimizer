---
name: e2e-fixture-skill
description: A helper skill for text processing tasks.
---

# E2E Fixture Skill

<!--
  Regression fixture for AGENTS.md's "Integration test" section. The generic `name:`
  and deliberately vague `description:` above are load-bearing: they keep the
  triggering baseline well below 1.0 (verified: single-model, single-repeat baseline
  mean_accuracy ~0.5-0.65, 0% recall on the should_trigger=true queries), which is
  what forces Gate 3's optimize loop to actually call the improver instead of exiting
  at iteration 0 on a saturated score. Do not "fix" this description or rename this
  skill to something more descriptive -- that would silently defeat the fixture's
  purpose and the next agent to run Gate 3 would get a false pass (no improver
  artifacts) without any error.
-->

Break down a regular expression into its component parts and explain what each part
matches. Use this when a user has a regex that isn't behaving as expected, or wants to
understand/document an existing pattern.
