"""How a call to an app's member endpoint fails; the runner decides what each failure means.

The failures and the codes a member client names them by are pure, so a runner's decisions read them without
the client or its libraries.
"""

from __future__ import annotations

from typing import Final

INVALID_ANSWER_CODE: Final = "invalid_answer"
"""The code of an answer that arrived but is not the kind of answer asked for."""
LISTEN_NOT_HONORED_CODE: Final = "listen_not_honored"
"""The code of a stream the app acknowledged without the subscription the runner asked for."""
UNSUPPORTED_VERSION_CODE: Final = "unsupported_version"
"""The code of an app that shares no protocol revision with the runner."""


# The base of the failures below, which name their kind; a suffix would repeat it.
class CallFailure(Exception):  # noqa: N818
    """A call did not give an answer the runner can use; an app's error status says the failed MCP method and answer."""

    method: str | None = None
    answer: str | None = None


class StatusFailure(CallFailure):
    """The server answered with an HTTP status that is not success.

    An app's answer says the MCP method that failed and what it carried: a code of the JSON-RPC error, else its
    content type; never its content.
    """

    def __init__(self, status: int, method: str | None = None, answer: str | None = None) -> None:
        super().__init__(f"HTTP status {status}")
        self.status = status
        self.method = method
        self.answer = answer


class Unreachable(CallFailure):
    """The server was not reached or timed out."""


class UnusableAnswer(CallFailure):
    """The server answered, but with an error or a body the runner cannot use; ``code`` says which."""

    def __init__(self, code: str, method: str | None = None, answer: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.method = method
        self.answer = answer


def status_code(status: int) -> str:
    return f"http_{status}"


def jsonrpc_code(code: int) -> str:
    return f"jsonrpc_{code}"
