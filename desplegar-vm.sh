#!/usr/bin/env bash
# Despliega el servidor de rastreo en la VM donde ya esta instalado Library.
# Enciende PostgreSQL, Redis y los seis microservicios si hacen falta, y arranca tracking en 5006.
# Se puede repetir sin riesgo. Uso: bash desplegar-vm.sh
# LIBRARY_DIR indica la carpeta de Library si no se encuentra sola.
set -u

AQUI="$(cd "$(dirname "$0")" && pwd)"
cd "$AQUI"

fallar() { echo "ERROR: $*" >&2; exit 1; }
paso() { echo; echo "== $*"; }
salud() { curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$1/health?format=json"; }
servicios_sanos() {
  for puerto in 5000 5001 5002 5003 5004 5005; do
    [ "$(salud "$puerto")" = 200 ] || return 1
  done
}
con_sudo() { if sudo -n true 2>/dev/null; then sudo "$@"; else return 1; fi; }

paso "1/6 Buscando Library"
LIB="${LIBRARY_DIR:-}"
if [ -z "$LIB" ]; then
  encontrados="$(con_sudo find /home /opt /srv /var/www -maxdepth 7 -path '*/apps/services/pedidos/app.py' \
    -not -path '*/node_modules/*' 2>/dev/null \
    || find "$HOME" -maxdepth 7 -path '*/apps/services/pedidos/app.py' 2>/dev/null)"
  encontrados="$(echo "$encontrados" | sort -u)"
  [ -n "$encontrados" ] || fallar "No encontre Library. Indica la carpeta: LIBRARY_DIR=/ruta bash desplegar-vm.sh"
  if [ "$(echo "$encontrados" | wc -l)" -gt 1 ]; then
    echo "$encontrados" | sed 's#/apps/services/pedidos/app.py##' >&2
    fallar "Hay varias copias de Library. Elige una: LIBRARY_DIR=/ruta bash desplegar-vm.sh"
  fi
  LIB="${encontrados%/apps/services/pedidos/app.py}"
fi
[ -f "$LIB/apps/services/pedidos/app.py" ] || fallar "$LIB no contiene apps/services/pedidos/app.py"
echo "Library: $LIB"

DUENO="$(stat -c %U "$LIB")"
[ "$DUENO" = "$(id -un)" ] || fallar "Library pertenece a '$DUENO'. Entra como ese usuario y repite: sudo su - $DUENO"

paso "2/6 Encendiendo PostgreSQL, Redis y los microservicios"
con_sudo systemctl start postgresql 2>/dev/null
for redis in redis-server redis valkey; do con_sudo systemctl start "$redis" 2>/dev/null; done
if servicios_sanos; then
  echo "Los seis microservicios ya responden."
else
  LEVANTAR="$LIB/levanta_todo.sh"
  [ -f "$LEVANTAR" ] || LEVANTAR="$LIB/scripts_vm/levanta_todo.sh"
  [ -f "$LEVANTAR" ] || fallar "No encontre levanta_todo.sh en $LIB"
  bash "$LEVANTAR" stop >/dev/null 2>&1
  bash "$LEVANTAR"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    servicios_sanos && break
    sleep 3
  done
  servicios_sanos || fallar "Algun microservicio no responde 200. Revisa $LIB/logs/ (503 = falta PostgreSQL o Redis)."
fi

paso "3/6 Instalando dependencias de tracking"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv 2>/dev/null || {
    rm -rf .venv
    con_sudo apt-get install -y python3-venv >/dev/null || fallar "Falta python3-venv: sudo apt install python3-venv"
    python3 -m venv .venv || fallar "No se pudo crear el entorno virtual."
  }
fi
.venv/bin/pip install -q -r requirements.txt bcrypt || fallar "No se pudieron instalar las dependencias."

probar_login() {
  .venv/bin/python - <<'PY'
import os, sys
import requests
from dotenv import dotenv_values
env = dotenv_values(".env")
r = requests.post("http://127.0.0.1:5000/login", params={"format": "json"}, timeout=10, json={
    "identity": env["SERVICE_ADMIN_IDENTITY"], "password": env["SERVICE_ADMIN_PASSWORD"],
})
sys.exit(0 if r.status_code == 200 and r.json().get("user", {}).get("role", "admin") == "admin" else 1)
PY
}

paso "4/6 Configurando la cuenta de servicio"
if [ -f .env ] && probar_login; then
  echo ".env ya existe y su cuenta inicia sesion."
else
  rm -f .env
  leer() {
    for archivo in "$LIB/.env" "$LIB/apps/services/pedidos/.env"; do
      valor="$(grep -E "^$1=" "$archivo" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' | sed -e "s/^[\"']//" -e "s/[\"']\$//")"
      [ -n "$valor" ] && { printf '%s' "$valor"; return; }
    done
  }
  export PGPASSWORD; PGPASSWORD="$(leer DB_PASSWORD)"
  [ -n "$PGPASSWORD" ] || fallar "No encontre DB_PASSWORD en $LIB/.env"
  sql() {
    psql -X -q -tA -v ON_ERROR_STOP=1 -h "$(leer DB_HOST)" -p "$(leer DB_PORT)" \
      -U "$(leer DB_USER)" -d "$(leer DB_NAME)"
  }
  ADMIN="$(echo "SELECT email FROM app_user WHERE role = 'admin';" | sql)" \
    || fallar "No se pudo consultar PostgreSQL con los datos de $LIB/.env"
  [ -n "$ADMIN" ] || fallar "La base no tiene un usuario admin."
  echo "Administrador: $ADMIN"
  echo "Escribe su contrasena. Si esa cuenta aun no tiene una valida, sera la nueva"
  echo "contrasena de TODAS las cuentas del seed que hoy no pueden iniciar sesion."
  CLAVE="${TRACKING_ADMIN_PASSWORD:-}"
  if [ -z "$CLAVE" ]; then
    read -rsp "Contrasena: " CLAVE; echo
  fi
  [ ${#CLAVE} -ge 10 ] || fallar "Usa al menos 10 caracteres."
  case "$CLAVE" in *\'*|*\\*) fallar "La contrasena no puede llevar comillas simples ni diagonales invertidas." ;; esac

  # Solo cambia cuentas sin hash bcrypt: login no puede validarlas, asi que no rompe ninguna que funcione.
  HASH="$(CLAVE="$CLAVE" .venv/bin/python -c 'import bcrypt, os; print(bcrypt.hashpw(os.environ["CLAVE"].encode(), bcrypt.gensalt()).decode())')"
  ACTIVADAS="$(sql <<SQL
UPDATE app_user SET password_hash = '$HASH', email_verified = TRUE
WHERE password_hash NOT LIKE '\$2%' RETURNING email;
SQL
)" || fallar "No se pudieron actualizar las cuentas del seed."
  [ -z "$ACTIVADAS" ] || { echo "Cuentas activadas con esa contrasena:"; echo "$ACTIVADAS" | sed 's/^/  /'; }

  umask 077
  {
    grep -v '^SERVICE_ADMIN_' .env.example
    echo "SERVICE_ADMIN_IDENTITY=$ADMIN"
    echo "SERVICE_ADMIN_PASSWORD='$CLAVE'"
  } > .env
  unset CLAVE HASH PGPASSWORD
  probar_login || { rm -f .env; fallar "$ADMIN no pudo iniciar sesion: la contrasena no es la correcta."; }
  echo "La cuenta de servicio inicia sesion."
fi

paso "5/6 Arrancando tracking"
pkill -f "$AQUI/app.py" 2>/dev/null
sleep 1
nohup "$AQUI/.venv/bin/python" "$AQUI/app.py" >> tracking.log 2>&1 &
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ "$(salud 5006)" = 200 ] && break
  sleep 1
done
[ "$(salud 5006)" = 200 ] || { tail -15 tracking.log >&2; fallar "tracking no responde en 5006."; }

paso "6/6 Listo"
for puerto in 5000 5001 5002 5003 5004 5005 5006; do echo "$puerto: $(salud "$puerto")"; done
IP="$(curl -s -m 2 -H 'Metadata-Flavor: Google' \
  http://169.254.169.254/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)"
echo "IP publica para la app de escritorio: ${IP:-no disponible}"
echo "Falta abrir el puerto 5006 en el firewall de GCP si aun no se hizo."
