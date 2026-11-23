"""Webhook receiver for Trybe order events.

Run:  uvicorn app:app --host 0.0.0.0 --port $PORT
Point each site's Trybe webhook config at  https://<host>/trybe-webhooks
with events: basket.submitted, basket_item.added, basket_item.updated
"""
import hashlib
import hmac
import json
import logging
import os
import time

from fastapi import BackgroundTasks, FastAPI, Request, Response

from labeler import Trybe, process_order

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("order-labels")

SECRET = os.environ["TRYBE_WEBHOOK_SECRET"].encode()
ORDER_EVENTS = {"basket.submitted"}
ITEM_EVENTS = {"basket_item.added", "basket_item.updated"}

app = FastAPI()
api = Trybe()

# Trybe delivers at-least-once; remember event ids for a day to drop repeats.
# In-memory is enough because process_order is idempotent anyway.
_seen: dict[str, float] = {}
_SEEN_TTL = 24 * 3600


def _is_duplicate(event_id: str) -> bool:
    now = time.time()
    for key in [k for k, t in _seen.items() if now - t > _SEEN_TTL]:
        del _seen[key]
    if event_id in _seen:
        return True
    _seen[event_id] = now
    return False


def _handle(order_id: str) -> None:
    try:
        process_order(api, order_id)
    except Exception:
        # The nightly sweep picks up anything that fails here.
        log.exception("failed to label order %s", order_id)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/trybe-webhooks")
async def trybe_webhooks(request: Request, background: BackgroundTasks):
    raw = await request.body()  # verify against the raw bytes, never re-serialised JSON
    expected = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("signature", "")):
        return Response(status_code=400)

    event = json.loads(raw)
    name, data = event.get("event"), event.get("data") or {}

    if name in ORDER_EVENTS:
        order_id = data.get("id")
    elif name in ITEM_EVENTS:
        order_id = data.get("basket_id")  # an order keeps its basket's id
    else:
        order_id = None

    if order_id and not _is_duplicate(event.get("id", "")):
        background.add_task(_handle, order_id)  # ack first, work after
    return Response(status_code=202)
