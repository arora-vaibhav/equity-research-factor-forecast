"""Methodology layer — pure-function implementations of academic signals.

Each module here implements a single paper / signal as a pure function,
unit-testable in isolation. Versioned via a module-level
`{SIGNAL}_VERSION` constant stamped onto every produced output row, so
the persisted classifier output is auditable and recalibratable.
"""
