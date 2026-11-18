"""Select the Qwen provider for selective recovery and AI Assist.

QWEN_PROVIDER = disabled (default) | alibaba_cloud. Legacy local transport is not selected.
Whether Qwen is asked at all is still controlled separately by QWEN_RECOVERY_MODE
(default off) and by the explicit AI Assist action, so selecting a provider never
enables inference by itself.
"""
from __future__ import annotations

import os


class ProviderDisabled(RuntimeError):
    """Raised instead of contacting any model when the provider is disabled."""


def provider_name() -> str:
    name = os.environ.get("QWEN_PROVIDER", "disabled").strip().lower() or "disabled"
    if name not in {"alibaba_cloud", "disabled"}:
        raise ProviderDisabled("QWEN_PROVIDER must be alibaba_cloud or disabled")
    return name


def select_qwen_provider():
    name = provider_name()
    if name == "disabled":
        raise ProviderDisabled("QWEN_PROVIDER_DISABLED")
    if name == "alibaba_cloud":
        from services.extraction.ai.cloud_qwen_provider import AlibabaCloudQwenProvider
        return AlibabaCloudQwenProvider()
    raise ProviderDisabled("QWEN_PROVIDER_DISABLED")


def provider_label(provider) -> str:
    """Provider name for run records; a provider without a string name is the local one."""
    name = getattr(provider, "name", None)
    return name if isinstance(name, str) else "ollama"
