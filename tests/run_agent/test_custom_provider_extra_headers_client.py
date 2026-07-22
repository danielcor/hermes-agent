"""Per-provider ``extra_headers`` applied to the OpenAI client (#3526 salvage).

Custom providers (``providers`` / ``custom_providers`` in config.yaml) can
declare an ``extra_headers`` dict that must land on the OpenAI client's
``default_headers`` at construction and survive header re-application on
credential swaps / rebuilds. Values may carry credentials — the plumbing must
never log them.
"""
from unittest.mock import MagicMock, patch

from run_agent import AIAgent

_PROXY_URL = "https://llm.internal.example.com/v1"
_PROXY_CONFIG = {
    "custom_providers": [
        {
            "name": "my-proxy",
            "base_url": _PROXY_URL,
            "api_key": "proxy-key",
            "extra_headers": {
                "CF-Access-Client-Id": "xxxx.access",
                "X-Client-Name": "hermes-agent",
            },
        }
    ]
}


@patch("run_agent.OpenAI")
def test_custom_provider_extra_headers_applied_at_construction(mock_openai):
    mock_openai.return_value = MagicMock()
    with patch("hermes_cli.config.load_config", return_value=_PROXY_CONFIG):
        agent = AIAgent(
            api_key="proxy-key",
            base_url=_PROXY_URL,
            model="my-model",
            provider="custom",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    headers = agent._client_kwargs["default_headers"]
    assert headers["CF-Access-Client-Id"] == "xxxx.access"
    assert headers["X-Client-Name"] == "hermes-agent"


@patch("run_agent.OpenAI")
def test_extra_headers_not_applied_for_other_base_url(mock_openai):
    mock_openai.return_value = MagicMock()
    with patch("hermes_cli.config.load_config", return_value=_PROXY_CONFIG):
        agent = AIAgent(
            api_key="other-key",
            base_url="http://localhost:8080/v1",
            model="my-model",
            provider="custom",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    headers = agent._client_kwargs.get("default_headers") or {}
    assert "CF-Access-Client-Id" not in headers
    assert "X-Client-Name" not in headers


@patch("run_agent.OpenAI")
def test_extra_headers_survive_header_reapplication(mock_openai):
    """_apply_client_headers_for_base_url (credential swaps, rebuilds) must
    re-apply per-provider extra_headers rather than dropping them."""
    mock_openai.return_value = MagicMock()
    with patch("hermes_cli.config.load_config", return_value=_PROXY_CONFIG):
        agent = AIAgent(
            api_key="proxy-key",
            base_url=_PROXY_URL,
            model="my-model",
            provider="custom",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent._client_kwargs.pop("default_headers", None)
        agent._apply_client_headers_for_base_url(_PROXY_URL)

    headers = agent._client_kwargs["default_headers"]
    assert headers["CF-Access-Client-Id"] == "xxxx.access"


@patch("run_agent.OpenAI")
def test_extra_headers_merge_with_global_default_headers(mock_openai):
    """Per-provider extra_headers win over global model.default_headers on
    key collisions; non-colliding globals are preserved."""
    mock_openai.return_value = MagicMock()
    config = {
        "model": {"default_headers": {"User-Agent": "curl/8.7.1", "X-Global": "1"}},
        "custom_providers": [
            {
                "name": "my-proxy",
                "base_url": _PROXY_URL,
                "api_key": "proxy-key",
                "extra_headers": {"User-Agent": "hermes-proxy", "X-Local": "2"},
            }
        ],
    }
    with patch("hermes_cli.config.load_config", return_value=config):
        agent = AIAgent(
            api_key="proxy-key",
            base_url=_PROXY_URL,
            model="my-model",
            provider="custom",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    headers = agent._client_kwargs["default_headers"]
    assert headers["User-Agent"] == "hermes-proxy"  # per-provider wins
    assert headers["X-Global"] == "1"
    assert headers["X-Local"] == "2"


@patch("run_agent.OpenAI")
def test_frozen_http_policy_bypasses_mutable_config_resolution(mock_openai):
    mock_openai.return_value = MagicMock()
    frozen = {
        "default_headers": {"Authorization": "frozen", "X-Lane": "review"},
        "ssl_ca_cert": "/frozen/lane-ca.pem",
        "ssl_verify": False,
    }
    with patch.object(AIAgent, "_apply_user_default_headers") as user_headers, patch(
        "hermes_cli.config.apply_custom_provider_tls_to_client_kwargs"
    ) as apply_tls, patch(
        "hermes_cli.config.apply_custom_provider_extra_headers_to_client_kwargs"
    ) as apply_headers:
        agent = AIAgent(
            api_key="proxy-key",
            base_url=_PROXY_URL,
            model="my-model",
            provider="custom",
            frozen_http_policy=frozen,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        assert _PROXY_URL not in agent._client_log_context()
        assert "base_url=<frozen-route>" in agent._client_log_context()
        client_kwargs = getattr(agent, "_client_kwargs")
        assert client_kwargs["default_headers"]["Authorization"] == "frozen"
        client_kwargs.pop("default_headers", None)
        agent._apply_client_headers_for_base_url(_PROXY_URL)
        assert client_kwargs["default_headers"]["Authorization"] == "frozen"
        assert client_kwargs["default_headers"]["X-Lane"] == "review"
        assert client_kwargs["ssl_ca_cert"] == "/frozen/lane-ca.pem"
        assert client_kwargs["ssl_verify"] is False
        agent._apply_client_headers_for_base_url("https://unexpected.invalid/v1")
        assert "Authorization" not in (client_kwargs.get("default_headers") or {})
        assert "ssl_ca_cert" not in client_kwargs
        assert "ssl_verify" not in client_kwargs

    user_headers.assert_not_called()
    apply_tls.assert_not_called()
    apply_headers.assert_not_called()
