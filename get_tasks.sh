#!/usr/bin/env bash
# get_tasks.sh [TASK...]: download tasks from the repository's tasks-v1 release into tasks/ (all of tasks.tsv by default)
# and check them against SHA256SUMS. An index or freshness task's archive holds the repository snapshot (app.tar, plus
# deps.tar for build dependencies) and one Harbor task folder per arm; the snapshot is linked into each arm's
# environment/. A retrieval task's archive is one Harbor task folder for all of its arms.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
base=${TASKS_URL:-https://github.com/ByteAsk/context-large-codebases-cpp/releases/download/tasks-v1}
mkdir -p "$here/tasks" "$here/.cache"
[ -f "$here/.cache/SHA256SUMS" ] || curl -fsSL "$base/SHA256SUMS" -o "$here/.cache/SHA256SUMS"
tasks=("$@"); [ ${#tasks[@]} -gt 0 ] || mapfile -t tasks < <(awk -F'\t' 'NR > 1 {print $1}' "$here/tasks.tsv")
for t in "${tasks[@]}"; do
  f="$here/.cache/$t.tar.gz"
  [ -f "$f" ] || curl -fL --retry 3 "$base/$t.tar.gz" -o "$f"
  (cd "$here/.cache" && grep " $t.tar.gz\$" SHA256SUMS | sha256sum -c --quiet -) || { echo "checksum mismatch: $t" >&2; exit 1; }
  rm -rf "$here/tasks/$t" && tar -xzf "$f" -C "$here/tasks"
  for d in "$here/tasks/$t"/*/; do
    [ -f "$d/environment/Dockerfile" ] || continue
    for a in app.tar deps.tar; do
      if [ -f "$here/tasks/$t/$a" ] && grep -q "source=$a" "$d/environment/Dockerfile"; then ln -f "$here/tasks/$t/$a" "$d/environment/$a"; fi
    done
  done
  echo "$t: ready"
done
