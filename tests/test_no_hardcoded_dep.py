"""
Isolation guarantees (CI-enforced), matching the containerization requirement: this node must never
(a) depend on Claude's local `rnpv_copilot` ROI tool, (b) reach into a sibling LABrador node, or
(c) carry secrets. If any of these regress, the node could break or influence other modules — so we
fail the build here rather than at integration time.
"""
import os
import re

_PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "highlander")
_PY = [os.path.join(_PKG, f) for f in os.listdir(_PKG) if f.endswith(".py")]

# forbidden: the local ROI tool, and any relative reach OUT of this node into a sibling node dir
FORBIDDEN_IMPORT = re.compile(r"\b(rnpv_copilot|roi[_-]agent)\b")
SIBLING_REACH = re.compile(r"(therapeutic-program-economics|research-evidence-mapper|"
                           r"trial-recruitment-forecaster|small-molecule-tractability-review)"
                           r".*(import|open\()")
SECRET = re.compile(r"(sk-ant-[A-Za-z0-9]|gxl_[A-Za-z0-9]|AKIA[0-9A-Z]{16})")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def test_no_dependency_on_local_roi_tool():
    for p in _PY:
        assert not FORBIDDEN_IMPORT.search(_read(p)), f"{os.path.basename(p)} references the local ROI tool"


def test_no_reach_into_sibling_nodes():
    for p in _PY:
        assert not SIBLING_REACH.search(_read(p)), f"{os.path.basename(p)} imports/opens a sibling node"


def test_no_secrets_committed():
    for p in _PY:
        assert not SECRET.search(_read(p)), f"{os.path.basename(p)} contains secret-shaped material"


def test_core_loop_is_stdlib_only():
    """The controller/tiers/archive/genome loop must import no third-party package (numpy/plotly/
    streamlit/anthropic are viz/LLM-only). This is what keeps the container tiny and dependency-light."""
    third_party = re.compile(r"^\s*(import|from)\s+(numpy|pandas|scipy|plotly|streamlit|anthropic)\b",
                             re.MULTILINE)
    for name in ("controller.py", "tiers.py", "archive.py", "genome.py", "adapters.py", "thesis.py"):
        src = _read(os.path.join(_PKG, name))
        assert not third_party.search(src), f"{name} pulled in a third-party dep — keep the core stdlib-only"
