"""Genera routes/monterrey_routes.json con geometrias reales de calles.

Consulta OSRM una sola vez y guarda el resultado en el repositorio: el servidor
de rastreo no depende de internet durante la simulacion.

Uso: python tools/generate_routes.py
"""
import json
import time
import urllib.request
from pathlib import Path

OSRM = "https://router.project-osrm.org/route/v1/driving"
OUTPUT = Path(__file__).resolve().parents[1] / "routes" / "monterrey_routes.json"

ORIGIN = {"name": "Centro de distribución Library, Barrio Antiguo", "lat": 25.6686, "lng": -100.3090}
DESTINATIONS = [
    ("Tecnológico de Monterrey", 25.6513, -100.2895),
    ("Parque Fundidora", 25.6785, -100.2855),
    ("UANL Ciudad Universitaria", 25.7262, -100.3110),
    ("Valle Oriente", 25.6490, -100.3380),
    ("Colonia Contry", 25.6170, -100.2740),
    ("Cumbres", 25.7480, -100.4000),
    ("Mitras Centro", 25.6980, -100.3460),
    ("Obispado", 25.6760, -100.3440),
]


def fetch_route(destination_lat, destination_lng):
    url = (
        f"{OSRM}/{ORIGIN['lng']},{ORIGIN['lat']};{destination_lng},{destination_lat}"
        "?overview=full&geometries=geojson"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "library-tracking-route-generator"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if payload.get("code") != "Ok":
        raise RuntimeError(f"OSRM respondio {payload.get('code')}")
    return payload["routes"][0]


def main():
    routes = []
    for index, (name, lat, lng) in enumerate(DESTINATIONS, start=1):
        route = fetch_route(lat, lng)
        points = [[round(point[1], 6), round(point[0], 6)] for point in route["geometry"]["coordinates"]]
        routes.append({
            "id": f"MTY-{index:02d}",
            "destination": name,
            "distance_m": round(route["distance"]),
            "points": points,
        })
        print(f"{name}: {len(points)} puntos, {route['distance'] / 1000:.1f} km")
        time.sleep(1)  # El servidor publico de OSRM pide un uso moderado.
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps({"origin": ORIGIN, "source": "OSRM / OpenStreetMap", "routes": routes}, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
