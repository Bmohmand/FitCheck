"""
ai_modules/search_existing.py
==============================
Runnable script to search through items already uploaded to Supabase.

Embeds a text query with the configured embedder, runs similarity search
via match_manifest_items (manifest_items table), and returns or prints results.

Usage as script:
  cd backend
  python -m ai_modules.search_existing "cold weather medical mission" --top-k 10
  python -m ai_modules.search_existing "rain gear" --category clothing

Usage as module:
  from ai_modules.search_existing import search_existing
  results = await search_existing("warm layers", top_k=5)
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

# Load backend .env before config is imported (config reads os.environ at import time)
from dotenv import load_dotenv
for _env_path in (
    Path(__file__).resolve().parent.parent / ".env",
    Path.cwd() / ".env",
):
    if _env_path.exists():
        load_dotenv(_env_path)
        break

from .config import (
    EMBEDDING_PROVIDER,
    EmbeddingProvider,
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY,
    VOYAGE_API_KEY,
    validate_config,
)
from .embedding_engine import create_embedder
from .models import RetrievedItem
from .vector_store import SupabaseVectorStore


async def search_existing(
    query: str,
    top_k: int = 10,
    category_filter: Optional[str] = None,
) -> list[RetrievedItem]:
    """
    Search Supabase manifest_items by semantic similarity to a text query.

    Args:
        query: Natural language search text.
        top_k: Number of nearest neighbors to return.
        category_filter: Optional category to restrict search (e.g. "clothing").

    Returns:
        List of RetrievedItem sorted by similarity (highest first).
    """
    embedder = create_embedder()
    store = SupabaseVectorStore()
    query_vector = await embedder.embed_text(query)
    return await store.search(
        query_vector=query_vector,
        top_k=top_k,
        category_filter=category_filter,
    )


def _print_results(query: str, items: list[RetrievedItem]) -> None:
    """Print a readable summary of search results."""
    print(f"\nQuery: \"{query}\"")
    print(f"Results: {len(items)} items\n")
    if not items:
        print("  (no matches)")
        return
    for i, item in enumerate(items, 1):
        ctx = item.context
        img = item.image_url or "—"
        summary = (ctx.utility_summary or "")[:80]
        if len(ctx.utility_summary or "") > 80:
            summary += "..."
        print(f"  {i}. {ctx.name}")
        print(f"     category: {ctx.inferred_category}  |  similarity: {item.score:.4f}")
        print(f"     image: {img}")
        print(f"     {summary}")
        print()


def _check_env() -> None:
    """Fail with a clear message if required env vars are missing."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print(
            "Error: SUPABASE_URL and SUPABASE_SERVICE_KEY are required.\n"
            "Set them in backend/.env (see backend/.env.example).",
            file=sys.stderr,
        )
        sys.exit(1)
    if EMBEDDING_PROVIDER == EmbeddingProvider.VOYAGE and not VOYAGE_API_KEY:
        print(
            "Error: VOYAGE_API_KEY is required when using Voyage embeddings.\n"
            "Set it in backend/.env, or use NEXUS_EMBEDDING_PROVIDER=clip_local for offline.",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search existing Supabase items by semantic similarity.",
    )
    parser.add_argument(
        "query",
        type=str,
        help="Natural language search query (e.g. 'cold weather medical mission')",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        metavar="N",
        help="Number of results (default: 10)",
    )
    parser.add_argument(
        "--category",
        type=str,
        default=None,
        metavar="NAME",
        help="Optional category filter (e.g. clothing, medical)",
    )
    args = parser.parse_args()

    _check_env()
    items = asyncio.run(
        search_existing(
            query=args.query,
            top_k=args.top_k,
            category_filter=args.category,
        )
    )
    _print_results(args.query, items)


if __name__ == "__main__":
    main()
