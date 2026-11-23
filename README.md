# Trybe order labels

Labels every Trybe order after purchase so the front desk knows who to pitch a
membership to.

| Result | When |
|---|---|
| **Member** | Customer has a qualifying membership, or buys one in this order |
| **Non-member** | Everyone else |
| **First Timer** (added on top) | No earlier submitted/settled order for this customer |
| **Order note** `[Membership pitch] ...` | Non-member with a treatment where they paid more than list price x 0.90 |

Orders with no customer attached are skipped.

## Setup

1. Create a Trybe API token, ideally on a dedicated "Automation" user: order
   notes show that user as the author.
2. `pip install -r requirements.txt`
3. `TRYBE_API_TOKEN=... python manage.py setup-labels` and copy the three
   `LABEL_ID_*` lines it prints into the environment.
4. Deploy `uvicorn app:app --host 0.0.0.0 --port $PORT` (Render web service).
5. In Trybe, for **each site**: Settings -> Integrations -> Webhooks -> New
   configuration. URL `https://<host>/trybe-webhooks`, the shared secret,
   events `basket.submitted`, `basket_item.added`, `basket_item.updated`, and a
   notification email (Trybe auto-disables a failing webhook).
6. Schedule `python manage.py sweep` nightly (Render cron job).

## Environment

| Variable | Required | Notes |
|---|---|---|
| `TRYBE_API_TOKEN` | yes | Bearer token |
| `TRYBE_WEBHOOK_SECRET` | yes (web service) | 12+ chars, same on every site's config |
| `LABEL_ID_FIRST_TIMER`, `LABEL_ID_NON_MEMBER` | yes | From `setup-labels` or the Trybe label response |
| `LABEL_ID_MEMBER` | no | Blank = members are left unlabelled |
| `TRYBE_BASE_URL` | no | `https://api.playground.try.be` to test |
| `DRY_RUN` | no | `1` logs decisions without writing to Trybe |
| `MEMBER_TREATMENT_DISCOUNT` | no | Default `0.10` |
| `MEMBER_TYPE_EXCLUDE` | no | Regex of membership type names that do not count as Member |
| `SWEEP_HOURS` | no | Lookback for the sweep, default `36` |
| `SITES` | no | Comma list of sites the sweep covers (`williamsburg,flatiron,atlantic_ave,philadelphia,berkeley_heights`); default all. Must match the sites the labels are enabled for |

`LABEL_ID_MEMBER` may be left blank: members then get no label and no note.
Skip `setup-labels` if the labels were already created in Trybe; just set the ids.

## Test before going live

Set `DRY_RUN=1` and run `python manage.py sweep`: it logs what every recent
order would get without changing anything in Trybe.
