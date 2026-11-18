import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
from pymilvus import DataType, MilvusClient

from backend.tools.fetch import blocked_page


def normalize_url(url: str):
    p = urlsplit(url)
    fragment = p.fragment if p.fragment.startswith("/") else ""
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, fragment))


def chunks(text: str, size=500, overlap=50):
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return [text[i : i + size] for i in range(0, len(text), size - overlap)]


class KnowledgeBase:
    def __init__(self, config, models):
        self.config, self.models = config, models
        self.client = None

    def connect(self):
        if self.client is None:
            self.client = MilvusClient(uri=self.config.milvus_uri, timeout=10)
        return self.client

    def ensure(self, dimension: int):
        client = self.connect()
        if client.has_collection("travel_kb"):
            fields = client.describe_collection("travel_kb")["fields"]
            existing = next(f["params"]["dim"] for f in fields if f["name"] == "vector")
            if int(existing) != dimension:
                raise ValueError("Embedding 维度发生变化，请迁移 travel_kb 集合；不会自动删库")
            return
        schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dimension)
        schema.add_field("text", DataType.VARCHAR, max_length=8192)
        for field in ("city", "poi_name", "doc_type", "source_level"):
            schema.add_field(field, DataType.VARCHAR, max_length=256)
        schema.add_field("source_url", DataType.VARCHAR, max_length=4096)
        schema.add_field("fetched_at", DataType.VARCHAR, max_length=64)
        schema.add_field("valid_until", DataType.VARCHAR, max_length=64)
        schema.add_field("hash", DataType.VARCHAR, max_length=64)
        index = client.prepare_index_params()
        index.add_index(
            "vector",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        client.create_collection("travel_kb", schema=schema, index_params=index)

    async def ingest(self, city: str, poi: str, source):
        import asyncio

        if blocked_page(source.text):
            raise ValueError("来源返回验证页面，不能入库")
        texts = chunks(source.text)
        if not texts:
            return 0
        vectors = await self.models.embed(texts)
        await asyncio.to_thread(self.ensure, len(vectors[0]))
        expires = (datetime.now(UTC) + timedelta(days=90)).isoformat()
        rows = []
        for text, vector in zip(texts, vectors, strict=True):
            digest = hashlib.sha256((normalize_url(str(source.url)) + text).encode()).hexdigest()
            rows.append(
                {
                    "id": digest,
                    "hash": digest,
                    "text": text,
                    "vector": vector,
                    "city": city,
                    "poi_name": poi,
                    "doc_type": "intro",
                    "source_url": str(source.url),
                    "source_level": source.level,
                    "fetched_at": source.fetched_at,
                    "valid_until": expires,
                }
            )
        await asyncio.to_thread(self.connect().upsert, "travel_kb", rows)
        return len(rows)

    async def search(self, city: str, query: str):
        import asyncio
        import json

        vectors = await self.models.embed([query])
        await asyncio.to_thread(self.ensure, len(vectors[0]))
        results = await asyncio.to_thread(
            self.connect().search,
            "travel_kb",
            vectors,
            filter=f"city == {json.dumps(city, ensure_ascii=False)}",
            limit=50,
            output_fields=[
                "text",
                "source_url",
                "fetched_at",
                "valid_until",
                "source_level",
                "poi_name",
            ],
        )
        hits = [
            r["entity"]
            for r in results[0]
            if r["entity"]["valid_until"] >= datetime.now(UTC).isoformat()
            and not blocked_page(r["entity"]["text"])
        ]
        if not hits:
            return []
        try:
            ranked = await self.models.rerank(query, [h["text"] for h in hits])
        except (httpx.HTTPError, KeyError, ValueError, IndexError):
            # Reranking only refines an already ordered vector result. When the
            # external reranker is unavailable, keep the retrieved order instead
            # of failing the whole research step.
            return hits[:8]
        return [hits[r["index"]] for r in ranked]

    def coverage(self, city: str):
        import json

        client = self.connect()
        if not client.has_collection("travel_kb"):
            return {"city": city, "chunks": 0, "sources": []}
        rows = client.query(
            "travel_kb",
            filter=f"city == {json.dumps(city, ensure_ascii=False)}",
            output_fields=["source_url", "fetched_at", "text"],
            limit=10000,
        )
        rows = [row for row in rows if not blocked_page(row["text"])]
        return {
            "city": city,
            "chunks": len(rows),
            "sources": sorted(set(row["source_url"] for row in rows)),
        }
