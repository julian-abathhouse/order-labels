"""Classify a Trybe order and write its labels + membership-pitch note.

Rules
-----
Membership status (exactly one):
  Member      customer has a qualifying membership, or buys one in this order
  Non-member  everyone else
First Timer (added on top): customer has no earlier submitted/settled order.
Pitch note (Non-member only): for each treatment where the guest paid more
  than the member price (list price x 0.90), say what they would have saved.

Everything is idempotent: labels are attached with the additive PUT, and the
note is found by its marker and only rewritten if its text changed.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("order-labels")

BASE_URL = os.getenv("TRYBE_BASE_URL", "https://api.try.be")
TOKEN = os.getenv("TRYBE_API_TOKEN", "")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

LABEL_FIRST_TIMER = os.getenv("LABEL_ID_FIRST_TIMER", "")
LABEL_MEMBER = os.getenv("LABEL_ID_MEMBER", "")
LABEL_NON_MEMBER = os.getenv("LABEL_ID_NON_MEMBER", "")

MEMBER_DISCOUNT = float(os.getenv("MEMBER_TREATMENT_DISCOUNT", "0.10"))

# Trybe "memberships" that should NOT count as Member (packs, partner
# programs, staff, tests). Matched against the membership type name.
MEMBER_TYPE_EXCLUDE = re.compile(
    os.getenv("MEMBER_TYPE_EXCLUDE", r"pack|test|employee| at bathhouse|soho house"),
    re.IGNORECASE,
)
# Membership statuses that mean "don't pitch this person".
MEMBER_STATUSES = {"active", "needs_attention", "needs_dd_mandate", "upcoming"}

# Order statuses that represent a real booking.
LIVE_STATUSES = {"submitted", "settled"}

NOTE_MARKER = "[Membership pitch]"


# --------------------------------------------------------------------------
# Trybe client
# --------------------------------------------------------------------------
class Trybe:
    def __init__(self, token: str = TOKEN, base_url: str = BASE_URL):
        self.http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=20,
        )

    def request(self, method: str, path: str, **kw) -> dict:
        for attempt in range(4):
            r = self.http.request(method, path, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            r.raise_for_status()
            return r.json() if r.content else {}
        r.raise_for_status()
        return {}

    def get_order(self, order_id: str) -> dict:
        return self.request("GET", f"/shop/orders/{order_id}")["data"]

    def list_orders(self, **params) -> list[dict]:
        return self.request("GET", "/shop/orders", params=params).get("data", [])

    def customer_memberships(self, customer_id: str) -> list[dict]:
        return self.request(
            "GET", f"/customers/customers/{customer_id}/memberships", params={"per_page": 100}
        ).get("data", [])

    def add_label(self, order_id: str, label_id: str) -> None:
        self.request("PUT", f"/shop/orders/{order_id}/labels/{label_id}")

    def remove_label(self, order_id: str, label_id: str) -> None:
        self.request("DELETE", f"/shop/orders/{order_id}/labels/{label_id}")

    def list_notes(self, order_id: str) -> list[dict]:
        return self.request("GET", f"/shop/orders/{order_id}/notes").get("data", [])

    def create_note(self, order_id: str, content: str) -> None:
        self.request("POST", f"/shop/orders/{order_id}/notes", json={"content": content})

    def update_note(self, order_id: str, note_id: str, content: str) -> None:
        self.request("PUT", f"/shop/orders/{order_id}/notes/{note_id}", json={"content": content})

    def delete_note(self, order_id: str, note_id: str) -> None:
        self.request("DELETE", f"/shop/orders/{order_id}/notes/{note_id}")


# --------------------------------------------------------------------------
# Classification (pure functions, no API calls)
# --------------------------------------------------------------------------
def _qualifies(type_name: str | None) -> bool:
    return bool(type_name) and not MEMBER_TYPE_EXCLUDE.search(type_name)


def buys_membership(order: dict) -> bool:
    """A real membership purchased in this same order counts as Member."""
    return any(
        p.get("item_type") == "membership"
        and p.get("status") != "cancelled"
        and _qualifies(p.get("type_name"))
        for p in order.get("purchase_items") or []
    )


def has_membership(memberships: list[dict]) -> bool:
    return any(
        m.get("status") in MEMBER_STATUSES and _qualifies((m.get("type") or {}).get("name"))
        for m in memberships
    )


def is_first_timer(order: dict, customer_orders: list[dict]) -> bool:
    """No other submitted/settled order placed before this one.

    Cancelled and no-show orders don't count: that guest still hasn't visited.
    """
    this_submitted = order.get("submitted_at") or ""
    for o in customer_orders:
        if o.get("id") == order.get("id") or o.get("status") not in LIVE_STATUSES:
            continue
        if (o.get("submitted_at") or "") <= this_submitted:
            return False
    return True


def _usd(cents: int) -> str:
    return f"${cents / 100:,.2f}".replace(".00", "")


def pitch_note(order: dict) -> str | None:
    """Text of the note, or None when there is no real saving to quote.

    Treatments live in package_items. All amounts are cents, pre-tax:
      price            list price
      discount_amount  item-level discounts already applied
    """
    lines, total = [], 0
    for item in order.get("package_items") or []:
        if item.get("status") == "cancelled":
            continue
        price = int(item.get("price") or 0)
        paid = price - int(item.get("discount_amount") or 0) - int(item.get("order_discount_amount") or 0)
        member_price = round(price * (1 - MEMBER_DISCOUNT))
        saving = paid - member_price
        if saving <= 0:
            continue
        total += saving
        lines.append(
            f"{item.get('type_name', 'Treatment')}: {_usd(member_price)} with a membership "
            f"(paid {_usd(paid)}, saves {_usd(saving)})"
        )
    if not lines:
        return None
    body = "; ".join(lines)
    if len(lines) > 1:
        body += f". Total saving {_usd(total)}"
    return f"{NOTE_MARKER} {body}."


@dataclass
class Decision:
    order_id: str
    member: bool = False
    first_timer: bool = False
    note: str | None = None
    skipped: str | None = None
    actions: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def process_order(api: Trybe, order_id: str) -> Decision:
    order = api.get_order(order_id)  # always re-read: the webhook payload may be stale
    d = Decision(order_id=order_id)

    if order.get("status") not in LIVE_STATUSES:
        d.skipped = f"order status {order.get('status')}"
        return d
    customer_id = order.get("customer_id")
    if not customer_id:
        d.skipped = "no customer on order"
        return d

    d.member = buys_membership(order) or has_membership(api.customer_memberships(customer_id))
    d.first_timer = is_first_timer(
        order,
        api.list_orders(
            customer_id=customer_id,
            status="settled,submitted",
            submitted_at_to=order.get("submitted_at"),
            per_page=10,
        ),
    )
    d.note = None if d.member else pitch_note(order)

    _apply_labels(api, order, d)
    _apply_note(api, order_id, d)
    log.info("order %s -> %s", order_id, d)
    return d


def _apply_labels(api: Trybe, order: dict, d: Decision) -> None:
    current = {label.get("id") for label in order.get("labels") or []}
    want = {LABEL_MEMBER if d.member else LABEL_NON_MEMBER}
    if d.first_timer:
        want.add(LABEL_FIRST_TIMER)
    # Only ever touch our own labels; staff labels are left alone.
    # A label id left blank (e.g. no Member label) is simply not applied.
    ours = {LABEL_MEMBER, LABEL_NON_MEMBER, LABEL_FIRST_TIMER} - {""}
    want &= ours

    for label_id in want - current:
        d.actions.append(f"add label {label_id}")
        if not DRY_RUN:
            api.add_label(d.order_id, label_id)
    for label_id in (current & ours) - want:
        d.actions.append(f"remove label {label_id}")
        if not DRY_RUN:
            api.remove_label(d.order_id, label_id)


def _apply_note(api: Trybe, order_id: str, d: Decision) -> None:
    existing = [n for n in api.list_notes(order_id) if (n.get("content") or "").startswith(NOTE_MARKER)]
    if d.note and not existing:
        d.actions.append("create note")
        if not DRY_RUN:
            api.create_note(order_id, d.note)
    elif d.note and existing[0].get("content") != d.note:
        d.actions.append("update note")
        if not DRY_RUN:
            api.update_note(order_id, existing[0]["id"], d.note)
    elif not d.note and existing:
        d.actions.append("delete stale note")
        if not DRY_RUN:
            api.delete_note(order_id, existing[0]["id"])
