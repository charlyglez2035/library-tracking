"""Simulador GPS: calcula la posicion del vehiculo a partir del tiempo transcurrido.

La simulacion no guarda posiciones: el estado es una funcion pura de
(ruta, segundos desde el inicio). Asi varios clientes ven lo mismo, una reconexion
continua donde iba la entrega y "Entregado" solo ocurre al terminar la ruta.
"""
import json
import math
import os
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

ROUTES_FILE = Path(__file__).with_name("routes") / "monterrey_routes.json"

CONFIRMED = "confirmed"
PREPARING = "preparing"
SHIPPED = "shipped"
IN_TRANSIT = "in_transit"
DELIVERED = "delivered"

STATUS_LABELS = {
    CONFIRMED: "Pedido confirmado",
    PREPARING: "Preparando pedido",
    SHIPPED: "Pedido enviado",
    IN_TRANSIT: "En ruta de entrega",
    DELIVERED: "Entregado",
}


@dataclass(frozen=True)
class Timing:
    """Segundos de cada etapa; la entrega dura lo mismo sea cual sea la distancia."""
    confirmed: float = 6
    preparing: float = 8
    shipped: float = 6
    driving: float = 150

    @classmethod
    def from_env(cls):
        return cls(
            confirmed=float(os.getenv("SIM_CONFIRMED_SECONDS", cls.confirmed)),
            preparing=float(os.getenv("SIM_PREPARING_SECONDS", cls.preparing)),
            shipped=float(os.getenv("SIM_SHIPPED_SECONDS", cls.shipped)),
            driving=float(os.getenv("SIM_DRIVING_SECONDS", cls.driving)),
        )

    @property
    def departure(self):
        return self.confirmed + self.preparing + self.shipped


def haversine_m(a, b):
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def bearing_deg(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    delta = math.radians(b[1] - a[1])
    y = math.sin(delta) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


class Route:
    def __init__(self, route_id, destination, points, origin_name):
        if len(points) < 2:
            raise ValueError(f"La ruta {route_id} necesita al menos dos puntos.")
        self.id = route_id
        self.destination = destination
        self.origin_name = origin_name
        self.points = points
        self.cumulative = [0.0]
        for start, end in zip(points, points[1:]):
            self.cumulative.append(self.cumulative[-1] + haversine_m(start, end))
        self.length_m = self.cumulative[-1]

    def locate(self, distance):
        """Devuelve (lat, lng, rumbo, indice del ultimo punto ya recorrido)."""
        distance = min(max(distance, 0.0), self.length_m)
        index = min(bisect_right(self.cumulative, distance) - 1, len(self.points) - 2)
        start, end = self.points[index], self.points[index + 1]
        span = self.cumulative[index + 1] - self.cumulative[index]
        ratio = (distance - self.cumulative[index]) / span if span else 0.0
        return (
            start[0] + (end[0] - start[0]) * ratio,
            start[1] + (end[1] - start[1]) * ratio,
            bearing_deg(start, end),
            index,
        )

    def public(self):
        return {
            "id": self.id,
            "origin": self.origin_name,
            "destination": self.destination,
            "distance_m": round(self.length_m),
            "points": self.points,
        }


def load_routes(path=ROUTES_FILE):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    origin = data["origin"]["name"]
    return [Route(r["id"], r["destination"], r["points"], origin) for r in data["routes"]]


def route_for_order(routes, order_id):
    """Asignacion estable: el mismo pedido siempre recorre la misma ruta."""
    return routes[order_id % len(routes)]


def vehicle_for_order(route, order_id):
    return f"REP-{route.id.split('-')[1]}-{order_id:05d}"


def state_at(route, elapsed, timing):
    """Estado de la entrega a los `elapsed` segundos de iniciar la simulacion."""
    elapsed = max(elapsed, 0.0)
    driven = elapsed - timing.departure
    progress = min(max(driven / timing.driving, 0.0), 1.0)
    distance = progress * route.length_m

    if progress >= 1.0:
        status = DELIVERED  # Unico camino a Entregado: haber agotado la ruta.
    elif driven >= 0:
        status = IN_TRANSIT
    elif elapsed >= timing.confirmed + timing.preparing:
        status = SHIPPED
    elif elapsed >= timing.confirmed:
        status = PREPARING
    else:
        status = CONFIRMED

    lat, lng, heading, index = route.locate(distance)
    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "heading": round(heading, 1),
        "progress": round(progress, 4),
        "traveled_index": index,
        "remaining_m": round(route.length_m - distance),
        "eta_seconds": round(max(timing.departure + timing.driving - elapsed, 0)),
    }
