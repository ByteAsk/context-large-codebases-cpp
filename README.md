# context-large-codebases-cpp

Code and tasks for **Build-Aware Context for Large C++ Codebases** (Pratyush Saini, CTO, ByteAsk Research,
pratyush@byteask.ai; Anirudha Kulkarni, CEO, ByteAsk Research, anirudha@byteask.ai; Aman Gupta, Carnegie Mellon
University, amangupt@alumni.cmu.edu; November 2026).

In large C and C++ codebases the meaning of a name depends on how the project is built: macros, templates, overloads and
conditional code decide which definition a call reaches. ByteAsk Context understands a codebase the way the project's
own build does and tells a coding agent where a change belongs. The paper measures what that costs and what it buys on
the largest C and C++ codebases:

- one machine indexes the Linux kernel's build in 9.3 minutes and LLVM's in 13.7, where Graphify did not finish LLVM
  in 3 hours;
- after an edit to one source file the index is up to date in seconds, and answers follow the edit;
- on issues from 16 projects, Context ranks a changed file first about twice as often as sgrep and three times as
  often as Probe.

You check these by running the tools yourself on the public tasks below and scoring what you ran.

Paper: [byteask.ai/research/context-large-codebases-cpp](https://byteask.ai/research/context-large-codebases-cpp). The
PDF is in this repository: `paper.pdf`.

## Tasks

`tasks.tsv` lists 15 public tasks in the [Harbor](https://github.com/laude-institute/harbor) task format, with the
arms each one runs:

- `index-linux` and `index-llvm`: build the tool's index of the Linux kernel v2.6.32 (x86_64 default configuration) or
  of LLVM (`llvm/` with the X86 and AArch64 targets) from a cold start, then answer one question from it. The metric is
  the time until the tool can answer. Arms: ByteAsk Context, Graphify, claude-context.
- `fresh-agisostack`: index AgIsoStack-plus-plus, make an uncommitted edit, bring the index up to date, and answer ten
  questions about the edited code. The metric is the share of answers that follow the edit. Arm: ByteAsk Context.
- 12 retrieval tasks: real issues from HDF5, SPIRV-Tools, PROJ, Arrow, Highway, QuantLib, OpenTelemetry C++ and
  OpenCV. Find the files the issue's fix changes; the first file is scored. Arms: ByteAsk Context, sgrep, Probe
  (keywords, and an agent-written query), Graphify. These are the 12 public tasks of
  [context-engines-cpp](https://github.com/ByteAsk/context-engines-cpp), unchanged.

Each task has:

- `instruction.md`: what to do and what to output;
- `environment/`: the repository at its base commit with no later commits (for the index tasks, the configured build
  tree with its compile database and the headers the build generates, object files removed), the tool, and the task's
  input (`/opt/task/`);
- `tests/`: the verifier and the answer it checks against. Index tasks: reward 1 when the answer names the right file
  among its first ten, with the time to ready in `metrics.json`; freshness: the share of right answers; retrieval:
  reward 1 when the first file is one the fix changed;
- `solution/`: the reference answer.

```bash
./get_tasks.sh                       # all 15 tasks from the tasks-v1 release
./get_tasks.sh index-linux           # or some of them
```

## Run it

You need Linux with Docker (compose v2 and buildx) and Harbor 0.25.0 (`uv tool install harbor==0.25.0`). The index
tasks ask for the paper's 48 CPUs and 150 GiB of memory; set `CPUS` and `MEMORY_MB` to run them on a smaller machine.
The other tasks need 4 CPUs and 16 GB.

| Arm | Tool | Set up as |
|---|---|---|
| `context` | ByteAsk Context, through its MCP server | [byteask.ai/context](https://byteask.ai/context): a git `origin` remote and a `compile_commands.json`; indexed with its collect script; the task text verbatim |
| `graphify` | Graphify 0.9.103 | `graphify extract . --code-only`, `graphify cluster-only .`, then `graphify query` |
| `claude-context` | claude-context 0.1.15 (Milvus 2.5.10, OpenAI `text-embedding-3-small`) | its MCP server: `index_codebase` with every C/C++ extension, then `search_code` |
| `sgrep` | sgrep 0.3.0 | `sgrep QUERY -n N --json` |
| `probe-keyword`, `probe-agent` | Probe 0.6.0-rc345 | `probe mcp`, tool `search_code` |

```bash
./run.sh graphify index-linux
OPENAI_API_KEY=... ./run.sh claude-context index-llvm     # the key goes to the task's embeddings proxy only
./run.sh sgrep lballabio__QuantLib-2792
```

**ByteAsk Context.** Set it up as [byteask.ai/context](https://byteask.ai/context) describes and sign in once. The arm
needs the access token of that sign-in in `BYTEASK_CONTEXT_TOKEN`: after you authenticate the `byteask` MCP server in
Claude Code (`/mcp`), it is in `~/.claude/.credentials.json`, under `mcpOAuth`. Indexing uploads the files the build
reads to ByteAsk, as the setup page explains.

```bash
export BYTEASK_CONTEXT_TOKEN=...
./run.sh context index-linux
./run.sh context fresh-agisostack
```

**Check a task.** `./run.sh oracle TASK` writes the reference answer (reward 1); `./run.sh nop TASK` writes nothing
(reward 0). Each trial's result, the tool's raw answers (`agent/calls.json`) and the verifier's output are in `jobs/`.

**Score what you ran.**

```bash
python3 score.py                     # every trial in jobs/; or python3 score.py OTHER_JOBS_DIR
```

`score.py` prints, per arm, the paper's metric for each kind of task you ran: the time until the tool could answer
(index), the answers in line with the edited code (freshness), and how often a changed file came first (retrieval, the
mean over a task's trials). Then it sets ByteAsk Context against each other arm on the tasks both ran. It needs Python
3.8 or newer and nothing else, and writes every trial's score to `jobs/scores.json`.

## The sample

Start with the 12 retrieval tasks: each needs 4 CPUs and 16 GB, and every arm but ByteAsk Context runs without a key.
Run each task in each of its arms (the paper ran Context three times per task):

```bash
./get_tasks.sh $(awk -F'\t' '$2 == "retrieval" {print $1}' tasks.tsv)
for t in $(awk -F'\t' '$2 == "retrieval" {print $1}' tasks.tsv); do
  for a in context sgrep probe-keyword probe-agent graphify; do ./run.sh "$a" "$t"; done
done
./run.sh context fresh-agisostack
python3 score.py
```

The index tasks need a large machine. The paper's runs had 48 virtual CPUs and 150 GiB of memory, with a 3-hour limit
per tool; on a smaller machine set the resources, and expect longer times:

```bash
for a in context graphify claude-context; do CPUS=16 MEMORY_MB=65536 ./run.sh "$a" index-linux; done
python3 score.py
```

## What to expect

The paper's numbers, from its own runs:

| Result | ByteAsk Context | Other tools |
|---|---|---|
| Linux kernel build (x86_64), time until it can answer | 557 s (9.3 minutes) | Graphify 705 s; claude-context 10,469 s, a partial index |
| LLVM build (X86 and AArch64), time until it can answer | 821 s (13.7 minutes) | Graphify unfinished in 3 h; claude-context 10,599 s, a partial index |
| Re-index after an edit to one source file | 5.8 s, against 105–112 s for a full index | |
| Answers in line with the code after an uncommitted edit | 9 of 10, against 0 of 10 from a stale index | |
| Issues where every tool indexed the changed files (30 issues, 7 projects): a changed file ranked first | 57% | sgrep 30%, Probe 20% (agent query), 13% (keywords) |
| Issues in the two projects of 5M lines and more (10 issues): a changed file ranked first | 60% | sgrep 20% |

claude-context stopped
at its own limit of 450,000 chunks on both codebases.

The paper's retrieval numbers come from 78 issues in 16 projects; this repository's 12 retrieval tasks are other
issues, so a small sample gives different rates: with 12 tasks, one task moves a rate by 8 points. Index times depend
on the machine: on fewer CPUs every tool takes longer. Context runs on ByteAsk's service, so its answers can change as
the service changes.

## Layout

```
tasks.tsv               the 15 Harbor tasks: name, kind, arms, repository, base commit
get_tasks.sh            download tasks from the tasks-v1 release into tasks/ and check them
run.sh                  one trial through Harbor: ./run.sh ARM TASK [JOBS_DIR]
agent.py                the Harbor agents of the arms
score.py                score every trial in jobs/: per-arm metrics, and Context against each other arm
paper.pdf               the paper
```

## Citation

```bibtex
@misc{saini2026largecodebases,
  title        = {Build-Aware Context for Large C++ Codebases},
  author       = {Saini, Pratyush and Kulkarni, Anirudha and Gupta, Aman},
  year         = {2026},
  month        = nov,
  institution  = {ByteAsk Research},
  howpublished = {\url{https://byteask.ai/research/context-large-codebases-cpp}},
  note         = {Preprint. Code and tasks: \url{https://github.com/ByteAsk/context-large-codebases-cpp}}
}
```

## License

The scripts are MIT licensed (`LICENSE`). Each repository snapshot in the tasks keeps its project's own license, and
each issue text belongs to its project.
