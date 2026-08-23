"""The one place every domain's models are imported so they attach to `Base.metadata`.

Alembic reads `Base.metadata` (via env.py); a table only appears there once its module is imported.
Register each new domain here — nowhere else — so migrations and autogenerate never miss a table.
"""

from __future__ import annotations

from podium.auth import _models as auth  # noqa: F401
from podium.companies import models as companies  # noqa: F401
from podium.conductor import models as conductor  # noqa: F401
from podium.events import models as events  # noqa: F401
from podium.runs import models as runs  # noqa: F401
from podium.timeline import models as timeline  # noqa: F401
from podium.users import models as users  # noqa: F401
from podium.workspaces import models as workspaces  # noqa: F401
