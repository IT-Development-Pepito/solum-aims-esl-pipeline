"""Test-only fault seams for the FR-016 recovery scenarios (#21).

Nothing here is production code. The package holds the pieces a recovery
test needs and a rolled-back fixture cannot give it: committed rows in the
dedicated test database, a source port that fails on cue, a TCP forwarder
that can refuse or cut a connection, and a runner process that can be killed
after a named checkpoint.
"""
