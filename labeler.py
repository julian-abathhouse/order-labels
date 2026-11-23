"""Classify a Trybe order and write its labels + membership-pitch note.

Rules
-----
Membership status (exactly one):
  Member      customer has a qualifying membership, or buys one in this order
  Non-member  everyone else
First Timer (added on top): customer has no earlier submitted/settled order.
Pitch note (Non-member only): what the guest would have saved as a member on
  their day pass (Core discount by site and weekday) and on each treatment
  (list price x 0.90). Lines with no real saving are left out.

Everything is idempotent: labels are attached with the additive PUT, and the
note is found by its marker and only rewritten if its text changed.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date

import httpx

log = logging.getLogger("order-labels")

BASE_URL = os.getenv("TRYBE_BASE_URL", "https://api.try.be")
TOKEN = os.getenv("TRYBE_API_TOKEN", "")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

LABEL_FIRST_TIMER = os.getenv("LABEL_ID_FIRST_TIMER", "")
LABEL_MEMBER = os.getenv("LABEL_ID_MEMBER", "")
LABEL_NON_MEMBER = os.getenv("LABEL_ID_NON_MEMBER", "")

MEMBER_DISCOUNT = float(os.getenv("MEMBER_TREATMENT_DISCOUNT", "0.10"))

# Core member day-pass discount by visit weekday (Mon..Sun), per site.
# A plain number is % off; a number with $ is a fixed dollar amount off.
# Override with DAY_PASS_DISCOUNT_<SITE> (or DAY_PASS_DISCOUNT_NYC for the
# three NYC sites), e.g. DAY_PASS_DISCOUNT_PHILADELPHIA=35,35,35,35,15,$20,$20
_NYC = "30,30,30,30,25,$10,$10"
_PHL = "35,35,35,35,15,$20,$20"
_BH = "25,25,25,25,15,$20,$20"
_DAY_PASS_SITES = {  # name: (site id, group env var, default)
    "WILLIAMSBURG": ("9b5df191-5a02-4514-b7e9-2ec2c36fcd0d", "NYC", _NYC),
    "FLATIRON": ("9b5df0be-b4b1-4e51-8606-79dd25a37f25", "NYC", _NYC),
    "ATLANTIC_AVE": ("a0e5c9e1-6837-4742-98d2-bd763ab26d86", "NYC", _NYC),
    "PHILADELPHIA": ("a1f9a972-0c39-42b7-ad41-bd98ee26c1cd", "", _PHL),
    "BERKELEY_HEIGHTS": ("a25e6ffb-fd9d-4700-a0e3-92e8fc215607", "", _BH),
}


def _parse_discounts(raw: str) -> list[tuple[str, float]]:
    """'25,15,$20' -> [('pct', 25.0), ('pct', 15.0), ('usd', 20.0)]"""
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(("usd" if "$" in part else "pct", float(part.replace("$", "").replace("%", ""))))
    return out


DAY_PASS_DISCOUNTS: dict[str, list[tuple[str, float]]] = {}
for _name, (_site_id, _group, _default) in _DAY_PASS_SITES.items():
    _raw = os.getenv(f"DAY_PASS_DISCOUNT_{_name}") or (
        os.getenv(f"DAY_PASS_DISCOUNT_{_group}") if _group else None
    ) or _default
    _vals = _parse_discounts(_raw)
    if len(_vals) == 7:
        DAY_PASS_DISCOUNTS[_site_id] = _vals
    else:
        log.warning("day-pass discount for %s needs 7 values, got %r: ignored", _name, _raw)
# Which sessions count as a day pass (matched against the item name).
DAY_PASS_NAME = re.compile(os.getenv("DAY_PASS_NAME", r"day pass"), re.IGNORECASE)

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


def _paid(item: dict) -> int:
    return (
        int(item.get("price") or 0)
        - int(item.get("discount_amount") or 0)
        - int(item.get("order_discount_amount") or 0)
    )


def _is_day_pass(item: dict) -> bool:
    return item.get("item_type") == "session" and bool(DAY_PASS_NAME.search(item.get("type_name") or ""))


def _day_pass_member_price(order: dict, item: dict) -> int | None:
    """Core member price (cents) for this day pass, or None if no rule applies."""
    by_day = DAY_PASS_DISCOUNTS.get(order.get("site_id") or "")
    try:
        weekday = date.fromisoformat((item.get("date") or "")[:10]).weekday()  # Mon=0
    except ValueError:
        return None
    if not by_day:
        return None
    kind, value = by_day[weekday]
    price = int(item.get("price") or 0)
    if kind == "usd":
        return max(price - round(value * 100), 0)
    return round(price * (1 - value / 100))


def pitch_note(order: dict) -> str | None:
    """Text of the note, or None when there is no real saving to quote.

    All amounts are cents, pre-tax: price = list price, discount_amount =
    item-level discounts already applied.
      Treatments  package_items (NYC) and booking_items of type "appointment"
                  (Philadelphia): member price = list x (1 - 10%).
      Day pass    booking_items of type "session" named like a day pass:
                  Core member price by site and weekday. Only one pass is
                  quoted (the dearest), since the discount is the member's own.
    """
    lines, total = [], 0
    bookings = [b for b in order.get("booking_items") or [] if b.get("status") != "cancelled"]

    passes = [b for b in bookings if _is_day_pass(b) and _day_pass_member_price(order, b) is not None]
    if passes:
        item = max(passes, key=lambda b: int(b.get("price") or 0))
        paid, member_price = _paid(item), _day_pass_member_price(order, item)
        if paid - member_price > 0:
            total += paid - member_price
            lines.append(
                f"Day Pass: {_usd(member_price)} as a Core member "
                f"(paid {_usd(paid)}, saves {_usd(paid - member_price)})"
            )

    treatments = [p for p in order.get("package_items") or [] if p.get("status") != "cancelled"]
    treatments += [b for b in bookings if b.get("item_type") == "appointment"]
    for item in treatments:
        price, paid = int(item.get("price") or 0), _paid(item)
        member_price = round(price * (1 - MEMBER_DISCOUNT))
        saving = paid - member_price
        if saving <= 0:
            continue
        total += saving
        name = re.sub(r"\s*\(.*?\)", "", item.get("type_name") or "Treatment").strip()
        lines.append(
            f"{name}: {_usd(member_price)} with a membership "
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
        log.info("order %s skipped: %s", order_id, d.skipped)
        return d
    customer_id = order.get("customer_id")
    if not customer_id:
        d.skipped = "no customer on order"
        log.info("order %s skipped: %s", order_id, d.skipped)
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