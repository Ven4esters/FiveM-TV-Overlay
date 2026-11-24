"""FiveM Video Overlay v2 — новая архитектура по модулям.

Правило слоёв: core ← sources ← stream ← app ← ui.
Зависимости идут только вниз. `core` не знает ни о чём.
"""

__version__ = "2.1.0"
APP_NAME = "FiveM Video Overlay"
