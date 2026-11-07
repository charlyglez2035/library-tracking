"""Clientes REST de los microservicios existentes (login, pedidos y pagos).

El rastreo no comparte JWT_SECRET ni toca PostgreSQL: reenvia el Bearer del usuario
a pedidos y pagos, que ya validan firma, expiracion, revocacion y propiedad.
"""
import os
import threading

import requests

TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "8"))


class UpstreamError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _call(method, url, token=None, payload=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.request(
            method, url, params={"format": "json"}, headers=headers, json=payload, timeout=TIMEOUT
        )
    except requests.RequestException as error:
        raise UpstreamError(503, "Un microservicio requerido no está disponible.") from error
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400:
        message = body.get("error") if isinstance(body, dict) else None
        raise UpstreamError(response.status_code, message or "Error en el microservicio.")
    return body


class Services:
    def __init__(self):
        self.login_url = os.getenv("LOGIN_SERVICE_URL", "http://127.0.0.1:5000").rstrip("/")
        self.pedidos_url = os.getenv("PEDIDOS_SERVICE_URL", "http://127.0.0.1:5004").rstrip("/")
        self.pagos_url = os.getenv("PAGOS_SERVICE_URL", "http://127.0.0.1:5005").rstrip("/")
        self.identity = os.getenv("SERVICE_ADMIN_IDENTITY", "")
        self.password = os.getenv("SERVICE_ADMIN_PASSWORD", "")
        self._admin_token = None
        self._lock = threading.Lock()

    # ---------- Operaciones con la identidad del usuario ----------

    def get_order(self, token, order_id):
        return _call("GET", f"{self.pedidos_url}/pedidos/{order_id}", token)

    def list_orders(self, token):
        return _call("GET", f"{self.pedidos_url}/pedidos", token)

    def get_payment(self, token, payment_id):
        return _call("GET", f"{self.pagos_url}/pagos/{payment_id}", token)

    # ---------- Operaciones con la cuenta de servicio ----------

    @property
    def can_act_as_admin(self):
        return bool(self.identity and self.password)

    def _login_admin(self):
        body = _call("POST", f"{self.login_url}/login", payload={
            "identity": self.identity, "password": self.password,
        })
        if body.get("role_id") != "admin":
            raise UpstreamError(503, "La cuenta de servicio no tiene rol admin.")
        return body["access_token"]

    def _as_admin(self, method, url, payload):
        if not self.can_act_as_admin:
            raise UpstreamError(503, "La pasarela simulada no tiene cuenta de servicio configurada.")
        with self._lock:
            token = self._admin_token or self._login_admin()
            self._admin_token = token
        try:
            return _call(method, url, token, payload)
        except UpstreamError as error:
            if error.status != 401:
                raise
        with self._lock:  # Token vencido o revocado: se renueva una vez.
            self._admin_token = self._login_admin()
            token = self._admin_token
        return _call(method, url, token, payload)

    def approve_payment(self, payment_id):
        return _as_payment(self._as_admin(
            "PATCH", f"{self.pagos_url}/pagos/{payment_id}", {"status": "approved"}
        ))

    def set_order_status(self, order_id, status):
        return self._as_admin("PATCH", f"{self.pedidos_url}/pedidos/{order_id}", {"status": status})


def _as_payment(body):
    return body["payment"]
