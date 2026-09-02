"""Resolve the running account to an authorization principal (FR-023, AD-018).

Until #28 supplies an authenticated web session, the only identity source is
the Windows account the process runs as, and the only role source is the
``ESL_OPERATOR_ROLES`` setting. This module joins the two so a CLI path
carries the same authorization and audit as the future web path (FR-029):
the principal's name is the account name, and its roles are whatever the
configuration assigns to that name, which may be nothing.
"""

from collections.abc import Callable

from esl_service.config import Settings, build_role_assignments
from esl_service.domain.authorization import Principal, principal_for
from esl_service.runtime.identity import current_user_name


def current_principal(
    settings: Settings, *, account_name: Callable[[], str] = current_user_name
) -> Principal:
    """Return the principal for the running account under configured roles.

    ``account_name`` is injectable so tests never depend on the Windows API.
    A blank name is refused: an unidentifiable caller cannot be audited.
    """

    identity = account_name().strip()
    if not identity:
        raise ValueError("the running account has no identity to authorize")
    return principal_for(identity, build_role_assignments(settings))
