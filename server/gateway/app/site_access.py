"""Load separately administered employee allowlists. An absent group grants no access."""
import os
import re

def configured_staff_group(name):
    raw = os.getenv(name, "")
    return frozenset(value.strip() for value in raw.split(",") if re.fullmatch(r"M[0-9]{8}", value.strip()))
