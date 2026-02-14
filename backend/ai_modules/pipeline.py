"""
nexus_ai/pipeline.py
=====================
Main orchestrator. This is the module Zihan imports into his FastAPI app.

Two core flows:
  1. INGEST:  image -> context extraction -> embedding -> upsert to Supabase
  2. SEARCH:  query text -> query embedding -> Supabase pgvector search -> synthesis

Usage in Zihan's FastAPI:
    from nexus_ai.pipeline import NexusPipeline

    pipeline = NexusPipeline()

    # In POST /api/ingest
    item_id = await pipeline.ingest(image_bytes, image_url="https://s3...")

    # In POST /api/search/semantic  (one call does everything now)
    plan = await pipeline.search("48-hour cold climate medical mission")

Env vars needed:
    OPENAI_API_KEY, VOYAGE_API_KEY (or GOOGLE_PROJECT_ID),
    SUPABASE_URL, SUPABASE_SERVICE_KEY
"""

import logging
import time
from typing import Optional

from .config import EMBEDDING_PROVIDER, validate_config
from .models import ItemContext, EmbeddingResult, RetrievedItem, MissionPlan, SearchQuery
from .context_extractor import ContextExtractor
from .embedding_engine import create_embedder, BaseEmbedder
from .mission_synthesizer import MissionSynthesizer
from .vector_store import SupabaseVectorStore

logger = logging.getLogger("nexus.pipeline")


class NexusPipeline:
    """
    Top-level orchestrator for all AI operations in Nexus.

    Zihan: instantiate this ONCE at FastAPI startup, then call its methods
    from your route handlers.
    """

    def __init__(self):
        # Validate environment
        warnings = validate_config()
        for w in warnings:
            logger.warning(f"CONFIG: {w}")

        # Initialize components
        self.extractor = ContextExtractor()
        self.embedder: BaseEmbedder = create_embedder()
        self.synthesizer = MissionSynthesizer()
        self.store = SupabaseVectorStore()

        logger.info(
            f"NexusPipeline initialized | "
            f"embedder={EMBEDDING_PROVIDER.value} "
            f"dim={self.embedder.dimension}"
        )

    # -------------------------------------------------------------------
    # FLOW 1: INGEST  --  Called by Zihan's POST /api/ingest
    # -------------------------------------------------------------------
    async def ingest(self, image_source: str | bytes, image_url: str = "") -> str:
        """
        Full ingest pipeline: image -> context -> embedding -> store in Supabase.

        Args:
            image_source: File path, URL, or raw bytes of the image.
            image_url: The public S3/R2 URL after Zihan uploads the image.

        Returns:
            The item's UUID (stored in Supabase).
        """
        t0 = time.time()

        # Step 1: Extract semantic context via Vision LLM
        logger.info("Step 1/3: Extracting context via GPT-4o Vision...")
        context: ItemContext = await self.extractor.extract(image_source)
        t1 = time.time()
        logger.info(f"  Context extracted in {t1 - t0:.1f}s: {context.name} [{context.inferred_category}]")

        # Step 2: Generate multimodal embedding
        logger.info("Step 2/3: Generating multimodal embedding...")
        vector: list[float] = await self.embedder.embed_item(image_source, context)
        t2 = time.time()
        logger.info(f"  Embedding generated in {t2 - t1:.1f}s: dim={len(vector)}")

        result = EmbeddingResult(
            vector=vector,
            dimension=len(vector),
            context=context,
            image_url=image_url,
        )

        # Step 3: Upsert into Supabase
        logger.info("Step 3/3: Upserting into Supabase...")
        item_id = await self.store.upsert(result, image_url=image_url)
        t3 = time.time()

        logger.info(f"Ingest complete in {t3 - t0:.1f}s | id={item_id}")
        return item_id

    async def ingest_batch(self, image_sources: list[tuple[str | bytes, str]]) -> list[str]:
        """
        Batch ingest for the demo seed phase (Hour 24-30).
        Processes items sequentially to avoid rate limits.

        Args:
            image_sources: List of (image_source, image_url) tuples.

        Returns:
            List of item UUIDs.
        """
        import asyncio
        ids = []
        for i, (src, url) in enumerate(image_sources):
            logger.info(f"Batch ingest [{i + 1}/{len(image_sources)}]")
            try:
                item_id = await self.ingest(src, image_url=url)
                ids.append(item_id)
            except Exception as e:
                logger.error(f"Failed to ingest item {i + 1}: {e}")
            # Small delay to avoid rate limits
            await asyncio.sleep(0.5)
        return ids

    # -------------------------------------------------------------------
    # FLOW 2: SEARCH  --  Called by Zihan's POST /api/search/semantic
    # -------------------------------------------------------------------
    async def search(
        self,
        query: str,
        top_k: int = 15,
        category_filter: Optional[str] = None,
        synthesize: bool = True,
    ) -> MissionPlan | list[RetrievedItem]:
        """
        Full search pipeline: query -> embed -> Supabase vector search -> synthesize.

        This is now a SINGLE call that does everything. Zihan's route handler
        just needs:
            plan = await pipeline.search("cold weather medical mission")

        Args:
            query: Natural language search text from Noah's UI.
            top_k: Number of nearest neighbors.
            category_filter: Optional category to restrict search.
            synthesize: If True, run LLM synthesis. If False, return raw results.

        Returns:
            MissionPlan (if synthesize=True) or list of RetrievedItem (if False).
        """
        t0 = time.time()

        # Step 1: Embed the query
        logger.info(f"Embedding query: '{query[:80]}'...")
        query_vector = await self.embedder.embed_text(query)

        # Step 2: Search Supabase pgvector
        logger.info(f"Searching Supabase (top_k={top_k})...")
        retrieved = await self.store.search(
            query_vector=query_vector,
            top_k=top_k,
            category_filter=category_filter,
        )
        t1 = time.time()
        logger.info(f"Retrieved {len(retrieved)} items in {t1 - t0:.1f}s")

        if not synthesize:
            return retrieved

        # Step 3: LLM synthesis into a mission plan
        logger.info("Synthesizing mission plan...")
        plan = await self.synthesizer.synthesize(query, retrieved)
        t2 = time.time()
        logger.info(
            f"Search complete in {t2 - t0:.1f}s | "
            f"{len(plan.selected_items)} selected, "
            f"{len(plan.rejected_items)} rejected"
        )
        return plan

    async def embed_query(self, query: str) -> list[float]:
        """
        Just embed a query without searching. Useful if Zihan wants
        to do custom queries against Supabase directly.
        """
        return await self.embedder.embed_text(query)

    # -------------------------------------------------------------------
    # UTILITY: Get setup SQL for Supabase
    # -------------------------------------------------------------------
    def get_setup_sql(self) -> str:
        """
        Returns the SQL Zihan needs to run once in Supabase SQL Editor.
        Automatically uses the correct vector dimension.
        """
        return self.store.get_setup_sql(dim=self.embedder.dimension)

    async def item_count(self) -> int:
        """How many items are in the database."""
        return await self.store.count()
