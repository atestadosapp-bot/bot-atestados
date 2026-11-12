import os
import tempfile
import threading
import traceback
import unicodedata
import libsql_client
from flask import Flask
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters, CallbackQueryHandler
)

# ==========================================
# CONFIGURACIÓN
# ==========================================
TOKEN = os.environ.get("TELEGRAM_TOKEN","")
TURSO_URL = "libsql://atestados-atestadosapp.aws-us-east-1.turso.io"
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "PON_AQUI_TU_TOKEN_SI_PRUEBAS_LOCAL")

ADMIN_INICIAL_ID = 6491907692

# Estados para el ConversationHandler de /registrar (ENROLAMIENTO)
(REG_DPI, REG_DEL, REG_AL, REG_NUM, REG_EST, REG_FIL, REG_COL, REG_PIL, REG_GENERAL) = range(9)

# Estados para el flujo de EVENTOS (REGISTRO CIVIL)
(EV_TIPO, EV_ANIO, EV_NUM, EV_FOLIO_INI, EV_FOLIO_FIN, EV_GENERAL, EV_EST, EV_FIL, EV_COL, EV_PIL) = range(20, 30)
(EV_BUS_FOLIO, EV_BUS_GENERAL) = range(30, 32)
(EV_EDIT_GENERAL, EV_EDIT_CAMPO, EV_EDIT_VALOR) = range(32, 35) # Nuevos estados para edición
ENROL_ARGS = 40  # Estado para las consultas/ediciones de Enrolamiento con botones
(EV_EDIT_MODO, EV_EDIT_ELEGIR) = range(41, 43)  # Edición de eventos: cómo buscar y cuál elegir
(EV_EDIT_CONFIRMAR, EV_FILTRO_ARGS) = range(43, 45)  # Confirmar eliminación / filtros de consulta

# ==========================================
# CONEXIÓN A LA BASE DE DATOS (TURSO)
# ==========================================
class _ClienteDB:
    """Una sola conexión a Turso, reutilizada por todo el bot (evita abrir una por consulta)."""
    def __init__(self):
        self._c = None
        self._lock = threading.RLock()

    def _cerrar(self):
        try:
            if self._c is not None:
                self._c.close()
        except Exception:
            pass
        self._c = None

    def execute(self, sql, args=None):
        with self._lock:
            for intento in (1, 2):
                try:
                    if self._c is None:
                        url_https = TURSO_URL.replace("libsql://", "https://")
                        self._c = libsql_client.create_client_sync(url_https, auth_token=TURSO_TOKEN)
                    if args is None:
                        return self._c.execute(sql)
                    return self._c.execute(sql, args)
                except Exception:
                    # Reabre la conexión y reintenta una vez; si vuelve a fallar, propaga el error
                    self._cerrar()
                    if intento == 2:
                        raise

_DB = _ClienteDB()

def conectar_db():
    return _DB

def _nombres_columnas(res):
    cols = getattr(res, "columns", None)
    if cols:
        return list(cols)
    desc = getattr(res, "description", None)
    if desc:
        return [c[0] for c in desc]
    return []

# ==========================================
# INICIALIZACIÓN DE TABLAS
# ==========================================
def inicializar_tabla_telegram():
    client = conectar_db()
    client.execute("""
        CREATE TABLE IF NOT EXISTS usuarios_telegram (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            nombre TEXT NOT NULL,
            rol TEXT NOT NULL,
            area TEXT DEFAULT 'enrolamiento',
            dpi_asignado TEXT,
            activo INTEGER DEFAULT 1,
            fecha_registro TEXT
        )
    """)
    try:
        client.execute("ALTER TABLE usuarios_telegram ADD COLUMN area TEXT DEFAULT 'enrolamiento'")
    except: pass

    res = client.execute("SELECT COUNT(*) FROM usuarios_telegram WHERE telegram_id = ?", (ADMIN_INICIAL_ID,))
    if res.rows[0][0] == 0:
        client.execute("""
            INSERT INTO usuarios_telegram (telegram_id, nombre, rol, area, dpi_asignado, activo, fecha_registro)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        """, (ADMIN_INICIAL_ID, "Alex Miranda", "admin", "enrolamiento", None, datetime.now().strftime("%Y-%m-%d %H:%M")))
        print(f"✅ Admin inicial registrado: {ADMIN_INICIAL_ID}")

def inicializar_tabla_eventos():
    client = conectar_db()
    client.execute("""
        CREATE TABLE IF NOT EXISTS eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo_evento TEXT NOT NULL,
            anio INTEGER NOT NULL,
            num_paquete INTEGER NOT NULL,
            folio_inicio INTEGER NOT NULL,
            folio_fin INTEGER NOT NULL,
            numero_general INTEGER,
            estanteria TEXT,
            fila TEXT,
            columna TEXT,
            pila TEXT,
            registrado_por TEXT,
            fecha_registro TEXT,
            UNIQUE(tipo_evento, anio, num_paquete)
        )
    """)
    try:
        client.execute("ALTER TABLE eventos ADD COLUMN numero_general INTEGER")
    except: pass

# ==========================================
# FUNCIONES DE USUARIOS TELEGRAM
# ==========================================
def usuario_autorizado(telegram_id):
    try:
        client = conectar_db()
        res = client.execute(
            "SELECT telegram_id, nombre, rol, area, dpi_asignado, activo FROM usuarios_telegram WHERE telegram_id = ?",
            (telegram_id,)
        )
        if res.rows and res.rows[0][5] == 1:
            fila = res.rows[0]
            return {"telegram_id": fila[0], "nombre": fila[1], "rol": fila[2], "area": fila[3] if fila[3] else "enrolamiento", "dpi_asignado": fila[4]}
        return None
    except Exception as e:
        print(f"Error usuario_autorizado: {e}")
        return None

def agregar_autorizado(telegram_id, nombre, rol, area, dpi_asignado=None):
    client = conectar_db()
    try:
        client.execute("""
            INSERT INTO usuarios_telegram (telegram_id, nombre, rol, area, dpi_asignado, activo, fecha_registro)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        """, (telegram_id, nombre, rol, area, dpi_asignado, datetime.now().strftime("%Y-%m-%d %H:%M")))
        return True, None
    except Exception as e:
        return False, str(e)

def listar_autorizados():
    client = conectar_db()
    res = client.execute("SELECT telegram_id, nombre, rol, area, dpi_asignado, activo FROM usuarios_telegram ORDER BY rol, nombre")
    return res.rows

# ==========================================
# FUNCIONES DE BASE DE DATOS (PAQUETES - ENROLAMIENTO)
# ==========================================
def obtener_columnas():
    client = conectar_db()
    res = client.execute("PRAGMA table_info(paquetes)")
    return [col[1] for col in res.rows] 

def formatear_paquete(fila, columnas):
    datos = dict(zip(columnas, fila))
    est, fil, col, pil = datos.get("estanteria", ""), datos.get("fila", ""), datos.get("columna", ""), datos.get("pila", "")
    datos["ubicacion_armada"] = f"E{est}-F{fil}-{col}-P{pil}"
    return datos

def buscar_por_dpi_y_numero(dpi, num_paquete):
    client = conectar_db()
    res = client.execute("SELECT * FROM paquetes WHERE usuario_codigo = ? AND num_paquete = ?", (dpi, num_paquete))
    if res.rows: return formatear_paquete(res.rows[0], obtener_columnas())
    return None

def buscar_por_numero_general(num_general):
    client = conectar_db()
    res = client.execute("SELECT * FROM paquetes WHERE numero_general = ?", (num_general,))
    if res.rows: return formatear_paquete(res.rows[0], obtener_columnas())
    return None

def buscar_por_usuario(dpi):
    client = conectar_db()
    res = client.execute("SELECT * FROM paquetes WHERE usuario_codigo = ? ORDER BY num_paquete", (dpi,))
    return [formatear_paquete(f, obtener_columnas()) for f in res.rows]

def buscar_por_dpi_y_fecha(dpi, fecha):
    client = conectar_db()
    res = client.execute("SELECT * FROM paquetes WHERE usuario_codigo = ? AND fecha_del <= ? AND fecha_al >= ? ORDER BY num_paquete", (dpi, fecha, fecha))
    return [formatear_paquete(f, obtener_columnas()) for f in res.rows]

def contar_paquetes():
    client = conectar_db()
    res = client.execute("SELECT COUNT(*) FROM paquetes")
    return res.rows[0][0]

def usuario_existe(dpi):
    try:
        client = conectar_db()
        res = client.execute("SELECT COUNT(*) FROM usuarios WHERE dpi = ?", (dpi,))
        return res.rows[0][0] > 0
    except: return False

def insertar_paquete(dpi, num, f_del, f_al, est, fil, col, pil, general=None):
    client = conectar_db()
    try:
        if general is None or str(general).lower() == "auto":
            # Un solo INSERT: el siguiente N° general se calcula dentro de la misma sentencia
            client.execute("""
                INSERT INTO paquetes (usuario_codigo, num_paquete, fecha_del, fecha_al, estanteria, fila, columna, pila, activo, numero_general)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, (SELECT COALESCE(MAX(numero_general), 0) + 1 FROM paquetes))
            """, (dpi, num, f_del, f_al, est, fil, col, pil))
            res = client.execute("SELECT numero_general FROM paquetes WHERE usuario_codigo = ? AND num_paquete = ?", (dpi, num))
            return (res.rows[0][0] if res.rows else None, None)
        elif str(general).lower() in ("blanco", "null", ""):
            nuevo_gen = None
        else:
            try:
                nuevo_gen = int(general)
                res = client.execute("SELECT COUNT(*) FROM paquetes WHERE numero_general = ?", (nuevo_gen,))
                if res.rows[0][0] > 0: return (None, f"El número general {nuevo_gen} ya está en uso")
            except ValueError: return (None, f"'{general}' no es un número válido")
        
        client.execute("""
            INSERT INTO paquetes (usuario_codigo, num_paquete, fecha_del, fecha_al, estanteria, fila, columna, pila, activo, numero_general)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (dpi, num, f_del, f_al, est, fil, col, pil, nuevo_gen))
        return (nuevo_gen, None)
    except Exception as e:
        traceback.print_exc()
        if "UNIQUE" in str(e).upper():
            return (None, f"Ya existe el paquete N° {num} para el usuario {dpi}.")
        return (None, f"Error ({type(e).__name__}): {e}")

def eliminar_paquete(dpi, num):
    client = conectar_db()
    client.execute("DELETE FROM paquetes WHERE usuario_codigo = ? AND num_paquete = ?", (dpi, num))
    return True

def actualizar_paquete(dpi, num, campo, valor):
    campos_validos = ["fecha_del", "fecha_al", "estanteria", "fila", "columna", "pila", "numero_general"]
    if campo not in campos_validos: return False
    client = conectar_db()
    try:
        if campo == "numero_general":
            valor_final = None if valor.lower() in ("blanco", "null", "") else int(valor)
        else: valor_final = valor
        client.execute(f"UPDATE paquetes SET {campo} = ? WHERE usuario_codigo = ? AND num_paquete = ?", (valor_final, dpi, num))
        return True
    except Exception as e:
        print(f"Error actualizar: {e}")
        return False

def agregar_usuario(dpi, nombre):
    client = conectar_db()
    try:
        client.execute("INSERT INTO usuarios (dpi, nombre) VALUES (?, ?)", (dpi, nombre))
        return True
    except: return False

def listar_usuarios():
    client = conectar_db()
    res = client.execute("SELECT dpi, nombre FROM usuarios ORDER BY nombre")
    return res.rows

def eliminar_usuario(dpi):
    client = conectar_db()
    client.execute("DELETE FROM usuarios WHERE dpi = ?", (dpi,))
    return True

def contar_paquetes_de_usuario(dpi):
    client = conectar_db()
    res = client.execute("SELECT COUNT(*) FROM paquetes WHERE usuario_codigo = ?", (dpi,))
    return res.rows[0][0]

# ==========================================
# FUNCIONES DE BASE DE DATOS (EVENTOS - REGISTRO CIVIL)
# ==========================================
def insertar_evento(tipo, anio, num_paq, folio_ini, folio_fin, num_general, est, fil, col, pil, operador):
    client = conectar_db()
    try:
        num_general_val = None if str(num_general).lower() in ("blanco", "null", "") else int(num_general)
        client.execute("""
            INSERT INTO eventos (tipo_evento, anio, num_paquete, folio_inicio, folio_fin, numero_general, estanteria, fila, columna, pila, registrado_por, fecha_registro)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tipo, anio, num_paq, folio_ini, folio_fin, num_general_val, est, fil, col, pil, operador, datetime.now().strftime("%Y-%m-%d %H:%M")))
        return True, None
    except Exception as e:
        return False, str(e)

def buscar_evento_por_folio(folio):
    """Devuelve una LISTA de paquetes: el mismo número puede existir en varios tipos de evento."""
    client = conectar_db()
    res = client.execute("""
        SELECT * FROM eventos
        WHERE folio_inicio <= ? AND folio_fin >= ?
        ORDER BY anio, tipo_evento, num_paquete
    """, (folio, folio))
    columnas = _nombres_columnas(res)
    return [dict(zip(columnas, fila)) for fila in res.rows]

def buscar_evento_por_general(num_general):
    client = conectar_db()
    res = client.execute("SELECT * FROM eventos WHERE numero_general = ?", (num_general,))
    if res.rows:
        columnas = _nombres_columnas(res)
        return dict(zip(columnas, res.rows[0]))
    return None

def actualizar_evento(num_general, campo, valor):
    client = conectar_db()
    try:
        # Manejo de valores nulos o en blanco
        if valor.lower() in ("blanco", "null", ""):
            valor_final = None
        elif campo in ["anio", "num_paquete", "folio_inicio", "folio_fin", "numero_general"]:
            valor_final = int(valor)
        else:
            valor_final = valor
            
        client.execute(f"UPDATE eventos SET {campo} = ? WHERE numero_general = ?", (valor_final, num_general))
        return True
    except Exception as e:
        print(f"Error actualizar evento: {e}")
        return False

CAMPOS_EVENTO_EDITABLES = ["tipo_evento", "anio", "num_paquete", "folio_inicio", "folio_fin",
                           "numero_general", "estanteria", "fila", "columna", "pila"]

def buscar_evento_por_id(id_evento):
    res = conectar_db().execute("SELECT * FROM eventos WHERE id = ?", (id_evento,))
    if res.rows:
        return dict(zip(_nombres_columnas(res), res.rows[0]))
    return None

def actualizar_evento_id(id_evento, campo, valor):
    """Actualiza un campo del paquete identificado por su id interno. Devuelve (ok, mensaje_error)."""
    if campo not in CAMPOS_EVENTO_EDITABLES:
        return False, "Campo no editable."
    client = conectar_db()
    try:
        if valor.lower() in ("blanco", "null", ""):
            if campo != "numero_general":
                return False, "Solo el N° General puede quedar en blanco."
            valor_final = None
        elif campo in ("anio", "num_paquete", "folio_inicio", "folio_fin", "numero_general"):
            valor_final = int(valor)
        else:
            valor_final = valor
        if campo == "numero_general" and valor_final is not None:
            r = client.execute("SELECT COUNT(*) FROM eventos WHERE numero_general = ? AND id <> ?", (valor_final, id_evento))
            if r.rows[0][0] > 0:
                return False, f"El N° General {valor_final} ya está en uso por otro paquete."
        client.execute(f"UPDATE eventos SET {campo} = ? WHERE id = ?", (valor_final, id_evento))
        return True, None
    except ValueError:
        return False, "Ese campo requiere un número entero."
    except Exception as e:
        return False, f"No se pudo guardar (¿ya existe ese Tipo/Año/Paquete?): {e}"

TIPOS_EVENTO = {"nacimiento": "Nacimiento", "matrimonio": "Matrimonio", "defuncion": "Defuncion",
                "divorcio": "Divorcio", "negativa": "Negativa"}

def normalizar_tipo(texto):
    t = unicodedata.normalize("NFD", texto.strip().lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return TIPOS_EVENTO.get(t)

def _filas_evento(sql, args=()):
    res = conectar_db().execute(sql, args)
    cols = _nombres_columnas(res)
    return [dict(zip(cols, f)) for f in res.rows]

def siguiente_num_paquete(tipo, anio):
    """El N° de paquete es correlativo dentro del año (y tipo) y reinicia al cambiar de año."""
    r = conectar_db().execute("SELECT MAX(num_paquete) FROM eventos WHERE tipo_evento = ? AND anio = ?", (tipo, anio))
    return (r.rows[0][0] or 0) + 1

def siguiente_folio_del(tipo, anio):
    """Sugiere el Evento DEL: último AL registrado + 1. Devuelve (valor, origen) o (None, None)."""
    r = conectar_db().execute("SELECT MAX(folio_fin) FROM eventos WHERE tipo_evento = ? AND anio = ?", (tipo, anio))
    if r.rows[0][0] is not None:
        return r.rows[0][0] + 1, f"el último paquete de {tipo} {anio} terminó en {r.rows[0][0]}"
    r = conectar_db().execute("SELECT MAX(folio_fin) FROM eventos WHERE tipo_evento = ?", (tipo,))
    if r.rows[0][0] is not None:
        return r.rows[0][0] + 1, f"el último AL de {tipo} (de otro año) fue {r.rows[0][0]}"
    return None, None

def siguiente_general():
    """El N° General es correlativo global (no reinicia por año)."""
    r = conectar_db().execute("SELECT MAX(numero_general) FROM eventos")
    return (r.rows[0][0] or 0) + 1

def buscar_evento_por_tap(tipo, anio, num):
    return _filas_evento("SELECT * FROM eventos WHERE tipo_evento = ? AND anio = ? AND num_paquete = ?", (tipo, anio, num))

def listar_eventos(anio, tipo=None):
    if tipo:
        return _filas_evento("SELECT * FROM eventos WHERE anio = ? AND tipo_evento = ? ORDER BY num_paquete", (anio, tipo))
    return _filas_evento("SELECT * FROM eventos WHERE anio = ? ORDER BY tipo_evento, num_paquete", (anio,))

def buscar_eventos_por_ubicacion(est, fil, col, pil):
    cond, args = [], []
    for campo, valor in (("estanteria", est), ("fila", fil), ("columna", col), ("pila", pil)):
        if valor != "*":
            cond.append(f"{campo} = ?"); args.append(valor.upper() if campo == "columna" else valor)
    if not cond: return []
    return _filas_evento("SELECT * FROM eventos WHERE " + " AND ".join(cond) + " ORDER BY anio, tipo_evento, num_paquete", tuple(args))

def eliminar_evento_id(id_evento):
    conectar_db().execute("DELETE FROM eventos WHERE id = ?", (id_evento,))
    return True

# ==========================================
# UTILIDADES
# ==========================================
def normalizar_fecha(fecha_str):
    fecha_str = fecha_str.replace("/", "-")
    partes = fecha_str.split("-")
    if len(partes) != 3: return None
    try:
        if len(partes[0]) == 4: a, m, d = int(partes[0]), int(partes[1]), int(partes[2])
        else: d, m, a = int(partes[0]), int(partes[1]), int(partes[2])
        return f"{a:04d}-{m:02d}-{d:02d}"
    except ValueError: return None

def formatear_mensaje(datos, encabezado="📦 *Paquete encontrado*"):
    num, num_gen, dpi = datos.get("num_paquete", "?"), datos.get("numero_general"), datos.get("usuario_codigo", "?")
    num_gen_texto = f"`{num_gen}`" if num_gen is not None else "_En blanco_"
    f_del, f_al = datos.get("fecha_del", "?"), datos.get("fecha_al", "?")
    ubicacion = datos.get("ubicacion_armada", "?")
    return (f"{encabezado}\n\n📦 N°: *{num}*\n🔢 General: {num_gen_texto}\n👤 Código: `{dpi}`\n📅 Rango: {f_del} al {f_al}\n📍 *Ubicación:* `{ubicacion}`")

# (La función generar_pdf_etiqueta se mantiene igual que en el código anterior)
def generar_pdf_etiqueta(dpi, fecha_del, fecha_al, num, num_gen, ubicacion):
    try: from fpdf import FPDF
    except ImportError: return None, "No tienes 'fpdf2'."
    def a_formato_mostrar(fecha):
        if not fecha or fecha == "?": return fecha
        try:
            partes = fecha.split("-")
            if len(partes) == 3: return f"{partes[2]}/{partes[1]}/{partes[0]}"
        except: pass
        return fecha
    del_mostrar, al_mostrar = a_formato_mostrar(fecha_del), a_formato_mostrar(fecha_al)
    num_str, gral = str(num), str(num_gen) if num_gen is not None else "-"
    try:
        pdf = FPDF(orientation="P", unit="mm", format="Letter"); pdf.add_page(); pdf.set_auto_page_break(auto=False)
        margen_izq, margen_sup, ancho_disponible = 10, 15, 215.9 - 20
        ancho_ancha, ancho_sep, ancho_angosta = ancho_disponible * 0.62, ancho_disponible * 0.06, ancho_disponible * 0.32
        x_ancha, x_sep, x_angosta, y_inicio, ancho_alto = margen_izq, margen_izq + ancho_ancha, margen_izq + ancho_ancha + ancho_sep, margen_sup, 70
        # ETIQUETA ANCHA
        pdf.set_draw_color(0, 0, 0); pdf.set_line_width(0.8); pdf.rect(x_ancha, y_inicio, ancho_ancha, ancho_alto)
        pdf.set_fill_color(221, 221, 221); pdf.rect(x_ancha, y_inicio, ancho_ancha, 12, style="F")
        pdf.set_xy(x_ancha, y_inicio + 2); pdf.set_font("Helvetica", "B", 20); pdf.cell(ancho_ancha, 8, dpi, align="C")
        pdf.set_line_width(0.8); pdf.line(x_ancha, y_inicio + 12, x_ancha + ancho_ancha, y_inicio + 12)
        pdf.set_xy(x_ancha + 3, y_inicio + 14); pdf.set_font("Helvetica", "B", 16); pdf.cell(ancho_ancha - 6, 8, f"DEL: {del_mostrar}", align="L")
        pdf.set_xy(x_ancha + 3, y_inicio + 22); pdf.cell(ancho_ancha - 6, 8, f"AL: {al_mostrar}", align="L")
        pdf.line(x_ancha, y_inicio + 30, x_ancha + ancho_ancha, y_inicio + 30)
        pdf.set_xy(x_ancha, y_inicio + 33); pdf.set_font("Helvetica", "B", 38); pdf.cell(ancho_ancha, 18, f"No. {num_str}", align="C")
        pdf.set_xy(x_ancha + 3, y_inicio + 52); pdf.set_font("Helvetica", "", 9); pdf.set_text_color(85, 85, 85); pdf.cell(ancho_ancha - 6, 5, f"Gral. N° {gral}", align="R")
        pdf.set_text_color(0, 0, 0); pdf.line(x_ancha, y_inicio + 58, x_ancha + ancho_ancha, y_inicio + 58)
        pdf.set_xy(x_ancha, y_inicio + 60); pdf.set_font("Helvetica", "", 14); pdf.cell(ancho_ancha, 8, f"Ubicación: {ubicacion}", align="C")
        # LÍNEA DE CORTE
        pdf.set_line_width(0.5); y_actual = y_inicio
        while y_actual < y_inicio + ancho_alto: pdf.line(x_sep + ancho_sep / 2, y_actual, x_sep + ancho_sep / 2, y_actual + 2); y_actual += 4
        # ETIQUETA ANGOSTA
        pdf.set_line_width(0.8); pdf.rect(x_angosta, y_inicio, ancho_angosta, ancho_alto)
        pdf.set_fill_color(221, 221, 221); pdf.rect(x_angosta, y_inicio, ancho_angosta, 10, style="F")
        pdf.set_xy(x_angosta, y_inicio + 1.5); pdf.set_font("Helvetica", "B", 14); pdf.cell(ancho_angosta, 7, dpi, align="C")
        pdf.line(x_angosta, y_inicio + 10, x_angosta + ancho_angosta, y_inicio + 10)
        pdf.set_xy(x_angosta, y_inicio + 12); pdf.set_font("Helvetica", "B", 12); pdf.cell(ancho_angosta, 6, f"DEL: {del_mostrar}", align="C")
        pdf.set_xy(x_angosta, y_inicio + 18); pdf.cell(ancho_angosta, 6, f"AL: {al_mostrar}", align="C")
        pdf.line(x_angosta, y_inicio + 25, x_angosta + ancho_angosta, y_inicio + 25)
        pdf.set_xy(x_angosta, y_inicio + 28); pdf.set_font("Helvetica", "B", 30); pdf.cell(ancho_angosta, 15, f"No. {num_str}", align="C")
        pdf.set_xy(x_angosta, y_inicio + 45); pdf.set_font("Helvetica", "", 8); pdf.set_text_color(85, 85, 85); pdf.cell(ancho_angosta - 2, 5, f"Gral. {gral}", align="R")
        pdf.set_text_color(0, 0, 0); pdf.line(x_angosta, y_inicio + 52, x_angosta + ancho_angosta, y_inicio + 52)
        pdf.set_xy(x_angosta, y_inicio + 54); pdf.set_font("Helvetica", "", 10); pdf.cell(ancho_angosta, 6, f"Ubic: {ubicacion}", align="C")
        ruta = os.path.join(tempfile.gettempdir(), f"etiquetas_{dpi}_{num}.pdf"); pdf.output(ruta); return ruta, None
    except Exception as e: return None, str(e)

def generar_pdf_etiqueta_evento(tipo, anio, num, folio_ini, folio_fin, num_gen, ubicacion):
    try: from fpdf import FPDF
    except ImportError: return None, "No tienes 'fpdf2'."
    num_str, gral = str(num), str(num_gen) if num_gen is not None else "-"
    encabezado = f"{tipo.upper()} - {anio}"
    try:
        pdf = FPDF(orientation="P", unit="mm", format="Letter"); pdf.add_page(); pdf.set_auto_page_break(auto=False)
        margen_izq, margen_sup, ancho_disponible = 10, 15, 215.9 - 20
        ancho_ancha, ancho_sep, ancho_angosta = ancho_disponible * 0.62, ancho_disponible * 0.06, ancho_disponible * 0.32
        x_ancha, x_sep, x_angosta, y_inicio, alto = margen_izq, margen_izq + ancho_ancha, margen_izq + ancho_ancha + ancho_sep, margen_sup, 70
        # ETIQUETA ANCHA
        pdf.set_draw_color(0, 0, 0); pdf.set_line_width(0.8); pdf.rect(x_ancha, y_inicio, ancho_ancha, alto)
        pdf.set_fill_color(221, 221, 221); pdf.rect(x_ancha, y_inicio, ancho_ancha, 12, style="F")
        pdf.set_xy(x_ancha, y_inicio + 2); pdf.set_font("Helvetica", "B", 20); pdf.cell(ancho_ancha, 8, encabezado, align="C")
        pdf.line(x_ancha, y_inicio + 12, x_ancha + ancho_ancha, y_inicio + 12)
        pdf.set_xy(x_ancha + 3, y_inicio + 14); pdf.set_font("Helvetica", "B", 16); pdf.cell(ancho_ancha - 6, 8, f"DEL: {folio_ini}", align="L")
        pdf.set_xy(x_ancha + 3, y_inicio + 22); pdf.cell(ancho_ancha - 6, 8, f"AL: {folio_fin}", align="L")
        pdf.line(x_ancha, y_inicio + 30, x_ancha + ancho_ancha, y_inicio + 30)
        pdf.set_xy(x_ancha, y_inicio + 33); pdf.set_font("Helvetica", "B", 38); pdf.cell(ancho_ancha, 18, f"No. {num_str}", align="C")
        pdf.set_xy(x_ancha + 3, y_inicio + 52); pdf.set_font("Helvetica", "", 9); pdf.set_text_color(85, 85, 85); pdf.cell(ancho_ancha - 6, 5, f"Gral. N° {gral}", align="R")
        pdf.set_text_color(0, 0, 0); pdf.line(x_ancha, y_inicio + 58, x_ancha + ancho_ancha, y_inicio + 58)
        pdf.set_xy(x_ancha, y_inicio + 60); pdf.set_font("Helvetica", "", 14); pdf.cell(ancho_ancha, 8, f"Ubicación: {ubicacion}", align="C")
        # LÍNEA DE CORTE
        pdf.set_line_width(0.5); y_actual = y_inicio
        while y_actual < y_inicio + alto: pdf.line(x_sep + ancho_sep / 2, y_actual, x_sep + ancho_sep / 2, y_actual + 2); y_actual += 4
        # ETIQUETA ANGOSTA
        pdf.set_line_width(0.8); pdf.rect(x_angosta, y_inicio, ancho_angosta, alto)
        pdf.set_fill_color(221, 221, 221); pdf.rect(x_angosta, y_inicio, ancho_angosta, 10, style="F")
        pdf.set_xy(x_angosta, y_inicio + 1.5); pdf.set_font("Helvetica", "B", 11); pdf.cell(ancho_angosta, 7, encabezado, align="C")
        pdf.line(x_angosta, y_inicio + 10, x_angosta + ancho_angosta, y_inicio + 10)
        pdf.set_xy(x_angosta, y_inicio + 12); pdf.set_font("Helvetica", "B", 12); pdf.cell(ancho_angosta, 6, f"DEL: {folio_ini}", align="C")
        pdf.set_xy(x_angosta, y_inicio + 18); pdf.cell(ancho_angosta, 6, f"AL: {folio_fin}", align="C")
        pdf.line(x_angosta, y_inicio + 25, x_angosta + ancho_angosta, y_inicio + 25)
        pdf.set_xy(x_angosta, y_inicio + 28); pdf.set_font("Helvetica", "B", 30); pdf.cell(ancho_angosta, 15, f"No. {num_str}", align="C")
        pdf.set_xy(x_angosta, y_inicio + 45); pdf.set_font("Helvetica", "", 8); pdf.set_text_color(85, 85, 85); pdf.cell(ancho_angosta - 2, 5, f"Gral. {gral}", align="R")
        pdf.set_text_color(0, 0, 0); pdf.line(x_angosta, y_inicio + 52, x_angosta + ancho_angosta, y_inicio + 52)
        pdf.set_xy(x_angosta, y_inicio + 54); pdf.set_font("Helvetica", "", 10); pdf.cell(ancho_angosta, 6, f"Ubic: {ubicacion}", align="C")
        ruta = os.path.join(tempfile.gettempdir(), f"etiqueta_ev_{tipo}_{anio}_{num}.pdf"); pdf.output(ruta); return ruta, None
    except Exception as e: return None, str(e)

# ==========================================
# VERIFICACIÓN DE ACCESO Y MENÚS
# ==========================================
async def verificar_acceso(update, roles_permitidos=None):
    user_id = update.effective_user.id
    usuario = usuario_autorizado(user_id)
    if not usuario:
        msg = "🚫 *No estás autorizado*\n\nContacta al administrador."
        if update.callback_query: await update.callback_query.answer(msg, show_alert=True)
        else: await update.message.reply_text(msg, parse_mode="Markdown")
        return None
    if roles_permitidos and usuario["rol"] not in roles_permitidos:
        msg = f"🚫 *No tienes permiso*\n\nTu rol es: `{usuario['rol']}`"
        if update.callback_query: await update.callback_query.answer("No tienes permiso.", show_alert=True)
        else: await update.message.reply_text(msg, parse_mode="Markdown")
        return None
    return usuario

def obtener_menu_principal(usuario):
    rol, area, teclado = usuario["rol"], usuario.get("area", "enrolamiento"), []
    if rol == "admin":
        teclado.extend([
            [InlineKeyboardButton("🔍 CONSULTAS DPI (Enrolamiento)", callback_data="menu_consulta_enrol")],
            [InlineKeyboardButton("✏️ EDICIÓN DPI (Enrolamiento)", callback_data="menu_edicion_enrol")],
            [InlineKeyboardButton("📖 CONSULTAS EVENTOS (Registro)", callback_data="menu_consulta_reg")],
            [InlineKeyboardButton("✏️ EDICIÓN EVENTOS (Registro)", callback_data="menu_edicion_reg")]
        ])
    else:
        if area == "enrolamiento":
            teclado.extend([[InlineKeyboardButton("🔍 CONSULTAS DPI", callback_data="menu_consulta_enrol")], [InlineKeyboardButton("✏️ EDICIÓN DPI", callback_data="menu_edicion_enrol")]])
        else:
            teclado.extend([[InlineKeyboardButton("📖 CONSULTAS EVENTOS", callback_data="menu_consulta_reg")], [InlineKeyboardButton("✏️ EDICIÓN EVENTOS", callback_data="menu_edicion_reg")]])
    if rol in ["admin", "operador1"]: teclado.append([InlineKeyboardButton("👥 GESTIÓN", callback_data="menu_gestion")])
    if rol == "admin": teclado.append([InlineKeyboardButton("🔐 AUTORIZACIÓN", callback_data="menu_autorizacion")])
    teclado.append([InlineKeyboardButton("👤 MI INFORMACIÓN", callback_data="menu_info")])
    return InlineKeyboardMarkup(teclado)
    
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    mensaje = f"👋 *Hola, {usuario['nombre']}*\nTu rol es: `{usuario['rol']}`\nÁrea: `{usuario.get('area', 'enrolamiento').capitalize()}`\n\nSelecciona una categoría:"
    teclado = obtener_menu_principal(usuario)
    if update.callback_query: await update.callback_query.edit_message_text(mensaje, reply_markup=teclado, parse_mode="Markdown")
    else: await update.message.reply_text(mensaje, reply_markup=teclado, parse_mode="Markdown")
        
async def botones_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    usuario = await verificar_acceso(update)
    if not usuario: return
    accion = query.data
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])

    if accion == "menu_inicio": await start(update, context)
    elif accion == "menu_info":
        dpi_info = f"\n👤 DPI asignado: `{usuario['dpi_asignado']}`" if usuario['dpi_asignado'] else ""
        texto = f"👤 *Tu información*\n\nNombre: {usuario['nombre']}\nRol: `{usuario['rol']}`\nÁrea: `{usuario.get('area', 'enrolamiento')}`\nID: `{usuario['telegram_id']}`{dpi_info}"
        await query.edit_message_text(texto, reply_markup=btn_volver, parse_mode="Markdown")
    elif accion == "menu_consulta_enrol":
        btn_en = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Buscar por DPI y N°", callback_data="enrol_buscar")],
            [InlineKeyboardButton("🔢 Buscar por N° General", callback_data="enrol_general")],
            [InlineKeyboardButton("📅 Buscar por fecha", callback_data="enrol_fecha")],
            [InlineKeyboardButton("👤 Paquetes de un usuario", callback_data="enrol_usuario")],
            [InlineKeyboardButton("📊 Total de paquetes", callback_data="enrol_total")],
            [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]
        ])
        await query.edit_message_text("🔍 *CONSULTAS DPI*\n\nSelecciona cómo buscar:", reply_markup=btn_en, parse_mode="Markdown")
    elif accion == "menu_edicion_enrol":
        btn_en = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Registrar paquete", callback_data="enrol_registrar")],
            [InlineKeyboardButton("✏️ Editar paquete", callback_data="enrol_editar")],
            [InlineKeyboardButton("🗑️ Eliminar paquete", callback_data="enrol_eliminar")],
            [InlineKeyboardButton("🏷️ Generar etiqueta PDF", callback_data="enrol_etiqueta")],
            [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]
        ])
        await query.edit_message_text("✏️ *EDICIÓN DPI*\n\nSelecciona una acción:", reply_markup=btn_en, parse_mode="Markdown")

    elif accion == "menu_consulta_reg":
        btn_reg = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Buscar por Evento (DEL/AL)", callback_data="accion_buscar_folio")],
            [InlineKeyboardButton("🔢 Buscar por N° General", callback_data="accion_buscar_general")],
            [InlineKeyboardButton("📦 Por Tipo, Año y Paquete", callback_data="evf_tap")],
            [InlineKeyboardButton("📋 Listar por Año / Tipo", callback_data="evf_lista")],
            [InlineKeyboardButton("📍 Por Ubicación", callback_data="evf_ubic")],
            [InlineKeyboardButton("📊 Totales por Año", callback_data="accion_totales_anio")],
            [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]
        ])
        await query.edit_message_text("📖 *CONSULTAS DE EVENTOS*\n\nSelecciona cómo buscar:", reply_markup=btn_reg, parse_mode="Markdown")
    
    elif accion == "menu_edicion_reg":
        btn_reg = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Registrar Evento", callback_data="accion_registrar_evento")],
            [InlineKeyboardButton("✏️ Editar Paquete de Evento", callback_data="accion_editar_evento")],
            [InlineKeyboardButton("🏷️ Generar Etiqueta PDF", callback_data="accion_etiqueta_evento")],
            [InlineKeyboardButton("🗑️ Eliminar Paquete de Evento", callback_data="accion_eliminar_evento")],
            [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]
        ])
        await query.edit_message_text("✏️ *EDICIÓN DE EVENTOS*\n\nSelecciona una acción:", reply_markup=btn_reg, parse_mode="Markdown")

    elif accion == "menu_gestion":
        if usuario["rol"] not in ["admin", "operador1"]:
            await query.answer("No tienes permiso.", show_alert=True); return
        texto = "👥 *GESTIÓN*\n\n`/usuarios`\n`/agregar_usuario <DPI> <Nombre>`\n`/eliminar_usuario <DPI>`"
        await query.edit_message_text(texto, reply_markup=btn_volver, parse_mode="Markdown")
    elif accion == "menu_autorizacion":
        if usuario["rol"] != "admin":
            await query.answer("Solo el admin.", show_alert=True); return
        texto = "🔐 *AUTORIZACIÓN*\n\n`/autorizar <ID> <Nombre> <Rol> <Área> [DPI]`\n`/lista_autorizados`"
        await query.edit_message_text(texto, reply_markup=btn_volver, parse_mode="Markdown")

# ==========================================
# COMANDOS DE TEXTO
# ==========================================
async def mi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Tu ID de Telegram es: `{update.effective_user.id}`", parse_mode="Markdown")

async def autorizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin"])
    if not usuario: return
    if len(context.args) < 4:
        await update.message.reply_text("⚠️ Uso: `/autorizar <ID> <nombre> <rol> <area> [dpi]`", parse_mode="Markdown"); return
    try: telegram_id = int(context.args[0])
    except: await update.message.reply_text("⚠️ El ID debe ser número."); return
    nombre, rol, area = context.args[1], context.args[2].lower(), context.args[3].lower()
    dpi = context.args[4] if len(context.args) > 4 else None
    exito, error = agregar_autorizado(telegram_id, nombre, rol, area, dpi)
    if exito: await update.message.reply_text(f"✅ Autorizado: {nombre} como {rol} en {area}", parse_mode="Markdown")
    else: await update.message.reply_text(f"❌ {error}")

async def lista_autorizados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin"])
    if not usuario: return
    lista = listar_autorizados(); mensaje = "👥 *Usuarios autorizados:*\n\n"
    for tg_id, nombre, rol, area, dpi, activo in lista:
        estado = "✅" if activo == 1 else "❌"
        mensaje += f"{estado} *{nombre}* | `{rol}` | `{area}` | ID: `{tg_id}`\n"
    await update.message.reply_text(mensaje, parse_mode="Markdown")

async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Uso: `/buscar <DPI> <N°>`", parse_mode="Markdown"); return
    dpi = context.args[0].strip()
    try: num = int(context.args[1])
    except: await update.message.reply_text("⚠️ Número inválido."); return
    res = buscar_por_dpi_y_numero(dpi, num)
    if not res: await update.message.reply_text(f"❌ No se encontró `{dpi}` N° {num}.", parse_mode="Markdown")
    else: await update.message.reply_text(formatear_mensaje(res), parse_mode="Markdown")

async def etiqueta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Uso: `/etiqueta <DPI> <N°>`", parse_mode="Markdown"); return
    dpi = context.args[0].strip()
    try: num = int(context.args[1])
    except: await update.message.reply_text("⚠️ Número inválido."); return
    try:
        res = buscar_por_dpi_y_numero(dpi, num)
        if not res:
            res = buscar_por_numero_general(num)
            if res and res.get("usuario_codigo") != dpi: res = None 
        if not res: 
            await update.message.reply_text(f"❌ No se encontró N° {num} para el DPI `{dpi}`.", parse_mode="Markdown"); return
        num_real = res.get("num_paquete")
        ruta, error = generar_pdf_etiqueta(dpi, res.get("fecha_del"), res.get("fecha_al"), num_real, res.get("numero_general"), res.get("ubicacion_armada"))
        if ruta:
            with open(ruta, "rb") as f:
                caption = f"🏷️ *Etiquetas del Paquete N° {num_real}*\n\n👤 DPI: `{dpi}`\n📅 {res.get('fecha_del')} al {res.get('fecha_al')}\n📍 `{res.get('ubicacion_armada')}`\n\n📄 *Formato:* PDF (listo para imprimir)"
                await update.message.reply_document(document=f, filename=f"etiqueta_{dpi}_{num_real}_{datetime.now().strftime('%H%M%S')}.pdf", caption=caption, parse_mode="Markdown")
            os.remove(ruta)
        else: await update.message.reply_text(f"❌ Error al generar PDF: {error}")
    except Exception as e: await update.message.reply_text(f"❌ Error inesperado: {str(e)}")

async def total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    await update.message.reply_text(f"📊 Total de paquetes DPI: *{contar_paquetes()}*", parse_mode="Markdown")

async def general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Uso: `/general <número>`", parse_mode="Markdown"); return
    try: num_gen = int(context.args[0])
    except ValueError: await update.message.reply_text("⚠️ El número debe ser entero."); return
    resultado = buscar_por_numero_general(num_gen)
    if not resultado: await update.message.reply_text(f"❌ No se encontró el general *{num_gen}*.", parse_mode="Markdown")
    else: await update.message.reply_text(formatear_mensaje(resultado), parse_mode="Markdown")

async def fecha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Uso: `/fecha <DPI> <AAAA-MM-DD>`", parse_mode="Markdown"); return
    dpi = context.args[0].strip(); fecha_norm = normalizar_fecha(context.args[1].strip())
    if not fecha_norm: await update.message.reply_text("⚠️ Fecha inválida.", parse_mode="Markdown"); return
    resultados = buscar_por_dpi_y_fecha(dpi, fecha_norm)
    if not resultados: await update.message.reply_text(f"❌ No hay paquetes del DPI `{dpi}` en esa fecha.", parse_mode="Markdown"); return
    mensaje = f"📅 *Paquetes del DPI* `{dpi}` que incluyen *{fecha_norm}*\n\n"
    for r in resultados:
        gen = r.get('numero_general'); gen_txt = f"`{gen}`" if gen is not None else "_blanco_"
        mensaje += f"📦 N° *{r.get('num_paquete')}* (Gen: {gen_txt})\n   📍 `{r.get('ubicacion_armada')}`\n\n"
        if len(mensaje) > 3800: await update.message.reply_text(mensaje, parse_mode="Markdown"); mensaje = ""
    if mensaje: await update.message.reply_text(mensaje, parse_mode="Markdown")

async def usuario_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Uso: `/usuario <DPI>`", parse_mode="Markdown"); return
    dpi = context.args[0].strip()
    if usuario["rol"] == "operador2" and dpi != usuario["dpi_asignado"]:
        await update.message.reply_text(f"🚫 Solo puedes consultar tu propio DPI: `{usuario['dpi_asignado']}`", parse_mode="Markdown"); return
    resultados = buscar_por_usuario(dpi)
    if not resultados: await update.message.reply_text(f"❌ No hay paquetes para el DPI `{dpi}`.", parse_mode="Markdown"); return
    mensaje = f"👤 *Paquetes del usuario* `{dpi}`\n📦 Total: *{len(resultados)}*\n\n"
    for r in resultados:
        gen = r.get('numero_general'); gen_txt = f"`{gen}`" if gen is not None else "_blanco_"
        mensaje += f"📦 N° *{r.get('num_paquete')}* (Gen: {gen_txt}) → `{r.get('ubicacion_armada')}`\n   📅 {r.get('fecha_del')} al {r.get('fecha_al')}\n\n"
        if len(mensaje) > 3800: await update.message.reply_text(mensaje, parse_mode="Markdown"); mensaje = ""
    if mensaje: await update.message.reply_text(mensaje, parse_mode="Markdown")

async def eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Uso: `/eliminar <DPI> <N°>`", parse_mode="Markdown"); return
    dpi = context.args[0]
    if usuario["rol"] == "operador2" and dpi != usuario["dpi_asignado"]:
        await update.message.reply_text("🚫 Solo puedes eliminar paquetes de tu propio DPI."); return
    try: num = int(context.args[1])
    except ValueError: await update.message.reply_text("⚠️ El número debe ser entero."); return
    if not buscar_por_dpi_y_numero(dpi, num): await update.message.reply_text(f"❌ No se encontró el paquete {num} del DPI `{dpi}`."); return
    if eliminar_paquete(dpi, num): await update.message.reply_text(f"✅ Paquete N° {num} eliminado.", parse_mode="Markdown")
    else: await update.message.reply_text("❌ No se pudo eliminar.")

async def editar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return
    if len(context.args) != 4:
        await update.message.reply_text("⚠️ Uso: `/editar <DPI> <N°> <campo> <valor>`\nCampos: `fecha_del`, `fecha_al`, `estanteria`, `fila`, `columna`, `pila`, `numero_general`", parse_mode="Markdown"); return
    dpi = context.args[0]
    if usuario["rol"] == "operador2" and dpi != usuario["dpi_asignado"]:
        await update.message.reply_text("🚫 Solo puedes editar paquetes de tu propio DPI."); return
    try: num = int(context.args[1])
    except ValueError: await update.message.reply_text("⚠️ El número debe ser entero."); return
    campo, valor = context.args[2], context.args[3]
    if actualizar_paquete(dpi, num, campo, valor): await update.message.reply_text(f"✅ Campo `{campo}` actualizado a `{valor}`.", parse_mode="Markdown")
    else: await update.message.reply_text(f"❌ No se pudo actualizar.")

async def agregar_usuario_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1"])
    if not usuario: return
    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Uso: `/agregar_usuario <DPI> <Nombre>`", parse_mode="Markdown"); return
    dpi, nombre = context.args[0], " ".join(context.args[1:])
    if agregar_usuario(dpi, nombre): await update.message.reply_text(f"✅ Usuario agregado: `{dpi}` - {nombre}", parse_mode="Markdown")
    else: await update.message.reply_text(f"❌ El DPI `{dpi}` ya existe.", parse_mode="Markdown")

async def usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1"])
    if not usuario: return
    lista = listar_usuarios()
    if not lista: await update.message.reply_text("No hay usuarios."); return
    mensaje = "👥 *Usuarios (tabla usuarios):*\n\n"
    for dpi, nombre in lista:
        total_paq = contar_paquetes_de_usuario(dpi)
        mensaje += f"• `{dpi}` - {nombre} ({total_paq} paq.)\n"
        if len(mensaje) > 3800: await update.message.reply_text(mensaje, parse_mode="Markdown"); mensaje = ""
    if mensaje: await update.message.reply_text(mensaje, parse_mode="Markdown")

async def eliminar_usuario_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1"])
    if not usuario: return
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Uso: `/eliminar_usuario <DPI>`", parse_mode="Markdown"); return
    dpi = context.args[0]
    if contar_paquetes_de_usuario(dpi) > 0:
        await update.message.reply_text(f"⚠️ El usuario tiene paquetes. Elimínalos primero.", parse_mode="Markdown"); return
    if eliminar_usuario(dpi): await update.message.reply_text(f"✅ Usuario `{dpi}` eliminado.", parse_mode="Markdown")
    else: await update.message.reply_text(f"❌ No se encontró.")

# ==========================================
# FLUJOS DE REGISTRO (ENROLAMIENTO Y EVENTOS)
# ==========================================
async def registrar_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return ConversationHandler.END
    if usuario["rol"] == "operador2":
        context.user_data["dpi"] = usuario["dpi_asignado"]
        await update.message.reply_text(f"📝 *Registro*\n👤 DPI: `{usuario['dpi_asignado']}`\n\n1️⃣ Fecha *DEL*:", parse_mode="Markdown")
        return REG_DEL
    if context.args and len(context.args) >= 8: return await registrar_directo(update, context, usuario)
    context.user_data.clear()
    await update.message.reply_text("📝 *Registro paso a paso*\n\n1️⃣ Ingresa el *DPI/código* del usuario:", parse_mode="Markdown")
    return REG_DPI

async def reg_dpi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    codigo = update.message.text.strip()
    if not codigo: await update.message.reply_text("⚠️ Intenta de nuevo:"); return REG_DPI
    if not usuario_existe(codigo): await update.message.reply_text(f"❌ El código `{codigo}` no está registrado.\nIntenta otro o /cancelar:", parse_mode="Markdown"); return REG_DPI
    context.user_data["dpi"] = codigo
    await update.message.reply_text("2️⃣ Fecha *DEL* (AAAA-MM-DD o DD/MM/AAAA):")
    return REG_DEL

async def reg_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fecha = normalizar_fecha(update.message.text.strip())
    if not fecha: await update.message.reply_text("⚠️ Fecha inválida. Usa AAAA-MM-DD:"); return REG_DEL
    context.user_data["fecha_del"] = fecha
    await update.message.reply_text("3️⃣ Fecha *AL*:")
    return REG_AL

async def reg_al(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fecha = normalizar_fecha(update.message.text.strip())
    if not fecha: await update.message.reply_text("⚠️ Fecha inválida:"); return REG_AL
    context.user_data["fecha_al"] = fecha
    await update.message.reply_text("4️⃣ *Número de paquete*:")
    return REG_NUM

async def reg_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: num = int(update.message.text.strip())
    except: await update.message.reply_text("⚠️ Debe ser número:"); return REG_NUM
    context.user_data["num_paquete"] = num
    await update.message.reply_text("5️⃣ *Estantería*:")
    return REG_EST

async def reg_est(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["estanteria"] = update.message.text.strip()
    await update.message.reply_text("6️⃣ *Fila*:")
    return REG_FIL

async def reg_fil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["fila"] = update.message.text.strip()
    await update.message.reply_text("7️⃣ *Columna* (A, B, C, D):")
    return REG_COL

async def reg_col(update: Update, context: ContextTypes.DEFAULT_TYPE):
    col = update.message.text.strip().upper()
    if col not in ["A", "B", "C", "D"]: await update.message.reply_text("⚠️ Debe ser A, B, C o D:"); return REG_COL
    context.user_data["columna"] = col
    await update.message.reply_text("8️⃣ *Pila/Nivel*:")
    return REG_PIL

async def reg_pil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pila"] = update.message.text.strip()
    await update.message.reply_text("9️⃣ *Número general* (número, 'auto' o 'blanco'):")
    return REG_GENERAL

async def reg_general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    general, datos = update.message.text.strip(), context.user_data
    nuevo_gen, error = insertar_paquete(datos["dpi"], datos["num_paquete"], datos["fecha_del"], datos["fecha_al"], datos["estanteria"], datos["fila"], datos["columna"], datos["pila"], general)
    if error: await update.message.reply_text(f"❌ {error}\n\nReintenta con /registrar"); context.user_data.clear(); return ConversationHandler.END
    gen_texto = f"`{nuevo_gen}`" if nuevo_gen is not None else "_En blanco_"
    await update.message.reply_text(f"✅ *Registrado*\n📦 N°: {datos['num_paquete']}\n🔢 Gen: {gen_texto}\n👤 `{datos['dpi']}`", parse_mode="Markdown")
    context.user_data.clear(); return ConversationHandler.END

async def registrar_directo(update: Update, context: ContextTypes.DEFAULT_TYPE, usuario):
    if len(context.args) not in (8, 9): return ConversationHandler.END
    dpi = context.args[0]
    try:
        num, f_del, f_al = int(context.args[1]), normalizar_fecha(context.args[2]), normalizar_fecha(context.args[3])
        est, fil, col, pil = context.args[4:8]
    except: return ConversationHandler.END
    general = context.args[8] if len(context.args) == 9 else None
    nuevo_gen, error = insertar_paquete(dpi, num, f_del, f_al, est, fil, col, pil, general)
    if error: await update.message.reply_text(f"❌ {error}")
    else: await update.message.reply_text(f"✅ Registrado N° {num}")
    return ConversationHandler.END

async def inicio_registrar_evento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("👶 Nacimiento", callback_data="tipo_Nacimiento")],
        [InlineKeyboardButton("💍 Matrimonio", callback_data="tipo_Matrimonio")],
        [InlineKeyboardButton("⚰️ Defunción", callback_data="tipo_Defuncion")],
        [InlineKeyboardButton("💔 Divorcio", callback_data="tipo_Divorcio")],
        [InlineKeyboardButton("🚫 Negativa", callback_data="tipo_Negativa")],
        [InlineKeyboardButton("🔙 Cancelar", callback_data="menu_inicio")]
    ])
    await query.edit_message_text("📝 *Registrar Evento*\n\nSelecciona el tipo:", reply_markup=teclado, parse_mode="Markdown")
    return EV_TIPO

async def flujo_evento_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    tipo = query.data.replace("tipo_", "")
    context.user_data["ev_tipo"] = tipo
    await query.edit_message_text(f"Tipo: *{tipo}*\n\nIngresa el *Año* (ej: 2023):", parse_mode="Markdown")
    return EV_ANIO

async def _responder(update, texto, teclado=None):
    if update.callback_query:
        await update.callback_query.edit_message_text(texto, reply_markup=teclado)
    else:
        await update.message.reply_text(texto, reply_markup=teclado)

def teclado_sugerencia(prefijo, valor, extra=None):
    filas = [[InlineKeyboardButton(f"✅ Sí, {valor}", callback_data=f"{prefijo}_si"),
              InlineKeyboardButton("✏️ Escribir otro", callback_data=f"{prefijo}_otro")]]
    if extra: filas.append(extra)
    filas.append([InlineKeyboardButton("🔙 Cancelar", callback_data="menu_inicio")])
    return InlineKeyboardMarkup(filas)

async def flujo_evento_anio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: anio = int(update.message.text.strip())
    except: await update.message.reply_text("⚠️ Debe ser un año válido:"); return EV_ANIO
    d = context.user_data
    d["ev_anio"] = anio
    sug = siguiente_num_paquete(d["ev_tipo"], anio)
    d["sug_num"] = sug
    await update.message.reply_text(f"{d['ev_tipo']} {anio}\n\nEl siguiente paquete del año sería el N° {sug}.\n¿Es correcto?",
                                    reply_markup=teclado_sugerencia("sugnum", sug))
    return EV_NUM

async def _pedir_folio_ini(update, context):
    d = context.user_data
    sug, origen = siguiente_folio_del(d["ev_tipo"], d["ev_anio"])
    if sug is None:
        await _responder(update, f"Paquete N° {d['ev_num']}.\n\nIngresa el Número de Evento DEL (ej: 18191):")
        return EV_FOLIO_INI
    d["sug_del"] = sug
    await _responder(update, f"Paquete N° {d['ev_num']}.\n\nSegún los registros ({origen}), el Evento DEL sería {sug}.\n¿Es correcto?",
                     teclado_sugerencia("sugdel", sug))
    return EV_FOLIO_INI

async def flujo_evento_num_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == "sugnum_si":
        context.user_data["ev_num"] = context.user_data["sug_num"]
        return await _pedir_folio_ini(update, context)
    await query.edit_message_text("Escribe el Número de Paquete:")
    return EV_NUM

async def flujo_evento_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: num = int(update.message.text.strip())
    except: await update.message.reply_text("⚠️ Debe ser un número entero:"); return EV_NUM
    d = context.user_data
    if buscar_evento_por_tap(d["ev_tipo"], d["ev_anio"], num):
        await update.message.reply_text(f"⚠️ Ya existe el paquete {num} de {d['ev_tipo']} {d['ev_anio']}. Escribe otro número:")
        return EV_NUM
    d["ev_num"] = num
    return await _pedir_folio_ini(update, context)

async def flujo_evento_folio_ini_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == "sugdel_si":
        d = context.user_data
        d["ev_folio_ini"] = d["sug_del"]
        await query.edit_message_text(f"DEL: {d['sug_del']}\n\nIngresa el Número de Evento AL (ej: 18290):")
        return EV_FOLIO_FIN
    await query.edit_message_text("Ingresa el Número de Evento DEL:")
    return EV_FOLIO_INI

async def flujo_evento_folio_ini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: folio = int(update.message.text.strip())
    except: await update.message.reply_text("⚠️ Debe ser un número entero:"); return EV_FOLIO_INI
    context.user_data["ev_folio_ini"] = folio
    await update.message.reply_text("Ingresa el Número de Evento AL (ej: 18290):")
    return EV_FOLIO_FIN

async def _pedir_general(update, context):
    sug = siguiente_general()
    context.user_data["sug_gen"] = sug
    await _responder(update, f"El siguiente N° General correlativo sería {sug}.\n¿Es correcto?",
                     teclado_sugerencia("suggen", sug, extra=[InlineKeyboardButton("⬜ Dejar en blanco", callback_data="suggen_blanco")]))
    return EV_GENERAL

async def flujo_evento_folio_fin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: folio = int(update.message.text.strip())
    except: await update.message.reply_text("⚠️ Debe ser un número entero:"); return EV_FOLIO_FIN
    if folio < context.user_data.get("ev_folio_ini", 0):
        await update.message.reply_text(f"⚠️ El AL no puede ser menor que el DEL ({context.user_data['ev_folio_ini']}). Escribe el AL de nuevo:")
        return EV_FOLIO_FIN
    context.user_data["ev_folio_fin"] = folio
    return await _pedir_general(update, context)

async def flujo_evento_general_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    d = context.user_data
    if query.data == "suggen_otro":
        await query.edit_message_text("Escribe el Número General (o 'blanco'):")
        return EV_GENERAL
    d["ev_general"] = str(d["sug_gen"]) if query.data == "suggen_si" else "blanco"
    await query.edit_message_text(f"N° General: {d['ev_general']}\n\nIngresa la Estantería:")
    return EV_EST

async def flujo_evento_general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    general = update.message.text.strip()
    if general.lower() not in ("blanco", "null", ""):
        try: n = int(general)
        except: await update.message.reply_text("⚠️ Escribe un número entero o 'blanco':"); return EV_GENERAL
        if buscar_evento_por_general(n):
            await update.message.reply_text(f"⚠️ El N° General {n} ya está en uso. Escribe otro (o 'blanco'):"); return EV_GENERAL
    context.user_data["ev_general"] = general
    await update.message.reply_text("Ingresa la Estantería:")
    return EV_EST

async def flujo_evento_est(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ev_est"] = update.message.text.strip()
    await update.message.reply_text("Ingresa la *Fila*:")
    return EV_FIL

async def flujo_evento_fil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ev_fil"] = update.message.text.strip()
    await update.message.reply_text("Ingresa la *Columna* (A, B, C, D):")
    return EV_COL

async def flujo_evento_col(update: Update, context: ContextTypes.DEFAULT_TYPE):
    col = update.message.text.strip().upper()
    if col not in ["A", "B", "C", "D"]: await update.message.reply_text("⚠️ Debe ser A, B, C o D:"); return EV_COL
    context.user_data["ev_col"] = col
    await update.message.reply_text("Ingresa la *Pila/Nivel*:")
    return EV_PIL

async def flujo_evento_pil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pil, datos = update.message.text.strip(), context.user_data
    usuario = await verificar_acceso(update)
    exito, error = insertar_evento(datos["ev_tipo"], datos["ev_anio"], datos["ev_num"], datos["ev_folio_ini"], datos["ev_folio_fin"], datos["ev_general"], datos["ev_est"], datos["ev_fil"], datos["ev_col"], pil, usuario["dpi_asignado"] or usuario["nombre"])
    if exito:
        gen_txt = f"`{datos['ev_general']}`" if datos['ev_general'].lower() not in ('blanco', 'null', '') else "_blanco_"
        await update.message.reply_text(f"✅ *Evento Registrado*\n\nTipo: {datos['ev_tipo']}\nAño: {datos['ev_anio']}\nPaquete: {datos['ev_num']}\nGen: {gen_txt}\nEventos: {datos['ev_folio_ini']} al {datos['ev_folio_fin']}\nUbicación: E{datos['ev_est']}-F{datos['ev_fil']}-{datos['ev_col']}-P{pil}", parse_mode="Markdown")
    else: await update.message.reply_text(f"❌ Error: {error}")
    context.user_data.clear(); return ConversationHandler.END

# --- BÚSQUEDAS DE EVENTOS ---
async def inicio_buscar_folio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("🔍 *Buscar por Evento*\n\nEscribe el número (DEL, AL o intermedio):", parse_mode="Markdown")
    return EV_BUS_FOLIO

def texto_evento(res):
    gen = res.get("numero_general")
    gen_txt = gen if gen is not None else "En blanco"
    return (f"📖 Evento encontrado\n\nTipo: {res['tipo_evento']}\nAño: {res['anio']}\n"
            f"Paquete N°: {res['num_paquete']}\nGen: {gen_txt}\n"
            f"Eventos: {res['folio_inicio']} al {res['folio_fin']}\n"
            f"Ubicación: E{res['estanteria']}-F{res['fila']}-{res['columna']}-P{res['pila']}")

async def flujo_buscar_folio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: folio = int(update.message.text.strip())
    except: await update.message.reply_text("⚠️ Debe ser un número entero:"); return EV_BUS_FOLIO
    resultados = buscar_evento_por_folio(folio)
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if resultados:
        mensaje = "\n\n".join(texto_evento(r) for r in resultados)
        if len(resultados) > 1:
            mensaje = f"ℹ️ El {folio} aparece en {len(resultados)} paquetes:\n\n" + mensaje
        await update.message.reply_text(mensaje, reply_markup=btn_volver)
    else: await update.message.reply_text(f"❌ No se encontró paquete para el evento {folio}.", reply_markup=btn_volver)
    return ConversationHandler.END

async def inicio_buscar_general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("🔢 *Buscar por N° General*\n\nEscribe el número general del paquete:", parse_mode="Markdown")
    return EV_BUS_GENERAL

async def flujo_buscar_general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: num_gen = int(update.message.text.strip())
    except: await update.message.reply_text("⚠️ Debe ser un número entero:"); return EV_BUS_GENERAL
    res = buscar_evento_por_general(num_gen)
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if res: await update.message.reply_text(texto_evento(res), reply_markup=btn_volver)
    else: await update.message.reply_text(f"❌ No se encontró paquete con N° General {num_gen}.", reply_markup=btn_volver)
    return ConversationHandler.END

# --- EDICIÓN, ETIQUETA Y ELIMINACIÓN DE EVENTOS (comparten la búsqueda del paquete) ---
def teclado_campos_evento():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("N° General", callback_data="campo_numero_general")],
        [InlineKeyboardButton("Tipo de evento", callback_data="campo_tipo_evento"), InlineKeyboardButton("Año", callback_data="campo_anio")],
        [InlineKeyboardButton("Paquete", callback_data="campo_num_paquete")],
        [InlineKeyboardButton("Evento DEL", callback_data="campo_folio_inicio"), InlineKeyboardButton("Evento AL", callback_data="campo_folio_fin")],
        [InlineKeyboardButton("Estantería", callback_data="campo_estanteria"), InlineKeyboardButton("Fila", callback_data="campo_fila")],
        [InlineKeyboardButton("Columna", callback_data="campo_columna"), InlineKeyboardButton("Pila", callback_data="campo_pila")],
        [InlineKeyboardButton("🔙 Cancelar", callback_data="menu_inicio")]
    ])

TITULOS_ACCION_EVENTO = {"editar": "✏️ Editar Paquete de Evento",
                         "etiqueta": "🏷️ Generar Etiqueta de Evento",
                         "eliminar": "🗑️ Eliminar Paquete de Evento"}

def parsear_tap(texto):
    """'Nacimiento 2022 27' -> ('Nacimiento', 2022, 27) o None."""
    partes = texto.split()
    if len(partes) != 3: return None
    tipo = normalizar_tipo(partes[0])
    try: anio, num = int(partes[1]), int(partes[2])
    except ValueError: return None
    return (tipo, anio, num) if tipo else None

async def inicio_editar_evento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return ConversationHandler.END
    accion = query.data.split("_")[1]  # editar / etiqueta / eliminar
    context.user_data.clear()
    context.user_data["ev_accion"] = accion
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔢 Por N° de registro (evento)", callback_data="edmodo_folio")],
        [InlineKeyboardButton("📋 Por N° General", callback_data="edmodo_general")],
        [InlineKeyboardButton("📦 Por Tipo, Año y Paquete", callback_data="edmodo_tap")],
        [InlineKeyboardButton("🔙 Cancelar", callback_data="menu_inicio")]
    ])
    await query.edit_message_text(f"{TITULOS_ACCION_EVENTO[accion]}\n\n¿Cómo quieres buscar el paquete?\n(Si aún no tiene N° General, usa el N° de registro o Tipo/Año/Paquete.)", reply_markup=teclado)
    return EV_EDIT_MODO

async def flujo_edit_modo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    modo = query.data.replace("edmodo_", "")
    context.user_data["edit_modo"] = modo
    if modo == "tap":
        await query.edit_message_text("Escribe: <Tipo> <Año> <Paquete>\nEjemplo: Nacimiento 2022 27\n(Tipos: Nacimiento, Matrimonio, Defuncion, Divorcio, Negativa)")
    else:
        etiqueta = "N° de registro (cualquier número dentro del rango DEL-AL del paquete)" if modo == "folio" else "N° General"
        await query.edit_message_text(f"Escribe el {etiqueta}:")
    return EV_EDIT_GENERAL

def _resumen_corto(r):
    gen = r.get("numero_general")
    return f"{r['tipo_evento']} {r['anio']} · Paq {r['num_paquete']} · {r['folio_inicio']}-{r['folio_fin']} · Gen {gen if gen is not None else '—'}"

async def _enviar_etiqueta_evento(mensaje, paquete):
    ubic = f"E{paquete['estanteria']}-F{paquete['fila']}-{paquete['columna']}-P{paquete['pila']}"
    ruta, error = generar_pdf_etiqueta_evento(paquete["tipo_evento"], paquete["anio"], paquete["num_paquete"],
                                              paquete["folio_inicio"], paquete["folio_fin"], paquete.get("numero_general"), ubic)
    if not ruta:
        await mensaje.reply_text(f"❌ Error al generar PDF: {error}"); return
    with open(ruta, "rb") as f:
        await mensaje.reply_document(document=f, filename=f"etiqueta_{paquete['tipo_evento']}_{paquete['anio']}_{paquete['num_paquete']}_{datetime.now().strftime('%H%M%S')}.pdf",
                                     caption=f"🏷️ Etiqueta: {_resumen_corto(paquete)}\nUbicación: {ubic}")
    os.remove(ruta)

async def _continuar_con_paquete(mensaje, context, paquete):
    """Con el paquete ya identificado, sigue según la acción elegida (editar / etiqueta / eliminar)."""
    context.user_data["edit_id"] = paquete["id"]
    accion = context.user_data.get("ev_accion", "editar")
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if accion == "etiqueta":
        await _enviar_etiqueta_evento(mensaje, paquete)
        await mensaje.reply_text("¿Algo más?", reply_markup=btn_volver)
        context.user_data.clear(); return ConversationHandler.END
    if accion == "eliminar":
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Sí, eliminar", callback_data="evdel_si")],
            [InlineKeyboardButton("🔙 Cancelar", callback_data="menu_inicio")]
        ])
        await mensaje.reply_text(f"⚠️ ¿Eliminar este paquete?\n\n{texto_evento(paquete)}\n\nEsta acción no se puede deshacer.", reply_markup=teclado)
        return EV_EDIT_CONFIRMAR
    await mensaje.reply_text(f"✅ Paquete seleccionado:\n{texto_evento(paquete)}\n\n¿Qué campo deseas editar?", reply_markup=teclado_campos_evento())
    return EV_EDIT_CAMPO

async def flujo_edit_general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    modo = context.user_data.get("edit_modo", "folio")
    if modo == "tap":
        datos = parsear_tap(texto)
        if not datos:
            await update.message.reply_text("⚠️ Formato inválido. Escribe: <Tipo> <Año> <Paquete> (ej: Nacimiento 2022 27) o /cancelar:")
            return EV_EDIT_GENERAL
        resultados = buscar_evento_por_tap(*datos)
    else:
        try: numero = int(texto)
        except: await update.message.reply_text("⚠️ Debe ser un número entero:"); return EV_EDIT_GENERAL
        if modo == "general":
            r = buscar_evento_por_general(numero)
            resultados = [r] if r else []
        else:
            resultados = buscar_evento_por_folio(numero)
    if not resultados:
        await update.message.reply_text("❌ No se encontró ningún paquete con esos datos. Intenta otro o /cancelar:")
        return EV_EDIT_GENERAL
    if len(resultados) == 1:
        return await _continuar_con_paquete(update.message, context, resultados[0])
    botones = [[InlineKeyboardButton(_resumen_corto(r), callback_data=f"edsel_{r['id']}")] for r in resultados]
    botones.append([InlineKeyboardButton("🔙 Cancelar", callback_data="menu_inicio")])
    await update.message.reply_text(f"Hay {len(resultados)} paquetes que coinciden. ¿Cuál eliges?", reply_markup=InlineKeyboardMarkup(botones))
    return EV_EDIT_ELEGIR

async def flujo_edit_elegir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    paquete = buscar_evento_por_id(int(query.data.replace("edsel_", "")))
    if not paquete:
        await query.edit_message_text("❌ Ese paquete ya no existe."); return ConversationHandler.END
    await query.edit_message_reply_markup(reply_markup=None)
    return await _continuar_con_paquete(query.message, context, paquete)

async def flujo_edit_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return ConversationHandler.END
    id_evento = context.user_data.get("edit_id")
    paquete = buscar_evento_por_id(id_evento) if id_evento else None
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if paquete and eliminar_evento_id(id_evento):
        await query.edit_message_text(f"✅ Paquete eliminado:\n{_resumen_corto(paquete)}", reply_markup=btn_volver)
    else:
        await query.edit_message_text("❌ No se pudo eliminar (¿ya no existe?).", reply_markup=btn_volver)
    context.user_data.clear()
    return ConversationHandler.END

async def flujo_edit_campo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    campo = query.data.replace("campo_", "")
    context.user_data["edit_campo"] = campo
    extra = "\n(Escribe 'blanco' para dejarlo vacío.)" if campo == "numero_general" else ""
    await query.edit_message_text(f"Has seleccionado editar: {campo}.\n\nEscribe el nuevo valor:{extra}")
    return EV_EDIT_VALOR

async def flujo_edit_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    valor = update.message.text.strip()
    id_evento = context.user_data.get("edit_id")
    campo = context.user_data.get("edit_campo")
    ok, error = actualizar_evento_id(id_evento, campo, valor)
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if ok:
        await update.message.reply_text(f"✅ {campo} actualizado a: {valor}", reply_markup=btn_volver)
    else:
        await update.message.reply_text(f"❌ {error}", reply_markup=btn_volver)
    context.user_data.clear()
    return ConversationHandler.END

EVF_AYUDA = {
    "tap":   ("📦 Por Tipo, Año y Paquete", "<Tipo> <Año> <Paquete>", "Nacimiento 2022 27"),
    "lista": ("📋 Listar por Año / Tipo", "<Año> [Tipo]", "2022 Nacimiento   (o solo 2022 para todos los tipos)"),
    "ubic":  ("📍 Por Ubicación", "<Estantería> <Fila> <Columna> <Pila>   (usa * para ignorar un dato)", "3 2 A *"),
}

async def evf_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    usuario = await verificar_acceso(update)
    if not usuario: return ConversationHandler.END
    clave = query.data.replace("evf_", "")
    titulo, formato, ejemplo = EVF_AYUDA[clave]
    context.user_data["evf_clave"] = clave
    await query.edit_message_text(f"{titulo}\n\nEscribe: {formato}\nEjemplo: {ejemplo}\n\n(/cancelar para salir)")
    return EV_FILTRO_ARGS

def _linea_lista(r):
    gen = r.get("numero_general")
    return (f"📦 {r['tipo_evento']} {r['anio']} · Paq {r['num_paquete']} · {r['folio_inicio']}-{r['folio_fin']} · "
            f"Gen {gen if gen is not None else '—'} · E{r['estanteria']}-F{r['fila']}-{r['columna']}-P{r['pila']}")

async def evf_recibir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clave = context.user_data.get("evf_clave")
    partes = update.message.text.split()
    resultados, error = [], None
    if clave == "tap":
        datos = parsear_tap(update.message.text)
        if datos: resultados = buscar_evento_por_tap(*datos)
        else: error = "Formato inválido. Ejemplo: Nacimiento 2022 27"
    elif clave == "lista":
        anio, tipo = None, None
        for p in partes:
            if p.isdigit(): anio = int(p)
            elif normalizar_tipo(p): tipo = normalizar_tipo(p)
        if anio is None: error = "Escribe el año. Ejemplo: 2022 Nacimiento"
        else: resultados = listar_eventos(anio, tipo)
    elif clave == "ubic":
        if len(partes) != 4: error = "Escribe 4 datos: Estantería Fila Columna Pila (usa * para ignorar uno). Ejemplo: 3 2 A *"
        else: resultados = buscar_eventos_por_ubicacion(*partes)
    else:
        return ConversationHandler.END
    if error:
        await update.message.reply_text(f"⚠️ {error}\nInténtalo de nuevo o /cancelar:")
        return EV_FILTRO_ARGS
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if not resultados:
        await update.message.reply_text("❌ No se encontraron paquetes con ese filtro.", reply_markup=btn_volver)
    else:
        bloque = f"Resultados: {len(resultados)} paquete(s)\n\n"
        for r in resultados:
            linea = (_linea_lista(r) if clave in ("lista", "ubic") else texto_evento(r)) + "\n\n"
            if len(bloque) + len(linea) > 3800:
                await update.message.reply_text(bloque); bloque = ""
            bloque += linea
        await update.message.reply_text(bloque or "Fin.", reply_markup=btn_volver)
    context.user_data.clear()
    return ConversationHandler.END

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelado.")
    return ConversationHandler.END

async def cancelar_a_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botón 'Volver/Cancelar' dentro de un flujo: cierra la conversación y muestra el menú."""
    context.user_data.clear()
    await update.callback_query.answer()
    await start(update, context)
    return ConversationHandler.END

async def totales_anio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    usuario = await verificar_acceso(update)
    if not usuario: return
    res = conectar_db().execute("""
        SELECT anio, tipo_evento, COUNT(*), COALESCE(SUM(folio_fin - folio_inicio + 1), 0)
        FROM eventos GROUP BY anio, tipo_evento ORDER BY anio, tipo_evento
    """)
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if not res.rows:
        await query.edit_message_text("📊 Aún no hay paquetes de eventos registrados.", reply_markup=btn_volver); return
    texto, anio_actual = "📊 Totales por año\n", None
    for anio, tipo, paquetes, registros in res.rows:
        if anio != anio_actual:
            texto += f"\n📅 {anio}\n"; anio_actual = anio
        texto += f"  • {tipo}: {paquetes} paquete(s), {registros} registro(s)\n"
    await query.edit_message_text(texto, reply_markup=btn_volver)

async def enrol_total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    usuario = await verificar_acceso(update)
    if not usuario: return
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    await query.edit_message_text(f"📊 Total de paquetes DPI: {contar_paquetes()}", reply_markup=btn_volver)

# Botones de Enrolamiento: piden los datos y reutilizan los comandos existentes
ENROL_COMANDOS = {
    "buscar":   (lambda: buscar,   "🔍 Buscar por DPI y N°",   "<DPI> <N°>",                      "PCOTTON320 1"),
    "general":  (lambda: general,  "🔢 Buscar por N° General", "<N° general>",                    "41"),
    "fecha":    (lambda: fecha,    "📅 Buscar por fecha",      "<DPI> <fecha DD/MM/AAAA>",        "PCOTTON320 15/03/2024"),
    "usuario":  (lambda: usuario_cmd, "👤 Paquetes de un usuario", "<DPI>",                       "PCOTTON320"),
    "etiqueta": (lambda: etiqueta, "🏷️ Etiqueta PDF",          "<DPI> <N°>",                      "PCOTTON320 1"),
    "eliminar": (lambda: eliminar, "🗑️ Eliminar paquete",      "<DPI> <N°>",                      "PCOTTON320 1"),
    "editar":   (lambda: editar,   "✏️ Editar paquete",        "<DPI> <N°> <campo> <valor>\nCampos: fecha_del, fecha_al, estanteria, fila, columna, pila, numero_general", "PCOTTON320 1 fila 3"),
}

async def enrol_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    usuario = await verificar_acceso(update)
    if not usuario: return ConversationHandler.END
    clave = query.data.replace("enrol_", "")
    _, titulo, formato, ejemplo = ENROL_COMANDOS[clave]
    context.user_data["enrol_cmd"] = clave
    await query.edit_message_text(f"{titulo}\n\nEscribe: {formato}\nEjemplo: {ejemplo}\n\n(/cancelar para salir)")
    return ENROL_ARGS

async def enrol_recibir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clave = context.user_data.pop("enrol_cmd", None)
    if clave not in ENROL_COMANDOS: return ConversationHandler.END
    funcion = ENROL_COMANDOS[clave][0]()
    context.args = update.message.text.split()
    await funcion(update, context)
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    await update.message.reply_text("¿Algo más?", reply_markup=btn_volver)
    return ConversationHandler.END

async def registrar_inicio_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Versión con botón de /registrar (el flujo paso a paso es el mismo)."""
    query = update.callback_query; await query.answer()
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return ConversationHandler.END
    context.user_data.clear()
    if usuario["rol"] == "operador2":
        context.user_data["dpi"] = usuario["dpi_asignado"]
        await query.message.reply_text(f"📝 *Registro*\n👤 DPI: `{usuario['dpi_asignado']}`\n\n1️⃣ Fecha *DEL*:", parse_mode="Markdown")
        return REG_DEL
    await query.message.reply_text("📝 *Registro paso a paso*\n\n1️⃣ Ingresa el *DPI/código* del usuario:", parse_mode="Markdown")
    return REG_DPI

async def manejar_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Registra el error en los logs y AVISA en Telegram en vez de quedarse en silencio."""
    if isinstance(context.error, Conflict):
        print("⚠️ Conflict: hay otra instancia del bot con el mismo token corriendo.")
        return
    print("❌ Error en el bot:")
    traceback.print_exception(type(context.error), context.error, context.error.__traceback__)
    try:
        if isinstance(update, Update):
            if update.callback_query:
                await update.callback_query.message.reply_text("❌ Ocurrió un error. Intenta de nuevo o usa /start.")
            elif update.effective_message:
                await update.effective_message.reply_text("❌ Ocurrió un error. Intenta de nuevo o usa /start.")
    except Exception:
        pass

# ==========================================
# MAIN Y SERVIDOR WEB PARA RENDER
# ==========================================
app_web = Flask(__name__)

@app_web.route('/')
def health_check(): return "Bot is running!"

if __name__ == "__main__":
    def iniciar_flask():
        import os
        puerto = int(os.environ.get("PORT", 8080))
        app_web.run(host="0.0.0.0", port=puerto)

    hilo_flask = threading.Thread(target=iniciar_flask)
    hilo_flask.daemon = True
    hilo_flask.start()

    print("🤖 Iniciando bot...")
    inicializar_tabla_telegram()
    inicializar_tabla_eventos()
    
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("mi_id", mi_id))
    app.add_handler(CommandHandler("buscar", buscar))
    app.add_handler(CommandHandler("etiqueta", etiqueta))
    app.add_handler(CommandHandler("total", total))
    app.add_handler(CommandHandler("general", general))
    app.add_handler(CommandHandler("fecha", fecha))
    app.add_handler(CommandHandler("usuario", usuario_cmd))
    app.add_handler(CommandHandler("eliminar", eliminar))
    app.add_handler(CommandHandler("editar", editar))
    app.add_handler(CommandHandler("agregar_usuario", agregar_usuario_cmd))
    app.add_handler(CommandHandler("usuarios", usuarios))
    app.add_handler(CommandHandler("eliminar_usuario", eliminar_usuario_cmd))  
    app.add_handler(CommandHandler("autorizar", autorizar))
    app.add_handler(CommandHandler("lista_autorizados", lista_autorizados))
    
    # 1. Flujo de Registro Enrolamiento
    conv_registro = ConversationHandler(
        entry_points=[CommandHandler("registrar", registrar_inicio), CallbackQueryHandler(registrar_inicio_cb, pattern="^enrol_registrar$")],
        states={
            REG_DPI: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_dpi)],
            REG_DEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_del)],
            REG_AL: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_al)],
            REG_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_num)],
            REG_EST: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_est)],
            REG_FIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_fil)],
            REG_COL: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_col)],
            REG_PIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_pil)],
            REG_GENERAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_general)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar), CallbackQueryHandler(cancelar_a_menu, pattern="^menu_inicio$")],
        per_message=False,
    )
    app.add_handler(conv_registro)

    # 2. Flujo de Registro Eventos
    conv_evento = ConversationHandler(
        entry_points=[CallbackQueryHandler(inicio_registrar_evento, pattern="^accion_registrar_evento$")],
        states={
            EV_TIPO: [CallbackQueryHandler(flujo_evento_tipo, pattern="^tipo_")],
            EV_ANIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_anio)],
            EV_NUM: [CallbackQueryHandler(flujo_evento_num_cb, pattern="^sugnum_"),
                     MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_num)],
            EV_FOLIO_INI: [CallbackQueryHandler(flujo_evento_folio_ini_cb, pattern="^sugdel_"),
                           MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_folio_ini)],
            EV_FOLIO_FIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_folio_fin)],
            EV_GENERAL: [CallbackQueryHandler(flujo_evento_general_cb, pattern="^suggen_"),
                         MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_general)],
            EV_EST: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_est)],
            EV_FIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_fil)],
            EV_COL: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_col)],
            EV_PIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_pil)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar), CallbackQueryHandler(cancelar_a_menu, pattern="^menu_inicio$")],
        per_message=False,
    )
    app.add_handler(conv_evento)

    # 3. Flujo de Búsqueda por Evento (Folio)
    conv_bus_folio = ConversationHandler(
        entry_points=[CallbackQueryHandler(inicio_buscar_folio, pattern="^accion_buscar_folio$")],
        states={EV_BUS_FOLIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_buscar_folio)]},
        fallbacks=[CommandHandler("cancelar", cancelar), CallbackQueryHandler(cancelar_a_menu, pattern="^menu_inicio$")],
        per_message=False,
    )
    app.add_handler(conv_bus_folio)

    # 4. Flujo de Búsqueda por Número General
    conv_bus_general = ConversationHandler(
        entry_points=[CallbackQueryHandler(inicio_buscar_general, pattern="^accion_buscar_general$")],
        states={EV_BUS_GENERAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_buscar_general)]},
        fallbacks=[CommandHandler("cancelar", cancelar), CallbackQueryHandler(cancelar_a_menu, pattern="^menu_inicio$")],
        per_message=False,
    )
    app.add_handler(conv_bus_general)

    # 5. Flujo de Edición de Eventos (NUEVO)
    conv_edit_evento = ConversationHandler(
        entry_points=[CallbackQueryHandler(inicio_editar_evento, pattern="^accion_(editar|etiqueta|eliminar)_evento$")],
        states={
            EV_EDIT_MODO: [CallbackQueryHandler(flujo_edit_modo, pattern="^edmodo_")],
            EV_EDIT_GENERAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_edit_general)],
            EV_EDIT_ELEGIR: [CallbackQueryHandler(flujo_edit_elegir, pattern="^edsel_")],
            EV_EDIT_CONFIRMAR: [CallbackQueryHandler(flujo_edit_eliminar, pattern="^evdel_si$")],
            EV_EDIT_CAMPO: [CallbackQueryHandler(flujo_edit_campo, pattern="^campo_")],
            EV_EDIT_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_edit_valor)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar), CallbackQueryHandler(cancelar_a_menu, pattern="^menu_inicio$")],
        per_message=False,
    )
    app.add_handler(conv_edit_evento)
    
    # 5a. Filtros de consulta de eventos
    conv_ev_filtros = ConversationHandler(
        entry_points=[CallbackQueryHandler(evf_inicio, pattern="^evf_(tap|lista|ubic)$")],
        states={EV_FILTRO_ARGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, evf_recibir)]},
        fallbacks=[CommandHandler("cancelar", cancelar), CallbackQueryHandler(cancelar_a_menu, pattern="^menu_inicio$")],
        per_message=False,
    )
    app.add_handler(conv_ev_filtros)

    # 5b. Consultas y ediciones de Enrolamiento con botones
    conv_enrol = ConversationHandler(
        entry_points=[CallbackQueryHandler(enrol_inicio, pattern="^enrol_(buscar|general|fecha|usuario|etiqueta|eliminar|editar)$")],
        states={ENROL_ARGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, enrol_recibir)]},
        fallbacks=[CommandHandler("cancelar", cancelar), CallbackQueryHandler(cancelar_a_menu, pattern="^menu_inicio$")],
        per_message=False,
    )
    app.add_handler(conv_enrol)
    app.add_handler(CallbackQueryHandler(enrol_total, pattern="^enrol_total$"))
    app.add_handler(CallbackQueryHandler(totales_anio, pattern="^accion_totales_anio$"))

    # 6. MANEJADOR DE BOTONES DEL MENÚ
    app.add_handler(CallbackQueryHandler(botones_menu, pattern="^menu_"))
    
    print("✅ Bot activo. Presiona /start en Telegram.")
    app.add_error_handler(manejar_error)
    app.run_polling(drop_pending_updates=True)
