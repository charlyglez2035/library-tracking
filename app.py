"""Servidor de rastreo GPS simulado: Flask + Flask-Sock, puerto 5006.

Adaptacion minima para lo que pedidos y pagos no ofrecen: codigos de rastreo,
simulacion de entregas y la pasarela de pago simulada. El README de la raiz lo explica.
"""
import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_sock import Sock
from simple_websocket import ConnectionClosed

load_dotenv(Path(__file__).with_name(".env"))

import simulator  # noqa: E402
from store import CODE_PATTERN, TrackingStore  # noqa: E402
from upstream import Services, UpstreamError  # noqa: E402

TRACKABLE_STATUSES = ("paid", "shipped", "delivered")
ORDER_SYNC_STEPS = ("paid", "shipped", "delivered")
AUTH_RECHECK_SECONDS = 30

# Codigos de cierre WebSocket de la aplicacion (rango 4000-4999), analogos a HTTP.
WS_CLOSE = {401: 4401, 403: 4403, 404: 4404, 409: 4409}


def bearer_token():
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


class ApiError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.message = message
        self.status = status


def create_app(services=None, store=None, routes=None, timing=None, tick_seconds=None):
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
    sock = Sock(app)

    services = services or Services()
    store = store or TrackingStore(os.getenv(
        "TRACKING_DB", str(Path(__file__).with_name("tracking.db"))
    ))
    routes = routes or simulator.load_routes()
    timing = timing or simulator.Timing.from_env()
    tick = tick_seconds if tick_seconds is not None else float(os.getenv("TRACKING_TICK_SECONDS", "1"))

    def require_token():
        token = bearer_token()
        if not token:
            raise ApiError("Bearer token ausente.", 401)
        return token

    def authorized_order(token, order_id):
        """Pedidos decide si el JWT es valido y si el pedido pertenece al usuario."""
        order = services.get_order(token, order_id)
        if order["status"] not in TRACKABLE_STATUSES:
            raise ApiError(
                f"El pedido está {order['status']}; solo se rastrean compras pagadas.", 409
            )
        return order

    def describe(shipment):
        route = simulator.route_for_order(routes, shipment["order_id"])
        return {
            "tracking_code": shipment["tracking_code"],
            "order_id": shipment["order_id"],
            "vehicle_id": simulator.vehicle_for_order(route, shipment["order_id"]),
            "route": route.public(),
        }

    def state_of(shipment):
        route = simulator.route_for_order(routes, shipment["order_id"])
        return simulator.state_at(route, time.time() - shipment["started_at"], timing)

    def sync_order_status(shipment, state):
        """Refleja envio y entrega en pedidos; sin cuenta de servicio solo se omite."""
        if not services.can_act_as_admin:
            return
        target = {
            simulator.SHIPPED: "shipped",
            simulator.IN_TRANSIT: "shipped",
            simulator.DELIVERED: "delivered",
        }.get(state["status"])
        if target is None:
            return
        for previous, next_status in zip(ORDER_SYNC_STEPS, ORDER_SYNC_STEPS[1:]):
            if ORDER_SYNC_STEPS.index(next_status) > ORDER_SYNC_STEPS.index(target):
                break
            if not store.claim_sync(shipment["tracking_code"], previous, next_status):
                continue
            try:
                services.set_order_status(shipment["order_id"], next_status)
            except UpstreamError as error:
                # 409: el pedido ya estaba en ese estado o un admin lo cambio; no se reintenta.
                app.logger.warning(
                    "No se sincronizo el pedido %s a %s: %s",
                    shipment["order_id"], next_status, error.message,
                )

    def find_authorized(token, code):
        if not CODE_PATTERN.fullmatch(code):
            raise ApiError("Código de rastreo inválido.", 404)
        shipment = store.find_by_code(code)
        if shipment is None:
            raise ApiError("Código de rastreo no encontrado.", 404)
        authorized_order(token, shipment["order_id"])
        return shipment

    @app.errorhandler(ApiError)
    def api_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(UpstreamError)
    def upstream_error(error):
        return jsonify(error=error.message), error.status

    @app.get("/health")
    def health():
        return jsonify(
            status="ok",
            routes=len(routes),
            payment_gateway="configured" if services.can_act_as_admin else "not_configured",
        )

    @app.post("/tracking")
    def create_tracking():
        token = require_token()
        body = request.get_json(silent=True)
        order_id = body.get("order_id") if isinstance(body, dict) else None
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 1:
            raise ApiError("order_id debe ser un entero positivo.", 400)
        authorized_order(token, order_id)
        shipment = store.get_or_create(order_id)
        state = state_of(shipment)
        sync_order_status(shipment, state)
        return jsonify(**describe(shipment), state=state), 201

    @app.get("/tracking")
    def list_tracking():
        orders = services.list_orders(require_token())
        shipments = store.find_by_orders([order["order_id"] for order in orders])
        return jsonify([
            {
                "tracking_code": shipment["tracking_code"],
                "order_id": shipment["order_id"],
                "status": state_of(shipment)["status"],
                "status_label": state_of(shipment)["status_label"],
            }
            for shipment in shipments
        ])

    @app.get("/tracking/<code>")
    def get_tracking(code):
        shipment = find_authorized(require_token(), code)
        state = state_of(shipment)
        sync_order_status(shipment, state)
        return jsonify(**describe(shipment), state=state)

    @app.post("/simulacion/pagos/<int:payment_id>/autorizar")
    def authorize_payment(payment_id):
        """Pasarela simulada: pagos sigue siendo quien cambia y guarda el estado."""
        token = require_token()
        services.get_payment(token, payment_id)  # 401/403/404 si no es del usuario.
        return jsonify(message="Pago procesado.", payment=services.approve_payment(payment_id))

    @sock.route("/ws/tracking/<code>")
    def tracking_socket(ws, code):
        token = bearer_token()
        try:
            if not token:
                raise ApiError("Bearer token ausente.", 401)
            shipment = find_authorized(token, code)
            state = state_of(shipment)
            ws.send(json.dumps({"type": "snapshot", **describe(shipment), "state": state}))
            last_auth_check = time.monotonic()
            while state["status"] != simulator.DELIVERED:
                time.sleep(tick)
                if time.monotonic() - last_auth_check >= AUTH_RECHECK_SECONDS:
                    # Un logout o un token vencido tambien termina la transmision.
                    authorized_order(token, shipment["order_id"])
                    last_auth_check = time.monotonic()
                state = state_of(shipment)
                sync_order_status(shipment, state)
                ws.send(json.dumps({"type": "position", "state": state}))
            sync_order_status(shipment, state)
            ws.close(1000, "Entrega completada.")
        except (ApiError, UpstreamError) as error:
            ws.close(WS_CLOSE.get(error.status, 1011), error.message[:120])
        except ConnectionClosed:
            pass

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run(
        host=os.getenv("TRACKING_HOST", "0.0.0.0"),
        port=int(os.getenv("TRACKING_PORT", "5006")),
        threaded=True,
    )
