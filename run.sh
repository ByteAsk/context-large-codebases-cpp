#!/usr/bin/env bash
# run.sh ARM TASK [JOBS_DIR]: one trial of one tool on one task, through Harbor 0.25.0.
#   ARM   context | graphify | claude-context | sgrep | probe-keyword | probe-agent | oracle | nop
#   TASK  a task name from tasks.tsv, fetched with ./get_tasks.sh (its arms are listed there)
# context         ByteAsk Context through its MCP server. Needs BYTEASK_CONTEXT_TOKEN (see the README).
# claude-context  needs OPENAI_API_KEY, for its embeddings (passed to the task's embeddings proxy only).
# graphify, sgrep, probe-keyword, probe-agent: open-source tools in the task's image, no key needed.
# oracle writes the reference answer (reward 1); nop writes nothing (reward 0).
# CPUS and MEMORY_MB override the task's resources (the index tasks ask for the paper's 48 CPUs and 150 GiB).
set -euo pipefail
arm=$1 task=$2 jobs=${3:-jobs}
here=$(cd "$(dirname "$0")" && pwd)
dir="$here/tasks/$task"
[ -d "$dir" ] || { echo "no $dir: run ./get_tasks.sh $task first" >&2; exit 1; }
if [ ! -f "$dir/task.toml" ]; then   # a task with one folder per arm; oracle and nop use the first arm's folder
  sub=$arm; case "$arm" in oracle|nop) sub=$(ls "$dir" | grep -v '\.tar$' | head -1);; esac
  dir="$dir/$sub"; [ -f "$dir/task.toml" ] || { echo "task $task has no arm $arm (see tasks.tsv)" >&2; exit 2; }
fi
export HARBOR_TELEMETRY=0 PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"
case "$arm" in
  context) : "${BYTEASK_CONTEXT_TOKEN:?set BYTEASK_CONTEXT_TOKEN}"; agent=(-a agent:ByteAskContext);;
  claude-context)
    : "${OPENAI_API_KEY:?set OPENAI_API_KEY}"
    export UPSTREAM_API_KEY="$OPENAI_API_KEY"; unset OPENAI_API_KEY OPENAI_BASE_URL   # the proxy holds the key
    agent=(-a agent:ClaudeContext);;
  graphify) agent=(-a agent:Graphify);;
  sgrep) agent=(-a agent:Sgrep);;
  probe-keyword) agent=(-a agent:ProbeKeyword);;
  probe-agent) agent=(-a agent:ProbeAgent);;
  oracle|nop) agent=(-a "$arm");;
  *) echo "unknown arm $arm" >&2; exit 2;;
esac
res=(); [ -n "${CPUS:-}" ] && res+=(--override-cpus "$CPUS"); [ -n "${MEMORY_MB:-}" ] && res+=(--override-memory-mb "$MEMORY_MB")
exec harbor run -q -y -p "$dir" "${agent[@]}" "${res[@]}" -n 1 -o "$jobs" --job-name "$arm-$task-$(date +%Y%m%d-%H%M%S)"
