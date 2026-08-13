# CHANGELOG

<!--
This file is maintained automatically by python-semantic-release, driven by
Conventional-Commit history on `main`. See `.github/workflows/release.yml` and the
`[tool.semantic_release]` section of `pyproject.toml`.

Do not remove the version-list marker below. python-semantic-release runs in
`update` mode and inserts each new release directly beneath that marker. With the
marker absent it silently leaves this file unchanged, which is what happened for
every release through 0.2.6.
-->

<!-- version list -->

## v0.2.6 (2026-08-13)

### Bug Fixes

- **cli**: 🩹 Warn when early exit leaves an unclean holdout
  ([#30](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/30),
  [`4ef71fd`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/4ef71fda33ff9f8de8e72ad4e62cbba53bbc71bd))

### Chores

- Bump prek from 0.4.12 to 0.4.13 in the python-dev group
  ([#29](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/29),
  [`6d68f6d`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/6d68f6dda5e25d0baee69acaad7434d7a19aaa62))

- Bump the python-dev group across 1 directory with 2 updates
  ([#26](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/26),
  [`4ccaaa6`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/4ccaaa693a96933492d3c9425e20a8951119222a))

- Update uv-build requirement
  ([#27](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/27),
  [`94c8ee7`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/94c8ee74b4f9a5551b62ff5b038b01c5867a1f94))

### Continuous Integration

- Bump step-security/harden-runner in the github-actions group
  ([#28](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/28),
  [`8fe1c95`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/8fe1c95bb16897fca21f981fe7b948122f93a2e4))

## v0.2.5 (2026-08-10)

### Bug Fixes

- **ci**: 🐛 Isolate uv-build updates
  ([#25](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/25),
  [`ca566ec`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/ca566ec5834c3dc35775898d36a91b82ad3a8d99))

### Chores

- Bump <https://github.com/zizmorcore/zizmor-pre-commit>
  ([#23](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/23),
  [`2c55597`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/2c555975202f1602c524767114311de84940aeda))

### Continuous Integration

- Bump pypa/gh-action-pypi-publish
  ([#22](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/22),
  [`786dc78`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/786dc78fa19cc23ef8300227502e5c22a37e8e69))

## v0.2.4 (2026-08-10)

### Bug Fixes

- **ci**: 🔒️ Guard Dependabot auto-approval
  ([#24](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/24),
  [`fcb923b`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/fcb923b410399423fe64680f4922e252552c0dd0))

### Chores

- Bump prek in the python-dev group across 1 directory
  ([#18](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/18),
  [`5bf7cfa`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/5bf7cfabecbc47fc0815a00037389424fecea7a3))

### Continuous Integration

- Bump the github-actions group across 1 directory with 3 updates
  ([#13](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/13),
  [`9747b87`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/9747b87f31308a4eb364bfb835a98ca1888486cf))

### Documentation

- 📝 Update README formatting
  ([#20](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/20),
  [`13617ad`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/13617ade04aa2c28dfcc9403402c7c8698d426e3))

## v0.2.3 (2026-07-31)

### Bug Fixes

- **ci**: Make the markdown-table-formatter hook hermetic
  ([#19](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/19),
  [`517744d`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/517744dd7490052913d6375edaacd32fdf45e70a))

### Chores

- Record Modern Python standard revision 2026.08.1
  ([#17](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/17),
  [`89ecebf`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/89ecebfb00bc98af068ccfd936f6ef00dd7e11b2))

- **deps**: Adopt ruff 0.16 rule set and bump pre-commit hooks
  ([#17](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/17),
  [`89ecebf`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/89ecebfb00bc98af068ccfd936f6ef00dd7e11b2))

- **deps**: Adopt ruff 0.16 rule set and restore the dropped lint floor
  ([#17](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/17),
  [`89ecebf`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/89ecebfb00bc98af068ccfd936f6ef00dd7e11b2))

- **deps**: Bump sync-pre-commit-deps to v0.0.5 and markdownlint-cli2 to v0.23.2
  ([#17](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/17),
  [`89ecebf`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/89ecebfb00bc98af068ccfd936f6ef00dd7e11b2))

### Continuous Integration

- Align ruff-format hook file scope with the direct command
  ([#17](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/17),
  [`89ecebf`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/89ecebfb00bc98af068ccfd936f6ef00dd7e11b2))

- Make prek autoupdate SHA-preserving by default and fix a stale frozen tag
  ([#17](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/17),
  [`89ecebf`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/89ecebfb00bc98af068ccfd936f6ef00dd7e11b2))

### Documentation

- Correct the bandit-vs-ruff rationale in AGENTS.md
  ([#17](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/17),
  [`89ecebf`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/89ecebfb00bc98af068ccfd936f6ef00dd7e11b2))

### Testing

- Gate the PY-STYLE-001 rule-family floor
  ([#17](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/17),
  [`89ecebf`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/89ecebfb00bc98af068ccfd936f6ef00dd7e11b2))

## v0.2.2 (2026-07-20)

### Bug Fixes

- Pin UTF-8 for file I/O and harden lockfile/lint compliance gates
  ([#8](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/8),
  [`2e15152`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/2e151525f58085654f13c1e98e83bfe62b287521))

### Testing

- Run the ascii-locale encoding proof from a UTF-8 file, not python -c
  ([#8](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/8),
  [`2e15152`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/2e151525f58085654f13c1e98e83bfe62b287521))

## v0.2.1 (2026-07-16)

### Bug Fixes

- Correct PyPI invocation and report launch
  ([`ac95b88`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/ac95b88b1235a048ea565894a3bc4ae083ea0ab8))

### Continuous Integration

- Add manual release recovery path
  ([`7332e55`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/7332e5562d6860d83d792744aeb99d02a2e80806))

## v0.2.0 (2026-07-16)

### Bug Fixes

- Correct live release gate assertions
  ([`4e8751e`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/4e8751e9b9959d0a37cc6784e6b72676fbe597a6))

### Chores

- ✅ Achieve 100% test coverage and fix darwin bash issues in hook sh syntax
  ([`4e8751e`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/4e8751e9b9959d0a37cc6784e6b72676fbe597a6))

- ⬆️ Upgrade dependencies
  ([`4e8751e`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/4e8751e9b9959d0a37cc6784e6b72676fbe597a6))

### Continuous Integration

- Restore pr-title action pin to latest v6.1.1
  ([#2](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/2),
  [`82bd0b5`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/82bd0b59dfcb1dc6f75bde0bb9541b41b1463704))

- Restrict Dependabot tiered cooldown to the uv ecosystem
  ([#3](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/3),
  [`5e5f155`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/5e5f155b715e91d3a348119af46cbe2c9dcbfdd4))

- Temporarily pin pr-title action to v6.1.0 to exercise Dependabot auto-merge
  ([#1](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/1),
  [`fca7ecc`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/fca7eccfdd93c02c6dc0b3fca663f2bc868a0022))

- 🔒️ decouple Dependabot auto-fix compute from privileged push
  ([#4](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/4),
  [`44c4da8`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/44c4da8922c974c17ccccb6a74d81c43ffa474fd))

- 🔒️ rebuild auto-fix commit via Git Data API instead of checkout
  ([#4](https://github.com/netopsengineer/agent-skill-description-optimizer/pull/4),
  [`44c4da8`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/44c4da8922c974c17ccccb6a74d81c43ffa474fd))

### Documentation

- 📝 Update title and formatting
  ([`c91684e`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/c91684ed29cfa9e60546a34027304ba6aa067939))

### Features

- Harden optimizer for first PyPI release
  ([`4e8751e`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/4e8751e9b9959d0a37cc6784e6b72676fbe597a6))

- ✨ Add end-to-end test procedure and fixture
  ([`4e8751e`](https://github.com/netopsengineer/agent-skill-description-optimizer/commit/4e8751e9b9959d0a37cc6784e6b72676fbe597a6))

## v0.1.0 (2026-07-15)

- Initial Release
