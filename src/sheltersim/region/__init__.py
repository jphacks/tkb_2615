"""Source-backed municipal research and synthetic single-shelter generation."""

from sheltersim.region.models import RegionResearchRequest, RegionResearchResult
from sheltersim.region.research import RegionResearchService

__all__ = ["RegionResearchRequest", "RegionResearchResult", "RegionResearchService"]
