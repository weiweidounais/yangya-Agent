#!/usr/bin/env python3
"""Refresh DRAFT exemplar vectors via SiliconFlow; key only from environment."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent.intent_scoring import build_vector_cache

if __name__ == "__main__":
    count, dimensions = build_vector_cache()
    print(f"Cached {count} DRAFT exemplar vectors ({dimensions} dimensions).")
