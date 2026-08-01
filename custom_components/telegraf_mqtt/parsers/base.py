"""Parser shared types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from ..models import MetricDescriptor


class PayloadParser(Protocol):
    """Protocol implemented by measurement payload parsers."""

    def __call__(self, payload: Mapping[str, Any]) -> Sequence[MetricDescriptor]:
        """Parse a Telegraf payload into metric descriptors."""
