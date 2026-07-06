"""
ansiedad_app v2 — Flask backend con registro de estudiantes e historial
"""

import json
import pickle
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = BASE_DIR / "model"
DB_PATH = BASE_DIR / "historial.db"
MAX_REGISTROS = 100
ZONA_LIMA = ZoneInfo("America/Lima")

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.secret_key = "ansiedad_app_secret_key_2024"

# Carga del modelo y metadatos
with open(MODEL_DIR / "model.pkl", "rb") as f:
    model = pickle.load(f)

with open(MODEL_DIR / "model.json", "r", encoding="utf-8") as f:
    meta = json.load(f)

CLASSES: list[str] = meta["classes"]
FEATURES: list[str] = meta["features"]


def get_db() -> sqlite3.Connection:
    """Abre una conexión nueva a la base de datos SQLite."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Crea la tabla de historial si no existe (persiste entre reinicios)."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evaluaciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                nivel TEXT NOT NULL,
                color TEXT NOT NULL,
                fecha TEXT NOT NULL,
                probabilidades TEXT NOT NULL
            )
        """)


init_db()


# --- Estructuras de datos propias (vistas en clase) ---

class NodoLista:
    """Nodo individual de una lista enlazada."""

    def __init__(self, dato):
        self.dato = dato
        self.siguiente = None


class ListaEnlazada:
    """Lista enlazada simple. Se usa para armar las recomendaciones (tips)
    de cada nivel de riesgo, recorriéndolas nodo por nodo en vez de usar
    una lista de Python común."""

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
        """Recorre la lista enlazada y arma una lista de Python con sus datos
        (necesario porque JSON no puede serializar nodos directamente)."""
        resultado = []
        actual = self.cabeza
        while actual is not None:
            resultado.append(actual.dato)
            actual = actual.siguiente
        return resultado


class Pila:
    """Pila (estructura LIFO). Se usa para poder deshacer la última
    evaluación registrada en el historial: cada vez que se guarda una
    evaluación, se apila su id; al deshacer, se desapila y se borra
    esa evaluación específica de la base de datos."""

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


# Pila global en memoria: guarda el id de cada evaluación reciente,
# para poder deshacer la última con un clic.
pila_deshacer = Pila()

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

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/iniciar", methods=["POST"])
def iniciar():
    nombre = request.form.get("nombre", "").strip()
    if not nombre:
        return redirect(url_for("index"))
    # Guardar nombre en sesión y redirigir a evaluación
    session["nombre"] = nombre
    return redirect(url_for("evaluacion"))


@app.route("/evaluacion")
def evaluacion():
    nombre = session.get("nombre")
    if not nombre:
        return redirect(url_for("index"))
    return render_template("evaluacion.html", nombre=nombre)


@app.route("/predict", methods=["POST"])
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
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO evaluaciones (nombre, nivel, color, fecha, probabilidades) "
                "VALUES (?, ?, ?, ?, ?)",
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

            # Si se pasa del tope, borrar los registros más antiguos
            conn.execute(
                "DELETE FROM evaluaciones WHERE id NOT IN ("
                "  SELECT id FROM evaluaciones ORDER BY id DESC LIMIT ?"
                ")",
                (MAX_REGISTROS,),
            )

        # Armamos las recomendaciones recorriendo una lista enlazada propia
        lista_tips = ListaEnlazada()
        for tip in info["tips"]:
            lista_tips.agregar(tip)

        return jsonify({
            "level": label,
            "color": info["color"],
            "icon": info["icon"],
            "description": info["description"],
            "tips": lista_tips.a_lista(),
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
def ver_historial():
    # Mostrar lista de registros, más reciente primero
    with get_db() as conn:
        filas = conn.execute(
            "SELECT nombre, nivel, color, fecha, probabilidades "
            "FROM evaluaciones ORDER BY id DESC"
        ).fetchall()

    registros = [
        {
            "nombre": fila["nombre"],
            "nivel": fila["nivel"],
            "color": fila["color"],
            "fecha": fila["fecha"],
            "probabilidades": json.loads(fila["probabilidades"]),
        }
        for fila in filas
    ]
    return render_template("historial.html", registros=registros)


@app.route("/historial/limpiar", methods=["POST"])
def limpiar_historial():
    with get_db() as conn:
        conn.execute("DELETE FROM evaluaciones")
    pila_deshacer.elementos.clear()
    return redirect(url_for("ver_historial"))


@app.route("/historial/deshacer", methods=["POST"])
def deshacer_historial():
    # Desapilamos el id de la última evaluación registrada y la borramos
    ultimo_id = pila_deshacer.desapilar()
    if ultimo_id is not None:
        with get_db() as conn:
            conn.execute("DELETE FROM evaluaciones WHERE id = ?", (ultimo_id,))
    return redirect(url_for("ver_historial"))


if __name__ == "__main__":
    print("\n  Servidor listo → http://localhost:5000\n")
    app.run(debug=True, host="127.0.0.1", port=5000)
