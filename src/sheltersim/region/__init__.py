"""Source-backed municipal research and synthetic single-shelter generation."""

from sheltersim.region.models import RegionResearchResult, RegionResearchRequest
from sheltersim.region.research import RegionResearchService

__all__ = ["RegionResearchRequest", "RegionResearchResult", "RegionResearchService"]
