"""Errors a 021 login can raise, importable without pulling in the whole adapter."""

from app.broker.zerotwoone.adapter import BrokerAuthFailed as AuthFailed  # noqa: F401
