"""
ansiedad_app v2 — Flask backend con registro de estudiantes e historial
"""

import json
import os
import pickle
import random
from datetime import datetime
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pymysql
import pymysql.cursors
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
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

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.secret_key = os.environ.get("SECRET_KEY", "ansiedad_app_secret_key_2024")

# Carga del modelo y metadatos
with open(MODEL_DIR / "model.pkl", "rb") as f:
    model = pickle.load(f)

with open(MODEL_DIR / "model.json", "r", encoding="utf-8") as f:
    meta = json.load(f)

CLASSES: list[str] = meta["classes"]
FEATURES: list[str] = meta["features"]

# frases cortas para la tarjeta de "recuerda que" en el inicio
FRASES = [
    "Respirar hondo 10 segundos ya es un buen comienzo.",
    "Un examen no define lo que vales.",
    "Está bien pedir ayuda, no es debilidad.",
    "Un paso a la vez es suficiente por hoy.",
    "Avanzar despacio también es avanzar.",
    "Tu ritmo también es válido.",
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


pila_deshacer = Pila()


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


@app.route("/registro", methods=["GET", "POST"])
def registro():
    error = None
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not nombre or not email or not password:
            error = "Completa todos los campos."
        elif len(password) < 4:
            error = "La contraseña debe tener al menos 4 caracteres."
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
                return redirect(url_for("index"))

    return render_template("registro.html", error=error)


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
            return redirect(url_for("index"))
        error = "Correo o contraseña incorrectos."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        nombre=session.get("nombre"),
        frase=random.choice(FRASES),
    )


@app.route("/evaluacion")
@login_required
def evaluacion():
    return render_template("evaluacion.html", nombre=session.get("nombre"))


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
                # Apilamos el id recién insertado para poder deshacerlo después
                pila_deshacer.apilar(cursor.lastrowid)

                # Si se pasa del tope, borrar los registros más antiguos.
                # MySQL no permite seleccionar de la misma tabla que se está
                # borrando directamente, por eso se envuelve en una subconsulta
                # aparte (tabla derivada).
                cursor.execute(
                    "DELETE FROM evaluaciones WHERE id NOT IN ("
                    "  SELECT id FROM ("
                    "    SELECT id FROM evaluaciones ORDER BY id DESC LIMIT %s"
                    "  ) AS recientes"
                    ")",
                    (MAX_REGISTROS,),
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

    # lista completa, mas reciente primero (como ya estaba antes)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, nombre, nivel, color, fecha, probabilidades "
                "FROM evaluaciones ORDER BY id DESC"
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
        # buscador con arbol: meto cada registro por nombre y despues
        # recorro el arbol en orden buscando coincidencias
        arbol = ArbolBusqueda()
        for registro in todos_los_registros:
            arbol.insertar(registro["nombre"].lower(), registro)
        registros = arbol.buscar_por_texto(query)

        # guardo la busqueda en la cola de "recientes"
        cola_busquedas = Cola(capacidad=5)
        cola_busquedas.elementos = session.get("busquedas_recientes", [])
        cola_busquedas.encolar(query)
        session["busquedas_recientes"] = cola_busquedas.elementos
    else:
        registros = todos_los_registros

    busquedas_recientes = list(reversed(session.get("busquedas_recientes", [])))

    return render_template(
        "historial.html",
        registros=registros,
        query=query,
        busquedas_recientes=busquedas_recientes,
    )


@app.route("/historial/detalle/<int:registro_id>")
@login_required
def detalle_historial(registro_id):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, nombre, nivel, color, fecha, probabilidades "
                "FROM evaluaciones ORDER BY id DESC"
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
    )


@app.route("/historial/limpiar", methods=["POST"])
@login_required
def limpiar_historial():
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM evaluaciones")
        conn.commit()
    finally:
        conn.close()
    pila_deshacer.elementos.clear()
    return redirect(url_for("ver_historial"))


@app.route("/historial/deshacer", methods=["POST"])
@login_required
def deshacer_historial():
    # Desapilamos el id de la última evaluación registrada y la borramos
    ultimo_id = pila_deshacer.desapilar()
    if ultimo_id is not None:
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM evaluaciones WHERE id = %s", (ultimo_id,))
            conn.commit()
        finally:
            conn.close()
    return redirect(url_for("ver_historial"))


if __name__ == "__main__":
    print("\n  Servidor listo → http://localhost:5000\n")
    app.run(debug=True, host="127.0.0.1", port=5000)
