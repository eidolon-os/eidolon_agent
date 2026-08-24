"""The credential every route on this app requires.

This surface is where a Host's whole conversational history lives: what someone
said to their Eidolon, what it said back, what it was asked to do and whether it
did. Until this module it was **unauthenticated** — anything that could reach
loopback could read every Owner's turns, cancel any task, revoke any Owner's
sessions, and hard-delete an Owner's runtime data, with no credential at all. The
only boundary was ``bind = "loopback"``, which is a boundary against the network
and not against the other things running on the same Host.

The guard is declared on each **router** rather than on the app that mounts them,
and the difference is not cosmetic: the routers here are mounted by the app
factory in production and by bare ``FastAPI()`` instances in most of the tests.
A guard on the factory would leave every one of those tests exercising an
unguarded surface — which is to say, not exercising the thing that ships. Carried
on the router, the credential travels with the routes wherever they are mounted,
and :mod:`test_admin_authority` asserts it by walking the app's own route table
rather than a list someone keeps up to date.

Nothing needs to be exempted this way. The OpenAPI document and the docs page
belong to the app rather than to these routers, so they answer without a
credential by construction — which is what we want: they describe the shape of
the surface rather than anyone's data, and a consumer that cannot read the schema
cannot check that its client still matches it.

Liveness is **not** exempt, and that is a change: the service registry used to
prove this app was alive by reading a conversation turn. A probe that has to read
someone's words to find out whether a process is up was always the wrong shape;
the health app on the other port answers ``/readyz`` without touching data.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Depends, HTTPException, Request, status

#: Where the credential comes from. The value never appears in settings YAML:
#: the same discipline every other secret here follows — the file names the
#: variable, the environment holds the secret.
SERVICE_TOKEN_ENV = "EIDOLON_AGENT_ADMIN_API_TOKEN"

def expected_token() -> str:
    return (os.environ.get(SERVICE_TOKEN_ENV) or "").strip()


def presented_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    scheme, separator, token = header.partition(" ")
    if separator != " " or scheme.lower() != "bearer":
        return ""
    return token.strip()


async def require_service_token(request: Request) -> None:
    """Refuse anything that does not carry this Host's Agent credential.

    ``503`` when the Host has none configured, ``401`` when the caller's does not
    match. The two are different facts and a caller can act on each: the first is
    an incomplete deployment, the second is a wrong client. Collapsing them into
    one answer is how "the credential was never set" gets read for weeks as "my
    token is wrong".

    Compared with :func:`hmac.compare_digest` because this is a secret, and the
    early-return comparison an ``==`` compiles to leaks its length by timing.
    """

    configured = expected_token()
    if not configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Agent admin API credential is not configured ({SERVICE_TOKEN_ENV})",
        )
    if not hmac.compare_digest(presented_token(request), configured):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Agent admin API credential is missing or wrong",
        )


#: What every router on this surface is built with. One object rather than five
#: copies of the same ``Depends`` call, so "this surface requires a credential"
#: is one decision that a reader can find.
AUTHORITY_DEPENDENCIES = [Depends(require_service_token)]
