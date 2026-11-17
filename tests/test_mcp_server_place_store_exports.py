import types
import typing
from collections.abc import Callable

import pytest

import agents.mcp_server
from agents.mcp_server import PlaceStore


def _named_types(annotation: object) -> set[object]:
    if isinstance(annotation, typing.TypeAliasType):
        return {annotation, *_named_types(annotation.__value__)}
    arguments = typing.get_args(annotation)
    if arguments:
        # a Callable's parameter types arrive as one list
        flattened = [
            item
            for argument in arguments
            for item in (argument if isinstance(argument, list) else [argument])
        ]
        return {item for argument in flattened for item in _named_types(argument)}
    return {annotation}


def _port_methods() -> list[Callable[..., object]]:
    return [
        getattr(PlaceStore, name)
        for name in dir(PlaceStore)
        if not name.startswith("_") and callable(getattr(PlaceStore, name))
    ]


def _own_types() -> set[object]:
    named: set[object] = set()
    for method in _port_methods():
        for annotation in typing.get_type_hints(method).values():
            named |= _named_types(annotation)
    return {
        item
        for item in named
        if isinstance(item, typing.TypeAliasType)
        or (
            isinstance(item, type)
            and not isinstance(item, types.GenericAlias)
            and item.__module__.startswith("agents.mcp_server")
        )
    }


@pytest.mark.parametrize("named", sorted(_own_types(), key=repr), ids=repr)
def test_every_type_in_the_place_store_port_is_exported(named: object) -> None:
    name = named.__name__  # type: ignore[attr-defined]
    assert getattr(agents.mcp_server, name) is named
