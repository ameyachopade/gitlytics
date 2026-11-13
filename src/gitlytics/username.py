"""Backward compatibility module for gitlytics.username.

Re-exports public username lookup and validation functions from gitlytics.core.
"""

from .core import (
    get_public_repos,
    get_public_user,
    validate_github_username,
)

__all__ = [
    "get_public_repos",
    "get_public_user",
    "validate_github_username",
]
