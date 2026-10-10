"""Password hashing (argon2id) and the password rules. Nothing here stores, logs or returns a password."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 10
MAX_LENGTH = 256  # bounds the work an attacker can make us do per request

_params = {"time_cost": 3, "memory_cost": 65536, "parallelism": 4}  # argon2-cffi / RFC 9106 low-memory defaults
_hasher = PasswordHasher(**_params)


def configure(*, time_cost: int, memory_cost: int, parallelism: int) -> None:
    """Change the work factor (tests use a cheap one). Existing hashes still verify; they are upgraded at next login."""
    global _hasher
    _params.update(time_cost=time_cost, memory_cost=memory_cost, parallelism=parallelism)
    _hasher = PasswordHasher(**_params)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def password_problem(password: str, email: str) -> str | None:
    """Why this password is not acceptable, or None. Length and not-the-email only: no composition rules."""
    if len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters."
    if len(password) > MAX_LENGTH:
        return f"Use at most {MAX_LENGTH} characters."
    if password.strip().lower() == email.strip().lower():
        return "The password can't be the same as your email."
    return None


# A hash of a random string, verified against when the email is unknown so that "no such user" and "wrong
# password" take about the same time.
_DUMMY = None


def dummy_verify(password: str) -> None:
    global _DUMMY
    if _DUMMY is None or needs_rehash(_DUMMY):
        _DUMMY = hash_password("not-a-real-password-" + "x" * 16)
    verify_password(_DUMMY, password)
