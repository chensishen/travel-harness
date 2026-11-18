import { useQuery } from "@tanstack/react-query";
import { MapContainer, TileLayer, GeoJSON, useMap } from "react-leaflet";
import { useEffect } from "react";
import L from "leaflet";
import type { FeatureCollection } from "geojson";
import { api } from "./api";

const colors = ["#176b5b", "#c87941", "#5665a3", "#b65669"];

function Bounds({ data }: { data: FeatureCollection }) {
  const map = useMap();
  useEffect(() => {
    const bounds = L.geoJSON(data).getBounds();
    if (bounds.isValid())
      map.fitBounds(bounds, { padding: [50, 50], maxZoom: 14 });
  }, [data, map]);
  return null;
}

export default function RouteMap({ jobId }: { jobId: string }) {
  const query = useQuery({
    queryKey: ["map", jobId],
    queryFn: () => api<FeatureCollection>(`/jobs/${jobId}/map`),
  });
  if (query.isError)
    return (
      <div className="notice error">地图加载失败：{query.error.message}</div>
    );
  if (!query.data?.features.length)
    return <div className="empty">暂无路线。文旅介绍任务不生成出行路线。</div>;
  return (
    <>
      <div className="notice">
        WGS84 · 路线、方式与时间轴一致；交通参考时长不代表实时班次。
      </div>
      <MapContainer center={[31.23, 121.47]} zoom={10} className="route-map">
        <TileLayer
          attribution="&copy; OpenStreetMap contributors"
          url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
        <GeoJSON
          data={query.data}
          style={(feature) => ({
            color: colors[((feature?.properties.day || 1) - 1) % 4],
            weight: 4,
            dashArray: feature?.properties.mode === "foot" ? "7 7" : undefined,
          })}
          pointToLayer={(feature, latlng) =>
            L.circleMarker(latlng, {
              radius: 9,
              fillColor: colors[(feature.properties.day - 1) % 4],
              color: "white",
              weight: 3,
              fillOpacity: 1,
            })
          }
          onEachFeature={(feature, layer) => {
            const element = document.createElement("span");
            element.textContent = feature.properties.name
              ? `第${feature.properties.day}天 · ${feature.properties.order}. ${feature.properties.name}`
              : `${feature.properties.duration_min} 分钟 · ${feature.properties.source}`;
            layer.bindTooltip(element);
          }}
        />
        <Bounds data={query.data} />
      </MapContainer>
    </>
  );
}
