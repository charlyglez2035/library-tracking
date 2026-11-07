# library-tracking

Servidor de rastreo GPS simulado para los microservicios de Library: Flask + Flask-Sock, puerto 5006.
Agrega códigos de rastreo, entregas simuladas por WebSocket sobre rutas reales de Monterrey y una pasarela de pago simulada.

No guarda pedidos ni pagos: reenvía el JWT del usuario a `pedidos`, que decide si el token es válido y si el pedido es suyo.

## Desplegar en la VM

```bash
git clone https://github.com/charlyglez2035/library-tracking.git
cd library-tracking
bash desplegar-vm.sh
```

El script enciende PostgreSQL, Redis y los seis microservicios si hacen falta, instala las dependencias en `.venv`, crea `.env` y arranca el servidor. Se puede repetir. El registro queda en `tracking.log`.

Pide la contraseña del usuario `admin` de Library, porque la pasarela simulada la usa para aprobar pagos. Las cuentas del seed sin hash bcrypt no pueden iniciar sesión, así que esa contraseña se les asigna. Las cuentas que ya funcionan no cambian.

## Pruebas

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests
```
