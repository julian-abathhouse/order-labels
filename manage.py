"""One-off and scheduled jobs.

  python manage.py setup-labels   create the three labels (once), print their ids
  python manage.py sweep          re-label recent orders the webhook missed
                                  (run nightly; Trybe has no webhook replay)
"""
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from labeler import LABEL_MEMBER, LABEL_NON_MEMBER, Trybe, process_order

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("order-labels")

SITE_IDS = {
    "williamsburg": "9b5df191-5a02-4514-b7e9-2ec2c36fcd0d",
    "flatiron": "9b5df0be-b4b1-4e51-8606-79dd25a37f25",
    "atlantic_ave": "a0e5c9e1-6837-4742-98d2-bd763ab26d86",
    "philadelphia": "a1f9a972-0c39-42b7-ad41-bd98ee26c1cd",
    "berkeley_heights": "a25e6ffb-fd9d-4700-a0e3-92e8fc215607",
}
LABELS = {  # name -> colour (lower-case hex, as Trybe requires)
    "First Timer": "#ffd166",
    "Member": "#06d6a0",
    "Non-member": "#ef476f",
}
SWEEP_HOURS = int(os.getenv("SWEEP_HOURS", "36"))
# Sites the labels are enabled for, e.g. SITES=philadelphia  (default: all).
# Trybe returns 422 if a label is attached to an order at a site it isn't enabled for.
SITES = [s.strip() for s in os.getenv("SITES", ",".join(SITE_IDS)).split(",") if s.strip()]


def setup_labels(api: Trybe) -> None:
    org_id = api.list_orders(per_page=1)[0]["organisation_id"]
    path = f"/shop/organisations/{org_id}/labels"
    existing = {l["name"]: l for l in api.request("GET", path, params={"per_page": 100})["data"]}
    for name, color in LABELS.items():
        label = existing.get(name) or api.request(
            "POST", path, json={"name": name, "color": color, "site_ids": list(SITE_IDS.values())}
        )["data"]
        missing = set(SITE_IDS.values()) - set(label.get("site_ids") or [])
        if missing:
            log.warning("label %r exists but is not enabled for sites: %s", name, missing)
        print(f"LABEL_ID_{name.upper().replace(' ', '_').replace('-', '_')}={label['id']}")


def sweep(api: Trybe) -> None:
    since = (datetime.now(timezone.utc) - timedelta(hours=SWEEP_HOURS)).isoformat()
    fixed = 0
    for site in SITES:
        site_id = SITE_IDS[site]
        for page in range(1, 200):
            orders = api.list_orders(
                site_id=site_id, status="settled,submitted",
                submitted_at_from=since, per_page=100, page=page,
            )
            if not orders:
                break
            for order in orders:
                labels = {l.get("id") for l in order.get("labels") or []}
                if order.get("customer_id") and not labels & ({LABEL_MEMBER, LABEL_NON_MEMBER} - {""}):
                    try:
                        process_order(api, order["id"])
                        fixed += 1
                    except Exception:
                        log.exception("sweep failed on order %s (%s)", order["id"], site)
    log.info("sweep done: labelled %d orders the webhook missed", fixed)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    {"setup-labels": setup_labels, "sweep": sweep}.get(
        command, lambda _: sys.exit(__doc__)
    )(Trybe())
