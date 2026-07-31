"""The agent brain, as a LangGraph state machine.

    understand → retrieve → [rank] → answer

`understand` updates long-term memory and works out what kind of question this
is. `retrieve` pulls grounding material. `rank` runs only for recommendation
questions and is pure arithmetic. `answer` writes the reply, and is the only
node allowed to call a generative model for prose.

The grounding contract is enforced structurally, not by hoping the model
behaves: if retrieval returns nothing, `answer` short-circuits to an honest
"I don't have that in the data" and never reaches the model at all.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from . import llm, retrieval
from . import persona as persona_mod
from .persona import Persona, StockScore

log = logging.getLogger(__name__)

Intent = Literal["recommend", "research", "profile", "general"]


class AgentState(TypedDict, total=False):
    user_id: str
    question: str
    history: list[dict[str, str]]

    intent: Intent
    persona: Persona
    persona_updated: bool
    tickers: list[str]

    chunks: list[retrieval.RetrievedChunk]
    context: str
    citations: list[dict[str, Any]]
    ranked: list[StockScore]
    excluded: list[StockScore]

    answer: str
    grounded: bool


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
ANSWER_SYSTEM = """You are an equity research analyst covering Indian listed \
companies (NSE/BSE) for one specific investor.

GROUNDING RULES — these override everything else:
- Use ONLY the numbered SOURCES provided. They are your entire world.
- Every factual claim, number, and recommendation must carry a citation like [1] or [2, 3].
- If the sources do not answer the question, say plainly: "I don't have that in \
the data I've ingested." Then say what you would need. NEVER estimate, recall \
from general knowledge, or infer a figure that is not written in a source.
- Never invent a price, ratio, date, or news event. A missing number is a \
finding worth reporting, not a gap to fill.

STYLE:
- All money is Indian Rupees, written as "Rs. 1,234.50" or "Rs. 4.2 lakh crore".
- Lead with the answer. Keep it tight — a few short paragraphs or a compact list.
- Reference the investor's stated profile when it changes your reasoning.
- You are an analyst, not an advisor: describe what the data shows and the \
risks in it. Do not tell the user to buy or sell, and add no disclaimer boilerplate \
beyond what the analysis itself needs."""

RECOMMEND_SYSTEM = (
    ANSWER_SYSTEM
    + """

This is a recommendation request. A deterministic scoring pass has already \
ranked the investor's followed stocks against their profile, and its output is \
given to you as SCREENING RESULTS.

- Present the top names in the order the screen produced. Do not re-rank them.
- For each, give one line on why it fits, citing SOURCES for any factual claim.
- If names were excluded by the investor's own rules, say so and give the reason.
- The screening scores themselves are computed from the fundamentals in the \
sources; you may state them without a citation, but any underlying figure needs one."""
)


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
async def understand(state: AgentState) -> AgentState:
    """Update memory from this turn, then classify the question."""
    question = state["question"]
    user_id = state["user_id"]

    # Memory write happens before retrieval so a profile stated in this very
    # message ("I avoid high debt — what should I buy?") applies immediately.
    updated = await persona_mod.learn_from_message(user_id, question)
    loaded = await persona_mod.load_persona(user_id)
    tickers = await retrieval.resolve_tickers(user_id, question)

    return {
        **state,
        "persona": loaded,
        "persona_updated": updated,
        "tickers": tickers,
        "intent": classify_intent(question, tickers),
    }


_RECOMMEND_PATTERNS = (
    r"\brecommend\b",
    r"\bwhat should i (buy|invest|pick|add)\b",
    r"\bsuggest\b",
    r"\bbest (stock|pick|bet)s?\b",
    r"\bfor my (profile|portfolio)\b",
    r"\bwhich (stock|one)s? (should|would|do)\b",
    r"\bwhat to buy\b",
    r"\btop picks?\b",
    r"\bshortlist\b",
)
_PROFILE_PATTERNS = (
    r"\bwhat do you know about me\b",
    # Allows a qualifier between "my" and the noun, so "my investor profile"
    # and "my risk preferences" match as readily as "my profile".
    r"\bmy (\w+ ){0,2}(profile|persona|preferences)\b",
    r"\bwho am i\b",
    r"\bwhat have you learn(ed|t)\b",
)


def classify_intent(question: str, tickers: list[str]) -> Intent:
    """Rule-based intent routing.

    A regex is the right tool here: the categories are few and phrased
    predictably, and spending an LLM call plus a round trip to distinguish
    "recommend something" from "tell me about TCS" would be exactly the
    wasteful pattern the brief warns against.
    """
    text = question.lower()

    # Recommendation is tested first because a request can name the profile
    # while asking for picks — "what should I buy for my profile" is a
    # recommendation, not a request to read the profile back. Checking profile
    # first swallowed it, and that phrasing is one of the dashboard's own
    # suggested prompts.
    if any(re.search(p, text) for p in _RECOMMEND_PATTERNS):
        return "recommend"
    if any(re.search(p, text) for p in _PROFILE_PATTERNS):
        return "profile"
    if tickers:
        return "research"
    return "general"


async def retrieve_node(state: AgentState) -> AgentState:
    chunks = await retrieval.retrieve(
        state["question"],
        user_id=state["user_id"],
        tickers=state.get("tickers") or None,
    )
    context, citations = retrieval.build_context(chunks)
    return {**state, "chunks": chunks, "context": context, "citations": citations}


async def rank_node(state: AgentState) -> AgentState:
    ranked, excluded = await persona_mod.rank_followed_stocks(state["user_id"], state["persona"])
    return {**state, "ranked": ranked, "excluded": excluded}


async def answer_node(state: AgentState) -> AgentState:
    chunks = state.get("chunks") or []
    ranked = state.get("ranked") or []
    intent = state.get("intent", "general")

    # The structural anti-hallucination guard: with nothing retrieved there is
    # nothing to ground an answer in, so no model call is made.
    if not chunks and not ranked:
        return {**state, "answer": _empty_answer(state), "grounded": False}

    if intent == "profile":
        return {**state, "answer": _profile_answer(state), "grounded": True}

    system = RECOMMEND_SYSTEM if intent == "recommend" else ANSWER_SYSTEM
    prompt = _build_prompt(state)

    try:
        completion = await llm.complete(
            system,
            _history_messages(state) + [llm.Message(role="user", content=prompt)],
            interactive=True,
        )
        text = completion.text.strip()
    except llm.LLMRefusal:
        text = (
            "I couldn't produce an answer for that request. Try rephrasing it "
            "as a question about one of the stocks you follow."
        )
    except llm.LLMError as exc:
        log.error("answer generation failed: %s", exc)
        # A quota rejection is temporary and self-resolving, and saying so is
        # far more useful than "unavailable" — which reads as broken.
        if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
            text = (
                "I've hit the model provider's rate limit, so I can't write the "
                "analysis right now — this clears on its own in a minute or two. "
                "The sources I retrieved for your question are below; they are "
                "the material the answer would have been built from."
            )
        else:
            text = (
                "The language model is unavailable right now, so I can't write "
                "the analysis. The sources I retrieved for your question are "
                "listed below — they are the material the answer would have "
                "been built from."
            )

    if state.get("persona_updated"):
        text += "\n\n_Noted and saved to your investor profile._"

    return {**state, "answer": text, "grounded": True}


def _history_messages(state: AgentState) -> list[llm.Message]:
    """Recent turns only — enough for follow-ups, not enough to drown the sources."""
    history = state.get("history") or []
    return [
        llm.Message(role=turn["role"], content=turn["content"])
        for turn in history[-6:]
        if turn.get("role") in ("user", "assistant") and turn.get("content")
    ]


def _build_prompt(state: AgentState) -> str:
    parts = [f"INVESTOR PROFILE:\n{state['persona'].describe()}"]

    ranked = state.get("ranked") or []
    if ranked:
        lines = []
        for rank, s in enumerate(ranked, start=1):
            price = f"Rs. {s.current_price:,.2f}" if s.current_price else "price unknown"
            factors = ", ".join(f"{k} {v:.2f}" for k, v in s.factors.items())
            lines.append(
                f"{rank}. {s.name} ({s.ticker}) — fit score {s.total:.3f}, "
                f"{price}, news sentiment {s.sentiment:+.2f}. "
                f"Factors: {factors}. Screen note: {s.reason}."
            )
        parts.append("SCREENING RESULTS (ranked by fit to profile):\n" + "\n".join(lines))

        excluded = state.get("excluded") or []
        if excluded:
            parts.append(
                "EXCLUDED BY THE INVESTOR'S OWN RULES:\n"
                + "\n".join(f"- {s.name} ({s.ticker}): {s.excluded_by}" for s in excluded)
            )

    parts.append("SOURCES:\n" + (state.get("context") or "(no sources retrieved)"))
    parts.append(f"QUESTION:\n{state['question']}")
    return "\n\n".join(parts)


def _empty_answer(state: AgentState) -> str:
    """The honest failure. Distinguishes 'no data yet' from 'no match'."""
    if not state.get("tickers"):
        return (
            "I don't have that in the data I've ingested.\n\n"
            "I can only answer from fundamentals and news for the tickers you "
            "follow. Follow a stock such as RELIANCE, TCS or HDFCBANK and I'll "
            "pull its screener.in fundamentals and recent Indian financial news, "
            "then answer with citations."
        )
    followed = ", ".join(state["tickers"])
    return (
        "I don't have that in the data I've ingested.\n\n"
        f"I have {followed} on file, but nothing I've stored addresses this "
        "question. Refreshing that ticker may pull in newer coverage — or try "
        "asking about its fundamentals, recent news, or sentiment."
    )


def _profile_answer(state: AgentState) -> str:
    """Answered from memory directly — no model call needed to read back facts."""
    p: Persona = state["persona"]
    if not p.facts and not p.rules:
        return (
            "I haven't learned anything about your investing style yet. Tell me "
            'things like *"I\'m conservative and dividend-focused"* or *"I avoid '
            "companies with debt-to-equity above 0.5\"* and I'll remember them "
            "and apply them to every recommendation from then on."
        )

    lines = ["Here's the investor profile I've built for you:\n"]
    if p.facts:
        lines.append("**What you've told me**")
        lines.extend(f"- {fact}" for fact in p.facts[:12])
    if p.rules:
        lines.append("\n**Hard rules I screen with**")
        lines.extend(
            f"- {r['field'].replace('_', ' ')} must be {r['op']} {r['value']:g}" for r in p.rules
        )
    lines.append("\n**How I weight factors for you**")
    lines.extend(
        f"- {factor}: {weight:.2f}"
        for factor, weight in sorted(p.weights.items(), key=lambda kv: -kv[1])
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
def _route_after_retrieve(state: AgentState) -> str:
    return "rank" if state.get("intent") == "recommend" else "compose"


def build_graph():
    """Assemble the agent graph.

    Node names must not collide with state keys: LangGraph rejects a graph
    where a node is called `answer` while the state also carries an `answer`
    field, since the two would be ambiguous when merging node output back into
    state. Hence `compose` for the node that produces `answer`.
    """
    graph = StateGraph(AgentState)
    graph.add_node("understand", understand)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("rank", rank_node)
    graph.add_node("compose", answer_node)

    graph.add_edge(START, "understand")
    graph.add_edge("understand", "retrieve")
    graph.add_conditional_edges(
        "retrieve", _route_after_retrieve, {"rank": "rank", "compose": "compose"}
    )
    graph.add_edge("rank", "compose")
    graph.add_edge("compose", END)
    return graph.compile()


_COMPILED = None


def get_graph():
    """Compile once and reuse — compilation is pure setup work."""
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = build_graph()
    return _COMPILED


async def run(
    *, user_id: str, question: str, history: list[dict[str, str]] | None = None
) -> AgentState:
    return await get_graph().ainvoke(
        {"user_id": user_id, "question": question, "history": history or []}
    )
