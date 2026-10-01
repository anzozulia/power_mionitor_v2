"""Worker supervision: progress stamps, the watchdog, the health file, the DB-outage log.

Docker restarts a container only when its process exits. An "unhealthy" status alone
restarts nothing (INV-13). So a stalled loop must end the worker process, and the main
thread watches the loops (D-15, OPS-05).
"""
