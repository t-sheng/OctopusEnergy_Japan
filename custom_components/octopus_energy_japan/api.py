"""Async GraphQL client for the Octopus Energy Japan Kraken API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from .const import GRAPHQL_URL, LOGGER, TOKEN_REFRESH_LEEWAY


class KrakenError(Exception):
    """Generic Kraken API error."""


class KrakenAuthError(KrakenError):
    """Raised when authentication fails or refresh token is rejected."""


class KrakenConnectionError(KrakenError):
    """Raised when the API is unreachable."""


_OBTAIN_TOKEN = """
mutation obtainKrakenToken($input: ObtainJSONWebTokenInput!) {
  obtainKrakenToken(input: $input) {
    token
    refreshToken
    refreshExpiresIn
    payload
  }
}
"""

_VIEWER_ACCOUNTS = """
query viewerAccounts {
  viewer {
    accounts {
      number
    }
  }
}
"""

_SUPPLY_POINT_AGREEMENTS = """
query supplyPointAgreements($accountNumber: String!) {
    account(accountNumber: $accountNumber) {
        properties {
            electricitySupplyPoints {
                spin
                agreements {
                    id
                    validFrom
                    validTo
                    params
                    product {
                        __typename
                        ... on ElectricitySingleStepProduct {
                            code
                            displayName
                            params
                            standingChargeUnitType
                            standingChargePricePerDay
                            standingCharges {
                                band
                                validFrom
                                validTo
                                unitType
                                pricePerUnit
                                pricePerUnitIncTax
                                threshold
                            }
                            consumptionCharges {
                                band
                                validFrom
                                validTo
                                unitType
                                pricePerUnit
                                pricePerUnitIncTax
                                timeOfUse
                            }
                        }
                        ... on ElectricitySteppedProduct {
                            code
                            displayName
                            params
                            standingChargeUnitType
                            standingChargePricePerDay
                            standingCharges {
                                band
                                validFrom
                                validTo
                                unitType
                                pricePerUnit
                                pricePerUnitIncTax
                                threshold
                            }
                            consumptionCharges {
                                band
                                validFrom
                                validTo
                                unitType
                                pricePerUnit
                                pricePerUnitIncTax
                                timeOfUse
                                stepStart
                                stepEnd
                            }
                        }
                        ... on ElectricityFitProduct {
                            code
                            displayName
                        }
                    }
                }
            }
        }
    }
}
"""

_HALF_HOURLY_READINGS = """
query halfHourlyReadings($accountNumber: String!, $fromDatetime: DateTime, $toDatetime: DateTime) {
  account(accountNumber: $accountNumber) {
    properties {
      electricitySupplyPoints {
        halfHourlyReadings(fromDatetime: $fromDatetime, toDatetime: $toDatetime) {
          startAt
          endAt
          value
        }
      }
    }
  }
}
"""

_SUPPLY_POINTS = """
query supplyPoints($accountNumber: String!) {
    supplyPoints(accountNumber: $accountNumber, first: 20) {
        edges {
            node {
                id
                externalIdentifier
                marketName
            }
        }
    }
}
"""

_SUPPLY_POINT_DAILY_READINGS = """
query supplyPointDailyReadings(
    $externalIdentifier: String!
    $marketName: String!
    $startAt: DateTime!
    $endAt: DateTime!
) {
    supplyPoint(externalIdentifier: $externalIdentifier, marketName: $marketName) {
        id
        externalIdentifier
        marketName
        readings(
            startAt: $startAt
            endAt: $endAt
            readingType: INTERVAL
            timeGranularity: DAY
            timezone: "Asia/Tokyo"
            units: [KILOWATT_HOURS]
        ) {
            importReadings(first: 100) {
                totalCount
                edgeCount
                edges {
                    node {
                        value
                        units
                        intervalStart
                        intervalEnd
                    }
                }
            }
        }
    }
}
"""

_INTROSPECT_TYPE = """
query introspectType($name: String!) {
  __type(name: $name) {
    name
    kind
    fields {
      name
      type {
        name
        kind
        ofType { name kind ofType { name kind ofType { name kind } } }
      }
    }
    possibleTypes {
      name
      fields {
        name
        type { name kind ofType { name kind } }
      }
    }
  }
}
"""

# Candidate field names for time-of-use rate schedules on a tariff.
_ACCOUNT_AGREEMENT_FIELD_CANDIDATES = (
    "electricityAgreements",
    "contributionAgreements",
)
_RATE_FIELD_CANDIDATES = (
    "unitRateSchedule",
    "unitRateSchedules",
    "unitRates",
    "timeOfUseRates",
    "rates",
    "timeBands",
    "rateBands",
)
_BASIC_CHARGE_FIELD_CANDIDATES = (
    "basicCharge",
    "basicCharges",
    "standingCharge",
    "monthlyBasicCharge",
)

# Canonical Japan-local TOU windows used by EV tariffs.
_TOU_TIME_RANGES: dict[str, list[tuple[str, str]]] = {
    "DAY": [("11:00", "13:00")],
    "NIGHT": [("01:00", "05:00")],
    "STANDARD": [("00:00", "01:00"), ("05:00", "11:00"), ("13:00", "00:00")],
}


@dataclass
class TokenInfo:
    """Cached Kraken JWT and its metadata."""

    token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None  # absolute UTC time JWT expires
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def is_expiring(self, leeway: timedelta = TOKEN_REFRESH_LEEWAY) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) + leeway >= self.expires_at


@dataclass
class Agreement:
    """A normalized active electricity agreement."""

    agreement_id: str | None
    valid_from: str | None
    valid_to: str | None
    mpan: str | None
    tariff_typename: str | None
    tariff_id: str | None
    tariff_display_name: str | None
    product_code: str | None
    tariff_code: str | None


class KrakenClient:
    """Lightweight async client for the OEJP Kraken GraphQL endpoint.

    Handles JWT acquisition, transparent refresh, and the small set of
    queries the integration needs. The tariff schema isn't publicly
    documented, so rate fetching is built around schema introspection
    plus a defensive fallback to known field-name candidates.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        email: str,
        password: str,
        refresh_token: str | None = None,
        request_timeout: float = 30.0,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._token: TokenInfo | None = None
        if refresh_token:
            self._token = TokenInfo(token="", refresh_token=refresh_token)
        self._auth_lock = asyncio.Lock()
        self._account_agreement_field: str | None = None
        self._account_agreement_field_resolved = False
        self._tariff_field_cache: dict[str, str | None] = {}

    # ---------------------------------------------------------------- auth

    @property
    def refresh_token(self) -> str | None:
        return self._token.refresh_token if self._token else None

    async def _login_with_password(self) -> TokenInfo:
        return await self._obtain_token({"email": self._email, "password": self._password})

    async def _refresh_with_token(self, refresh_token: str) -> TokenInfo:
        return await self._obtain_token({"refreshToken": refresh_token})

    async def _obtain_token(self, variables_input: dict[str, str]) -> TokenInfo:
        data = await self._raw_request(
            _OBTAIN_TOKEN,
            variables={"input": variables_input},
            authenticated=False,
        )
        payload_data = data.get("obtainKrakenToken")
        if not payload_data or not payload_data.get("token"):
            raise KrakenAuthError("Kraken did not return a token")

        token = payload_data["token"]
        refresh_token = payload_data.get("refreshToken")
        payload = payload_data.get("payload") or {}

        expires_at: datetime | None = None
        exp = payload.get("exp") if isinstance(payload, dict) else None
        if isinstance(exp, (int, float)):
            expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)

        info = TokenInfo(
            token=token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            raw_payload=payload if isinstance(payload, dict) else {},
        )
        self._token = info
        return info

    async def ensure_token(self) -> str:
        """Return a valid JWT, refreshing or logging in as needed."""
        async with self._auth_lock:
            if self._token and self._token.token and not self._token.is_expiring():
                return self._token.token

            if self._token and self._token.refresh_token:
                try:
                    info = await self._refresh_with_token(self._token.refresh_token)
                    return info.token
                except KrakenAuthError as err:
                    LOGGER.debug("Refresh-token rejected, falling back to password login: %s", err)

            info = await self._login_with_password()
            return info.token

    # --------------------------------------------------------------- core

    async def _raw_request(
        self,
        query: str,
        *,
        variables: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if authenticated:
            token = await self.ensure_token()
            headers["Authorization"] = f"JWT {token}"

        body = {"query": query}
        if variables is not None:
            body["variables"] = variables

        try:
            async with self._session.post(
                GRAPHQL_URL,
                json=body,
                headers=headers,
                timeout=self._timeout,
            ) as resp:
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise KrakenConnectionError(f"Could not reach Kraken API: {err}") from err

        if "errors" in payload and payload["errors"]:
            messages = [str(e.get("message", e)) for e in payload["errors"]]
            joined = "; ".join(messages)
            if any(_looks_like_auth_error(m) for m in messages):
                raise KrakenAuthError(joined)
            raise KrakenError(joined)

        return payload.get("data") or {}

    async def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run an authenticated GraphQL query, retrying once on auth failure."""
        try:
            return await self._raw_request(query, variables=variables, authenticated=True)
        except KrakenAuthError:
            # Force re-auth and try once more.
            self._token = TokenInfo(token="", refresh_token=None) if self._token else None
            return await self._raw_request(query, variables=variables, authenticated=True)

    # --------------------------------------------------------- public API

    async def authenticate(self) -> TokenInfo:
        """Perform initial authentication. Used by the config flow."""
        return await self._login_with_password()

    async def get_account_number(self) -> str:
        data = await self.gql(_VIEWER_ACCOUNTS)
        viewer = data.get("viewer") or {}
        accounts = viewer.get("accounts") or []
        if not accounts:
            raise KrakenError("No accounts associated with this Octopus Japan login")
        number = accounts[0].get("number")
        if not number:
            raise KrakenError("Account number missing from viewer response")
        return number

    async def resolve_account_agreement_field(self, account_number: str) -> str:
        """Return the Account field that exposes electricity agreements."""
        if self._account_agreement_field_resolved and self._account_agreement_field:
            return self._account_agreement_field

        attempted: list[str] = []

        try:
            type_info = await self.introspect_type("Account")
        except KrakenError as err:
            LOGGER.debug(
                "Account introspection failed (%s); falling back to agreement-field probes",
                err,
            )
            type_info = None

        if type_info and type_info.get("fields"):
            available = {field["name"] for field in type_info["fields"]}
            for candidate in _ACCOUNT_AGREEMENT_FIELD_CANDIDATES:
                attempted.append(candidate)
                if candidate in available:
                    self._account_agreement_field = candidate
                    self._account_agreement_field_resolved = True
                    LOGGER.debug("Resolved account agreement field via introspection: %s", candidate)
                    return candidate

        errors: list[str] = []
        for candidate in _ACCOUNT_AGREEMENT_FIELD_CANDIDATES:
            if candidate not in attempted:
                attempted.append(candidate)
            try:
                await self.gql(
                    _build_active_agreement_query(candidate),
                    {"accountNumber": account_number},
                )
            except KrakenError as err:
                LOGGER.debug("Agreement field probe %s failed: %s", candidate, err)
                errors.append(f"{candidate}: {err}")
                continue

            self._account_agreement_field = candidate
            self._account_agreement_field_resolved = True
            LOGGER.debug("Resolved account agreement field via probe: %s", candidate)
            return candidate

        self._account_agreement_field_resolved = True
        joined_candidates = ", ".join(attempted)
        joined_errors = "; ".join(errors) if errors else "no matching fields discovered"
        raise KrakenError(
            "Could not resolve account agreement field "
            f"(tried: {joined_candidates}). Enable DEBUG logging for schema diagnostics. "
            f"Last errors: {joined_errors}"
        )

    async def get_active_agreement(self, account_number: str) -> Agreement | None:
        data = await self.gql(_SUPPLY_POINT_AGREEMENTS, {"accountNumber": account_number})
        selected = _select_supply_point_agreement(data)
        if selected is None:
            return None
        supply_point, node, product = selected
        return Agreement(
            agreement_id=_string_or_none(node.get("id")),
            valid_from=node.get("validFrom"),
            valid_to=node.get("validTo"),
            mpan=supply_point.get("spin"),
            tariff_typename=product.get("__typename"),
            tariff_id=product.get("code"),
            tariff_display_name=product.get("displayName"),
            product_code=product.get("code"),
            tariff_code=product.get("code"),
        )

    async def introspect_type(self, name: str) -> dict[str, Any] | None:
        data = await self.gql(_INTROSPECT_TYPE, {"name": name})
        return data.get("__type")

    async def discover_tariff_fields(self, tariff_typename: str) -> tuple[str | None, str | None]:
        """Return the (rate_schedule_field, basic_charge_field) names for a tariff type.

        Uses GraphQL introspection on the concrete type and falls back to None
        if the introspection schema is hidden.
        """
        if tariff_typename in self._tariff_field_cache:
            cached = self._tariff_field_cache[tariff_typename]
            return cached, self._tariff_field_cache.get(f"{tariff_typename}__basic")

        rate_field: str | None = None
        basic_field: str | None = None
        try:
            type_info = await self.introspect_type(tariff_typename)
        except KrakenError as err:
            LOGGER.debug("Tariff introspection failed (%s); will try field-name fallbacks", err)
            type_info = None

        if type_info and type_info.get("fields"):
            available = {f["name"] for f in type_info["fields"]}
            for candidate in _RATE_FIELD_CANDIDATES:
                if candidate in available:
                    rate_field = candidate
                    break
            for candidate in _BASIC_CHARGE_FIELD_CANDIDATES:
                if candidate in available:
                    basic_field = candidate
                    break

        self._tariff_field_cache[tariff_typename] = rate_field
        self._tariff_field_cache[f"{tariff_typename}__basic"] = basic_field
        LOGGER.debug(
            "Resolved tariff fields for %s: rate=%s basic=%s",
            tariff_typename,
            rate_field,
            basic_field,
        )
        return rate_field, basic_field

    async def fetch_tariff_rates(
        self,
        account_number: str,
        agreement: Agreement,
    ) -> dict[str, Any]:
        """Fetch the rate breakdown for an active agreement.

        Returns a dict with keys: rates (list), basic_charge (dict|None), raw (dict).
        Each rate has: slug, label, value, unit, time_ranges, valid_from, valid_to.
        """
        if not agreement.tariff_typename:
            return {"rates": [], "basic_charge": None, "raw": {}}

        data = await self.gql(_SUPPLY_POINT_AGREEMENTS, {"accountNumber": account_number})
        selected = _select_supply_point_agreement(data)
        if selected is None:
            return {"rates": [], "basic_charge": None, "raw": {}}

        _, node, product = selected
        tariff_data = _parse_supply_point_tariff(node, product)
        if not tariff_data["rates"] and tariff_data["basic_charge"] is None:
            LOGGER.warning(
                "Could not resolve rate data for product type %s; no rate sensors will be created",
                agreement.tariff_typename,
            )
        return tariff_data

    async def _probe_rate_field(
        self,
        account_number: str,
        agreement: Agreement,
        agreement_field: str,
    ) -> str | None:
        for candidate in _RATE_FIELD_CANDIDATES:
            query = _build_tariff_rate_query(
                agreement.tariff_typename,
                agreement_field,
                candidate,
                None,
            )
            try:
                await self.gql(query, {"accountNumber": account_number})
            except KrakenError as err:
                LOGGER.debug("Rate field probe %s failed: %s", candidate, err)
                continue
            self._tariff_field_cache[agreement.tariff_typename] = candidate
            LOGGER.debug("Rate field probe succeeded with %s", candidate)
            return candidate
        return None

    async def get_half_hourly_readings(
        self,
        account_number: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[dict[str, Any]]:
        data = await self.gql(
            _HALF_HOURLY_READINGS,
            {
                "accountNumber": account_number,
                "fromDatetime": from_dt.isoformat(),
                "toDatetime": to_dt.isoformat(),
            },
        )
        readings: list[dict[str, Any]] = []
        for prop in (data.get("account") or {}).get("properties") or []:
            for sp in prop.get("electricitySupplyPoints") or []:
                for r in sp.get("halfHourlyReadings") or []:
                    value = r.get("value")
                    try:
                        value_f = float(value) if value is not None else None
                    except (TypeError, ValueError):
                        value_f = None
                    readings.append(
                        {
                            "start_at": r.get("startAt"),
                            "end_at": r.get("endAt"),
                            "value": value_f,
                        }
                    )
        readings.sort(key=lambda r: r["start_at"] or "")
        return readings

    async def get_daily_consumption_readings(self, account_number: str) -> list[dict[str, Any]]:
        supply_points = await self.gql(_SUPPLY_POINTS, {"accountNumber": account_number})
        edges = (supply_points.get("supplyPoints") or {}).get("edges") or []
        if not edges:
            return []

        node = (edges[0] or {}).get("node") or {}
        external_identifier = node.get("externalIdentifier")
        market_name = node.get("marketName")
        if not external_identifier or not market_name:
            return []

        now = datetime.now(timezone.utc)
        start = now - timedelta(days=7)
        data = await self.gql(
            _SUPPLY_POINT_DAILY_READINGS,
            {
                "externalIdentifier": external_identifier,
                "marketName": market_name,
                "startAt": start.isoformat(),
                "endAt": now.isoformat(),
            },
        )

        readings: list[dict[str, Any]] = []
        supply_point = data.get("supplyPoint") or {}
        import_readings = (supply_point.get("readings") or {}).get("importReadings") or {}
        for reading in import_readings.get("edges") or []:
            node = (reading or {}).get("node") or {}
            value = node.get("value")
            try:
                value_f = float(value) if value is not None else None
            except (TypeError, ValueError):
                value_f = None
            readings.append(
                {
                    "start_at": node.get("intervalStart"),
                    "end_at": node.get("intervalEnd"),
                    "value": value_f,
                }
            )

        readings.sort(key=lambda r: r["start_at"] or "", reverse=True)
        return readings


# --------------------------------------------------------------- helpers


def _looks_like_auth_error(message: str) -> bool:
    lowered = message.lower()
    return (
        "kt-ct-1135" in lowered
        or "kt-ct-1139" in lowered
        or "signature has expired" in lowered
        or "decoding signature" in lowered
        or "invalid token" in lowered
        or "unauthor" in lowered
    )


def _build_tariff_rate_query(
    tariff_typename: str,
    agreement_field: str,
    rate_field: str | None,
    basic_field: str | None,
) -> str:
    """Compose a GraphQL query that pulls rate + basic charge data for a tariff."""
    parts: list[str] = ["__typename", "id", "displayName", "productCode", "tariffCode"]

    if rate_field:
        parts.append(
            rate_field + " {\n"
            "          name\n"
            "          displayName\n"
            "          timeRanges { start end }\n"
            "          unitRate { unit value validFrom validTo }\n"
            "          unit\n"
            "          value\n"
            "          validFrom\n"
            "          validTo\n"
            "        }"
        )

    if basic_field:
        parts.append(
            basic_field + " {\n"
            "          unit\n"
            "          value\n"
            "          validFrom\n"
            "          validTo\n"
            "        }"
        )

    inline_fragment = "\n          ".join(parts)
    return (
        "query tariffRates($accountNumber: String!) {\n"
        "  account(accountNumber: $accountNumber) {\n"
        f"    {agreement_field}(active: true) {{\n"
        "      tariff {\n"
        "        ... on " + tariff_typename + " {\n"
        "          " + inline_fragment + "\n"
        "        }\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


def _build_active_agreement_query(agreement_field: str) -> str:
    return (
        "query activeAgreement($accountNumber: String!) {\n"
        "  account(accountNumber: $accountNumber) {\n"
        "    number\n"
        f"    {agreement_field}(active: true) {{\n"
        "      id\n"
        "      validFrom\n"
        "      validTo\n"
        "      meterPoint {\n"
        "        mpan\n"
        "      }\n"
        "      tariff {\n"
        "        __typename\n"
        "        id\n"
        "        displayName\n"
        "        productCode\n"
        "        tariffCode\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


def _slugify(label: str) -> str:
    out = []
    for ch in label.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_", "/"):
            out.append("_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "rate"


def _parse_rate_entries(raw: Any) -> list[dict[str, Any]]:
    """Normalize a tariff's rate-schedule field into a list of rate dicts.

    Handles three shapes the API has been observed (or assumed) to use:
      - list of {name, timeRanges, unitRate{value,unit}, validFrom, validTo}
      - list of {displayName, value, unit, validFrom, validTo}
      - dict (single flat rate) with {value, unit}
    """
    if raw is None:
        return []

    if isinstance(raw, dict):
        # Single flat rate (e.g., StandardTariff with one unitRate)
        value = raw.get("value")
        unit = raw.get("unit") or "JPY/kWh"
        if value is None:
            return []
        label = raw.get("displayName") or raw.get("name") or "Unit Rate"
        return [
            {
                "slug": _slugify(label),
                "label": label,
                "value": _to_float(value),
                "unit": unit,
                "time_ranges": [],
                "valid_from": raw.get("validFrom"),
                "valid_to": raw.get("validTo"),
            }
        ]

    if not isinstance(raw, list):
        return []

    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        unit_rate = entry.get("unitRate") or {}
        value = entry.get("value", unit_rate.get("value") if isinstance(unit_rate, dict) else None)
        unit = entry.get("unit") or (unit_rate.get("unit") if isinstance(unit_rate, dict) else None) or "JPY/kWh"
        label = entry.get("displayName") or entry.get("name") or "Unit Rate"
        time_ranges = []
        for tr in entry.get("timeRanges") or []:
            if isinstance(tr, dict):
                time_ranges.append((tr.get("start"), tr.get("end")))
        slug = _slugify(label)
        # Disambiguate duplicate labels.
        suffix = 2
        unique = slug
        while unique in seen:
            unique = f"{slug}_{suffix}"
            suffix += 1
        seen.add(unique)
        parsed.append(
            {
                "slug": unique,
                "label": label,
                "value": _to_float(value),
                "unit": unit,
                "time_ranges": time_ranges,
                "valid_from": entry.get("validFrom") or (unit_rate.get("validFrom") if isinstance(unit_rate, dict) else None),
                "valid_to": entry.get("validTo") or (unit_rate.get("validTo") if isinstance(unit_rate, dict) else None),
            }
        )
    return parsed


def _parse_basic_charge(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, dict):
        return None
    value = _to_float(raw.get("value"))
    if value is None:
        return None
    return {
        "value": value,
        "unit": raw.get("unit") or "JPY/month",
        "valid_from": raw.get("validFrom"),
        "valid_to": raw.get("validTo"),
    }


def _select_supply_point_agreement(
    data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    candidates: list[tuple[bool, bool, datetime, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    now = datetime.now(timezone.utc)
    minimum = datetime.min.replace(tzinfo=timezone.utc)

    for prop in (data.get("account") or {}).get("properties") or []:
        for supply_point in prop.get("electricitySupplyPoints") or []:
            for agreement in supply_point.get("agreements") or []:
                product = agreement.get("product") or {}
                if not product:
                    continue
                valid_from = _parse_datetime(agreement.get("validFrom")) or minimum
                valid_to = _parse_datetime(agreement.get("validTo"))
                is_current = valid_from <= now and (valid_to is None or now < valid_to)
                open_ended = valid_to is None
                candidates.append(
                    (is_current, open_ended, valid_from, supply_point, agreement, product)
                )

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    _, _, _, supply_point, agreement, product = candidates[0]
    return supply_point, agreement, product


def _parse_supply_point_tariff(
    agreement: dict[str, Any],
    product: dict[str, Any],
) -> dict[str, Any]:
    rates = _parse_supply_point_rates(product.get("consumptionCharges"))
    basic_charge = _parse_supply_point_basic_charge(
        agreement,
        product,
        product.get("standingCharges"),
    )
    return {
        "rates": rates,
        "basic_charge": basic_charge,
        "raw": {
            "agreement": agreement,
            "product": product,
        },
    }


def _parse_supply_point_rates(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []

    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        value = _to_float(entry.get("pricePerUnitIncTax"))
        if value is None:
            value = _to_float(entry.get("pricePerUnit"))
        if value is None:
            continue

        label_source = entry.get("timeOfUse") or entry.get("band") or "Unit Rate"
        label = str(label_source).replace("_", " ").title()
        slug = _slugify(str(entry.get("timeOfUse") or entry.get("band") or label))
        time_of_use = _string_or_none(entry.get("timeOfUse"))
        time_ranges = _time_ranges_for_time_of_use(time_of_use)
        suffix = 2
        unique = slug
        while unique in seen:
            unique = f"{slug}_{suffix}"
            suffix += 1
        seen.add(unique)

        parsed.append(
            {
                "slug": unique,
                "label": label,
                "value": value,
                "unit": "JPY/kWh",
                "time_ranges": time_ranges,
                "valid_from": entry.get("validFrom"),
                "valid_to": entry.get("validTo"),
            }
        )
    return parsed


def _time_ranges_for_time_of_use(value: str | None) -> list[tuple[str, str]]:
    if not value:
        return []

    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    for key, ranges in _TOU_TIME_RANGES.items():
        if key in normalized:
            return ranges

    LOGGER.debug("No known TOU time-window mapping for label: %s", value)
    return []


def _parse_supply_point_basic_charge(
    agreement: dict[str, Any],
    product: dict[str, Any],
    standing_charges: Any,
) -> dict[str, Any] | None:
    standing_entries = standing_charges if isinstance(standing_charges, list) else []
    current_entry = None
    for entry in standing_entries:
        if not isinstance(entry, dict):
            continue
        current_entry = entry
        if _is_current_window(entry.get("validFrom"), entry.get("validTo")):
            break

    value = _to_float(product.get("standingChargePricePerDay"))
    if value is not None:
        return {
            "value": value,
            "unit": "JPY/day",
            "valid_from": (current_entry or {}).get("validFrom") or agreement.get("validFrom"),
            "valid_to": (current_entry or {}).get("validTo") or agreement.get("validTo"),
        }

    if current_entry is None:
        return None

    value = _to_float(current_entry.get("pricePerUnitIncTax"))
    if value is None:
        value = _to_float(current_entry.get("pricePerUnit"))
    if value is None:
        return None

    unit_type = str(current_entry.get("unitType") or "")
    unit = "JPY/day" if "DAY" in unit_type else "JPY"
    return {
        "value": value,
        "unit": unit,
        "valid_from": current_entry.get("validFrom") or agreement.get("validFrom"),
        "valid_to": current_entry.get("validTo") or agreement.get("validTo"),
    }


def _is_current_window(valid_from: Any, valid_to: Any) -> bool:
    now = datetime.now(timezone.utc)
    start = _parse_datetime(valid_from)
    end = _parse_datetime(valid_to)
    return (start is None or start <= now) and (end is None or now < end)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
