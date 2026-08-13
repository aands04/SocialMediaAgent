"""Tenant-scoped creative preference learning.

The package deliberately exposes structured, fail-closed persistence services
and a fail-open prompt supplement.  It never exposes protected prompts to club
users and it never learns across club boundaries.
"""

from app.creative.director import CreativeDirective, build_creative_directive
from app.creative.feedback import record_feedback, safe_record_feedback

__all__ = [
    "CreativeDirective",
    "build_creative_directive",
    "record_feedback",
    "safe_record_feedback",
]
