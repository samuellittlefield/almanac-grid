"""Client for BLM MLRS (Mineral & Land Records System) case actions.

MLRS (https://mlrs.blm.gov) is a Salesforce Experience Cloud site. Each case
has a public page at /s/blm-case/{SF_ID}/x, and its data comes from guest
Aura calls (POST /s/sfsites/aura). None of this is a documented API; it was
found by watching a case page's network requests.

The page's own Related Records tab uses
`RelatedListViewDataManagerController.getItems`, but that list layout leaves
the action code and name blank for guests (the Action__r lookup isn't
readable). The Lightning UI API's `getRelatedListRecords` action, which takes
an explicit field list, does return the case action's own formula fields
`Code__c` (the LR2000 disposition action code) and `Action_Name__c`, so that's
what this module calls: one POST per case.

Fragility: an Aura POST must carry the framework id (`fwuid`) and the app's
loaded-component id of the current Salesforce release. Both are read from a
case page at start-up and again whenever the server answers
`aura:clientOutOfSync`, so a Salesforce release doesn't break a long run. If
BLM renames the fields or related lists below, `fetch_case` raises
`MlrsError` and the ingestion falls back to the GIS layer disposition.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse

import requests

BASE_URL = "https://mlrs.blm.gov"
CASE_PAGE_URL = BASE_URL + "/s/blm-case/{sf_id}/x"
AURA_URL = BASE_URL + "/s/sfsites/aura"
USER_AGENT = "almanac-grid-ingest/2.0"

# At most 2 requests per second, counting page loads.
MIN_INTERVAL_SECONDS = 0.5

ACTION_FIELDS = (
    "Id",
    "Name",
    "Code__c",
    "Action_Name__c",
    "Status__c",
    "Date_Filed__c",
    "Action_Date__c",
    "Action_Decision_Date__c",
    "Effective_Date__c",
)
CUSTOMER_FIELDS = ("Id", "Account__r.Name", "Interest_Relationship__c")

_RELATED_LISTS = {
    "actions": ("Case_Actions__r", "Case_Action__c", ACTION_FIELDS),
    "customers": ("Case_Customers__r", "Case_Customer__c", CUSTOMER_FIELDS),
}
_PAGE_SIZE = 200
_RECORDS_DESCRIPTOR = "aura://RelatedListUiController/ACTION$getRelatedListRecords"
_APP = "siteforce:communityApp"


class MlrsError(RuntimeError):
    """MLRS answered, but not with case records (endpoint changed, case gone)."""


def parse_aura_context(page_html: str) -> tuple[str, str]:
    """(fwuid, app loaded-component id) from a case page's bootstrap script URL,
    where the Aura context is URL-encoded JSON."""
    decoded = urllib.parse.unquote(page_html)
    fwuid = re.search(r'"fwuid"\s*:\s*"([^"]+)"', decoded)
    app = re.search(r'"APPLICATION@markup://siteforce:communityApp"\s*:\s*"([^"]+)"', decoded)
    if not fwuid or not app:
        raise MlrsError("no Aura framework id on the MLRS case page")
    return fwuid.group(1), app.group(1)


def _flatten(fields: dict) -> dict:
    """UI API record fields -> {name: value}, with lookups as "Account__r.Name"."""
    flat = {}
    for name, cell in fields.items():
        value = cell.get("value") if isinstance(cell, dict) else cell
        if isinstance(value, dict) and "fields" in value:
            for sub_name, sub_value in _flatten(value["fields"]).items():
                flat[f"{name}.{sub_name}"] = sub_value
        else:
            flat[name] = value
    return flat


def parse_records_response(body: dict) -> tuple[list[dict], str | None]:
    """(flattened records, next page token) from one getRelatedListRecords
    action result."""
    if body.get("state") != "SUCCESS" or not body.get("returnValue"):
        message = json.dumps(body.get("error"))[:300]
        raise MlrsError(f"MLRS related-list call failed: {message}")
    value = body["returnValue"]
    return [_flatten(record["fields"]) for record in value.get("records", [])], value.get("nextPageToken")


class MlrsClient:
    def __init__(self, session: requests.Session | None = None, min_interval: float = MIN_INTERVAL_SECONDS):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.min_interval = min_interval
        self.requests_made = 0
        self._last_request = 0.0
        self._context: tuple[str, str] | None = None

    def _throttle(self) -> None:
        wait = self._last_request + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        self.requests_made += 1

    def _bootstrap(self, sf_id: str) -> None:
        self._throttle()
        response = self.session.get(CASE_PAGE_URL.format(sf_id=sf_id), timeout=60)
        response.raise_for_status()
        self._context = parse_aura_context(response.text)

    def _post(self, actions: list[dict]) -> list[dict]:
        fwuid, app_id = self._context
        context = {
            "mode": "PROD",
            "fwuid": fwuid,
            "app": _APP,
            "loaded": {f"APPLICATION@markup://{_APP}": app_id},
            "dn": [],
            "globals": {},
            "uad": True,
        }
        self._throttle()
        response = self.session.post(
            AURA_URL,
            params={"r": "1"},
            data={
                "message": json.dumps({"actions": actions}),
                "aura.context": json.dumps(context),
                "aura.pageURI": "/s/",
                "aura.token": "null",
            },
            timeout=60,
        )
        response.raise_for_status()
        text = response.text
        if "aura:clientOutOfSync" in text[:2000] or "aura:invalidSession" in text[:2000]:
            raise _OutOfSync()
        try:
            return json.loads(text)["actions"]
        except (ValueError, KeyError):
            raise MlrsError(f"unexpected MLRS response: {text[:200]!r}") from None

    def _post_with_resync(self, sf_id: str, actions: list[dict]) -> list[dict]:
        if self._context is None:
            self._bootstrap(sf_id)
        for attempt in range(4):
            try:
                return self._post(actions)
            except _OutOfSync:
                # A Salesforce release changed the framework id: re-read it.
                self._bootstrap(sf_id)
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(5 * (attempt + 1))
        raise MlrsError("MLRS kept answering clientOutOfSync")

    def fetch_case(self, sf_id: str, with_customers: bool = False) -> dict:
        """{"actions": [...], "customers": [...]} for one case, each a list of
        flattened records. Customers are only requested when asked for (the
        GIS layers already carry them for rights-of-way)."""
        wanted = ["actions"] + (["customers"] if with_customers else [])
        result: dict[str, list[dict]] = {name: [] for name in wanted}
        tokens: dict[str, str | None] = {name: "0" for name in wanted}
        while tokens:
            actions = []
            for name, token in tokens.items():
                related_list, sobject, fields = _RELATED_LISTS[name]
                params = {
                    "parentRecordId": sf_id,
                    "relatedListId": related_list,
                    "fields": [f"{sobject}.{field}" for field in fields],
                    "pageSize": _PAGE_SIZE,
                }
                if token != "0":
                    params["pageToken"] = token
                actions.append(
                    {"id": f"{name};a", "descriptor": _RECORDS_DESCRIPTOR, "callingDescriptor": "UNKNOWN", "params": params}
                )
            answers = {answer["id"].split(";")[0]: answer for answer in self._post_with_resync(sf_id, actions)}
            next_tokens = {}
            for name in tokens:
                if name not in answers:
                    raise MlrsError(f"MLRS returned no {name} result")
                records, next_token = parse_records_response(answers[name])
                result[name].extend(records)
                if next_token:
                    next_tokens[name] = next_token
            tokens = next_tokens
        return result


class _OutOfSync(Exception):
    pass
