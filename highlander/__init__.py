"""Hypothesis Highlander.

The production API compares immutable LABrador output packets without calling
producer modules or inventing missing scores. The original quality-diversity
search remains available as an explicitly legacy/demo surface.
"""
from .genome import Genome, AXES, BOLDNESS
from .archive import Archive
from .controller import Highlander
from .thesis import IndicationThesis, Evidence
from .packet_consumer import (
    ADAPTER_DISPATCH,
    REQUEST_SCHEMA_VERSION,
    compare_packet_request,
)
from .packet_contracts import ContractError, ModulePacket, packet_from_dict
from .packet_portfolio import ComparisonPolicy, ObjectiveRule, PortfolioResult
from .ra_demo import (
    RA_DEMO_POLICY_ID,
    RA_DEMO_RESULT_SCHEMA_VERSION,
    compare_ra_demo_snapshot,
)
from . import tiers, generator, adapters, thesis, viz

__all__ = [
    "ADAPTER_DISPATCH",
    "Archive",
    "AXES",
    "BOLDNESS",
    "ComparisonPolicy",
    "ContractError",
    "Evidence",
    "Genome",
    "Highlander",
    "IndicationThesis",
    "ModulePacket",
    "ObjectiveRule",
    "PortfolioResult",
    "RA_DEMO_POLICY_ID",
    "RA_DEMO_RESULT_SCHEMA_VERSION",
    "REQUEST_SCHEMA_VERSION",
    "adapters",
    "compare_packet_request",
    "compare_ra_demo_snapshot",
    "generator",
    "packet_from_dict",
    "thesis",
    "tiers",
    "viz",
]
