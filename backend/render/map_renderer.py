from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLORS = ["#176B5B", "#C87941", "#5665A3", "#B65669"]


def dashed_line(draw, points, color, width=7):
    import math

    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        length = math.hypot(x2 - x1, y2 - y1)
        if not length:
            continue
        for start in range(0, math.ceil(length), 20):
            end = min(start + 12, length)
            draw.line(
                [
                    (x1 + (x2 - x1) * start / length, y1 + (y2 - y1) * start / length),
                    (x1 + (x2 - x1) * end / length, y1 + (y2 - y1) * end / length),
                ],
                fill=color,
                width=width,
            )


def font(size):
    for path in (
        "data/fonts/SourceHanSansSC-Regular.otf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def render_map(days, path: Path, title: str):
    if days and all(d.basemap for d in days):
        return render_basemap(days, path, title)
    points = [
        (p.lng, p.lat) for day in days for p in day.stops if p.lat is not None and p.lng is not None
    ]
    if not points:
        return ""
    points += [
        tuple(point) for day in days for segment in day.segments for point in segment.polyline
    ]
    image = Image.new("RGB", (1600, 1000), "#F2F5EE")
    draw = ImageDraw.Draw(image)
    xs, ys = zip(*points)
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    spanx, spany = max(maxx - minx, 0.01), max(maxy - miny, 0.01)
    # Apply one scale to both axes after correcting for latitude.
    import math

    correction = math.cos(math.radians((miny + maxy) / 2))
    scale = min(850 / (spanx * correction), 580 / spany)
    centerx, centery = (minx + maxx) / 2, (miny + maxy) / 2

    def project(lng, lat):
        return (1080 + (lng - centerx) * correction * scale, 560 - (lat - centery) * scale)

    draw.text((65, 35), title, fill="#16382F", font=font(40))
    draw.text(
        (65, 100),
        "路线示意图 · WGS84 · 无地理底图；请结合交互地图查看",
        fill="#61736A",
        font=font(24),
    )
    legend_y = 205
    detailed = sum(len(day.stops) for day in days) * 34 + len(days) * 56 <= 700
    for i, day in enumerate(days):
        color = COLORS[i % len(COLORS)]
        draw.text((65, legend_y), f"第 {day.day} 天", fill=color, font=font(26))
        legend_y += 38
        if detailed:
            for j, poi in enumerate(day.stops):
                label = f"{day.day}.{j + 1}  {poi.name}"
                while draw.textlength(label, font=font(22)) > 470:
                    label = label[:-2].rstrip("…") + "…"
                draw.text((75, legend_y), label, fill="#16382F", font=font(22))
                legend_y += 34
            legend_y += 18
        for segment in (
            [day.arrival_segment] if getattr(day, "arrival_segment", None) else []
        ) + day.segments:
            line = [project(*point) for point in segment.polyline]
            if len(line) > 1:
                draw.line(line, fill=color, width=7)
        for j, poi in enumerate(day.stops):
            if poi.lng is None or poi.lat is None:
                continue
            x, y = project(poi.lng, poi.lat)
            draw.ellipse((x - 24, y - 24, x + 24, y + 24), fill=color, outline="white", width=3)
            draw.text((x, y), f"{day.day}.{j + 1}", anchor="mm", fill="white", font=font(17))
    if not detailed:
        draw.text((65, 860), "景点顺序见每日路线图", fill="#61736A", font=font(22))
    draw.text(
        (65, 945),
        "道路数据 © OpenStreetMap contributors · 公共交通时间为估算 · 坐标须复核",
        fill="#61736A",
        font=font(21),
    )
    image.save(path)
    return str(path)


def render_basemap(days, path, title):
    from types import SimpleNamespace

    from backend.tools.osm import mercator

    base = days[0].basemap
    image = Image.open(base["image"]).convert("RGB").resize((1600, 1000))
    draw = ImageDraw.Draw(image)
    west, south, east, north = base["bounds"]
    x0, y0 = mercator(west, north)
    x1, y1 = mercator(east, south)

    def project(lng, lat):
        x, y = mercator(lng, lat)
        return ((x - x0) / (x1 - x0) * 1600, (y - y0) / (y1 - y0) * 1000)

    labels = []
    features = geojson(SimpleNamespace(days=days))["features"]
    for index, day in enumerate(days):
        color = COLORS[index % len(COLORS)]
        for feature in features:
            if (
                feature["properties"]["day"] != day.day
                or feature["geometry"]["type"] != "LineString"
            ):
                continue
            points = [project(*p) for p in feature["geometry"]["coordinates"]]
            if feature["properties"]["mode"] in {"foot", "transit_schematic"}:
                dashed_line(draw, points, color)
            else:
                draw.line(points, fill=color, width=7)
        if day.origin:
            x, y = project(day.origin.lng, day.origin.lat)
            draw.rectangle((x - 14, y - 14, x + 14, y + 14), fill=color, outline="white", width=3)
            draw.text(
                (max(5, min(x + 20, 1200)), max(5, y - 35)),
                "起点 · " + day.origin.name,
                font=font(26),
                fill=color,
                stroke_width=2,
                stroke_fill="white",
            )
        for segment in (
            [day.arrival_segment] if getattr(day, "arrival_segment", None) else []
        ) + day.segments:
            points = [project(*p) for p in segment.polyline]
            if len(points) < 3 or not segment.route_verified:
                raise ValueError("路线缺少真实路径，不能绘制直线替代")
            x, y = points[len(points) // 2]
            text = f"{segment.duration_min:g} 分钟"
            box = draw.textbbox((x, y), text, font=font(26))
            draw.rectangle((box[0] - 5, box[1] - 3, box[2] + 5, box[3] + 3), fill="white")
            draw.text((x, y), text, font=font(26), fill=color)
        for j, poi in enumerate(day.stops):
            x, y = project(poi.lng, poi.lat)
            draw.ellipse((x - 20, y - 20, x + 20, y + 20), fill=color, outline="white", width=3)
            draw.text((x, y), f"{day.day}.{j + 1}", anchor="mm", font=font(17), fill="white")
            text = poi.name
            width = draw.textlength(text, font=font(32))
            tx = max(5, min(x + 26, 1595 - width))
            ty = y - 34
            while any(abs(ty - py) < 35 and abs(tx - px) < max(width, pw) for px, py, pw in labels):
                ty += 38
            ty = min(ty, 920)
            labels.append((tx, ty, width))
            draw.rectangle((tx - 4, ty - 2, tx + width + 5, ty + 41), fill="white")
            draw.text((tx, ty), text, font=font(32), fill="#16382F")
    # Latitude-corrected scale bar in metres, computed from actual map extent.
    import math

    meters = (east - west) * 111320 * math.cos(math.radians((south + north) / 2))
    scale = next((n for n in [5000, 2000, 1000, 500, 200, 100] if n < meters / 4), 50)
    length = scale / meters * 1600
    draw.rectangle((30, 925, 30 + length, 932), fill="#16382F")
    draw.text((30, 935), f"{scale} m", fill="#16382F", font=font(19))
    draw.rectangle((1030, 960, 1600, 1000), fill="white")
    draw.text((1040, 966), "© OpenStreetMap contributors", font=font(19), fill="#16382F")
    if any(
        s.geometry_mode == "transit_schematic"
        for d in days
        for s in ([d.arrival_segment] if d.arrival_segment else []) + d.segments
    ):
        draw.rectangle((20, 10, 980, 46), fill="white")
        draw.text(
            (30, 14),
            "轨道虚线为站序示意；步行段来自道路路网，非实时导航",
            font=font(22),
            fill="#16382F",
        )
    image.save(path)
    for day in days:
        day.map_coverage = base["coverage"]
    return str(path)


def geojson(document):
    features = []
    for day in document.days:
        if day.origin and day.origin.lng is not None and day.origin.lat is not None:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [day.origin.lng, day.origin.lat]},
                    "properties": {
                        "day": day.day,
                        "order": 0,
                        "name": day.origin.name,
                        "role": "origin",
                    },
                }
            )
        for index, poi in enumerate(day.stops):
            if poi.lat is not None and poi.lng is not None:
                features.append(
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [poi.lng, poi.lat]},
                        "properties": {"day": day.day, "order": index + 1, "name": poi.name},
                    }
                )
        for segment in (
            [day.arrival_segment] if getattr(day, "arrival_segment", None) else []
        ) + day.segments:
            for part in segment.parts or [
                {"mode": segment.geometry_mode, "coordinates": segment.polyline}
            ]:
                features.append(
                    {
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": part["coordinates"]},
                        "properties": {
                            "day": day.day,
                            "duration_min": segment.duration_min,
                            "estimated": segment.estimated,
                            "source": segment.source,
                            "mode": part["mode"],
                            "from": segment.from_name,
                            "to": segment.to_name,
                        },
                    }
                )
    return {"type": "FeatureCollection", "features": features}
