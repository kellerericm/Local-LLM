"""Generic jobs: the model writes the whole plan (design §6.3)."""
from . import register
from .base import Template

GENERIC = register(Template(
    name="generic",
    label="General task",
    description="The agent plans the work itself; you approve the plan before it runs.",
))
