"""LiteLLM's ``/model/info`` answer and a model's ``ModelPrices``: the one place of its key names.

``model_prices_in`` reads an answer; ``model_info_of`` writes an entry as the gateway does, for
``FakeGateway``. A structure the answer should have and lacks raises ``ValueError``,
``KeyError`` or ``TypeError``, which the backend reports as a malformed reply.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from enum import StrEnum
from typing import Final

from pydantic import JsonValue

from agents.tools import JsonObject
from agents.turn import ModelPrices, PriceStep

_WINDOW_KEY: Final = "max_input_tokens"
_PRICE_KEYS: Final = {
    "input_per_token": "input_cost_per_token",
    "output_per_token": "output_cost_per_token",
    "cache_read_per_token": "cache_read_input_token_cost",
    "cache_write_5m_per_token": "cache_creation_input_token_cost",
    "cache_write_1h_per_token": "cache_creation_input_token_cost_above_1hr",
}
_BASE_PRICES: Final = tuple(_PRICE_KEYS)[:4]
# The gateway writes a missing input or output price as 0 and a missing cache price as null.
_WRITTEN_ZERO_WHEN_MISSING: Final = frozenset({"input_per_token", "output_per_token"})
_STEP_KEY: Final = re.compile(
    rf"(?:{'|'.join(map(re.escape, _PRICE_KEYS.values()))})_above_(?P<thousands>\d+)k_tokens"
)
_UNSET_STEPS: Final = (128, 200, 272, 512)
_THOUSAND: Final = 1000


class ModelPricesFailure(StrEnum):
    MODEL_NOT_LISTED = "model_not_listed"
    ENTRIES_DIFFER = "entries_differ"
    BASE_PRICE_MISSING = "base_price_missing"


class ModelPricesError(Exception):
    """The answer is well formed and gives no one usable price record for the model."""

    def __init__(self, reason: ModelPricesFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason


def model_prices_in(document: JsonValue, model: str) -> ModelPrices:
    """The prices of the model every entry of that name agrees on."""
    if not isinstance(document, dict) or not isinstance(entries := document["data"], list):
        raise TypeError("the answer holds no list of entries")
    infos = [
        entry["model_info"]
        for entry in entries
        if isinstance(entry, dict) and entry.get("model_name") == model
    ]
    if not infos:
        raise ModelPricesError(ModelPricesFailure.MODEL_NOT_LISTED)
    read = [_prices_of(info) for info in infos]
    if any(prices != read[0] for prices in read):
        raise ModelPricesError(ModelPricesFailure.ENTRIES_DIFFER)
    return read[0]


def _prices_of(info: JsonValue) -> ModelPrices:
    if not isinstance(info, dict):
        raise TypeError("an entry's model_info is not an object")
    base = {name: info.get(_PRICE_KEYS[name]) for name in _PRICE_KEYS}
    if any(not base[name] for name in _BASE_PRICES):
        raise ModelPricesError(ModelPricesFailure.BASE_PRICE_MISSING)
    return ModelPrices.model_validate(
        {
            "context_window_tokens": info[_WINDOW_KEY],
            **base,
            "steps": _steps_of(info),
        }
    )


def _steps_of(info: Mapping[str, JsonValue]) -> tuple[PriceStep, ...]:
    """The thresholds the keys name, each with its prices, those with no price left out."""
    thousands = sorted(
        {int(found["thousands"]) for key in info if (found := _STEP_KEY.fullmatch(key))}
    )
    steps = (_step_at(info, count) for count in thousands)
    return tuple(
        step for step in steps if any(getattr(step, name) is not None for name in _PRICE_KEYS)
    )


def _step_at(info: Mapping[str, JsonValue], thousands: int) -> PriceStep:
    prices = {
        name: info.get(f"{key}_above_{thousands}k_tokens") for name, key in _PRICE_KEYS.items()
    }
    return PriceStep.model_validate({"above_prompt_tokens": thousands * _THOUSAND, **prices})


def model_info_of(
    prices: ModelPrices, unpriced: Collection[str] = (), absent: Collection[str] = ()
) -> JsonObject:
    """An entry's ``model_info`` as the gateway writes it, other tiers and unset steps included.

    Each name in ``unpriced`` is a price field the entry has no price for. Each name in
    ``absent``, a price field or ``context_window_tokens``, is one whose key the entry leaves out.
    """
    info: JsonObject = {_WINDOW_KEY: prices.context_window_tokens}
    for name, key in _PRICE_KEYS.items():
        price: float | None = None if name in unpriced else getattr(prices, name)
        if price is None and name in _WRITTEN_ZERO_WHEN_MISSING:
            price = 0
        info[key] = price
        info[f"{key}_batches"] = None if price is None else price / 2
        for count in _UNSET_STEPS:
            info[f"{key}_above_{count}k_tokens"] = None
        for step in prices.steps:
            info[f"{key}_above_{step.above_prompt_tokens // _THOUSAND}k_tokens"] = getattr(
                step, name
            )
    for name in absent:
        del info[_WINDOW_KEY if name == "context_window_tokens" else _PRICE_KEYS[name]]
    return info
