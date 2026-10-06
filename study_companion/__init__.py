"""PC Anki study companion."""

__version__ = "0.1.3"

# Import Anki integration only inside Anki. This keeps the timer testable alone.
try:
    import aqt  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    from . import addon  # noqa: F401
