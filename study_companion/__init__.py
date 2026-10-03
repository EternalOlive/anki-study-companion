"""PC Anki study companion."""

# Import Anki integration only inside Anki. This keeps the timer testable alone.
try:
    import aqt  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    from . import addon  # noqa: F401
