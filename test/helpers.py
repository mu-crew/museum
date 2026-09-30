"""Load the scripts, which have no .py suffix, as modules."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
SCRIPTS = ROOT / "skills" / "museum" / "scripts"


def load(name: str, where: Path = BIN) -> ModuleType:
    path = where / name
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module
