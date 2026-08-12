"""Provider registry."""

from agent_capacity.providers.azure import AzureProvider
from agent_capacity.providers.base import ComputeProvider, ProviderObservation
from agent_capacity.providers.fly import FlyProvider

PROVIDERS: dict[str, ComputeProvider] = {
    "fly": FlyProvider(),
    "azure": AzureProvider(),
}

REQUIRED_METHODS = (
    "ready", "identity", "regions", "build_plan", "parse_submission", "status_command",
    "parse_status", "logs_command", "cancel_command", "cleanup_command",
    "classify_failure", "result_strategy",
)


def contract_complete(provider: ComputeProvider) -> bool:
    return all(callable(getattr(provider, method, None)) for method in REQUIRED_METHODS)


def get_provider(name: str) -> ComputeProvider:
    try:
        return PROVIDERS[name]
    except KeyError as error:
        raise ValueError(f"unsupported provider: {name}") from error


__all__ = ["ComputeProvider", "ProviderObservation", "contract_complete", "get_provider"]
