"""The weekly chart (CHRT-01…CHRT-08): the week model, its one read of the timeline, and more.

Importing this package must import neither Pillow nor any Django model: the web process
imports ``powermon.models`` and has to stay small (1 vCPU / 1–2 GB VPS), so every module
here is imported by name. The renderer is imported only inside
``lifecycle.chart_content``: by the worker, and by the web process only inside its chart
preview view (``powermon/web/chart_preview.py``, UI-06), never at import time.
"""
