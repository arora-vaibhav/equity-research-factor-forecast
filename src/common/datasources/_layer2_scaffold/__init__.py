"""Layer-2 source scaffolds.

These classes exist to validate that the BaseDataSource interface
accommodates news / events / sentiment data shapes BEFORE Layer 2 is
brainstormed and built. They MUST NOT be registered in
config/datasources.yaml's enabled_sources.

All fetch_* methods raise NotImplementedError. Implementations land in
the Layer 2 design phase.
"""
