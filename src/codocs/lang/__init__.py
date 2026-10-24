"""language adapters: Go (golang/*), Python, Java, C and C++ (tree-sitter) and COBOL.

importing this package never imports tree-sitter, the non-Go adapters or the loop modules.
"""

from __future__ import annotations

from codocs.lang.base import ALL_LANGS, LANG_ORDER, LangTarget, Unit
from codocs.lang.registry import adapter, adapter_for, detect_languages

__all__ = ["LANG_ORDER", "ALL_LANGS", "adapter", "adapter_for", "detect_languages", "Unit", "LangTarget"]
