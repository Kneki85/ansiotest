"""
ansiedad_app v2 — Flask backend con registro de estudiantes e historial
"""

import json
import os
import pickle
import random
import secrets
import smtplib
from datetime import datetime
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pymysql
import pymysql.cursors
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_wtf import CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = BASE_DIR / "model"
MAX_REGISTROS = 100
ZONA_LIMA = ZoneInfo("America/Lima")

# Intentamos cargar un archivo .env local (si existe) para no tener que
# escribir las credenciales de la base de datos a mano cada vez que se
# prueba en la propia compu. En Render, estas variables se configuran
# directamente en el panel de "Environment", no con este archivo.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Credenciales de la base de datos MySQL, leídas desde variables de entorno.
# NUNCA se escriben directo en el código, porque este repositorio es público.
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "ansiedad_app")

# Aiven (y la mayoría de proveedores de MySQL en la nube) exige conexiones
# cifradas (SSL). Si se define DB_SSL_CA (ruta al certificado que te da
# Aiven), la conexión se hace con SSL. Si no está definida (por ejemplo,
# al probar con un MySQL local que no lo exige), se conecta sin SSL.
DB_SSL_CA = os.environ.get("DB_SSL_CA", "")

# Correo para "recuperar contraseña", vía Gmail SMTP. Igual que con la BD,
# las credenciales van solo en variables de entorno (nunca en el código).
# En Gmail, MAIL_PASSWORD tiene que ser una "contraseña de aplicación"
# (no la contraseña normal de la cuenta): myaccount.google.com/apppasswords
MAIL_HOST = os.environ.get("MAIL_HOST", "smtp.gmail.com")
MAIL_PORT = int(os.environ.get("MAIL_PORT", "465"))
MAIL_USER = os.environ.get("MAIL_USER", "")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
RESET_TOKEN_MINUTOS = 30

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32).hex()
if not os.environ.get("SECRET_KEY"):
    print("[aviso] SECRET_KEY no está en las variables de entorno; se generó una temporal "
          "(las sesiones se invalidan en cada reinicio). Configúrala en Render.")

csrf = CSRFProtect(app)

# Carga del modelo y metadatos
with open(MODEL_DIR / "model.pkl", "rb") as f:
    model = pickle.load(f)

with open(MODEL_DIR / "model.json", "r", encoding="utf-8") as f:
    meta = json.load(f)

CLASSES: list[str] = meta["classes"]
FEATURES: list[str] = meta["features"]

# cuentas con acceso al panel de administrador (ven todos los registros,
# no solo los propios). se controla asi, por email, en vez de guardarlo
# en la bd, para no tener que tocar la tabla usuarios ya existente
ADMIN_EMAILS = {"jhonelvissanchezjimenez80@gmail.com"}

# frases cortas para la tarjeta de "recuerda que" en el inicio
FRASES = [
    "Respirar hondo 10 segundos ya es un buen comienzo.",
    "Un examen no define lo que vales.",
    "Está bien pedir ayuda, no es debilidad.",
    "Un paso a la vez es suficiente por hoy.",
    "Avanzar despacio también es avanzar.",
    "Tu ritmo también es válido.",
]

# (valor, etiqueta, color) para el check-in de ánimo del día
ESTADOS_ANIMO = [
    ("bien", "Bien", "#1f9d6c"),
    ("tranquilo", "Tranquilo", "#6e63d6"),
    ("normal", "Normal", "#8d93ab"),
    ("cansado", "Cansado", "#b7791f"),
    ("mal", "Mal", "#d1453d"),
]


def get_db() -> pymysql.connections.Connection:
    """Abre una conexión nueva a la base de datos MySQL.

    cursorclass=DictCursor hace que cada fila se pueda leer como
    diccionario (fila["nombre"]), igual que hacíamos antes con
    sqlite3.Row.
    """
    conexion_kwargs = dict(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    if DB_SSL_CA:
        conexion_kwargs["ssl_ca"] = DB_SSL_CA
        conexion_kwargs["ssl_verify_cert"] = True
    return pymysql.connect(**conexion_kwargs)


def init_db() -> None:
    """Crea la tabla de historial si no existe (persiste entre reinicios,
    porque ahora vive en un servidor MySQL aparte, no en un archivo local)."""
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS evaluaciones (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    nombre VARCHAR(120) NOT NULL,
                    nivel VARCHAR(20) NOT NULL,
                    color VARCHAR(20) NOT NULL,
                    fecha VARCHAR(20) NOT NULL,
                    probabilidades TEXT NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS usuarios (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    nombre VARCHAR(120) NOT NULL,
                    email VARCHAR(150) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    fecha_registro VARCHAR(20) NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            # columnas para "recuperar contraseña"; en un ALTER TABLE aparte
            # porque si el usuario ya tenia la tabla creada de antes, el
            # CREATE TABLE IF NOT EXISTS de arriba no la iba a actualizar.
            for columna_sql in (
                "ALTER TABLE usuarios ADD COLUMN reset_token VARCHAR(120) NULL",
                "ALTER TABLE usuarios ADD COLUMN reset_expira BIGINT NULL",
            ):
                try:
                    cursor.execute(columna_sql)
                except pymysql.err.OperationalError:
                    pass  # la columna ya existe, no hay nada que hacer
        conn.commit()
    finally:
        conn.close()


init_db()


# --- Estructuras de datos (las que pide el curso) ---
# ya tenia lista enlazada y pila del entregable anterior, lo demas
# (cola, lista doble, arreglo y arbol) lo meti ahora que el profe pidio
# que se vea el uso de todas

class NodoLista:
    # nodo de la lista enlazada simple
    def __init__(self, dato):
        self.dato = dato
        self.siguiente = None


class ListaEnlazada:
    # lista enlazada simple, la uso para las recomendaciones (tips) segun el nivel
    def __init__(self):
        self.cabeza = None

    def agregar(self, dato) -> None:
        nuevo_nodo = NodoLista(dato)
        if self.cabeza is None:
            self.cabeza = nuevo_nodo
            return
        actual = self.cabeza
        while actual.siguiente is not None:
            actual = actual.siguiente
        actual.siguiente = nuevo_nodo

    def a_lista(self) -> list:
        # paso los nodos a una lista normal pq el jsonify no puede con nodos
        resultado = []
        actual = self.cabeza
        while actual is not None:
            resultado.append(actual.dato)
            actual = actual.siguiente
        return resultado


class Pila:
    # pila (LIFO) para el botón de deshacer del historial.
    # cuando guardo una evaluacion apilo su id, si le doy a deshacer
    # desapilo y borro esa fila de la bd
    def __init__(self):
        self.elementos: list = []

    def apilar(self, dato) -> None:
        self.elementos.append(dato)

    def desapilar(self):
        if self.esta_vacia():
            return None
        return self.elementos.pop()

    def ver_tope(self):
        if self.esta_vacia():
            return None
        return self.elementos[-1]

    def esta_vacia(self) -> bool:
        return len(self.elementos) == 0


# nota: antes habia una pila_deshacer global aca, pero eso hacia que
# un usuario pudiera deshacer la evaluacion de otro. ahora cada quien
# tiene su propia pila guardada en su sesion (ver /predict y /historial)


# --- de aca para abajo es lo nuevo que pidio el profe ---

class Cola:
    # cola (FIFO), la uso para las busquedas recientes del historial.
    # la primera busqueda que entra es la primera que sale cuando se
    # llena (max 5)
    def __init__(self, capacidad: int = 5):
        self.elementos: list = []
        self.capacidad = capacidad

    def encolar(self, dato) -> None:
        # si ya estaba esa busqueda la quito y la vuelvo a meter al final
        if dato in self.elementos:
            self.elementos.remove(dato)
        self.elementos.append(dato)
        if len(self.elementos) > self.capacidad:
            self.desencolar()

    def desencolar(self):
        if self.esta_vacia():
            return None
        return self.elementos.pop(0)

    def esta_vacia(self) -> bool:
        return len(self.elementos) == 0

    def a_lista(self) -> list:
        return list(reversed(self.elementos))  # la mas reciente primero


class NodoDoble:
    # nodo con puntero al anterior y al siguiente (para recorrer los 2 lados)
    def __init__(self, dato):
        self.dato = dato
        self.anterior = None
        self.siguiente = None


class ListaDoble:
    # lista doblemente enlazada, para moverme entre registros del
    # historial (anterior/siguiente) sin usar indices de una lista comun
    def __init__(self):
        self.cabeza = None
        self.cola = None

    def agregar(self, dato) -> None:
        nuevo_nodo = NodoDoble(dato)
        if self.cabeza is None:
            self.cabeza = nuevo_nodo
            self.cola = nuevo_nodo
            return
        nuevo_nodo.anterior = self.cola
        self.cola.siguiente = nuevo_nodo
        self.cola = nuevo_nodo

    def buscar_nodo(self, condicion):
        # recorre y devuelve el primer nodo que cumpla la condicion
        actual = self.cabeza
        while actual is not None:
            if condicion(actual.dato):
                return actual
            actual = actual.siguiente
        return None


class ArregloCircular:
    # arreglo de tamaño fijo (buffer circular). a diferencia de una lista
    # normal que crece sin limite, este tiene capacidad fija y cuando se
    # llena el dato nuevo pisa al mas viejo. lo uso para la tendencia de
    # los ultimos resultados del estudiante
    def __init__(self, capacidad: int = 5):
        self.capacidad = capacidad
        self.datos = [None] * capacidad
        self.siguiente_indice = 0
        self.cantidad = 0

    def agregar(self, dato) -> None:
        self.datos[self.siguiente_indice] = dato
        self.siguiente_indice = (self.siguiente_indice + 1) % self.capacidad
        self.cantidad = min(self.cantidad + 1, self.capacidad)

    def obtener_todos(self) -> list:
        # devuelve del mas viejo al mas nuevo dentro de la ventana actual
        if self.cantidad < self.capacidad:
            return [d for d in self.datos[: self.cantidad]]
        inicio = self.siguiente_indice
        return self.datos[inicio:] + self.datos[:inicio]


class NodoArbol:
    def __init__(self, clave, dato):
        self.clave = clave
        self.dato = dato
        self.izquierda = None
        self.derecha = None


class ArbolBusqueda:
    # arbol binario de busqueda, ordenado por nombre en minuscula.
    # lo uso para el buscador del historial: inserto cada registro como
    # nodo y despues recorro el arbol "en orden" para listar lo que
    # coincida con la busqueda
    def __init__(self):
        self.raiz = None

    def insertar(self, clave, dato) -> None:
        self.raiz = self._insertar_nodo(self.raiz, clave, dato)

    def _insertar_nodo(self, nodo, clave, dato):
        if nodo is None:
            return NodoArbol(clave, dato)
        if clave < nodo.clave:
            nodo.izquierda = self._insertar_nodo(nodo.izquierda, clave, dato)
        else:
            nodo.derecha = self._insertar_nodo(nodo.derecha, clave, dato)
        return nodo

    def buscar_por_texto(self, texto: str) -> list:
        # recorrido en orden (in-order), ya sale ordenado alfabeticamente
        resultados = []
        self._recorrer_en_orden(self.raiz, texto.lower().strip(), resultados)
        return resultados

    def _recorrer_en_orden(self, nodo, texto, resultados) -> None:
        if nodo is None:
            return
        self._recorrer_en_orden(nodo.izquierda, texto, resultados)
        if texto == "" or texto in nodo.clave:
            resultados.append(nodo.dato)
        self._recorrer_en_orden(nodo.derecha, texto, resultados)

RISK_INFO: dict[str, dict] = {
    "Bajo": {
        "color": "green",
        "icon": "✓",
        "description": "Tu perfil indica un nivel de ansiedad bajo. Mantén tus hábitos saludables.",
        "tips": [
            "Continúa con tu rutina de ejercicio y descanso.",
            "Conserva tus redes de apoyo social.",
            "Sigue gestionando bien tu tiempo de estudio.",
        ],
    },
    "Moderado": {
        "color": "yellow",
        "icon": "⚠",
        "description": "Tu perfil muestra señales de ansiedad moderada. Hay áreas que puedes mejorar.",
        "tips": [
            "Establece horarios regulares de sueño (7-9 horas).",
            "Incorpora al menos 30 min de actividad física al día.",
            "Habla con alguien de confianza sobre tus preocupaciones.",
        ],
    },
    "Alto": {
        "color": "red",
        "icon": "✕",
        "description": "Tu perfil indica un nivel de ansiedad elevado. Se recomienda buscar apoyo.",
        "tips": [
            "Considera hablar con un profesional de salud mental.",
            "Prioriza el descanso y reduce la carga académica si es posible.",
            "Busca apoyo en familiares, amigos o servicios de bienestar universitario.",
        ],
    },
}


# --- Rutas ---

def login_required(vista):
    # decorador para proteger rutas, si no hay sesion te manda al login
    @wraps(vista)
    def envoltura(*args, **kwargs):
        if not session.get("usuario_id"):
            return redirect(url_for("login"))
        return vista(*args, **kwargs)

    return envoltura


def admin_required(vista):
    # como login_required pero ademas exige que el email este en ADMIN_EMAILS
    @wraps(vista)
    def envoltura(*args, **kwargs):
        if not session.get("usuario_id"):
            return redirect(url_for("login"))
        if session.get("email") not in ADMIN_EMAILS:
            return redirect(url_for("index"))
        return vista(*args, **kwargs)

    return envoltura


@app.context_processor
def inject_es_admin():
    # disponible en todos los templates sin tener que pasarlo en cada ruta
    return {"es_admin": session.get("email") in ADMIN_EMAILS}


@app.route("/registro", methods=["GET", "POST"])
def registro():
    error = None
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not nombre or not email or not password:
            error = "Completa todos los campos."
        elif len(password) < 8:
            error = "La contraseña debe tener al menos 8 caracteres."
        else:
            conn = get_db()
            nuevo_id = None
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT id FROM usuarios WHERE email = %s", (email,))
                    if cursor.fetchone():
                        error = "Ya existe una cuenta con ese correo."
                    else:
                        cursor.execute(
                            "INSERT INTO usuarios (nombre, email, password_hash, fecha_registro) "
                            "VALUES (%s, %s, %s, %s)",
                            (
                                nombre,
                                email,
                                generate_password_hash(password),
                                datetime.now(ZONA_LIMA).strftime("%d/%m/%Y %H:%M"),
                            ),
                        )
                        nuevo_id = cursor.lastrowid
                conn.commit()
            finally:
                conn.close()

            if not error and nuevo_id is not None:
                session["usuario_id"] = nuevo_id
                session["nombre"] = nombre
                session["email"] = email
                return redirect(url_for("index"))

    return render_template("registro.html", error=error)


def enviar_correo_reset(destinatario: str, link: str) -> bool:
    """Manda el correo con el link para restablecer la contraseña.
    Devuelve False (sin reventar la app) si no está configurado el correo
    o si Gmail rechaza el envío, para que el flujo no se caiga."""
    if not MAIL_USER or not MAIL_PASSWORD:
        print("[aviso] MAIL_USER/MAIL_PASSWORD no configurados; no se envió el correo de recuperación.")
        return False

    msg = EmailMessage()
    msg["Subject"] = "Recupera tu contraseña — AnsioTest"
    msg["From"] = MAIL_USER
    msg["To"] = destinatario
    msg.set_content(
        "Hola,\n\n"
        "Recibimos una solicitud para restablecer tu contraseña en AnsioTest.\n"
        f"Este enlace es válido por {RESET_TOKEN_MINUTOS} minutos:\n\n"
        f"{link}\n\n"
        "Si no fuiste tú, simplemente ignora este correo."
    )
    try:
        with smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT) as smtp:
            smtp.login(MAIL_USER, MAIL_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as e:
        print(f"[error] No se pudo enviar el correo de recuperación: {e}")
        return False


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, nombre, password_hash FROM usuarios WHERE email = %s",
                    (email,),
                )
                usuario = cursor.fetchone()
        finally:
            conn.close()

        if usuario and check_password_hash(usuario["password_hash"], password):
            session["usuario_id"] = usuario["id"]
            session["nombre"] = usuario["nombre"]
            session["email"] = email

            # reconstruyo la pila de "deshacer" con lo que ya tiene en la BD,
            # asi el boton funciona tambien con evaluaciones de sesiones
            # anteriores, no solo con las que se hagan a partir de ahora
            conn = get_db()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM evaluaciones WHERE nombre = %s ORDER BY id ASC",
                        (usuario["nombre"],),
                    )
                    ids_existentes = [fila["id"] for fila in cursor.fetchall()]
            finally:
                conn.close()
            session["pila_deshacer"] = ids_existentes

            return redirect(url_for("index"))
        error = "Correo o contraseña incorrectos."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/olvide-password", methods=["GET", "POST"])
def olvide_password():
    mensaje = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()

        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id FROM usuarios WHERE email = %s", (email,))
                usuario = cursor.fetchone()
                if usuario:
                    token = secrets.token_urlsafe(32)
                    expira = int(datetime.now(ZONA_LIMA).timestamp()) + RESET_TOKEN_MINUTOS * 60
                    cursor.execute(
                        "UPDATE usuarios SET reset_token = %s, reset_expira = %s WHERE id = %s",
                        (token, expira, usuario["id"]),
                    )
                    conn.commit()
                    link = url_for("reset_password", token=token, _external=True)
                    enviar_correo_reset(email, link)
        finally:
            conn.close()

        # el mismo mensaje exista o no la cuenta, para no revelar por esta vía
        # qué correos están registrados
        mensaje = "Si ese correo está registrado, te mandamos un link para restablecer tu contraseña."

    return render_template("olvide_password.html", mensaje=mensaje)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, reset_expira FROM usuarios WHERE reset_token = %s", (token,)
            )
            usuario = cursor.fetchone()
    finally:
        conn.close()

    ahora = int(datetime.now(ZONA_LIMA).timestamp())
    token_valido = usuario and usuario["reset_expira"] and ahora <= usuario["reset_expira"]

    if not token_valido:
        return render_template("reset_password.html", token_valido=False, error=None)

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirmar = request.form.get("confirmar", "")
        if len(password) < 8:
            error = "La contraseña debe tener al menos 8 caracteres."
        elif password != confirmar:
            error = "Las contraseñas no coinciden."
        else:
            conn = get_db()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE usuarios SET password_hash = %s, reset_token = NULL, reset_expira = NULL "
                        "WHERE id = %s",
                        (generate_password_hash(password), usuario["id"]),
                    )
                conn.commit()
            finally:
                conn.close()
            return redirect(url_for("login"))

    return render_template("reset_password.html", token_valido=True, error=error)


@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        nombre=session.get("nombre"),
        frase=random.choice(FRASES),
        estados_animo=ESTADOS_ANIMO,
        animo_hoy=session.get("animo_hoy"),
        activo="inicio",
    )


@app.route("/animo", methods=["POST"])
@login_required
def guardar_animo():
    estado = request.form.get("estado", "")
    valores_validos = {v for v, _, _ in ESTADOS_ANIMO}
    if estado in valores_validos:
        session["animo_hoy"] = estado
    return redirect(url_for("index"))


@app.route("/evaluacion")
@login_required
def evaluacion():
    return render_template("evaluacion.html", nombre=session.get("nombre"), activo="evaluacion")


@app.route("/perfil")
@login_required
def perfil():
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT email, fecha_registro FROM usuarios WHERE id = %s",
                (session.get("usuario_id"),),
            )
            usuario = cursor.fetchone()

            cursor.execute(
                "SELECT COUNT(*) AS total FROM evaluaciones WHERE nombre = %s",
                (session.get("nombre"),),
            )
            total = cursor.fetchone()["total"]
    finally:
        conn.close()

    return render_template(
        "perfil.html",
        nombre=session.get("nombre"),
        email=usuario["email"] if usuario else "",
        fecha_registro=usuario["fecha_registro"] if usuario else "",
        total_evaluaciones=total,
        activo="perfil",
    )


@app.route("/progreso")
@login_required
def progreso():
    nombre_usuario = session.get("nombre")
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT nivel, color, fecha FROM evaluaciones "
                "WHERE nombre = %s ORDER BY id DESC",
                (nombre_usuario,),
            )
            filas = cursor.fetchall()
    finally:
        conn.close()

    registros = [
        {"nivel": f["nivel"], "color": f["color"], "fecha": f["fecha"]}
        for f in filas
    ]

    # conteo por nivel, en el mismo orden que las clases del modelo
    conteo_por_nivel = []
    for nivel in CLASSES:
        cantidad = sum(1 for r in registros if r["nivel"] == nivel)
        color = RISK_INFO[nivel]["color"]
        conteo_por_nivel.append((nivel, cantidad, color))

    # puntos del grafico de evolucion: orden cronologico (mas viejo primero),
    # el nivel de riesgo se pasa a un numero (indice en CLASSES) para poder
    # ubicarlo en el eje Y. lo calculo aca en vez de en el template porque
    # con jinja las cuentas de coordenadas se vuelven ilegibles.
    serie = list(reversed(registros))
    puntos_grafico = []
    if len(serie) >= 2:
        max_valor = len(CLASSES) - 1
        n = len(serie)
        for i, r in enumerate(serie):
            valor = CLASSES.index(r["nivel"])
            x = 10 + (i / (n - 1)) * 580
            y = 170 - (valor / max_valor) * 150 if max_valor else 95
            puntos_grafico.append({
                "x": round(x, 1),
                "y": round(y, 1),
                "color": r["color"],
                "nivel": r["nivel"],
                "fecha": r["fecha"],
            })

    return render_template(
        "progreso.html",
        nombre=nombre_usuario,
        registros=registros,
        puntos_grafico=puntos_grafico,
        conteo_por_nivel=conteo_por_nivel,
        activo="progreso",
    )


@app.route("/admin")
@admin_required
def admin_panel():
    query = request.args.get("q", "").strip()

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, nombre, nivel, color, fecha FROM evaluaciones ORDER BY id DESC"
            )
            filas = cursor.fetchall()
    finally:
        conn.close()

    todos_los_registros = [
        {"id": f["id"], "nombre": f["nombre"], "nivel": f["nivel"], "color": f["color"], "fecha": f["fecha"]}
        for f in filas
    ]

    if query:
        # mismo arbol de busqueda que en /historial, aca sobre todos los registros
        arbol = ArbolBusqueda()
        for registro in todos_los_registros:
            arbol.insertar(registro["nombre"].lower(), registro)
        registros = arbol.buscar_por_texto(query)
    else:
        registros = todos_los_registros

    total_usuarios_query = "SELECT COUNT(*) AS total FROM usuarios"
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(total_usuarios_query)
            total_usuarios = cursor.fetchone()["total"]
    finally:
        conn.close()

    return render_template(
        "admin.html",
        nombre=session.get("nombre"),
        registros=registros,
        query=query,
        total_evaluaciones=len(todos_los_registros),
        total_usuarios=total_usuarios,
        activo="admin",
    )


@app.route("/predict", methods=["POST"])
@login_required
def predict():
    nombre = session.get("nombre", "Estudiante")
    try:
        data = request.get_json(force=True)
        values = [float(data[f]) for f in FEATURES]
        X = np.array(values, dtype=np.float64).reshape(1, -1)

        prediction = int(model.predict(X)[0])
        probabilities = model.predict_proba(X)[0]

        label = CLASSES[prediction]
        info = RISK_INFO[label]

        # Guardar resultado en la base de datos (máx. MAX_REGISTROS entradas)
        probabilidades_dict = {
            CLASSES[i]: {"pct": round(float(p) * 100, 1), "color": ["green", "yellow", "red"][i]}
            for i, p in enumerate(probabilities)
        }
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO evaluaciones (nombre, nivel, color, fecha, probabilidades) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        nombre,
                        label,
                        info["color"],
                        datetime.now(ZONA_LIMA).strftime("%d/%m/%Y %H:%M"),
                        json.dumps(probabilidades_dict, ensure_ascii=False),
                    ),
                )
                # Apilamos el id recién insertado para poder deshacerlo después.
                # va en la sesion del usuario, no en una pila global, para que
                # cada quien deshaga solo sus propias evaluaciones
                pila_usuario = Pila()
                pila_usuario.elementos = session.get("pila_deshacer", [])
                pila_usuario.apilar(cursor.lastrowid)
                session["pila_deshacer"] = pila_usuario.elementos

                # Si se pasa del tope, borrar los registros más antiguos
                # de ESTE usuario (antes borraba entre todos, mezclando
                # el limite de una cuenta con el de otra).
                # MySQL no permite seleccionar de la misma tabla que se está
                # borrando directamente, por eso se envuelve en una subconsulta
                # aparte (tabla derivada).
                cursor.execute(
                    "DELETE FROM evaluaciones WHERE nombre = %s AND id NOT IN ("
                    "  SELECT id FROM ("
                    "    SELECT id FROM evaluaciones WHERE nombre = %s "
                    "    ORDER BY id DESC LIMIT %s"
                    "  ) AS recientes"
                    ")",
                    (nombre, nombre, MAX_REGISTROS),
                )
            conn.commit()
        finally:
            conn.close()

        # armamos las recomendaciones recorriendo la lista enlazada
        lista_tips = ListaEnlazada()
        for tip in info["tips"]:
            lista_tips.agregar(tip)

        # arreglo circular con los ultimos 5 niveles, para la tendencia
        buffer_tendencia = ArregloCircular(capacidad=5)
        for nivel_previo in session.get("tendencia", []):
            buffer_tendencia.agregar(nivel_previo)
        buffer_tendencia.agregar(label)
        tendencia_actual = buffer_tendencia.obtener_todos()
        session["tendencia"] = tendencia_actual

        return jsonify({
            "level": label,
            "color": info["color"],
            "icon": info["icon"],
            "description": info["description"],
            "tips": lista_tips.a_lista(),
            "tendencia": tendencia_actual,
            "probabilities": {
                CLASSES[i]: round(float(p) * 100, 1)
                for i, p in enumerate(probabilities)
            },
        })
    except KeyError as e:
        return jsonify({"error": f"Campo faltante: {e}"}), 400
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"Valor inválido: {e}"}), 400


@app.route("/historial")
@login_required
def ver_historial():
    query = request.args.get("q", "").strip()
    nada_que_deshacer = request.args.get("nada_que_deshacer") == "1"
    nombre_usuario = session.get("nombre")

    # solo las evaluaciones de la cuenta que inicio sesion (antes
    # mostraba las de todos, no correspondia una vez que hay login)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, nombre, nivel, color, fecha, probabilidades "
                "FROM evaluaciones WHERE nombre = %s ORDER BY id DESC",
                (nombre_usuario,),
            )
            filas = cursor.fetchall()
    finally:
        conn.close()

    todos_los_registros = [
        {
            "id": fila["id"],
            "nombre": fila["nombre"],
            "nivel": fila["nivel"],
            "color": fila["color"],
            "fecha": fila["fecha"],
            "probabilidades": json.loads(fila["probabilidades"]),
        }
        for fila in filas
    ]

    if query:
        # buscador con arbol: en /historial cada usuario ya ve solo SUS
        # propios registros, asi que buscar por nombre no serviria de nada
        # (todos tendrian el mismo nombre). Por eso la clave es nivel+fecha,
        # para poder filtrar por ejemplo "alto" o una fecha/mes.
        arbol = ArbolBusqueda()
        for registro in todos_los_registros:
            clave = f"{registro['nivel'].lower()} {registro['fecha'].lower()}"
            arbol.insertar(clave, registro)
        registros = arbol.buscar_por_texto(query)

        # guardo la busqueda en la cola de "recientes"
        cola_busquedas = Cola(capacidad=5)
        cola_busquedas.elementos = session.get("busquedas_recientes", [])
        cola_busquedas.encolar(query)
        session["busquedas_recientes"] = cola_busquedas.elementos
    else:
        registros = todos_los_registros

    busquedas_recientes = list(reversed(session.get("busquedas_recientes", [])))

    # paginacion: 10 registros por pagina, sobre la lista ya filtrada
    # (si hay busqueda, pagina los resultados de la busqueda)
    POR_PAGINA = 10
    try:
        pagina = int(request.args.get("page", 1))
    except ValueError:
        pagina = 1
    total_registros = len(registros)
    total_paginas = max(1, (total_registros + POR_PAGINA - 1) // POR_PAGINA)
    pagina = min(max(pagina, 1), total_paginas)
    inicio = (pagina - 1) * POR_PAGINA
    registros_pagina = registros[inicio:inicio + POR_PAGINA]

    return render_template(
        "historial.html",
        registros=registros_pagina,
        query=query,
        busquedas_recientes=busquedas_recientes,
        nombre=nombre_usuario,
        activo="resultados",
        pagina=pagina,
        total_paginas=total_paginas,
        total_registros=total_registros,
        nada_que_deshacer=nada_que_deshacer,
    )


@app.route("/historial/detalle/<int:registro_id>")
@login_required
def detalle_historial(registro_id):
    nombre_usuario = session.get("nombre")
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, nombre, nivel, color, fecha, probabilidades "
                "FROM evaluaciones WHERE nombre = %s ORDER BY id DESC",
                (nombre_usuario,),
            )
            filas = cursor.fetchall()
    finally:
        conn.close()

    # armo la lista doble en el mismo orden que se ve en el historial,
    # para poder saltar al registro anterior/siguiente
    lista = ListaDoble()
    for fila in filas:
        lista.agregar(fila)

    nodo = lista.buscar_nodo(lambda f: f["id"] == registro_id)
    if nodo is None:
        return redirect(url_for("ver_historial"))

    registro = {
        "id": nodo.dato["id"],
        "nombre": nodo.dato["nombre"],
        "nivel": nodo.dato["nivel"],
        "color": nodo.dato["color"],
        "fecha": nodo.dato["fecha"],
        "probabilidades": json.loads(nodo.dato["probabilidades"]),
    }
    # ojo: "siguiente" en la lista es hacia atras en el tiempo (mas viejo)
    # y "anterior" es hacia adelante (mas reciente), por como los fui
    # agregando (el mas nuevo primero)
    id_mas_antiguo = nodo.siguiente.dato["id"] if nodo.siguiente else None
    id_mas_reciente = nodo.anterior.dato["id"] if nodo.anterior else None

    return render_template(
        "detalle.html",
        registro=registro,
        id_mas_antiguo=id_mas_antiguo,
        id_mas_reciente=id_mas_reciente,
        nombre=nombre_usuario,
        activo="resultados",
    )


@app.route("/historial/limpiar", methods=["POST"])
@login_required
def limpiar_historial():
    nombre_usuario = session.get("nombre")
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM evaluaciones WHERE nombre = %s", (nombre_usuario,))
        conn.commit()
    finally:
        conn.close()
    session["pila_deshacer"] = []
    return redirect(url_for("ver_historial"))


@app.route("/historial/deshacer", methods=["POST"])
@login_required
def deshacer_historial():
    # Desapilamos el id de la última evaluación de ESTE usuario y la borramos
    pila_usuario = Pila()
    pila_usuario.elementos = session.get("pila_deshacer", [])
    ultimo_id = pila_usuario.desapilar()
    session["pila_deshacer"] = pila_usuario.elementos
    if ultimo_id is not None:
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM evaluaciones WHERE id = %s AND nombre = %s",
                    (ultimo_id, session.get("nombre")),
                )
            conn.commit()
        finally:
            conn.close()
        return redirect(url_for("ver_historial"))

    # pila vacia: no habia nada que deshacer, aviso en vez de quedarme callado
    return redirect(url_for("ver_historial", nada_que_deshacer=1))


if __name__ == "__main__":
    print("\n  Servidor listo → http://localhost:5000\n")
    app.run(debug=True, host="127.0.0.1", port=5000)
