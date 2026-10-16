"""supabase_client.py - Phase 2 ONLY: Supabase auth + saved-portfolio persistence.

Fully isolated from the Phase 1 analysis path. engine.py and the Phase 1 UI
never import this module, so the core platform runs with no Supabase config
(Requirements 11.5, 11.7). Implemented in Tasks 13-14.
"""

from __future__ import annotations

# Intentionally no top-level `supabase` import so Phase 1 runs without the
# dependency configured. The client is created lazily in Task 13.
