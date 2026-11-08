"""Harbor agents for the arms of context-large-codebases-cpp: one per tool, each set up as its own public documentation
says. Three kinds of task (the task's /opt/task/task.json, "kind"):

  index      build the tool's index of a large codebase from a cold start, then answer one question from it; the arm
             writes the files it returned to /app/answer.txt and the time from the start of indexing to that first
             answer to /app/.ready.json
  fresh      index a repository, apply an uncommitted edit, bring the index up to date as the tool documents, and
             answer ten questions about the edited code (/app/answers/<id>.txt)
  retrieval  (no "kind") the context-engines-cpp tasks: find the files an issue's fix changes (/app/answer.txt)

  ByteAskContext  ByteAsk Context as https://byteask.ai/context describes it: a git `origin` remote and a
                  compile_commands.json; index through the ByteAsk MCP server (index_repository, then its collect
                  script: preview, upload, status); after an edit, the collect script's `sync`; then the task's tool
                  calls (find_code_for_task with the text exactly as written). Needs BYTEASK_CONTEXT_TOKEN (README).
  Graphify        Graphify 0.9.103: `graphify extract . --code-only`, `graphify cluster-only .`, then `graphify query`.
  ClaudeContext   claude-context 0.1.15 through its own MCP server: index_codebase (every C/C++ extension), wait for its
                  indexer, then search_code; embeddings from OpenAI (text-embedding-3-small) through the task's
                  embeddings proxy, vectors in the task's Milvus. Needs OPENAI_API_KEY (README).
  Sgrep, ProbeKeyword, ProbeAgent   the retrieval tasks' other arms, as in context-engines-cpp.

Use with: PYTHONPATH=. harbor run -p tasks/TASK[/ARM] -a agent:Graphify ... (see run.sh)."""
import json
import os
import re
import shlex
import time
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

LOG = "/logs/agent"
CAP = 600          # seconds per tool call, as in the paper
CONTEXT_MCP = os.environ.get("BYTEASK_CONTEXT_URL", "https://context.byteask.ai/mcp")


def strip_app(p):
    return re.sub(r"^(\./|/app/|app/)", "", p.strip())


def parse_sgrep(out):
    hits = json.loads(out)
    if not isinstance(hits, list):
        raise ValueError("not a list of hits")
    return [strip_app(h.get("path", "")) for h in hits]


def parse_probe(out):
    lines = out.splitlines()
    if not lines or not lines[0].startswith("Pattern:"):
        raise ValueError("not a Probe search result")
    m = re.search(r"<matches>(.*?)(?:</matches>|<skipped_files>|\Z)", out, re.S)
    if m:
        return [strip_app(f.group(1)) for f in re.finditer(r'<file path="([^"]+)">', m.group(1))]
    if "No results found." in out:
        return []
    raise ValueError("neither file blocks nor Probe's empty result")


def parse_graphify(out):
    if not re.match(r"Graph: .*\| (\d+) nodes found", out):
        raise ValueError("not a Graphify query result")
    return [strip_app(x.group(2)) for x in re.finditer(r"NODE\s+(.+?)\s+\[src=([^\s\]]+)(?:\s+loc=L(\d+))?", out)]


def parse_context(out):
    files = [m.group(3) for m in re.finditer(r"(?m)^\s*(\d+)\.\s+(\S.*?)\s+—\s+([^\s:]+):(\d+)", out)]
    if not files:
        raise ValueError("no ranked places in the answer")
    return [strip_app(f) for f in files]


class _Arm(BaseAgent):
    KEY = ""          # the arm's key in inputs.json

    def __init__(self, logs_dir, model_name=None, **kwargs):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)

    def version(self) -> str:
        return "1.0.0"

    async def _sh(self, env, cmd, timeout=CAP, envs=None):
        r = await env.exec(command=cmd, timeout_sec=timeout, env=envs)
        return r.return_code, (r.stdout or ""), (r.stderr or "")

    async def _tool(self, env, cmd, timeout=CAP, envs=None):
        """one tool call: its stdout and stderr kept apart (the container's exec merges them otherwise)"""
        rc, _, _ = await self._sh(env, f"({cmd}) > /tmp/ce-out 2> /tmp/ce-err", timeout=timeout, envs=envs)
        _, out, _ = await self._sh(env, "cat /tmp/ce-out", timeout=120)
        _, err, _ = await self._sh(env, "tail -c 4000 /tmp/ce-err", timeout=120)
        return rc, out, err

    async def _inputs(self, env):
        rc, out, _ = await self._sh(env, "cat /opt/task/inputs.json")
        return json.loads(out)[self.KEY]

    async def _answer(self, env, files, raw):
        seen = []
        for f in files:
            if f and f not in seen:
                seen.append(f)
        Path(self.logs_dir).mkdir(parents=True, exist_ok=True)
        (Path(self.logs_dir) / "calls.json").write_text(json.dumps(raw, indent=1))
        body = "\n".join(seen) + ("\n" if seen else "")
        await self._sh(env, f"mkdir -p {LOG} && printf %s {shlex.quote(body)} > /app/answer.txt")

    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    async def _task(self, env):
        _, tj, _ = await self._sh(env, "cat /opt/task/task.json")
        return json.loads(tj)

    async def _ready(self, env, tool, ready, seconds):
        rec = json.dumps({"tool": tool, "ready": bool(ready), "seconds": round(seconds, 1)})
        await self._sh(env, f"printf %s {shlex.quote(rec)} > /app/.ready.json")


def parse_paths(out):
    """file paths (path:line) in a tool's text answer, in order"""
    pat = r"((?:/app/)?[\w./+-]+\.(?:c|cc|cpp|cxx|c\+\+|h|hh|hpp|hxx|h\+\+|inl|ipp|tcc|def|inc|S)):\d+"
    return [strip_app(m.group(1)) for m in re.finditer(pat, out)]


class Sgrep(_Arm):
    KEY = "sgrep"

    @staticmethod
    def name() -> str:
        return "sgrep"

    async def run(self, instruction, environment, context):
        raw, files = [], []
        # sgrep keeps its index in its cache folder; the first search builds it (and downloads its embedding model)
        await self._sh(environment, "mkdir -p /tmp/sgrep-cache", timeout=60)
        env = {"SGREP_CACHE_DIR": "/tmp/sgrep-cache", "SGREP_CACHE_MAX_BYTES": "68719476736"}
        await self._sh(environment, "cd /app && sgrep 'parse command line options and print usage' -n 5 > /dev/null",
                       timeout=3600, envs=env)
        for a in await self._inputs(environment):
            n = f" -n {int(a['n'])}" if a.get("n") else ""
            rc, out, err = await self._tool(environment, f"cd /app && sgrep {shlex.quote(a['query'])}{n} --json", envs=env)
            raw.append({"args": a, "rc": rc, "out": out, "err": err[-2000:]})
            try:
                files += parse_sgrep(out) if rc == 0 else []
            except ValueError:
                pass
        await self._answer(environment, files, raw)


class _Probe(_Arm):
    async def run(self, instruction, environment, context):
        raw, files = [], []
        for a in await self._inputs(environment):
            cmd = ("cd /app && node /opt/ce/mcp_call.mjs --stdio 'probe mcp --timeout 300' search_code "
                   f"{shlex.quote(json.dumps(a))}")
            rc, out, err = await self._tool(environment, cmd)
            raw.append({"args": a, "rc": rc, "out": out, "err": err[-2000:]})
            try:
                files += parse_probe(out)
            except ValueError:
                pass
        await self._answer(environment, files, raw)


class ProbeKeyword(_Probe):
    KEY = "probe-keyword"

    @staticmethod
    def name() -> str:
        return "probe-keyword"


class ProbeAgent(_Probe):
    KEY = "probe-agent"

    @staticmethod
    def name() -> str:
        return "probe-agent"


class Graphify(_Arm):
    KEY = "graphify"

    @staticmethod
    def name() -> str:
        return "graphify"

    async def run(self, instruction, environment, context):
        task = await self._task(environment)
        if task.get("kind") == "index":
            return await self._index(environment, task)
        raw, files = [], []
        # the documented offline build, code only, in a copy of the tree (it writes graphify-out/ next to the code)
        rc, out, err = await self._sh(environment, "rm -rf /tmp/gview && cp -a /app /tmp/gview && cd /tmp/gview && "
                                      "graphify extract . --code-only && graphify cluster-only .", timeout=3600)
        raw.append({"build": rc, "err": err[-2000:]})
        genv = {"GRAPHIFY_OUT": "/tmp/gout", "GRAPHIFY_MAX_GRAPH_BYTES": "2GB"}
        for a in await self._inputs(environment):
            opt = (f" --depth {int(a['depth'])}" if a.get("depth") else "") + (f" --budget {int(a['budget'])}" if a.get("budget") else "")
            rc, out, err = await self._tool(environment, f"cd /tmp/gview && graphify query {shlex.quote(a['question'])} "
                                          f"--graph /tmp/gview/graphify-out/graph.json{opt}", envs=genv)
            raw.append({"args": a, "rc": rc, "out": out, "err": err[-2000:]})
            try:
                files += parse_graphify(out) if rc == 0 else []
            except ValueError:
                pass
        await self._answer(environment, files, raw)


    async def _index(self, env, task):
        """the repository's tracked files (git archive: the source tree without the build's outputs), Graphify's
        documented code-only build, then one query; the deadline is the paper's 3 hours"""
        raw = []
        await self._sh(env, "rm -rf /tmp/gview && mkdir /tmp/gview && git -C /app archive HEAD | tar -x -C /tmp/gview", timeout=3600)
        dl = int(task["deadline_s"])
        t0 = time.time()
        rc, out, err = await self._sh(env, "cd /tmp/gview && GRAPHIFY_MAX_WORKERS=$(nproc) graphify extract . --code-only "
                                      "&& graphify cluster-only .", timeout=dl)
        raw.append({"build": rc, "seconds": round(time.time() - t0, 1), "err": err[-2000:]})
        ready, files = False, []
        if rc == 0 and time.time() - t0 < dl:
            genv = {"GRAPHIFY_OUT": "/tmp/gout", "GRAPHIFY_MAX_GRAPH_BYTES": "2GB"}
            rc, out, err = await self._tool(env, f"cd /tmp/gview && graphify query {shlex.quote(task['probe'])} "
                                            "--graph /tmp/gview/graphify-out/graph.json", envs=genv)
            raw.append({"query": rc, "out": out[-20000:], "err": err[-2000:]})
            try:
                files = parse_graphify(out) if rc == 0 else []
                ready = rc == 0
            except ValueError:
                pass
        await self._ready(env, "graphify", ready, time.time() - t0)
        await self._answer(env, files, raw)


class ClaudeContext(_Arm):
    """claude-context 0.1.15 through its own MCP server (/opt/cc/cc_client.mjs): index_codebase on /app with every
    C/C++ extension, wait for its indexer (the paper's 3-hour limit), then search_code with the question. Embeddings go
    through the task's embeddings proxy (it holds OPENAI_API_KEY; see run.sh), vectors to the task's Milvus."""

    @staticmethod
    def name() -> str:
        return "claude-context"

    async def run(self, instruction, environment, context):
        task = await self._task(environment)
        raw = []
        # its vector database must answer first (the task's Milvus service)
        await self._sh(environment, "for i in $(seq 1 120); do curl -sf http://milvus:9091/healthz > /dev/null && break; "
                       "sleep 5; done", timeout=700)
        dl = int(os.environ.get("CC_DEADLINE_S", task["deadline_s"]))
        e = {"HOME": "/tmp/cch", "OPENAI_API_KEY": "dummy-arm-key", "OPENAI_BASE_URL": "http://embed-proxy:8080/v1",
             "EMBEDDING_PROVIDER": "OpenAI", "EMBEDDING_MODEL": "text-embedding-3-small", "EMBEDDING_BATCH_SIZE": "100",
             "MILVUS_ADDRESS": "milvus:19530", "CLAUDE_CONTEXT_BACKGROUND_SYNC": "false",
             "CLAUDE_CONTEXT_TRIGGER_WATCHER": "false"}
        await self._sh(environment, "mkdir -p /tmp/cch", timeout=60)
        rc, out, err = await self._tool(environment, f"cd /app && node /opt/cc/cc_client.mjs {dl} {shlex.quote(task['probe'])}",
                                        timeout=dl + 1200, envs=e)
        raw.append({"rc": rc, "out": out[-20000:], "err": err[-4000:]})
        last = {}
        for line in out.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if "ready" in d:
                last = d
        files = parse_paths(last.get("search", ""))
        await self._ready(environment, "claude-context", bool(last.get("ready")) and bool(files), float(last.get("seconds") or 0))
        await self._answer(environment, files, raw)


MCP_CLIENT = r'''
import json, os, sys, urllib.request
url, token = os.environ["CTX_URL"], os.environ["CTX_TOKEN"]
hdr = {"Authorization": "Bearer " + token, "content-type": "application/json", "accept": "application/json, text/event-stream"}
sid, i = None, 0
def rpc(method, params, notify=False):
    global sid, i
    i += 1
    msg = {"jsonrpc": "2.0", "method": method, "params": params, **({} if notify else {"id": i})}
    h = dict(hdr, **({"mcp-session-id": sid} if sid else {}))
    r = urllib.request.urlopen(urllib.request.Request(url, json.dumps(msg).encode(), h), timeout=900)
    txt = r.read().decode()
    sid = r.headers.get("mcp-session-id") or sid
    if (r.headers.get("content-type") or "").startswith("text/event-stream"):
        txt = "\n".join(l[5:] for l in txt.splitlines() if l.startswith("data:"))
    return json.loads(txt) if txt.strip() else {}
rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "context-engines-cpp", "version": "1"}})
rpc("notifications/initialized", {}, notify=True)
r = rpc("tools/call", {"name": sys.argv[1], "arguments": json.loads(sys.argv[2])})
res = r.get("result") or {}
print("\n".join(c.get("text", "") for c in res.get("content", [])))
sys.exit(1 if ("error" in r or res.get("isError")) else 0)
'''


class ByteAskContext(_Arm):
    """ByteAsk Context over its MCP server (https://context.byteask.ai/mcp), set up as https://byteask.ai/context says:
    a git origin remote and a compile_commands.json, index once (index_repository and its steps, the user having allowed
    indexing), then find_code_for_task with the task text exactly as written."""
    KEY = "context"

    @staticmethod
    def name() -> str:
        return "byteask-context"

    async def setup(self, environment: BaseEnvironment) -> None:
        if not os.environ.get("BYTEASK_CONTEXT_TOKEN"):
            raise RuntimeError("set BYTEASK_CONTEXT_TOKEN (see the README)")
        await environment.upload_file(source_path=self._client_file(), target_path="/opt/ce/ctx_mcp.py")

    def _client_file(self):
        p = Path(self.logs_dir) / "ctx_mcp.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(MCP_CLIENT)
        return p

    async def _mcp(self, env, tool, args, timeout=900):
        e = {"CTX_URL": CONTEXT_MCP, "CTX_TOKEN": os.environ["BYTEASK_CONTEXT_TOKEN"]}
        return await self._tool(env, f"python3 /opt/ce/ctx_mcp.py {tool} {shlex.quote(json.dumps(args))}", timeout=timeout, envs=e)

    async def _index_steps(self, env, task, raw, compdb, timeout):
        """index_repository's steps: fetch and check the collect script, preview (sends nothing), upload with the
        one-time key, then `status`, which waits until the index is ready or failed. True when ready."""
        rc, out, err = await self._mcp(env, "index_repository", {"repo_url": task["repo_url"], "workdir": "/app"})
        raw.append({"index_repository": rc, "out": out, "err": err[-2000:]})
        fetch = next((l.strip() for l in out.splitlines() if l.strip().startswith("mkdir -p ~/.cache/byteask-context")), None)
        key = re.search(r"BYTEASK_KEY=(\S+)", out)
        if not (fetch and key):
            return False
        k = {"BYTEASK_KEY": key.group(1)}
        script = "~/.cache/byteask-context/collect.py"
        rc1, o1, e1 = await self._sh(env, f"cd /app && {fetch.split(' && echo')[0]}", timeout=300)
        rc2, o2, e2 = await self._tool(env, f"cd /app && python3 {script} preview --compdb {shlex.quote(compdb)} --json",
                                       timeout=timeout)
        try:
            pid = json.loads(o2).get("preview_id")
        except ValueError:
            pid = None
        raw.append({"collect": [rc1, rc2], "preview": o2[-4000:], "err": (e1 + e2)[-2000:]})
        if not pid:
            return False
        rc3, o3, e3 = await self._sh(env, f"cd /app && python3 {script} upload --preview {shlex.quote(pid)}", timeout=timeout, envs=k)
        raw.append({"upload": rc3, "out": o3[-2000:], "err": e3[-2000:]})
        rc4, o4, _ = await self._sh(env, f"cd /app && python3 {script} status", timeout=timeout, envs=k)
        raw.append({"status": rc4, "out": o4[-2000:]})
        return rc3 == 0 and rc4 == 0

    async def _origin(self, env, url):
        await self._sh(env, f"cd /app && (git remote get-url origin >/dev/null 2>&1 || git remote add origin {shlex.quote(url)})")

    async def _run_index(self, env, task):
        raw = []
        await self._origin(env, task["repo_url"])
        t0 = time.time()
        dl = int(task["deadline_s"])
        ok = await self._index_steps(env, task, raw, task["compdb"], dl)
        files, ready = [], False
        if ok and time.time() - t0 < dl:
            rc, out, err = await self._mcp(env, "find_code_for_task", {"task": task["probe"], "workdir": "/app",
                                                                       "repo_url": task["repo_url"]})
            raw.append({"find_code_for_task": rc, "out": out[-20000:], "err": err[-2000:]})
            try:
                files = parse_context(out) if rc == 0 else []
                ready = rc == 0
            except ValueError:
                files = parse_paths(out) if rc == 0 else []
                ready = rc == 0 and bool(files)
        await self._ready(env, "byteask-context", ready, time.time() - t0)
        await self._answer(env, files, raw)

    async def _run_fresh(self, env, task):
        raw = []
        await self._origin(env, task["repo_url"])
        src = "/app" if task["cmake_source"] == "." else f"/app/{task['cmake_source']}"
        rc, _, _ = await self._sh(env, f"mkdir -p {LOG} && cmake -S {src} -B /app/build/byteask -DCMAKE_EXPORT_COMPILE_COMMANDS=ON "
                                  + " ".join(shlex.quote(a) for a in task["cmake_args"]) + f" > {LOG}/configure.log 2>&1", timeout=1800)
        raw.append({"configure": rc})
        ok = await self._index_steps(env, task, raw, "/app/build/byteask", 2700)
        # the uncommitted edit, then the collect script's `sync` (it pushes the changed files with the key the upload saved)
        rc, _, err = await self._sh(env, f"cd /app && git apply {shlex.quote(task['edit'])}", timeout=120)
        raw.append({"edit": rc, "err": err[-1000:]})
        t0 = time.time()
        rc, out, err = await self._sh(env, "cd /app && python3 ~/.cache/byteask-context/collect.py sync", timeout=1800)
        raw.append({"sync": rc, "seconds": round(time.time() - t0, 1), "out": out[-2000:], "err": err[-2000:]})
        await self._sh(env, "mkdir -p /app/answers", timeout=60)
        for q in task["questions"]:
            args = dict(q["arguments"], workdir="/app", repo_url=task["repo_url"])
            rc, out, err = await self._mcp(env, q["tool"], args)
            raw.append({"id": q["id"], "rc": rc, "out": out[-8000:], "err": err[-1000:]})
            await self._sh(env, f"printf %s {shlex.quote(out)} > /app/answers/{q['id']}.txt", timeout=60)
        Path(self.logs_dir).mkdir(parents=True, exist_ok=True)
        (Path(self.logs_dir) / "calls.json").write_text(json.dumps(dict(indexed=ok, calls=raw), indent=1))

    async def run(self, instruction, environment, context):
        task = await self._task(environment)
        if task.get("kind") == "index":
            return await self._run_index(environment, task)
        if task.get("kind") == "fresh":
            return await self._run_fresh(environment, task)
        raw = []
        # 1. prerequisites (byteask.ai/context): a git origin remote and a compile_commands.json from the project's own
        #    CMake configure (configure only, no build), in build/byteask
        src = "/app" if task["cmake_source"] == "." else f"/app/{task['cmake_source']}"
        prep = (f"cd /app && (git remote get-url origin >/dev/null 2>&1 || git remote add origin {shlex.quote(task['url'])}) && "
                f"cmake -S {src} -B /app/build/byteask -DCMAKE_EXPORT_COMPILE_COMMANDS=ON "
                + " ".join(shlex.quote(a) for a in task["cmake_args"]) + f" > {LOG}/configure.log 2>&1")
        rc, _, _ = await self._sh(environment, f"mkdir -p {LOG} && " + prep, timeout=1800)
        raw.append({"configure": rc})
        # 2. index (index_repository returns the steps: fetch and check the collect script, preview, upload with a key)
        rc, out, err = await self._mcp(environment, "index_repository", {"repo_url": task["url"], "workdir": "/app"})
        raw.append({"index_repository": rc, "out": out, "err": err[-2000:]})
        fetch = next((l.strip() for l in out.splitlines() if l.strip().startswith("mkdir -p ~/.cache/byteask-context")), None)
        key = re.search(r"BYTEASK_KEY=(\S+)", out)
        if fetch and key:
            k = {"BYTEASK_KEY": key.group(1)}
            script = "~/.cache/byteask-context/collect.py"
            rc1, o1, e1 = await self._sh(environment, f"cd /app && {fetch.split(' && echo')[0]}", timeout=300)
            rc2, o2, e2 = await self._tool(environment, f"cd /app && python3 {script} preview --compdb /app/build/byteask "
                                         "--json", timeout=1800)
            try:
                pid = json.loads(o2).get("preview_id")
            except ValueError:
                pid = None
            raw.append({"collect": [rc1, rc2], "preview": o2[-4000:], "err": (e1 + e2)[-2000:]})
            if pid:
                rc3, o3, e3 = await self._sh(environment, f"cd /app && python3 {script} upload --preview {shlex.quote(pid)}",
                                             timeout=3600, envs=k)
                raw.append({"upload": rc3, "out": o3[-2000:], "err": e3[-2000:]})
                # 3. wait until the index is ready (`status` waits for ready or failed), up to 45 minutes
                rc4, o4, _ = await self._sh(environment, f"cd /app && python3 {script} status", timeout=2700, envs=k)
                raw.append({"status": rc4, "out": o4[-2000:]})
        # 4. the task, exactly as written
        files = []
        for a in await self._inputs(environment):
            rc, out, err = await self._mcp(environment, "find_code_for_task", dict(a, workdir="/app", repo_url=task["url"]))
            raw.append({"args": {"task": a.get("task", "")[:200]}, "rc": rc, "out": out, "err": err[-2000:]})
            try:
                files += parse_context(out) if rc == 0 else []
            except ValueError:
                pass
        await self._answer(environment, files, raw)
