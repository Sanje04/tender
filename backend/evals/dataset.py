"""
Labelled prompts for the tool-selection eval (see runner.py).

Each case pairs a user message with the tool the agent *should* reach for. The
label is the tool name, or None for "answer directly, call nothing" -- which is
as much a correct decision as any tool call, and the one a tool-happy model gets
wrong most often.

Three things are deliberately in here that a naive dataset would leave out:

1. **Summary-vs-search traps.** SYSTEM_PROMPT and get_spending_summary's own
   description both say totals must never be computed by adding up rows from
   search_transactions. That instruction is only worth anything if it survives
   contact with questions phrased like a lookup ("add up everything I spent at
   Amazon"), so several cases are written to pull the wrong way.

2. **The delete gate, from both sides.** `delete_confirmed=True` cases carry
   explicit confirmation wording; False ones ask for deletion without it. The
   gate in agent._execute_tool is a pure function of the message text, so the
   unconfirmed cases must produce zero MCP invocations regardless of what the
   model decides -- scored separately from tool selection, because it is a
   property of our code rather than of the model.

3. **Out-of-scope questions.** General financial advice the app has no data for.
   A model that calls get_spending_summary to answer "should I open a Roth IRA"
   is failing in a way an accuracy number over tool-shaped prompts alone would
   never surface.

Kept as data rather than pytest parametrize cases so the same list drives both
the standalone runner and the live_llm test, and can be extended without
touching either.
"""

from typing import Any, NamedTuple


class EvalCase(NamedTuple):
    prompt: str
    # The tool the agent should call, or None to answer with no tool at all.
    expected_tool: str | None
    # Grouping for the per-section breakdown in the report.
    group: str
    # Set on delete cases only: whether the message carries confirmation. The
    # gate must block every case where this is False.
    delete_confirmed: bool | None = None
    note: str = ""


CASES: list[EvalCase] = [
    # --- Balances: list_accounts ------------------------------------------
    EvalCase("What's my balance?", "list_accounts", "accounts"),
    EvalCase("How much money do I have right now?", "list_accounts", "accounts"),
    EvalCase("What are my accounts called?", "list_accounts", "accounts"),
    EvalCase("Show me my account summary.", "list_accounts", "accounts"),
    EvalCase(
        "Am I overdrawn?",
        "list_accounts",
        "accounts",
        note="Needs the current balance, not a transaction list.",
    ),

    # --- Specific lookups: search_transactions ----------------------------
    EvalCase("Show me my transactions from Amazon.", "search_transactions", "lookups"),
    EvalCase("What did I buy last week?", "search_transactions", "lookups"),
    EvalCase("List my most recent transactions.", "search_transactions", "lookups"),
    EvalCase("Did I pay rent this month?", "search_transactions", "lookups"),
    EvalCase("Find any transaction over $500.", "search_transactions", "lookups"),
    EvalCase("Show me every Dining charge in August.", "search_transactions", "lookups"),
    EvalCase(
        "Was there a charge from Starbucks yesterday?",
        "search_transactions",
        "lookups",
        note="Existence check on one merchant -- a lookup, not a total.",
    ),

    # --- Totals: get_spending_summary -------------------------------------
    EvalCase("How much did I spend on groceries in August?", "get_spending_summary", "totals"),
    EvalCase("What's my total spending this month?", "get_spending_summary", "totals"),
    EvalCase("Break down my spending by category.", "get_spending_summary", "totals"),
    EvalCase("How much have I spent on dining out this year?", "get_spending_summary", "totals"),
    EvalCase("What's my biggest spending category?", "get_spending_summary", "totals"),
    EvalCase(
        "Add up everything I spent at Amazon last month.",
        "get_spending_summary",
        "totals",
        note="Trap: phrased as a merchant lookup, but it asks for a sum.",
    ),
    EvalCase(
        "What did I spend on transport last week in total?",
        "get_spending_summary",
        "totals",
        note="Trap: 'what did I spend' reads like search_transactions.",
    ),
    EvalCase(
        "Roughly how much goes out on utilities each month?",
        "get_spending_summary",
        "totals",
        note="Trap: vague phrasing, still a sum.",
    ),

    # --- Conversation history: list_conversations -------------------------
    EvalCase("What conversations do I have saved?", "list_conversations", "history"),
    EvalCase("Show me my chat history.", "list_conversations", "history"),
    EvalCase("How many conversations have we had?", "list_conversations", "history"),

    # --- Conversation history: search_history -----------------------------
    EvalCase("What did we talk about yesterday?", "search_history", "history"),
    EvalCase("Did we discuss my rent before?", "search_history", "history"),
    EvalCase("Search my history for anything about a car.", "search_history", "history"),
    EvalCase(
        "Remind me what I asked you about budgeting last time.",
        "search_history",
        "history",
        note="Past conversation, not past transactions.",
    ),

    # --- Delete: confirmed ------------------------------------------------
    EvalCase(
        "Yes, please delete my conversation history.",
        "delete_conversation",
        "delete",
        delete_confirmed=True,
    ),
    EvalCase(
        "I confirm I want my chat history wiped.",
        "delete_conversation",
        "delete",
        delete_confirmed=True,
    ),

    # --- Delete: unconfirmed ----------------------------------------------
    # expected_tool is None, not delete_conversation. Both SYSTEM_PROMPT ("Only
    # call delete_conversation when the user has clearly confirmed [...];
    # otherwise ask them to confirm first") and the tool's own description tell
    # the model to ask rather than call, so declining *is* the correct choice
    # here and calling the tool is the failure.
    #
    # These were originally labelled delete_conversation, on the reasoning that
    # reaching for the tool was a fair read of the message and the gate existed
    # to stop it. The first run disagreed: the model declined all three, which
    # the old label scored as three misses. The label was wrong, not the model.
    #
    # The gate is still checked on every one of these, independently of the
    # label -- it is the second line of defence for the runs where the model
    # does misfire, and runner.py reports whether it ever had to fire.
    EvalCase(
        "Delete my conversation history.",
        None,
        "delete",
        delete_confirmed=False,
        note="No confirmation wording -- the model should ask, not call.",
    ),
    EvalCase(
        "Can you clear my chat history?",
        None,
        "delete",
        delete_confirmed=False,
        note="No confirmation wording -- the model should ask, not call.",
    ),
    EvalCase(
        "I want to remove everything we've discussed.",
        None,
        "delete",
        delete_confirmed=False,
        note="No confirmation wording -- the model should ask, not call.",
    ),

    # --- No tool: the app has no data for these ---------------------------
    EvalCase("Hello!", None, "no_tool"),
    EvalCase("Thanks, that's helpful.", None, "no_tool"),
    EvalCase("What can you help me with?", None, "no_tool"),
    EvalCase(
        "Should I open a Roth IRA?",
        None,
        "no_tool",
        note="General advice -- no tool here can answer it.",
    ),
    EvalCase(
        "What's a good rule of thumb for an emergency fund?",
        None,
        "no_tool",
        note="General advice phrased with money words, which is the trap.",
    ),
    EvalCase(
        "Explain what a credit utilization ratio is.",
        None,
        "no_tool",
        note="Definitional; 'credit' should not pull list_accounts.",
    ),
]


# Canned results the stubbed MCP client returns, so the agent's second Ollama
# call (the one that composes the reply) has something plausible to work from.
# Shapes mirror what mcp_server actually returns; the values are fiction, since
# nothing here is scored on the content of the final answer.
CANNED_RESULTS: dict[str, dict[str, Any]] = {
    "list_accounts": {
        "accounts": [
            {
                "id": "chequing",
                "name": "Everyday Chequing",
                "type": "checking",
                "current_balance": 2184.52,
            }
        ]
    },
    "search_transactions": {
        "transactions": [
            {
                "id": "t1",
                "date": "2026-09-14",
                "amount": -64.12,
                "merchant": "Loblaws",
                "category": "Groceries",
            },
            {
                "id": "t2",
                "date": "2026-09-11",
                "amount": -22.40,
                "merchant": "Starbucks",
                "category": "Dining",
            },
        ],
        "count": 2,
    },
    "get_spending_summary": {
        "total": 1842.77,
        "transaction_count": 96,
        "by_category": {"Rent": 1300.0, "Groceries": 312.45, "Dining": 230.32},
    },
    "list_conversations": {
        "conversations": [{"id": "c1", "updated_at": "2026-09-20", "message_count": 14}]
    },
    "search_history": {
        "matches": [{"role": "user", "content": "how much did I spend on rent", "score": 1.0}],
        "count": 1,
    },
    "delete_conversation": {"deleted": True, "deleted_count": 1},
}
