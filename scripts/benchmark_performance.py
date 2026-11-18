"""Reproducible latency replay plus an optional real embedding-cache probe."""

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.config import Settings  # noqa: E402
from backend.app.store import Store  # noqa: E402
from backend.harness.graph import Harness, safe_error  # noqa: E402
from backend.tools.llm import Models  # noqa: E402


async def replay(root, concurrency):
    config = Settings(_env_file=None, research_concurrency=concurrency, fetch_concurrency=concurrency)
    harness = Harness(config, Store(root))
    fetch_limit = asyncio.Semaphore(concurrency)
    counters = {'search': 0, 'extract': 0, 'fetch': 0, 'ingest': 0}

    async def search(query):
        counters['search'] += 1
        await asyncio.sleep(.08)
        return f'https://{query}.example/visit https://{query}.example/history'

    async def extract(instruction, data, schema):
        counters['extract'] += 1
        await asyncio.sleep(.04)
        return schema(sources=[dict(id='', title='公开资料', url=url, level='L3', fetched_at='') for url in data['results'].split()])

    async def cached(*args):
        return []

    async def fetch(url):
        async with fetch_limit:
            counters['fetch'] += 1
            await asyncio.sleep(.06)
            return '公开网页完整正文：' + url

    async def ingest(*args):
        counters['ingest'] += 1
        await asyncio.sleep(.02)

    harness.search.search, harness.models.json = search, extract
    harness.kb.search, harness.kb.ingest = cached, ingest
    harness.fetch = SimpleNamespace(fetch=fetch)
    state = {'spec': {'city': '杭州'}, 'outline': {'queries': ['a', 'b', 'c', 'd']}, 'warnings': []}
    started = time.monotonic()
    await harness.researcher({'id': 'replay'}, state, root)
    elapsed = time.monotonic()-started
    sources = [{k:v for k,v in source.items() if k != 'fetched_at'} for source in state['sources']]
    return elapsed, sources, counters


async def main(live):
    output = {'replay_note': '固定响应与固定网络延迟回放，不代表真实城市全流程速度', 'samples': []}
    with tempfile.TemporaryDirectory(prefix='travel-performance-') as directory:
        root = Path(directory)
        for attempt in range(3):
            serial_dir, parallel_dir = root / f'serial-{attempt}', root / f'parallel-{attempt}'
            serial_dir.mkdir()
            parallel_dir.mkdir()
            serial, old_sources, old_counts = await replay(serial_dir, 1)
            parallel, sources, counts = await replay(parallel_dir, 2)
            assert old_sources == sources and old_counts == counts
            output['samples'].append({'serial_s': round(serial,3), 'parallel_s': round(parallel,3), 'same_sources_and_ids': True, 'calls': counts})
        old = statistics.median(s['serial_s'] for s in output['samples'])
        new = statistics.median(s['parallel_s'] for s in output['samples'])
        output['replay_median'] = {'serial_s': old, 'parallel_s': new, 'reduction_percent': round((1-new/old)*100,1)}
        if live:
            config = Settings()
            models = Models(config, Store(root / 'live-cache'))
            try:
                data = json.loads(Path('data/jobs/35257983-409b-4933-bcd8-112e6007793e/deliverable.json').read_text())
                texts = [source['text'][:4000] for source in data['sources'][:2]]
                started = time.monotonic()
                cold = await models.embed(texts)
                cold_time = time.monotonic()-started
                started = time.monotonic()
                warm = await models.embed(texts)
                warm_time = time.monotonic()-started
                assert cold == warm
                output['real_embedding'] = {'status':'passed', 'texts':len(texts), 'dimensions':[len(v) for v in cold], 'cold_s':round(cold_time,3), 'cached_s':round(warm_time,3), 'identical_vectors':True, 'content_hash':hashlib.sha256(json.dumps(texts,ensure_ascii=False).encode()).hexdigest()}
            except Exception as error:
                output['real_embedding'] = {'status':'failed', 'error':safe_error(error, config)}
            finally:
                await models.aclose()
    target = Path('data/qa/performance-benchmark.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 1 if output.get('real_embedding',{}).get('status') == 'failed' else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.live)))
