"""Adapters to external systems (AD-002, AD-003, FR-018).

An adapter speaks a transport and reports typed outcomes; it never decides
business rules. This is the only package allowed to import a database driver
or an HTTP client, and the architecture tests enforce that the domain and the
orchestration layer depend on the ports in ``application.contracts`` rather
than on anything here.
"""
