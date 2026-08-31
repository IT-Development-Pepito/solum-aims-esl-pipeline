"""Orchestration layer.

Modules here coordinate domain rules and adapter ports. They depend on
interfaces only, never on a concrete adapter or a transport library, so the
vendor boundary stays replaceable (FR-018, AD-002).
"""
