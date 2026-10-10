"""Who is the caller? This is the one place that answers it.

Every authenticated route receives an `Actor` from `app.auth.deps.current_user`. Code that needs to know who is
acting (a store filtering rows, the audit log, a second approver) takes an `Actor` or its `id`; it never reads a
user id out of a request body, a query string or a header.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    id: str
    email: str
    display_name: str

    @property
    def label(self) -> str:
        """What to show in the interface: the name if there is one, otherwise the email."""
        return self.display_name or self.email


def default_display_name(email: str) -> str:
    """The part of the email before the @, used when the user gave no name."""
    return email.split("@", 1)[0]
