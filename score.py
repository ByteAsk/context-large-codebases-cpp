#!/usr/bin/env python3
"""score.py [JOBS_DIR]: score every trial in JOBS_DIR (default jobs/, as written by ./run.sh), print the paper's
metrics per arm on the tasks you ran, then ByteAsk Context against each other arm on the tasks both ran, and write
JOBS_DIR/scores.json. Python 3.8+, standard library only.

  index      time until the tool could answer (the arm's record, verifier/metrics.json "seconds"), and whether that
             answer named the right file among its first ten (reward 1)
  fresh      answers in line with the edited code, of the ten questions (verifier/metrics.json)
  retrieval  a changed file ranked first (reward 1, else 0); with several trials of a task, the mean over them"""
import collections, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS = sys.argv[1] if len(sys.argv) > 1 else "jobs"
ARMS = ["context", "graphify", "claude-context", "sgrep", "probe-keyword", "probe-agent", "oracle", "nop"]
JOB = re.compile(r"^(%s)-(.+)-\d{8}-\d{6}$" % "|".join(sorted(map(re.escape, ARMS), key=len, reverse=True)))
NAME = {"context": "ByteAsk Context", "graphify": "Graphify", "claude-context": "claude-context", "sgrep": "sgrep",
        "probe-keyword": "Probe, keywords", "probe-agent": "Probe, agent query", "oracle": "oracle", "nop": "nop"}


def load(p):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def kinds():
    out = {}
    with open(os.path.join(HERE, "tasks.tsv")) as f:
        for line in list(f)[1:]:
            c = line.rstrip("\n").split("\t")
            out[c[0]] = c[1]
    return out


def trial(t, kind):
    r = {"reward": None, "error": None}
    res = load(f"{t}/result.json") or {}
    if res.get("exception_info"):
        r["error"] = res["exception_info"].get("exception_type", "error")
    try:
        r["reward"] = float(open(f"{t}/verifier/reward.txt").read().strip())
    except (OSError, ValueError):
        pass
    m = load(f"{t}/verifier/metrics.json") or {}
    if kind == "index":
        r["ready"] = bool(m.get("ready"))
        r["seconds"] = m.get("seconds") if r["ready"] else None
    elif kind == "fresh":
        r["right"], r["questions"] = m.get("right", 0), m.get("questions", 10)
    return r


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def main():
    kind = kinds()
    rows = []
    for job in sorted(os.listdir(JOBS)) if os.path.isdir(JOBS) else []:
        m = JOB.match(job)
        if not m or m.group(2) not in kind:
            continue
        arm, task = m.groups()
        for t in sorted(os.listdir(f"{JOBS}/{job}")):
            if os.path.exists(f"{JOBS}/{job}/{t}/result.json") and os.path.isdir(f"{JOBS}/{job}/{t}/verifier"):
                rows.append({"job": job, "arm": arm, "task": task, "kind": kind[task], **trial(f"{JOBS}/{job}/{t}", kind[task])})
    with open(f"{JOBS}/scores.json" if os.path.isdir(JOBS) else os.devnull, "w") as f:
        json.dump(rows, f, indent=1)
    print(f"{len(rows)} trials in {JOBS}")
    if not rows:
        return
    # per (kind, task, arm): the trials
    cell = collections.defaultdict(list)
    for r in rows:
        cell[(r["kind"], r["task"], r["arm"])].append(r)
    arms = [a for a in ARMS if any(r["arm"] == a for r in rows)]

    idx = sorted({r["task"] for r in rows if r["kind"] == "index"})
    if idx:
        print("\nIndex: time until the tool could answer (s)")
        for task in idx:
            for a in arms:
                rs = cell.get(("index", task, a))
                if rs:
                    v = ", ".join((f"{r['seconds']:,.0f}" if r["seconds"] is not None else "ready, no time recorded")
                                  + ("" if r["reward"] == 1.0 else " (answer missed the file)") if r["ready"]
                                  else "not ready" for r in rs)
                    print(f"  {task:14s} {NAME[a]:20s} {v}")

    fr = [r for r in rows if r["kind"] == "fresh"]
    if fr:
        print("\nFreshness: answers in line with the edited code")
        for a in arms:
            rs = [r for r in fr if r["arm"] == a]
            if rs:
                print(f"  {NAME[a]:20s} " + ", ".join(f"{r['right']} of {r['questions']}" for r in rs))

    rt = sorted({r["task"] for r in rows if r["kind"] == "retrieval"})
    task_mean = lambda t, a: mean([r["reward"] or 0.0 for r in cell.get(("retrieval", t, a), [])])
    if rt:
        print("\nRetrieval: a changed file ranked first (mean over a task's trials)")
        for a in arms:
            ts = [t for t in rt if ("retrieval", t, a) in cell]
            if ts:
                hits = sum(task_mean(t, a) for t in ts)
                runs = sum(len(cell[("retrieval", t, a)]) for t in ts)
                print(f"  {NAME[a]:20s} {hits:5.2f} of {len(ts):2d} tasks ({100 * hits / len(ts):3.0f}%), {runs} trials")

    errs = collections.Counter(r["arm"] for r in rows if r["error"])
    if errs:
        print("\nTrials that ended in an error (scored as they stand): "
              + ", ".join(f"{NAME[a]} {n}" for a, n in errs.items()))

    head = False
    for a in arms:
        if a in ("context", "oracle", "nop"):
            continue
        out = []
        both = [t for t in rt if ("retrieval", t, "context") in cell and ("retrieval", t, a) in cell]
        if both:
            c, o = sum(task_mean(t, "context") for t in both), sum(task_mean(t, a) for t in both)
            out.append(f"retrieval on {len(both)} tasks: first file {100 * c / len(both):.0f}% against "
                       f"{100 * o / len(both):.0f}% ({100 * (c - o) / len(both):+.0f} points)")
        for t in idx:
            cs, os_ = cell.get(("index", t, "context")), cell.get(("index", t, a))
            if cs and os_:
                sec = lambda rs: min((r["seconds"] for r in rs if r["ready"] and r["seconds"] is not None), default=None)
                c, o = sec(cs), sec(os_)
                txt = lambda s: f"{s:,.0f} s" if s is not None else "not ready"
                out.append(f"{t}: {txt(c)} against {txt(o)}" + (f" (x{o / c:.1f} faster)" if c and o else ""))
        if out:
            if not head:
                print("\nByteAsk Context against each other arm, on the tasks both ran")
                head = True
            print(f"  vs {NAME[a]}: " + "; ".join(out))
    print(f"\nwrote {JOBS}/scores.json")


if __name__ == "__main__":
    main()
