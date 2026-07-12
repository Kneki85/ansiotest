NOVEDAD: LOGIN

Ahora la app pide iniciar sesión. La primera vez, entra a /registro y crea tu
cuenta (nombre, correo, contraseña). Las veces siguientes usa /login.

CONFIGURAR LA BASE DE DATOS (MySQL) — SOLO LA PRIMERA VEZ

1. Copia el archivo ".env.example" y renómbralo a ".env"
2. Rellena tus datos reales de Aiven (host, puerto, usuario, clave, nombre de la base)
3. Nunca subas el archivo ".env" a GitHub (ya está en .gitignore, no hace falta que hagas nada extra)

INSTALAR DEPENDENCIAS

pip install -r requirements.txt

EJECUTAR 

python app.py

ENTORNO VIRTUAL



MEJOR ESTO QUE FUNCIOCE DE TIRON OJALA

python -m venv .venv; Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt; python app.py


EN LINUX

CREAR EL ENTORNO VIRTUAL

python3 -m venv .venv

ACTIVAR EL ENTORNO VIRTUAL

source .venv/bin/activate

INSTALAR LAS DEPENDENCIAS

pip install -r requirements.txt

EJECUTAR

python app.py

para salir de modo virtual
deactivate


