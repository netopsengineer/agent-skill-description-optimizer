#!/usr/bin/env bash
# Keep original scanner output and exit status; only the scoped policy adjudicates.
set -euo pipefail

output_dir="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/optimizer-dependency-audit"
mkdir -p "$output_dir"
summary_args=()
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  summary_args=(--summary "$GITHUB_STEP_SUMMARY")
fi

if uv audit --no-config --locked --preview-features audit --output-format json \
  > "$output_dir/uv.json" 2> "$output_dir/uv.log"; then
  uv_exit=0
else
  uv_exit=$?
fi
printf '%s\n' "$uv_exit" > "$output_dir/uv.exit-code"
cat "$output_dir/uv.log" "$output_dir/uv.json"
if uv run python -m scripts.uv_advisory_policy \
  --report "$output_dir/uv.json" --exit-code "$uv_exit" "${summary_args[@]}"; then
  uv_gate=0
else
  uv_gate=1
fi

# The Dockerfile is the single digest-pinned version owner, updated by Dependabot.
# Build errors cannot be mistaken for a scan with accepted vulnerability findings.
docker build --pull --tag optimizer-osv-scanner .github/security-scanner \
  > "$output_dir/osv-build.log" 2>&1 || {
  cat "$output_dir/osv-build.log"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "OSV scanner could not start; dependency audit is blocked." >> "$GITHUB_STEP_SUMMARY"
  fi
  exit 1
}
if docker run --rm --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --user "$(id -u):$(id -g)" --env HOME=/tmp --env XDG_CACHE_HOME=/tmp/cache \
  --tmpfs /tmp:rw,noexec,nosuid,size=128m \
  --volume "$PWD:/src:ro" --workdir /src optimizer-osv-scanner \
  scan source --recursive --all-packages --format=json \
  --config=/src/.github/osv-scanner-empty.toml ./ \
  > "$output_dir/osv.json" 2> "$output_dir/osv.log"; then
  osv_exit=0
else
  osv_exit=$?
fi
printf '%s\n' "$osv_exit" > "$output_dir/osv.exit-code"
cat "$output_dir/osv.log" "$output_dir/osv.json"
if uv run python -m scripts.advisory_policy \
  --report "$output_dir/osv.json" --scan-exit "$osv_exit" \
  --output "$output_dir/osv-adjudicated.json" "${summary_args[@]}"; then
  osv_gate=0
else
  osv_gate=1
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  printf "Dependency policy exit status: uv=%s, OSV=%s (0=pass, 1=blocked). Raw evidence retained.\n" \
    "$uv_gate" "$osv_gate" >> "$GITHUB_STEP_SUMMARY"
fi

if [ "$uv_gate" -ne 0 ] || [ "$osv_gate" -ne 0 ]; then
  echo 'Dependency audit failed; inspect the original scanner reports and policy decision.' >&2
  exit 1
fi
