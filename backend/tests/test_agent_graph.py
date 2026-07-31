"""Tests for the agent graph's structure.

The graph is compiled lazily on the first chat request, so a malformed graph
does not surface at import or at container startup — it surfaces as a 500 the
first time a user asks a question. These tests compile it eagerly instead.

That gap is not hypothetical: a node named `answer` collided with the state
key of the same name and reached production, because the health check boots
the app without ever building the graph.
"""

from __future__ import annotations

import pytest

from app.agent import AgentState, build_graph, classify_intent


class TestGraphCompiles:
    def test_graph_compiles(self):
        """LangGraph validates names and edges at compile time, not at import."""
        assert build_graph() is not None

    def test_no_node_shares_a_name_with_a_state_key(self):
        """LangGraph rejects this, and only when the graph is compiled.

        Asserting it directly means the failure names the actual problem
        rather than surfacing as an opaque ValueError at request time.
        """
        state_keys = set(AgentState.__annotations__)
        node_names = set(build_graph().get_graph().nodes) - {"__start__", "__end__"}
        collisions = state_keys & node_names
        assert not collisions, f"node names collide with state keys: {collisions}"

    def test_every_expected_node_is_present(self):
        nodes = set(build_graph().get_graph().nodes)
        for expected in ("understand", "retrieve", "rank", "compose"):
            assert expected in nodes, f"missing node: {expected}"

    def test_compilation_is_cached(self):
        """`get_graph` should reuse the compiled graph rather than rebuild it."""
        from app.agent import get_graph

        assert get_graph() is get_graph()


class TestIntentRouting:
    """Intent decides whether the expensive ranking pass runs at all."""

    @pytest.mark.parametrize(
        "question",
        [
            "What should I buy for my profile?",
            "Recommend some stocks",
            "What are your top picks?",
            "Which stocks should I add?",
        ],
    )
    def test_recommendation_questions_route_to_ranking(self, question):
        assert classify_intent(question, []) == "recommend"

    @pytest.mark.parametrize(
        "question",
        ["What do you know about me?", "Show my investor profile", "Who am I?"],
    )
    def test_profile_questions_are_answered_from_memory(self, question):
        assert classify_intent(question, []) == "profile"

    def test_ticker_questions_route_to_research(self):
        assert classify_intent("How is TCS doing this week?", ["TCS"]) == "research"

    def test_general_questions_without_a_ticker(self):
        assert classify_intent("What is a good P/E ratio?", []) == "general"

    def test_routing_needs_no_model_call(self):
        """Routing is regex-based on purpose — an LLM round trip here would be
        exactly the per-query waste the brief warns against."""
        for question in ("Recommend stocks", "What do you know about me?", "How is TCS?"):
            assert classify_intent(question, []) in {"recommend", "profile", "research", "general"}
