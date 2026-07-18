"""The product composition root (CP-0): typed sub-facades over a company's engines.

``CompanyControlPlane`` is to the product what ``src/company.build()`` is to the engine — the
one place the four engines' facades compose into a product surface. HTTP routers and dashboard
snapshots map onto its sub-facades 1:1; nothing past the plane sees engine internals.
"""

from podium.control._plane import CompanyControlPlane, ControlPlaneProvider

__all__ = [
    "CompanyControlPlane",
    "ControlPlaneProvider",
]
