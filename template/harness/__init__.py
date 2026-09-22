"""Web app test harness. The CLI carries every side effect; the skills carry the procedure."""


class HarnessError(Exception):
    """A refusal or failure the operator must act on. The message says what to fix."""
