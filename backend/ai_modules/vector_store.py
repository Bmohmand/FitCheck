"""
nexus_ai/vector_store.py
=========================
Supabase pgvector integration. Replaces Pinecone as the vector database.

Advantages over Pinecone for this hackathon:
  - Zihan already has Supabase for auth — one fewer service to manage
  - Free tier is generous (500MB, plenty for 50-200 items)
  - Full SQL power for metadata filtering alongside vector search
  - No separate API key / dashboard to juggle at 3am

SETUP (run once in Supabase SQL Editor):
  See the setup_sql() method or copy the SQL from the docstring below.
"""

import json
import logging
from typing import Optional

from supabase import create_client, AsyncClient

from .config import SUPABASE_URL, SUPABASE_SERVICE_KEY, get_embedding_dim
from .models import ItemContext, EmbeddingResult, RetrievedItem

logger = logging.getLogger("nexus.vectorstore")

# ---------------------------------------------------------------------------
# SQL to run ONCE in Supabase SQL Editor (Dashboard → SQL Editor → New Query)
# ---------------------------------------------------------------------------
SETUP_SQL = """
-- 1. Enable pgvector extension
create extension if not exists vector;

-- 2. Create the items table
create table if not exists nexus_items (
    id              uuid primary key default gen_random_uuid(),
    embedding       vector({dim}),           -- set to your provider's dimension
    image_url       text,
    name            text not null,
    inferred_category text,
    primary_material  text,
    weight_estimate   text,
    thermal_rating    text,
    water_resistance  text,
    medical_application text,
    utility_summary   text,
    semantic_tags     jsonb default '[]'::jsonb,
    durability        text,
    compressibility   text,
    created_at        timestamptz default now()
);

-- 3. Create an HNSW index for fast cosine similarity search
create index if not exists nexus_items_embedding_idx
    on nexus_items
    using hnsw (embedding vector_cosine_ops)
    with (m = 16, ef_construction = 64);

-- 4. Create the similarity search function
create or replace function match_nexus_items (
    query_embedding vector({dim}),
    match_count     int default 15,
    filter_category text default null
)
returns table (
    id                uuid,
    similarity        float,
    image_url         text,
    name              text,
    inferred_category text,
    primary_material  text,
    weight_estimate   text,
    thermal_rating    text,
    water_resistance  text,
    medical_application text,
    utility_summary   text,
    semantic_tags     jsonb,
    durability        text,
    compressibility   text
)
language plpgsql
as $$
begin
    return query
    select
        ni.id,
        1 - (ni.embedding <=> query_embedding) as similarity,
        ni.image_url,
        ni.name,
        ni.inferred_category,
        ni.primary_material,
        ni.weight_estimate,
        ni.thermal_rating,
        ni.water_resistance,
        ni.medical_application,
        ni.utility_summary,
        ni.semantic_tags,
        ni.durability,
        ni.compressibility
    from nexus_items ni
    where (filter_category is null or ni.inferred_category = filter_category)
    order by ni.embedding <=> query_embedding
    limit match_count;
end;
$$;
"""


class SupabaseVectorStore:
    """
    Handles all vector storage and retrieval via Supabase pgvector.

    Usage (in Zihan's FastAPI):
        from nexus_ai.vector_store import SupabaseVectorStore
        store = SupabaseVectorStore()

        # Upsert
        await store.upsert(embedding_result, image_url="https://...")

        # Search
        items = await store.search(query_vector, top_k=15)
    """

    def __init__(self, url: str = SUPABASE_URL, key: str = SUPABASE_SERVICE_KEY):
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required")
        self.client = create_client(url, key)
        logger.info("Supabase vector store initialized")

    def get_setup_sql(self, dim: Optional[int] = None) -> str:
        """
        Returns the SQL to set up the table. Run this ONCE in Supabase SQL Editor.
        Automatically uses the correct dimension for your embedding provider.
        """
        dimension = dim or get_embedding_dim()
        return SETUP_SQL.replace("{dim}", str(dimension))

    async def upsert(self, result: EmbeddingResult, image_url: str = "") -> str:
        """
        Insert or update an item in the vector store.

        Args:
            result: EmbeddingResult from pipeline.ingest()
            image_url: S3/R2 URL after uploading the image

        Returns:
            The item's UUID
        """
        ctx = result.context
        row = {
            "id": result.item_id,
            "embedding": result.vector,
            "image_url": image_url,
            "name": ctx.name,
            "inferred_category": ctx.inferred_category,
            "primary_material": ctx.primary_material,
            "weight_estimate": ctx.weight_estimate,
            "thermal_rating": ctx.thermal_rating,
            "water_resistance": ctx.water_resistance,
            "medical_application": ctx.medical_application,
            "utility_summary": ctx.utility_summary,
            "semantic_tags": ctx.semantic_tags,
            "durability": ctx.durability,
            "compressibility": ctx.compressibility,
        }

        response = self.client.table("nexus_items").upsert(row).execute()
        logger.info(f"Upserted item: {ctx.name} ({result.item_id})")
        return result.item_id

    async def search(
        self,
        query_vector: list[float],
        top_k: int = 15,
        category_filter: Optional[str] = None,
    ) -> list[RetrievedItem]:
        """
        Perform cosine similarity search via the match_nexus_items RPC function.

        Args:
            query_vector: The embedded query from pipeline.embed_query()
            top_k: Number of nearest neighbors to return
            category_filter: Optional category to restrict search

        Returns:
            List of RetrievedItem sorted by similarity (highest first)
        """
        response = self.client.rpc(
            "match_nexus_items",
            {
                "query_embedding": query_vector,
                "match_count": top_k,
                "filter_category": category_filter,
            },
        ).execute()

        items = []
        for row in response.data:
            items.append(RetrievedItem(
                item_id=str(row["id"]),
                score=float(row["similarity"]),
                image_url=row.get("image_url"),
                context=ItemContext(
                    name=row["name"],
                    inferred_category=row.get("inferred_category", "misc"),
                    primary_material=row.get("primary_material"),
                    weight_estimate=row.get("weight_estimate"),
                    thermal_rating=row.get("thermal_rating"),
                    water_resistance=row.get("water_resistance"),
                    medical_application=row.get("medical_application"),
                    utility_summary=row.get("utility_summary", ""),
                    semantic_tags=row.get("semantic_tags", []),
                    durability=row.get("durability"),
                    compressibility=row.get("compressibility"),
                ),
            ))

        logger.info(f"Search returned {len(items)} items (top score: {items[0].score:.4f})" if items else "Search returned 0 items")
        return items

    async def delete(self, item_id: str) -> None:
        """Remove an item from the store."""
        self.client.table("nexus_items").delete().eq("id", item_id).execute()
        logger.info(f"Deleted item: {item_id}")

    async def count(self) -> int:
        """Get total number of items in the store."""
        response = self.client.table("nexus_items").select("id", count="exact").execute()
        return response.count or 0
