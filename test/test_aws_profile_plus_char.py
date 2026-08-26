"""Regression test for issue #6042: AWS profile names containing '+'.

AWS IAM Identity Center (SSO) commonly generates profile names that encode
``<permission-set>+<account-alias>`` (e.g. ``AdminAccess+dev``). The ``+``
character is safe: profiles are passed only as ``aws`` CLI argv elements and
``+`` is neither a shell metacharacter nor able to start an option flag.

Both the authoritative validation (``instances/validation.py``) and the early-
reject guard (``instances/registry.py``) must accept ``+`` in profile names.
"""
import re

import pytest

from kiro_crew.instances.validation import (
    SsmValidationError,
    validate_aws_profile,
)


class TestAwsProfilePlusChar:
    """Ensure '+' is accepted in AWS profile names (issue #6042)."""

    def test_sso_derived_profile_accepted(self):
        """AdminAccess+dev is a real-world SSO profile name."""
        assert validate_aws_profile("AdminAccess+dev") == "AdminAccess+dev"

    def test_plus_in_various_positions(self):
        """'+' anywhere in the name (not leading '-') is valid."""
        assert validate_aws_profile("a+b") == "a+b"
        assert validate_aws_profile("prod+admin") == "prod+admin"
        assert validate_aws_profile("my.org+role_name") == "my.org+role_name"

    def test_leading_dash_still_rejected(self):
        """Option-injection guard must still block leading '-'."""
        with pytest.raises(SsmValidationError):
            validate_aws_profile("-badprofile")
        with pytest.raises(SsmValidationError):
            validate_aws_profile("-oProxyCommand=x")

    def test_shell_metacharacters_still_rejected(self):
        """Shell-unsafe characters must remain blocked."""
        for bad in ("profile;rm", "a|b", "a&b", "$(cmd)", "a`cmd`b"):
            with pytest.raises(SsmValidationError):
                validate_aws_profile(bad)

    def test_registry_regex_accepts_plus(self):
        """The early-reject regex in registry.py must also allow '+'."""
        from kiro_crew.instances.registry import _AWS_PROFILE_RE

        assert _AWS_PROFILE_RE.match("AdminAccess+dev")
        assert _AWS_PROFILE_RE.match("a+b+c")
        # Still rejects metacharacters
        assert not _AWS_PROFILE_RE.match("a;b")
        assert not _AWS_PROFILE_RE.match("a|b")
        assert not _AWS_PROFILE_RE.match("a&b")
