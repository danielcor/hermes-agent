"""Named delegation routes keep credential and ACP transport state immutable."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent.copilot_acp_client import CopilotACPClient
from run_agent import AIAgent


@pytest.mark.parametrize(
    ("method_name", "attributes", "resolver_target"),
    [
        (
            "_try_refresh_codex_client_credentials",
            {"provider": "openai-codex", "api_mode": "codex_responses"},
            "hermes_cli.auth.resolve_codex_runtime_credentials",
        ),
        (
            "_try_refresh_nous_client_credentials",
            {"provider": "nous", "api_mode": "chat_completions"},
            "hermes_cli.auth.resolve_nous_runtime_credentials",
        ),
        (
            "_try_refresh_vertex_client_credentials",
            {"provider": "vertex", "api_mode": "chat_completions"},
            "agent.vertex_adapter.get_vertex_config",
        ),
        (
            "_try_refresh_copilot_client_credentials",
            {"provider": "copilot", "api_mode": "chat_completions"},
            "hermes_cli.copilot_auth.resolve_copilot_token",
        ),
        (
            "_try_refresh_anthropic_client_credentials",
            {
                "provider": "anthropic",
                "api_mode": "anthropic_messages",
                "_anthropic_api_key": "frozen-anthropic-key",
            },
            "agent.anthropic_adapter.resolve_anthropic_token",
        ),
    ],
)
def test_named_route_never_reresolves_credentials(
    method_name, attributes, resolver_target
):
    agent = object.__new__(AIAgent)
    for name, value in attributes.items():
        setattr(agent, name, value)
    agent._frozen_http_policy = {}

    with patch(resolver_target) as resolver:
        assert getattr(agent, method_name)() is False

    resolver.assert_not_called()


def test_frozen_http_policy_preserves_legacy_positional_slots():
    legacy_prefill = [{"role": "user", "content": "legacy positional prefill"}]
    legacy_args: list[Any] = [None] * 48 + [legacy_prefill]

    with patch("agent.agent_init.init_agent") as init_agent:
        agent = AIAgent(*legacy_args)

    assert init_agent.call_args.args == (agent,)
    assert init_agent.call_args.kwargs["prefill_messages"] is legacy_prefill
    assert init_agent.call_args.kwargs["frozen_http_policy"] is None


def test_copilot_acp_explicit_empty_args_do_not_reload_ambient_args():
    with patch("agent.copilot_acp_client._resolve_args") as resolve_args:
        client = CopilotACPClient(command="trusted-acp", args=[])

    assert client._acp_args == []
    resolve_args.assert_not_called()


def test_anthropic_named_route_initializes_frozen_policy():
    policy = {
        "default_headers": {"X-Route": "frozen"},
        "ssl_ca_cert": "/trusted/ca.pem",
        "ssl_verify": False,
    }
    with patch("agent.anthropic_adapter.build_anthropic_client") as build:
        agent = AIAgent(
            provider="anthropic",
            api_mode="anthropic_messages",
            api_key="frozen-anthropic-key",
            model="claude-test",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            frozen_http_policy=policy,
        )

    assert agent._frozen_http_policy == policy
    assert agent._frozen_http_base_url == ""
    assert build.call_args.kwargs["frozen_http_policy"] == policy


def test_minimax_named_route_does_not_replace_frozen_key_with_ambient_provider():
    with patch(
        "hermes_cli.auth.build_minimax_oauth_token_provider"
    ) as build_token_provider, patch(
        "agent.anthropic_adapter.build_anthropic_client"
    ) as build_client:
        AIAgent(
            provider="minimax-oauth",
            api_mode="anthropic_messages",
            api_key="frozen-minimax-key",
            base_url="https://api.minimax.io/anthropic",
            model="MiniMax-M2.5",
            quiet_mode=True,
            skip_memory=True,
            skip_context_files=True,
            frozen_http_policy={},
        )

    build_token_provider.assert_not_called()
    assert build_client.call_args.args[0] == "frozen-minimax-key"
    assert build_client.call_args.kwargs["frozen_http_policy"] == {}


def test_minimax_legacy_route_keeps_dynamic_token_provider():
    dynamic_provider = MagicMock(name="dynamic_minimax_token_provider")
    with patch(
        "hermes_cli.auth.build_minimax_oauth_token_provider",
        return_value=dynamic_provider,
    ) as build_token_provider, patch(
        "agent.anthropic_adapter.build_anthropic_client"
    ) as build_client:
        AIAgent(
            provider="minimax-oauth",
            api_mode="anthropic_messages",
            api_key="initial-minimax-key",
            base_url="https://api.minimax.io/anthropic",
            model="MiniMax-M2.5",
            quiet_mode=True,
            skip_memory=True,
            skip_context_files=True,
        )

    build_token_provider.assert_called_once_with()
    assert build_client.call_args.args[0] is dynamic_provider
    assert "frozen_http_policy" not in build_client.call_args.kwargs


def test_anthropic_builder_applies_frozen_headers_and_tls():
    from agent.anthropic_adapter import build_anthropic_client

    sdk = MagicMock()
    http_client = MagicMock()
    policy = {
        "default_headers": {"X-Route": "frozen"},
        "ssl_ca_cert": "/trusted/ca.pem",
        "ssl_verify": False,
    }
    with patch("agent.anthropic_adapter._get_anthropic_sdk", return_value=sdk), patch(
        "agent.ssl_verify.resolve_httpx_verify", return_value="frozen-verify"
    ) as resolve_verify, patch("httpx.Client", return_value=http_client) as client_cls:
        build_anthropic_client(
            "frozen-anthropic-key",
            "https://api.anthropic.com",
            frozen_http_policy=policy,
        )

    kwargs = sdk.Anthropic.call_args.kwargs
    assert kwargs["default_headers"]["X-Route"] == "frozen"
    assert kwargs["http_client"] is http_client
    resolve_verify.assert_called_once_with(
        ca_bundle="/trusted/ca.pem", ssl_verify=False
    )
    assert client_cls.call_args.kwargs["verify"] == "frozen-verify"


def test_named_bedrock_anthropic_uses_frozen_credential_snapshot():
    credential_snapshot = MagicMock()
    with patch(
        "agent.anthropic_adapter.build_anthropic_bedrock_client"
    ) as build_client:
        agent = AIAgent(
            provider="bedrock",
            api_mode="anthropic_messages",
            api_key="aws-sdk",
            base_url="https://bedrock-runtime.us-west-2.amazonaws.com",
            model="global.anthropic.claude-sonnet-4-6",
            quiet_mode=True,
            skip_memory=True,
            skip_context_files=True,
            frozen_http_policy={},
            frozen_bedrock_credentials=credential_snapshot,
            frozen_bedrock_guardrail={},
        )

    build_client.assert_called_once_with(
        "us-west-2", credential_snapshot=credential_snapshot
    )
    assert agent._frozen_bedrock_credentials is credential_snapshot


def test_named_bedrock_converse_uses_frozen_guardrail_without_config_reload():
    credential_snapshot = MagicMock()
    frozen_guardrail = {
        "guardrailIdentifier": "guard-A",
        "guardrailVersion": "1",
    }
    ambient = {
        "bedrock": {
            "guardrail": {
                "guardrail_identifier": "guard-B",
                "guardrail_version": "2",
            }
        }
    }
    with patch("hermes_cli.config.load_config", return_value=ambient):
        agent = AIAgent(
            provider="bedrock",
            api_mode="bedrock_converse",
            api_key="aws-sdk",
            base_url="https://bedrock-runtime.us-west-2.amazonaws.com",
            model="amazon.nova-pro-v1:0",
            quiet_mode=True,
            skip_memory=True,
            skip_context_files=True,
            frozen_http_policy={},
            frozen_bedrock_credentials=credential_snapshot,
            frozen_bedrock_guardrail=frozen_guardrail,
        )

    assert agent._frozen_bedrock_credentials is credential_snapshot
    assert agent._bedrock_guardrail_config == frozen_guardrail
