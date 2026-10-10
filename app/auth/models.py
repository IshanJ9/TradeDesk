from datetime import datetime

from pydantic import Field

from app.schemas import Model


class RegisterRequest(Model):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)
    display_name: str = Field(default="", max_length=80)
    accepts_no_advice: bool = False


class LoginRequest(Model):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class ChangePasswordRequest(Model):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class Me(Model):
    id: str
    email: str
    display_name: str
    created_at: datetime | None = None


class Session(Model):
    """Returned after register, login, change-password and on every page load. The CSRF token is sent back in the
    X-CSRF-Token header on every request that changes something; it is not a credential on its own."""

    user: Me
    csrf_token: str
