"""FixPilot — a phone-first, autonomous software maintenance & debugging agent.

FixPilot turns a bug report (voice, text, log, or screenshot) into a verified,
reviewable patch without leaving the developer's smartphone:

    "I found a bug" -> "I understand why" -> "I fixed it" -> "I verified it"

The package is intentionally dependency-free (Python 3.10+ standard library
only) so that it can run anywhere — a dev laptop, a CI runner, or the machine
paired with the phone through iQOO Office Kit.
"""

__version__ = "0.4.0"
__all__ = ["__version__"]

PRODUCT = "FixPilot"
TAGLINE = "Bug reports in. Verified patches out. From your phone."
