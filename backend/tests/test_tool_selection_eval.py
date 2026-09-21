"""
The one part of the tool-selection eval that is a pass/fail assertion rather
than a measurement (see evals/runner.py for the eval itself).

Accuracy is a number to track, not a threshold to gate on -- it moves with the
model and with sampling, so asserting on it would produce a test that fails for
reasons nobody introduced. The delete gate is the opposite: it is our own code,
it is a pure function of the message text, and it must hold on every run
regardless of what the model decides. So that is what is asserted here.

Marked live_llm because it drives a real Ollama, exactly like the eval. CI runs
`-m "not live_llm"` and skips it. Run it locally with:

    .\\.venv\\Scripts\\python.exe -m pytest -m live_llm tests/test_tool_selection_eval.py
"""

import asyncio

import pytest


@pytest.mark.live_llm
def test_delete_gate_holds_against_a_real_model() -> None:
    # Imported inside the test, not at module scope: evals.runner configures
    # logging and loads .env at import time, and pytest imports every test
    # module during collection -- including this one on a `not live_llm` run
    # that never executes it. A module-level import would let a skipped test
    # reconfigure logging for the whole session.
    from evals.dataset import CASES
    from evals.runner import run_eval

    # One confirmed and one unconfirmed delete: the happy path and the failure
    # path for the gate, and nothing else. The full set lives in the eval, which
    # is not what a test suite is for.
    confirmed_case = next(c for c in CASES if c.delete_confirmed is True)
    unconfirmed_case = next(c for c in CASES if c.delete_confirmed is False)

    report = asyncio.run(run_eval([confirmed_case, unconfirmed_case]))
    results = {r["prompt"]: r for r in report["results"]}

    unconfirmed = results[unconfirmed_case.prompt]
    assert unconfirmed["invocations"] == [], (
        "An unconfirmed delete reached the tool layer. The gate in "
        "agent._execute_tool is the only thing standing between a model "
        f"misfire and real data loss. Model chose: {unconfirmed['model_choice']!r}"
    )

    # The confirmed case is here to prove the gate is not simply refusing
    # everything -- a gate that blocks every delete would pass the assertion
    # above and be useless.
    confirmed = results[confirmed_case.prompt]
    if confirmed["model_choice"] == "delete_conversation":
        assert confirmed["invocations"] == ["delete_conversation"], (
            "The model asked for a confirmed delete and the gate blocked it anyway."
        )
