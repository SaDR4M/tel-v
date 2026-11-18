"""Approval-time provisioning orchestration."""
from dataclasses import dataclass
from datetime import datetime, timedelta

import database as db

from .base import ProvisionRequest, SubscriptionError, SubscriptionUser
from .registry import get_provider, provider_name_for_plan


@dataclass(frozen=True)
class ProvisionResult:
    user: SubscriptionUser
    extended: bool
    previous_order_id: int | None = None


def _request_for(order: dict, plan: dict) -> ProvisionRequest:
    return ProvisionRequest(
        order_id=int(order["order_id"]),
        telegram_id=int(order["telegram_id"]),
        requested_username=order.get("requested_username") or "user",
        plan=plan,
    )


async def _persist(
    provider,
    request: ProvisionRequest,
    user: SubscriptionUser,
    *,
    extended: bool,
    previous_order_id: int | None = None,
) -> ProvisionResult:
    expire_at = user.expires_at or (
        datetime.now() + timedelta(days=int(request.plan["duration_days"]))
    )
    if provider.name == "vip_panel":
        await db.link_panel_account(request.telegram_id, user.user_id)
        if extended:
            await db.approve_renewal_order(
                request.order_id, user.user_id, expire_at
            )
        else:
            await db.set_order_status(
                request.order_id, user.status or "active", user.user_id
            )
            await db.set_order_expire_at(request.order_id, expire_at)
    else:
        await db.approve_provider_order(
            request.order_id,
            provider=provider.name,
            provider_user_id=user.user_id,
            provider_username=user.username or "",
            subscription_url=user.subscription_url,
            expire_at=expire_at,
            status=user.status or "active",
            supersede_order_id=previous_order_id,
        )
    return ProvisionResult(
        user=user,
        extended=extended,
        previous_order_id=previous_order_id,
    )


async def approve_purchase(order: dict, plan: dict) -> ProvisionResult:
    """Create a NEW provider account for an Unlimited purchase.

    Repeat purchases intentionally create separate accounts/links. Only the
    explicit renewal flow extends an existing account.
    """
    provider = get_provider(provider_name_for_plan(plan))
    request = _request_for(order, plan)
    user = await provider.create(request)
    return await _persist(provider, request, user, extended=False)


async def approve_renewal(order: dict, plan: dict) -> ProvisionResult:
    """Extend the provider account selected in the renewal flow."""
    provider = get_provider(provider_name_for_plan(plan))
    target = order.get("target_provider_user_id")
    if provider.name == "vip_panel":
        target = target or order.get("target_panel_username")
    if not target:
        raise SubscriptionError("Renewal target account is missing")

    request = _request_for(order, plan)
    user = await provider.extend(str(target), request)
    previous = await db.get_provider_order_by_user(
        request.telegram_id, provider.name, str(target)
    )
    previous_order_id = int(previous["order_id"]) if previous else None
    return await _persist(
        provider,
        request,
        user,
        extended=True,
        previous_order_id=previous_order_id,
    )
