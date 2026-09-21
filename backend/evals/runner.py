"""
Tool-selection eval: does the model reach for the right tool?

Run it from backend/ with a local Ollama up:

    .\\.venv\\Scripts\\python.exe -m evals.runner
    .\\.venv\\Scripts\\python.exe -m evals.runner --model llama3.1:8b --repeat 3

It drives the real `agent.run()` -- the same system prompt, the same one-round
tool loop, the same delete gate -- against the real tool schemas, with only two
things replaced:

  * `mcp_client.ensure_tools` returns the schemas translated straight from
    `mcp_server.TOOL_DEFINITIONS`, so no MCP server process is needed and the
    model sees byte-for-byte what it sees in production.
  * `mcp_client.call_tool` returns a canned result and records the invocation,
    so no MongoDB is needed and nothing is ever actually deleted.

Ollama itself is *not* mocked. That is the point: this measures the model.

## What it records, and why two signals rather than one

Each case captures both what the model *asked for* and what actually *reached*
the tool layer. They differ on exactly the cases worth caring about: an
unconfirmed `delete_conversation` is a correct read of the user's message that
the gate in `agent._execute_tool` must stop before it goes anywhere. Recording
only invocations would score that as the model failing to pick a tool; recording
only the model's choice would make the gate invisible. So:

  * **selection accuracy** -- did `model_chose` match the label
  * **gate effectiveness** -- did every unconfirmed delete record zero invocations

## On the numbers

Tool selection is sampled from a distribution, so a single pass over ~40 prompts
has real variance. `--repeat` runs the whole set N times and reports the mean
with a per-pass spread; quote a repeated run, not a single one.

Latency comes from the same `time.perf_counter()` samples `observability` feeds
its histograms, but kept raw here rather than read back out of the buckets --
percentiles off ~40 exact samples beat percentiles off bucket boundaries. First
(tool-bearing) and second (reply-composing) calls are reported separately,
because the first carries six schemas in its prompt and the second carries none.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv

load_dotenv(BACKEND_DIR / ".env")

import observability  # noqa: E402  (must follow the sys.path/env setup above)

# Configured before mcp_server is imported, because that module configures
# logging at import time and configure_logging is first-call-wins -- without
# this, every line this process emits would be labelled service="mcp". WARNING
# by default because the runner prints its own per-case line and agent.py's INFO
# line duplicates it; LOG_LEVEL=INFO in the environment still overrides.
os.environ.setdefault("LOG_LEVEL", "WARNING")
observability.configure_logging("eval")

import agent  # noqa: E402
import mcp_client  # noqa: E402
import mcp_server  # noqa: E402

from evals.dataset import CANNED_RESULTS, CASES, EvalCase  # noqa: E402

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


class Recorder:
    """Collects one run's observations.

    `invocations` is the list of tool names that got past the delete gate and
    the membership check -- i.e. what a real MCP server would have been asked to
    run. `model_choice` is what the model put in `tool_calls` on the first call,
    before any of our own logic saw it.
    """

    def __init__(self) -> None:
        self.invocations: list[str] = []
        self.model_choice: str | None = None
        self.first_call_seconds: float | None = None
        self.second_call_seconds: float | None = None


def _ollama_schemas() -> list[dict[str, Any]]:
    """The production tool schemas, without running the MCP server.

    Goes through `mcp_client._to_ollama_schema` rather than hand-building the
    Ollama shape, so if that translation ever changes the eval changes with it.
    A tiny shim object stands in for the MCP SDK's Tool type, which only matters
    for its three attributes.
    """

    class _Tool:
        def __init__(self, definition: dict[str, Any]) -> None:
            self.name = definition["name"]
            self.description = definition["description"]
            self.inputSchema = definition["inputSchema"]  # noqa: N815 (MCP's own casing)

    schemas = []
    for definition in mcp_server.TOOL_DEFINITIONS:
        schema = mcp_client._to_ollama_schema(_Tool(definition))
        if schema is None:
            raise RuntimeError(f"Could not translate tool {definition['name']!r}")
        schemas.append(schema)
    return schemas


# Stands in for the second Ollama call when reply composition is skipped; this
# shape is all agent.run() reads off it.
_SKIPPED_REPLY = {"message": {"content": "(reply composition skipped by the eval)"}}


async def _run_case(
    case: EvalCase, schemas: list[dict[str, Any]], compose_reply: bool = False
) -> dict[str, Any]:
    """Drive one prompt through the real agent, recording both signals."""
    recorder = Recorder()

    original_call_ollama = agent._call_ollama

    async def recording_call_ollama(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        # The tool decision is made entirely by the first call; the second only
        # turns a tool result into prose, which nothing here scores. Skipping it
        # halves the wall clock without touching a measured behaviour -- the
        # delete gate and the invocation record both run before it.
        if not tools and not compose_reply:
            return dict(_SKIPPED_REPLY)

        started = time.perf_counter()
        response = await original_call_ollama(messages, tools)
        elapsed = time.perf_counter() - started

        if tools:
            recorder.first_call_seconds = elapsed
            # Read the model's raw choice here, before the delete gate or the
            # membership check in _execute_tool has had a chance to drop it.
            calls = response.get("message", {}).get("tool_calls") or []
            if calls:
                recorder.model_choice = calls[0].get("function", {}).get("name")
        else:
            recorder.second_call_seconds = elapsed
        return response

    async def recording_call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        recorder.invocations.append(name)
        return dict(CANNED_RESULTS.get(name, {"ok": True}))

    async def fixed_ensure_tools() -> list[dict[str, Any]]:
        return list(schemas)

    # Saved and restored around the case rather than left in place: the
    # live_llm test imports these same modules, and a leaked stub would make a
    # later test pass for the wrong reason.
    originals = (mcp_client.call_tool, mcp_client.ensure_tools, mcp_client.cached_tool_names)

    agent._call_ollama = recording_call_ollama
    mcp_client.call_tool = recording_call_tool
    mcp_client.ensure_tools = fixed_ensure_tools
    # _execute_tool checks membership against the cache, so it has to agree with
    # the schemas above or every call would be refused as an unknown tool.
    mcp_client.cached_tool_names = lambda: {s["function"]["name"] for s in schemas}

    started = time.perf_counter()
    error: str | None = None
    reply = ""
    try:
        reply = await agent.run(case.prompt)
    except Exception as exc:  # noqa: BLE001 -- one bad case must not end the run
        error = f"{type(exc).__name__}: {exc}"
    total_seconds = time.perf_counter() - started

    agent._call_ollama = original_call_ollama
    mcp_client.call_tool, mcp_client.ensure_tools, mcp_client.cached_tool_names = originals

    selection_correct = recorder.model_choice == case.expected_tool

    # Only meaningful on unconfirmed deletes; None elsewhere so it cannot be
    # averaged into a number it has nothing to do with.
    #
    # Two distinct things get recorded, because "the gate held" is trivially
    # true when the model never tried. `gate_fired` says the gate actually did
    # work: the model asked for the delete and our code stopped it before it
    # left the process. A run where gate_fired is never true is a run where the
    # model's own judgement was sufficient -- good news, and not evidence that
    # the gate itself works.
    gate_held: bool | None = None
    gate_fired: bool | None = None
    if case.delete_confirmed is False:
        gate_held = recorder.invocations == []
        gate_fired = recorder.model_choice == "delete_conversation" and gate_held

    return {
        "prompt": case.prompt,
        "group": case.group,
        "expected_tool": case.expected_tool,
        "model_choice": recorder.model_choice,
        "invocations": recorder.invocations,
        "selection_correct": selection_correct,
        "gate_held": gate_held,
        "gate_fired": gate_fired,
        "delete_confirmed": case.delete_confirmed,
        "note": case.note,
        "first_call_seconds": recorder.first_call_seconds,
        "second_call_seconds": recorder.second_call_seconds,
        "total_seconds": round(total_seconds, 3),
        "reply_chars": len(reply),
        "error": error,
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile. Exact on the sample, which is what we want on
    ~40 observations -- interpolation would invent precision we do not have."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * len(ordered)) - 1))
    return round(ordered[index], 3)


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in results if r["error"] is None]
    correct = [r for r in scored if r["selection_correct"]]

    by_group: dict[str, dict[str, int]] = {}
    for r in scored:
        bucket = by_group.setdefault(r["group"], {"correct": 0, "total": 0})
        bucket["total"] += 1
        bucket["correct"] += int(r["selection_correct"])

    # Only the mistakes, so the report says *what* it confused for what rather
    # than just how often. This is the part that tells you whether to fix a tool
    # description or accept the model's limits.
    confusions: dict[str, int] = {}
    for r in scored:
        if not r["selection_correct"]:
            key = f"{r['expected_tool']} -> {r['model_choice']}"
            confusions[key] = confusions.get(key, 0) + 1

    gate_cases = [r for r in scored if r["gate_held"] is not None]
    gate_fired = [r for r in gate_cases if r["gate_fired"]]
    firsts = [r["first_call_seconds"] for r in scored if r["first_call_seconds"] is not None]
    seconds = [r["second_call_seconds"] for r in scored if r["second_call_seconds"] is not None]

    return {
        "cases_scored": len(scored),
        "cases_errored": len(results) - len(scored),
        "selection_accuracy": round(len(correct) / len(scored), 4) if scored else None,
        "by_group": {
            g: {**v, "accuracy": round(v["correct"] / v["total"], 4)} for g, v in by_group.items()
        },
        "confusions": dict(sorted(confusions.items(), key=lambda kv: -kv[1])),
        "delete_gate": {
            "unconfirmed_cases": len(gate_cases),
            "no_invocation_reached_mcp": sum(1 for r in gate_cases if r["gate_held"]),
            "held": all(r["gate_held"] for r in gate_cases) if gate_cases else None,
            # How often the model asked for the delete anyway and the gate was
            # the thing that stopped it.
            "gate_had_to_fire": len(gate_fired),
            "model_declined_unaided": len(gate_cases) - len(gate_fired),
        },
        "latency_seconds": {
            "first_call_with_tools": {
                "n": len(firsts),
                "p50": _percentile(firsts, 0.50),
                "p95": _percentile(firsts, 0.95),
                "max": round(max(firsts), 3) if firsts else None,
            },
            "second_call_no_tools": {
                "n": len(seconds),
                "p50": _percentile(seconds, 0.50),
                "p95": _percentile(seconds, 0.95),
                "max": round(max(seconds), 3) if seconds else None,
            },
        },
    }


async def run_eval(
    cases: list[EvalCase], repeat: int = 1, compose_reply: bool = False
) -> dict[str, Any]:
    """Run every case `repeat` times and return the full report."""
    schemas = _ollama_schemas()
    started = time.perf_counter()

    passes: list[list[dict[str, Any]]] = []
    for pass_index in range(repeat):
        results: list[dict[str, Any]] = []
        for case_index, case in enumerate(cases, 1):
            result = await _run_case(case, schemas, compose_reply=compose_reply)
            result["pass"] = pass_index + 1
            results.append(result)
            mark = "ok " if result["selection_correct"] else "MISS"
            if result["error"]:
                mark = "ERR "
            print(
                f"  [{pass_index + 1}/{repeat}] {case_index:>2}/{len(cases)} {mark} "
                f"{case.expected_tool or '(no tool)':<22} "
                f"got {str(result['model_choice'] or '(no tool)'):<22} "
                f"{result['total_seconds']:>6.2f}s  {case.prompt[:44]}",
                flush=True,
            )
        passes.append(results)

    flat = [r for p in passes for r in p]
    summary = _summarize(flat)
    per_pass = [_summarize(p)["selection_accuracy"] for p in passes]

    return {
        "model": agent.OLLAMA_MODEL,
        "ollama_base_url": agent.OLLAMA_BASE_URL,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passes": repeat,
        "composed_replies": compose_reply,
        "wall_clock_seconds": round(time.perf_counter() - started, 1),
        "summary": summary,
        "per_pass_accuracy": per_pass,
        "accuracy_spread": (
            {"min": min(per_pass), "max": max(per_pass)} if repeat > 1 and per_pass else None
        ),
        "accuracy_stdev": (
            round(statistics.stdev([a for a in per_pass if a is not None]), 4)
            if repeat > 1 and len([a for a in per_pass if a is not None]) > 1
            else None
        ),
        "results": flat,
    }


def _print_summary(report: dict[str, Any]) -> None:
    s = report["summary"]
    print()
    print("=" * 68)
    print(f"  Tool-selection accuracy:  {s['selection_accuracy']:.1%}", end="")
    print(f"   ({s['cases_scored']} cases, {report['passes']} pass(es))")
    if report.get("accuracy_spread"):
        spread = report["accuracy_spread"]
        print(f"  Per-pass spread:          {spread['min']:.1%} - {spread['max']:.1%}", end="")
        if report.get("accuracy_stdev") is not None:
            print(f"   (sd {report['accuracy_stdev']:.3f})", end="")
        print()
    print("=" * 68)

    print("\n  By group:")
    for group, v in sorted(s["by_group"].items(), key=lambda kv: kv[1]["accuracy"]):
        print(f"    {group:<12} {v['accuracy']:>6.1%}  ({v['correct']}/{v['total']})")

    if s["confusions"]:
        print("\n  Mistakes (expected -> chosen):")
        for pair, count in s["confusions"].items():
            print(f"    {count:>3}x  {pair}")
    else:
        print("\n  No mistakes.")

    gate = s["delete_gate"]
    if gate["unconfirmed_cases"]:
        verdict = "HELD" if gate["held"] else "**BREACHED**"
        n = gate["unconfirmed_cases"]
        print()
        print(f"  Delete gate: {verdict}")
        print(
            f"    {gate['no_invocation_reached_mcp']}/{n} unconfirmed deletes sent zero MCP invocations"
        )
        print(
            f"    {gate['model_declined_unaided']}/{n} the model declined unaided; "
            f"{gate['gate_had_to_fire']}/{n} the gate had to stop it"
        )

    lat = s["latency_seconds"]
    print("\n  Ollama latency (seconds):")
    for label, key in (("with tools ", "first_call_with_tools"), ("no tools   ", "second_call_no_tools")):
        v = lat[key]
        if v["n"]:
            print(f"    {label} p50 {v['p50']:>6.2f}   p95 {v['p95']:>6.2f}   max {v['max']:>6.2f}   n={v['n']}")

    if s["cases_errored"]:
        print(f"\n  {s['cases_errored']} case(s) errored -- see the report JSON.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Run the whole set N times and report the mean plus spread (default 1).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override OLLAMA_MODEL for this run, e.g. to compare two models.",
    )
    parser.add_argument(
        "--group",
        default=None,
        help="Only run cases in this group (accounts, lookups, totals, history, delete, no_tool).",
    )
    parser.add_argument(
        "--full-reply",
        action="store_true",
        help=(
            "Also make the second Ollama call, the one that composes the "
            "natural-language reply. Roughly doubles the wall clock and changes "
            "no score; use it when you want to read the answers."
        ),
    )
    parser.add_argument(
        "--no-save", action="store_true", help="Print the summary without writing a report file."
    )
    args = parser.parse_args()

    if args.model:
        agent.OLLAMA_MODEL = args.model

    cases = [c for c in CASES if args.group is None or c.group == args.group]
    if not cases:
        print(f"No cases in group {args.group!r}.", file=sys.stderr)
        return 2

    print(f"Model: {agent.OLLAMA_MODEL}   Ollama: {agent.OLLAMA_BASE_URL}")
    mode = "with reply composition" if args.full_reply else "tool decision only"
    print(f"Cases: {len(cases)} x {args.repeat} pass(es)   ({mode})\n")

    report = asyncio.run(run_eval(cases, repeat=args.repeat, compose_reply=args.full_reply))
    _print_summary(report)

    if not args.no_save:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_model = agent.OLLAMA_MODEL.replace(":", "-").replace("/", "-")
        path = REPORTS_DIR / f"{stamp}-{safe_model}.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n  Report: {path.relative_to(BACKEND_DIR)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
