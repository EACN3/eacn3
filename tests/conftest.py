"""Root conftest: alias eacn3 → eacn so test imports work."""
import sys
import eacn
sys.modules["eacn3"] = eacn
# Also alias sub-packages so 'from eacn3.core...' etc. resolve
for name, mod in list(sys.modules.items()):
    if name.startswith("eacn."):
        sys.modules["eacn3." + name[5:]] = mod
