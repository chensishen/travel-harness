"""Bounded OSM requests, raster tiles with a real vector-data fallback."""

import asyncio
import io
import math
import time
from collections import Counter
from urllib.parse import urlencode

import httpx
from PIL import Image, ImageDraw


def mercator(lng, lat, zoom=0):
    n = 256 * 2**zoom
    phi = math.radians(lat)
    return ((lng + 180) / 360 * n, (1 - math.asinh(math.tan(phi)) / math.pi) / 2 * n)


def inverse(x, y, zoom):
    n = 256 * 2**zoom
    return [x / n * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))]


def coverage(image):
    pixels = list(image.convert("RGB").resize((256, 160)).getdata())
    # Before adding routes or labels: uniform/failed tiles cannot pass on annotations alone.
    quantized = [tuple(v // 12 for v in p) for p in pixels]
    return 1 - Counter(quantized).most_common(1)[0][1] / len(pixels)


class OSM:
    def __init__(self, config, store, mcp=None):
        self.config, self.store = config, store
        self.mcp = mcp
        self.unavailable_until = {}

    async def overpass(self, query):
        key = self.store.key("overpass-v11", query)
        cached = self.store.cached(key, 7)
        if cached is not None:
            return cached
        errors = []
        for endpoint in dict.fromkeys([self.config.overpass_base_url, "https://overpass.private.coffee/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]):
            if self.unavailable_until.get(endpoint,0)>time.monotonic():
                errors.append('近期连接失败，短暂冷却')
                continue
            try:
                async with httpx.AsyncClient(timeout=45, headers={"User-Agent": self.config.http_user_agent}) as client:
                    result = await client.post(endpoint, data={"data": query})
                    result.raise_for_status()
                    data = result.json()
                    if data.get("remark"):
                        raise ValueError("地图服务未完整返回请求结果")
                    self.store.put_cache(key, data)
                    return data
            except (httpx.HTTPError, ValueError) as error:
                errors.append(type(error).__name__)
                self.unavailable_until[endpoint]=time.monotonic()+120
        if self.mcp:
            try:
                data=await self.mcp.fetch_json(self.config.overpass_base_url+'?'+urlencode({'data':query}))
                if not isinstance(data.get('elements'),list) or data.get('remark'):
                    raise ValueError('远程地图API未完整返回elements')
                self.store.put_cache(key,data)
                return data
            except (ValueError,RuntimeError,httpx.HTTPError) as error:
                errors.append('MCP:'+type(error).__name__)
        raise ValueError("地图地理数据不可用：" + ", ".join(errors))

    async def transit(self, a, b):
        query = (
            '[out:json][timeout:25];('
            f'node(around:1500,{a.lat},{a.lng})[railway];'
            f'node(around:1500,{b.lat},{b.lng})[railway];'
            f'node(around:1500,{a.lat},{a.lng})[public_transport];'
            f'node(around:1500,{b.lat},{b.lng})[public_transport];'
            ')->.stops;rel(bn.stops)[route~"^(subway|light_rail|tram|bus)$"]->.routes;.routes out geom;node(r.routes);out tags;'
        )
        data = await self.overpass(query)
        return {**data, "_fetched_at": self.store.cache_created_at(self.store.key("overpass-v11", query))}

    async def basemap(self, days, folder, name, closeup=False):
        points = [(p.lng, p.lat) for d in days for p in d.stops]
        points += [tuple(p) for d in days for s in d.segments for p in s.polyline]
        points += [tuple(p) for d in days if getattr(d,'arrival_segment',None) for p in d.arrival_segment.polyline]
        if not points:
            raise ValueError("没有可绘制的已核对坐标")
        xs, ys = zip(*points)
        pad_x, pad_y = max((max(xs) - min(xs)) * .1, .0018 if closeup else .006), max((max(ys) - min(ys)) * .1, .0012 if closeup else .004)
        bounds = [min(xs) - pad_x, min(ys) - pad_y, max(xs) + pad_x, max(ys) + pad_y]
        for zoom in (range(17,15,-1) if closeup else range(16,8,-1)):
            west, north = mercator(bounds[0], bounds[3], zoom)
            east, south = mercator(bounds[2], bounds[1], zoom)
            x0, y0, x1, y1 = math.floor(west / 256), math.floor(north / 256), math.floor(east / 256), math.floor(south / 256)
            if (x1 - x0 + 1) * (y1 - y0 + 1) <= 16:
                break
        cache = self.config.data_dir / "map-tiles"
        cache.mkdir(parents=True, exist_ok=True)
        # Reuse a slightly wider real basemap when every tile is already available.
        # This also lets a replanned subset use the previously validated neighbourhood tiles.
        for cached_zoom in (range(zoom,zoom-1,-1) if closeup else range(zoom, max(zoom - 3, 9) - 1, -1)):
            west, north = mercator(bounds[0], bounds[3], cached_zoom)
            east, south = mercator(bounds[2], bounds[1], cached_zoom)
            cached_bounds = (math.floor(west / 256), math.floor(north / 256), math.floor(east / 256), math.floor(south / 256))
            cx0, cy0, cx1, cy1 = cached_bounds
            if all((cache / f"{cached_zoom}-{x}-{y}.png").exists() for x in range(cx0, cx1 + 1) for y in range(cy0, cy1 + 1)):
                zoom = cached_zoom
                x0, y0, x1, y1 = cached_bounds
                break
        semaphore = asyncio.Semaphore(3)

        async def tile(x, y):
            path = cache / f"{zoom}-{x}-{y}.png"
            async with semaphore:
                if not path.exists():
                    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": self.config.http_user_agent}) as client:
                        response = await client.get(f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png")
                        response.raise_for_status()
                        im = Image.open(io.BytesIO(response.content)).convert("RGB")
                        if im.size != (256, 256):
                            raise ValueError("地图瓦片尺寸无效")
                        im.save(path)
                return x, y, Image.open(path).convert("RGB")

        try:
            tiles = await asyncio.gather(*(tile(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)))
            canvas = Image.new("RGB", ((x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256))
            for x, y, image in tiles:
                canvas.paste(image, ((x - x0) * 256, (y - y0) * 256))
            score = coverage(canvas)
            if score < .30:
                raise ValueError("底图非底色像素不足 30%")
            lower = inverse(x0 * 256, (y1 + 1) * 256, zoom)
            upper = inverse((x1 + 1) * 256, y0 * 256, zoom)
            path = folder / f"{name}-basemap.png"
            canvas.save(path)
            return {"kind": "raster", "image": str(path), "bounds": [lower[0], lower[1], upper[0], upper[1]], "coverage": score}
        except (httpx.HTTPError, ValueError, OSError):
            # A failed tile image never becomes an apparently valid blank route diagram.
            bbox = f"{bounds[1]},{bounds[0]},{bounds[3]},{bounds[2]}"
            data = await self.overpass(
                f'[out:json][timeout:30];(way({bbox})[natural~"water|wood"];'
                f'way({bbox})[landuse];way({bbox})[leisure=park];'
                f'way({bbox})[highway~"primary|secondary|tertiary|residential|pedestrian"];'
                f'way({bbox})[railway=subway];node({bbox})[railway=station];);out geom;'
            )
            canvas = Image.new("RGB", (1600, 1000), "#F2F5EE")
            draw = ImageDraw.Draw(canvas)
            wx, ny = mercator(bounds[0], bounds[3])
            ex, sy = mercator(bounds[2], bounds[1])

            def project(lng, lat):
                x, y = mercator(lng, lat)
                return ((x - wx) / (ex - wx) * 1600, (y - ny) / (sy - ny) * 1000)

            elements = data.get("elements", [])
            for element in elements:
                tags = element.get("tags", {})
                geometry = element.get("geometry", [])
                line = [project(p["lon"], p["lat"]) for p in geometry]
                if len(line) < 2:
                    continue
                if tags.get("natural") in {"water", "wood"} or "landuse" in tags or tags.get("leisure") == "park":
                    draw.polygon(line, fill="#BCDDE7" if tags.get("natural") == "water" else "#D5E2CC")
                if "highway" in tags:
                    draw.line(line, fill="#FFFFFF", width=8)
                if tags.get("railway") == "subway":
                    draw.line(line, fill="#A885A3", width=5)
            score = coverage(canvas)
            if score < .30:
                raise ValueError("矢量底图覆盖不足，未输出无底图路线图")
            path = folder / f"{name}-basemap.png"
            canvas.save(path)
            return {"kind": "vector", "image": str(path), "bounds": bounds, "coverage": score, "elements": elements}
