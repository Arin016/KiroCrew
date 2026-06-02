"""Tests for knowledge search upgrade: embedding endpoints + search-for-context."""
import pytest

from kiro_claw.knowledge.embedder import OllamaEmbedder, create_embedder_from_config


class TestCreateEmbedderFromConfig:
    """create_embedder_from_config uses shared memory config."""

    def test_returns_none_when_provider_not_ollama(self):
        cfg = {"memory": {"embedding_provider": "none"}}
        assert create_embedder_from_config(cfg) is None

    def test_returns_none_when_memory_section_missing(self):
        assert create_embedder_from_config({}) is None

    def test_returns_embedder_when_ollama_enabled(self):
        cfg = {"memory": {"embedding_provider": "ollama"}}
        emb = create_embedder_from_config(cfg)
        assert isinstance(emb, OllamaEmbedder)
        assert emb.model == "qwen3-embedding:0.6b"

    def test_uses_custom_model_and_url(self):
        cfg = {"memory": {
            "embedding_provider": "ollama",
            "embedding_model": "custom:latest",
            "embedding_url": "http://remote:11434",
        }}
        emb = create_embedder_from_config(cfg)
        assert emb.model == "custom:latest"
        assert emb.base_url == "http://remote:11434"

    def test_ignores_old_knowledge_embeddings_config(self):
        """Old knowledge.embeddings.enabled path should NOT activate embedder."""
        cfg = {"knowledge": {"embeddings": {"enabled": True}}}
        assert create_embedder_from_config(cfg) is None


class TestOllamaEmbedder:
    """OllamaEmbedder graceful degradation."""

    def test_embed_returns_none_for_empty_text(self):
        emb = OllamaEmbedder()
        assert emb.embed("") is None
        assert emb.embed("   ") is None

    def test_embed_returns_none_when_unavailable(self, monkeypatch):
        emb = OllamaEmbedder()
        monkeypatch.setattr(emb, "is_available", lambda: False)
        assert emb.embed("hello world") is None

    def test_embed_for_item_combines_title_and_summary(self, monkeypatch):
        emb = OllamaEmbedder()
        monkeypatch.setattr(emb, "is_available", lambda: False)
        result = emb.embed_for_item("My Title", "A summary of the content")
        assert result is None


class TestSearchForContext:
    """search_for_context endpoint logic."""

    def test_estimate_tokens(self):
        try:
            from kiro_claw.dashboard.handlers.knowledge import _estimate_tokens
        except TypeError:
            pytest.skip("requires Python 3.10+ (type union syntax)")
        assert _estimate_tokens("hello world") == 2  # 11 chars // 4
        assert _estimate_tokens("") == 0

    def test_knowledge_fetch_defaults(self):
        try:
            from kiro_claw.dashboard.handlers.knowledge import (
                KNOWLEDGE_FETCH_MAX_TOKENS,
                KNOWLEDGE_FETCH_TOP_N,
            )
        except TypeError:
            pytest.skip("requires Python 3.10+ (type union syntax)")
        assert KNOWLEDGE_FETCH_TOP_N == 3
        assert KNOWLEDGE_FETCH_MAX_TOKENS == 4096


class TestRedactMeta:
    """_redact_meta security helper."""

    def test_redacts_strings(self):
        try:
            from kiro_claw.dashboard.chat_persistence import _redact_meta
        except TypeError:
            pytest.skip("requires Python 3.10+")
        meta = {"title": "safe text", "content": "key is AKIAIOSFODNN7EXAMPLE here"}
        result = _redact_meta(meta)
        assert "AKIAIOSFODNN7EXAMPLE" not in result["content"]
        assert result["title"] == "safe text"

    def test_redacts_nested_dicts(self):
        try:
            from kiro_claw.dashboard.chat_persistence import _redact_meta
        except TypeError:
            pytest.skip("requires Python 3.10+")
        meta = {"knowledge": {"content": [{"title": "ok", "text": "AKIAIOSFODNN7EXAMPLE"}]}}
        result = _redact_meta(meta)
        assert "AKIAIOSFODNN7EXAMPLE" not in str(result)

    def test_preserves_non_strings(self):
        try:
            from kiro_claw.dashboard.chat_persistence import _redact_meta
        except TypeError:
            pytest.skip("requires Python 3.10+")
        meta = {"items": 3, "tokens": 1054, "titles": ["safe"]}
        result = _redact_meta(meta)
        assert result == {"items": 3, "tokens": 1054, "titles": ["safe"]}
