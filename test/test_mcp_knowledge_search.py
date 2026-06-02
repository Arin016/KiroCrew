"""Tests for local_knowledge_search tool in mcp_core."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_claw.validation import ValidationError


@pytest.fixture
def mock_db_exists(tmp_path):
    """Create a fake knowledge.db so the path check passes."""
    db_dir = tmp_path / "workspace" / "knowledge"
    db_dir.mkdir(parents=True)
    (db_dir / "knowledge.db").touch()
    return tmp_path


class TestKnowledgeSearchDBMissing:
    @patch("kiro_claw.mcp_core.config_dir")
    def test_returns_not_configured_when_db_missing(self, mock_config_dir, tmp_path):
        mock_config_dir.return_value = tmp_path  # no knowledge.db here
        from kiro_claw.mcp_core import _call_tool_inner

        result = _call_tool_inner("local_knowledge_search", {"query": "auth"})
        assert "not configured" in result


class TestKnowledgeSearchValidation:
    def test_empty_query_raises_validation_error(self):
        from kiro_claw.mcp_core import _call_tool_inner

        with pytest.raises(ValidationError):
            _call_tool_inner("local_knowledge_search", {"query": ""})

    def test_whitespace_query_raises_validation_error(self):
        from kiro_claw.mcp_core import _call_tool_inner

        with pytest.raises(ValidationError):
            _call_tool_inner("local_knowledge_search", {"query": "   "})

    def test_limit_over_max_raises_validation_error(self):
        from kiro_claw.mcp_core import _call_tool_inner

        with pytest.raises(ValidationError):
            _call_tool_inner("local_knowledge_search", {"query": "test", "limit": 99})


class TestKnowledgeSearchResults:
    @patch("kiro_claw.mcp_core.config_dir")
    @patch("kiro_claw.mcp_core.HybridRetriever")
    @patch("kiro_claw.mcp_core.KnowledgeStore")
    @patch("kiro_claw.mcp_core.create_embedder_from_config")
    def test_no_results_returns_message(
        self, mock_embedder, mock_store_cls, mock_retriever_cls, mock_config_dir, mock_db_exists
    ):
        mock_config_dir.return_value = mock_db_exists
        mock_embedder.return_value = None
        mock_retriever_cls.return_value.search.return_value = []
        from kiro_claw.mcp_core import _call_tool_inner

        result = _call_tool_inner("local_knowledge_search", {"query": "nonexistent"})
        assert "No relevant knowledge found" in result

    @patch("kiro_claw.mcp_core.config_dir")
    @patch("kiro_claw.mcp_core.HybridRetriever")
    @patch("kiro_claw.mcp_core.KnowledgeStore")
    @patch("kiro_claw.mcp_core.create_embedder_from_config")
    def test_low_score_filtered_out(
        self, mock_embedder, mock_store_cls, mock_retriever_cls, mock_config_dir, mock_db_exists
    ):
        mock_config_dir.return_value = mock_db_exists
        mock_embedder.return_value = None
        mock_retriever_cls.return_value.search.return_value = [
            {"title": "Weak Match", "content": "low relevance", "score": 0.005, "source": "s1"}
        ]
        from kiro_claw.mcp_core import _call_tool_inner

        result = _call_tool_inner("local_knowledge_search", {"query": "test"})
        assert "No relevant knowledge found" in result

    @patch("kiro_claw.mcp_core.config_dir")
    @patch("kiro_claw.mcp_core.HybridRetriever")
    @patch("kiro_claw.mcp_core.KnowledgeStore")
    @patch("kiro_claw.mcp_core.create_embedder_from_config")
    def test_formatted_output(
        self, mock_embedder, mock_store_cls, mock_retriever_cls, mock_config_dir, mock_db_exists
    ):
        mock_config_dir.return_value = mock_db_exists
        mock_embedder.return_value = None
        mock_retriever_cls.return_value.search.return_value = [
            {
                "title": "Auth Design",
                "content": "JWT tokens with 15min expiry.",
                "score": 0.035,
                "source": "src-1",
            }
        ]
        mock_store_cls.return_value.db.execute.return_value.fetchone.return_value = {
            "name": "design-docs/auth.md"
        }

        from kiro_claw.mcp_core import _call_tool_inner

        result = _call_tool_inner("local_knowledge_search", {"query": "auth"})

        assert "Auth Design" in result
        assert "design-docs/auth.md" in result
        assert "JWT tokens" in result
        assert "Knowledge Library" in result
        # No score or relevance metadata exposed
        assert "0.035" not in result

    @patch("kiro_claw.mcp_core.config_dir")
    @patch("kiro_claw.mcp_core.HybridRetriever")
    @patch("kiro_claw.mcp_core.KnowledgeStore")
    @patch("kiro_claw.mcp_core.create_embedder_from_config")
    def test_default_limit_is_3(
        self, mock_embedder, mock_store_cls, mock_retriever_cls, mock_config_dir, mock_db_exists
    ):
        mock_config_dir.return_value = mock_db_exists
        mock_embedder.return_value = None
        mock_retriever_cls.return_value.search.return_value = []

        from kiro_claw.mcp_core import _call_tool_inner

        _call_tool_inner("local_knowledge_search", {"query": "test"})
        mock_retriever_cls.return_value.search.assert_called_once_with("test", limit=3)


class TestKnowledgeSearchToolDefinition:
    def test_tool_listed(self):
        from kiro_claw.mcp_core import _list_tools

        tools = _list_tools()
        names = [t["name"] for t in tools]
        assert "local_knowledge_search" in names

    def test_tool_has_required_query(self):
        from kiro_claw.mcp_core import _list_tools

        tools = _list_tools()
        tool = next(t for t in tools if t["name"] == "local_knowledge_search")
        assert "query" in tool["inputSchema"]["required"]
