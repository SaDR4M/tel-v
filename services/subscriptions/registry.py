"""Plan/order -> subscription provider resolution."""
from .base import SubscriptionError
from .dante_api import DanteProvider
from .vip_panel import VipPanelProvider

_PROVIDERS = {
    "vip_panel": VipPanelProvider(),
    "dante_api": DanteProvider(),
}

DEFAULT_PROVIDER = "vip_panel"


def get_provider(name: str):
    try:
        return _PROVIDERS[name]
    except KeyError as exc:
        raise SubscriptionError(f"Unknown subscription provider: {name}") from exc


def provider_name_for_plan(plan: dict) -> str:
    return plan.get("provider") or DEFAULT_PROVIDER


def provider_name_for_order(order) -> str | None:
    provider = order.get("provider") if isinstance(order, dict) else order.provider
    if provider:
        return provider
    panel_username = (
        order.get("panel_username")
        if isinstance(order, dict)
        else order.panel_username
    )
    if panel_username:
        return DEFAULT_PROVIDER
    return None


def provider_for_order(order):
    name = provider_name_for_order(order)
    return get_provider(name) if name else None
