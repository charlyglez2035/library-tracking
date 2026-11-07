"""Pruebas del simulador y de la autorizacion, con pedidos y pagos simulados en memoria."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import simulator  # noqa: E402
from app import create_app  # noqa: E402
from simulator import Route, Timing, state_at  # noqa: E402
from store import TrackingStore  # noqa: E402
from upstream import UpstreamError  # noqa: E402

TIMING = Timing(confirmed=2, preparing=2, shipped=2, driving=10)
ROUTE = Route("MTY-01", "Destino", [[25.67, -100.31], [25.68, -100.31], [25.68, -100.30]], "Origen")
USERS = {"token-ana": 1, "token-luis": 2}


class FakeServices:
    can_act_as_admin = True

    def __init__(self):
        self.orders = {
            7: {"order_id": 7, "user_id": 1, "status": "paid"},
            8: {"order_id": 8, "user_id": 2, "status": "paid"},
            9: {"order_id": 9, "user_id": 1, "status": "pending"},
        }
        self.payments = {30: {"payment_id": 30, "user_id": 1, "status": "pending"}}
        self.status_changes = []

    def _user(self, token):
        if token not in USERS:
            raise UpstreamError(401, "Bearer token invalido.")
        return USERS[token]

    def get_order(self, token, order_id):
        user = self._user(token)
        order = self.orders.get(order_id)
        if order is None:
            raise UpstreamError(404, "Pedido no encontrado.")
        if order["user_id"] != user:
            raise UpstreamError(403, "No tienes permisos para este pedido.")
        return order

    def list_orders(self, token):
        user = self._user(token)
        return [order for order in self.orders.values() if order["user_id"] == user]

    def get_payment(self, token, payment_id):
        user = self._user(token)
        payment = self.payments[payment_id]
        if payment["user_id"] != user:
            raise UpstreamError(403, "No tienes permisos para este pago.")
        return payment

    def approve_payment(self, payment_id):
        self.payments[payment_id]["status"] = "approved"
        return self.payments[payment_id]

    def set_order_status(self, order_id, status):
        self.status_changes.append((order_id, status))
        self.orders[order_id]["status"] = status


@pytest.fixture
def services():
    return FakeServices()


@pytest.fixture
def client(services, tmp_path):
    routes = [Route(f"MTY-{i:02d}", f"Destino {i}", ROUTE.points, "Origen") for i in range(1, 9)]
    app = create_app(services, TrackingStore(tmp_path / "t.db"), routes, TIMING, tick_seconds=0)
    return app.test_client()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("elapsed, status", [
    (0, "confirmed"), (2, "preparing"), (4, "shipped"), (6, "in_transit"), (15.9, "in_transit"),
    (16, "delivered"), (500, "delivered"),
])
def test_stages_follow_the_timeline(elapsed, status):
    assert state_at(ROUTE, elapsed, TIMING)["status"] == status


def test_delivered_only_at_the_end_of_the_route():
    before = state_at(ROUTE, 15.9, TIMING)
    after = state_at(ROUTE, 16, TIMING)
    assert before["status"] != "delivered" and before["remaining_m"] > 0
    assert after["remaining_m"] == 0 and after["progress"] == 1
    assert [after["lat"], after["lng"]] == ROUTE.points[-1]


def test_vehicle_waits_at_origin_and_then_advances():
    assert [state_at(ROUTE, 5, TIMING)[key] for key in ("lat", "lng")] == ROUTE.points[0]
    middle = state_at(ROUTE, 11, TIMING)
    assert 0 < middle["progress"] < 1 and middle["traveled_index"] >= 0


def test_real_routes_are_all_different_and_stay_in_monterrey():
    routes = simulator.load_routes()
    assert len({route.destination for route in routes}) == len(routes) >= 8
    for route in routes:
        assert route.length_m > 1000
        assert all(25.5 < lat < 25.85 and -100.5 < lng < -100.15 for lat, lng in route.points)


def test_different_orders_get_different_routes_and_vehicles():
    routes = simulator.load_routes()
    first, second = (simulator.route_for_order(routes, order) for order in (7, 8))
    assert first.id != second.id
    assert simulator.vehicle_for_order(first, 7) != simulator.vehicle_for_order(second, 8)


def test_code_is_stable_for_the_same_order(client):
    first = client.post("/tracking", json={"order_id": 7}, headers=auth("token-ana")).get_json()
    second = client.post("/tracking", json={"order_id": 7}, headers=auth("token-ana")).get_json()
    assert first["tracking_code"] == second["tracking_code"]
    assert first["tracking_code"].startswith("LIB-MTY-") and first["tracking_code"].endswith("000007")


def test_tracking_requires_a_token(client):
    assert client.post("/tracking", json={"order_id": 7}).status_code == 401
    assert client.get("/tracking").status_code == 401


def test_unpaid_order_cannot_be_tracked(client):
    response = client.post("/tracking", json={"order_id": 9}, headers=auth("token-ana"))
    assert response.status_code == 409


def test_foreign_order_cannot_get_a_code(client):
    response = client.post("/tracking", json={"order_id": 8}, headers=auth("token-ana"))
    assert response.status_code == 403


def test_foreign_code_cannot_be_read(client):
    code = client.post("/tracking", json={"order_id": 8}, headers=auth("token-luis")).get_json()["tracking_code"]
    assert client.get(f"/tracking/{code}", headers=auth("token-ana")).status_code == 403
    assert client.get(f"/tracking/{code}", headers=auth("token-luis")).status_code == 200


def test_list_only_contains_own_shipments(client):
    client.post("/tracking", json={"order_id": 7}, headers=auth("token-ana"))
    client.post("/tracking", json={"order_id": 8}, headers=auth("token-luis"))
    listed = client.get("/tracking", headers=auth("token-ana")).get_json()
    assert [item["order_id"] for item in listed] == [7]


def test_unknown_code_is_not_found(client):
    assert client.get("/tracking/LIB-MTY-2026-999999", headers=auth("token-ana")).status_code == 404
    assert client.get("/tracking/x", headers=auth("token-ana")).status_code == 404


def test_gateway_checks_owner_before_approving(client, services):
    response = client.post("/simulacion/pagos/30/autorizar", headers=auth("token-luis"))
    assert response.status_code == 403
    assert services.payments[30]["status"] == "pending"


def test_gateway_returns_the_state_from_pagos(client, services):
    response = client.post("/simulacion/pagos/30/autorizar", headers=auth("token-ana"))
    assert response.status_code == 200
    assert response.get_json()["payment"]["status"] == "approved"


def test_order_status_is_synced_once_when_delivery_finishes(services, tmp_path):
    store = TrackingStore(tmp_path / "t.db")
    routes = [ROUTE]
    instant = Timing(confirmed=0, preparing=0, shipped=0, driving=0.001)
    client = create_app(services, store, routes, instant, tick_seconds=0).test_client()
    store.get_or_create(7)
    code = store.build_code(7)
    with store._connect() as conn:
        conn.execute("UPDATE shipment SET started_at = 0")
    client.get(f"/tracking/{code}", headers=auth("token-ana"))
    client.get(f"/tracking/{code}", headers=auth("token-ana"))
    assert services.status_changes == [(7, "shipped"), (7, "delivered")]


def _socket_messages(app, code, token):
    """Ejecuta el handler WebSocket sobre un socket falso y devuelve lo que envio."""
    import flask_sock

    sent, closed = [], []

    class FakeServer:
        mode = "werkzeug"
        connected = False

        def __init__(self, environ, **_options):
            pass

        def send(self, message):
            sent.append(json.loads(message))

        def close(self, reason=None, message=None):
            closed.append((reason, message))

    original = flask_sock.Server
    flask_sock.Server = FakeServer
    try:
        handshake = {"Connection": "Upgrade", "Upgrade": "websocket", **auth(token)}
        app.test_client().get(f"/ws/tracking/{code}", headers=handshake)
    finally:
        flask_sock.Server = original
    return sent, closed


def test_socket_streams_only_to_the_owner(services, tmp_path):
    store = TrackingStore(tmp_path / "t.db")
    instant = Timing(confirmed=0, preparing=0, shipped=0, driving=0.01)
    app = create_app(services, store, [ROUTE], instant, tick_seconds=0.002)
    code = store.get_or_create(7)["tracking_code"]

    sent, closed = _socket_messages(app, code, "token-ana")
    assert sent[0]["type"] == "snapshot" and sent[0]["order_id"] == 7
    assert sent[-1]["state"]["status"] == "delivered"
    assert closed[0][0] == 1000  # Flask-Sock agrega su propio close al terminar el handler.

    sent, closed = _socket_messages(app, code, "token-luis")
    assert sent == [] and closed[0][0] == 4403

    sent, closed = _socket_messages(app, code, "token-falso")
    assert sent == [] and closed[0][0] == 4401
