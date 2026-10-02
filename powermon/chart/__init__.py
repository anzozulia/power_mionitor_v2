"""The weekly chart (CHRT-01…CHRT-08): the week model, its one read of the timeline, and more.

Importing this package must import neither Pillow nor any Django model: the web process
imports ``powermon.models`` and has to stay small (1 vCPU / 1–2 GB VPS), so every module
here is imported by name, and only the worker imports the renderer.
"""
