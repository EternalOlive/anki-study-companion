"""Confetti celebration module powered by canvas-confetti.

Fires a lightweight, high-performance particle effect in Anki's main webview
when a user finishes a 10-minute slot in 1st place (solo or tied).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    from aqt import mw
    from aqt.utils import tooltip
except ImportError:
    mw = None
    tooltip = lambda *args, **kwargs: None


_CONFETTI_JS_CACHE: str | None = None


def get_confetti_script() -> str:
    """Load the minified canvas-confetti script."""
    global _CONFETTI_JS_CACHE
    if _CONFETTI_JS_CACHE is None:
        path = Path(__file__).parent / "confetti.js"
        if path.exists():
            try:
                _CONFETTI_JS_CACHE = path.read_text(encoding="utf-8")
            except Exception:
                _CONFETTI_JS_CACHE = ""
        else:
            _CONFETTI_JS_CACHE = ""
    return _CONFETTI_JS_CACHE


def get_fire_js(particle_count: int = 40) -> str:
    """Generate JavaScript to ensure canvas-confetti is loaded and fire."""
    confetti_lib = get_confetti_script()
    return f"""(function() {{
  function _fire() {{
    if (typeof window.confetti === 'function') {{
      window.confetti({{
        particleCount: {particle_count},
        spread: 60,
        startVelocity: 28,
        ticks: 130,
        origin: {{ y: 0.7 }},
        colors: ['#34C759', '#FF9500', '#AF52DE', '#FF2D55', '#30B0C7']
      }});
    }}
  }}
  if (typeof window.confetti === 'function') {{
    _fire();
  }} else {{
    try {{
      {confetti_lib}
      _fire();
    }} catch (e) {{}}
  }}
}})();"""


def trigger_confetti(
    controller: Any = None,
    *,
    is_tie: bool = False,
    answers: int | None = None,
    custom_message: str | None = None,
) -> bool:
    """Trigger celebratory confetti in Anki's main webview and show tooltip."""
    web = getattr(mw, "web", None)
    if web is not None and hasattr(web, "eval"):
        try:
            web.eval(get_fire_js())
        except Exception:
            pass

    # Message tooltip
    if custom_message:
        msg = custom_message
    elif controller is not None and hasattr(controller, "t"):
        if is_tie:
            msg = controller.t(
                f"10분 구간 공동 1등 달성! ({answers}회)" if answers else "10분 구간 공동 1등 달성!",
                f"Tied for 1st place in the 10-minute bin! ({answers} cards)" if answers else "Tied for 1st place in the 10-minute bin!",
            )
        else:
            msg = controller.t(
                f"10분 구간 1등 달성! ({answers}회)" if answers else "10분 구간 1등 달성!",
                f"1st place in the 10-minute bin! ({answers} cards)" if answers else "1st place in the 10-minute bin!",
            )
    else:
        msg = "10분 구간 1등 달성!"

    try:
        tooltip(msg, period=3500)
    except Exception:
        pass
    return True
