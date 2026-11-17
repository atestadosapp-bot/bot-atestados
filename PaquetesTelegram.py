import asyncio
import contextvars
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
import threading
import traceback
import unicodedata
import libsql_client
from flask import Flask, Response
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters, CallbackQueryHandler, TypeHandler
)

# ==========================================
# CONFIGURACIÓN
# ==========================================
TOKEN = os.environ.get("TELEGRAM_TOKEN","")
TURSO_URL = "libsql://atestados-atestadosapp.aws-us-east-1.turso.io"
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "PON_AQUI_TU_TOKEN_SI_PRUEBAS_LOCAL")

ADMIN_INICIAL_ID = 6491907692

GT_TZ = timezone(timedelta(hours=-6))  # Guatemala (el servidor de Render trabaja en UTC)
def ahora_gt():
    return datetime.now(GT_TZ).strftime("%Y-%m-%d %H:%M:%S")

# Usuario del bot que está ejecutando la acción actual (lo fija fijar_actor antes de cada handler)
ACTOR_ACTUAL = contextvars.ContextVar("actor_actual", default=None)
_CACHE_ACTOR = {}

# Estados para el ConversationHandler de /registrar (ENROLAMIENTO)
(REG_DPI, REG_DEL, REG_AL, REG_NUM, REG_EST, REG_FIL, REG_COL, REG_PIL, REG_GENERAL) = range(9)

# Estados para el flujo de EVENTOS (REGISTRO CIVIL)
(EV_TIPO, EV_ANIO, EV_NUM, EV_FOLIO_INI, EV_FOLIO_FIN, EV_GENERAL, EV_EST, EV_FIL, EV_COL, EV_PIL) = range(20, 30)
(EV_BUS_FOLIO, EV_BUS_GENERAL) = range(30, 32)
(EV_EDIT_GENERAL, EV_EDIT_CAMPO, EV_EDIT_VALOR) = range(32, 35) # Nuevos estados para edición
ENROL_ARGS = 40  # Estado para las consultas/ediciones de Enrolamiento con botones
(EV_EDIT_MODO, EV_EDIT_ELEGIR) = range(41, 43)  # Edición de eventos: cómo buscar y cuál elegir
(EV_EDIT_CONFIRMAR, EV_FILTRO_ARGS) = range(43, 45)  # Confirmar eliminación / filtros de consulta
REG_UBIC = 45  # Registro de enrolamiento: confirmar ubicación sugerida
BIT_TEXTO = 52  # Búsqueda en la bitácora
(LT_MODO, LT_VALOR) = range(53, 55)  # Etiquetas en lote
EV_UBIC = 55  # Registro de eventos: confirmar ubicación sugerida
(CG_DATOS, CG_CONFIRMAR) = range(56, 58)  # Carga masiva
(EP_MODO, EP_VALOR, EP_ELEGIR, EP_CAMPO, EP_NUEVO, EP_CONFIRMAR) = range(46, 52)  # Buscar / editar / etiqueta / eliminar (enrolamiento)

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
        """, (ADMIN_INICIAL_ID, "Alex Miranda", "admin", "enrolamiento", None, ahora_gt()[:16]))
        print(f"✅ Admin inicial registrado: {ADMIN_INICIAL_ID}")

def inicializar_papelera_bitacora():
    client = conectar_db()
    client.execute("""
        CREATE TABLE IF NOT EXISTS papelera (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tabla TEXT NOT NULL,
            datos TEXT NOT NULL,
            resumen TEXT,
            eliminado_por TEXT,
            fecha TEXT,
            restaurado INTEGER DEFAULT 0
        )""")
    client.execute("""
        CREATE TABLE IF NOT EXISTS bitacora (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT,
            telegram_id INTEGER,
            usuario TEXT,
            rol TEXT,
            accion TEXT,
            entidad TEXT,
            detalle TEXT
        )""")

def bitacora(accion, entidad, detalle):
    """Registra una acción en la bitácora. Nunca interrumpe la operación si falla."""
    try:
        actor = ACTOR_ACTUAL.get() or (None, "desconocido", "")
        conectar_db().execute(
            "INSERT INTO bitacora (fecha, telegram_id, usuario, rol, accion, entidad, detalle) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ahora_gt(), actor[0], actor[1], actor[2], accion, entidad, detalle))
    except Exception as e:
        print(f"⚠️ No se pudo escribir en la bitácora: {e}")

def mover_a_papelera(tabla, fila, resumen):
    """Guarda una copia del registro (JSON) en la papelera. Si falla, lanza error y NO se debe borrar."""
    actor = ACTOR_ACTUAL.get() or (None, "desconocido", "")
    conectar_db().execute(
        "INSERT INTO papelera (tabla, datos, resumen, eliminado_por, fecha, restaurado) VALUES (?, ?, ?, ?, ?, 0)",
        (tabla, json.dumps(fila, ensure_ascii=False), resumen, actor[1], ahora_gt()))

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
        """, (telegram_id, nombre, rol, area, dpi_asignado, ahora_gt()[:16]))
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

def _insertar_paquete(dpi, num, f_del, f_al, est, fil, col, pil, general=None):
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

def siguiente_num_paquete_usuario(dpi):
    r = conectar_db().execute("SELECT MAX(num_paquete) FROM paquetes WHERE usuario_codigo = ?", (dpi,))
    return (r.rows[0][0] or 0) + 1

def siguiente_general_enrol():
    r = conectar_db().execute("SELECT MAX(numero_general) FROM paquetes")
    return (r.rows[0][0] or 0) + 1

def ultima_ubicacion_usuario(dpi):
    """Ubicación del último paquete (mayor N°) del usuario: (est, fila, col, pila, num) o None."""
    r = conectar_db().execute("""SELECT estanteria, fila, columna, pila, num_paquete FROM paquetes
                                 WHERE usuario_codigo = ? ORDER BY num_paquete DESC LIMIT 1""", (dpi,))
    return tuple(r.rows[0]) if r.rows else None

def paquete_en_ubicacion(est, fil, col, pil, excluir=None):
    """Devuelve (usuario, num_paquete) del paquete que ocupa esa ubicación, o None."""
    r = conectar_db().execute("""SELECT usuario_codigo, num_paquete FROM paquetes
                                 WHERE estanteria = ? AND fila = ? AND columna = ? AND pila = ?""", (est, fil, col, pil))
    for fila_ in r.rows:
        if excluir and (fila_[0], fila_[1]) == excluir: continue
        return (fila_[0], fila_[1])
    return None

CAMPOS_ENROL_EDITABLES = ["numero_general", "fecha_del", "fecha_al", "num_paquete", "estanteria", "fila", "columna", "pila"]

def _actualizar_paquete_clave(dpi, num, campo, valor):
    """Actualiza un campo del paquete (dpi + N°). Devuelve (ok, mensaje, num_vigente)."""
    if campo not in CAMPOS_ENROL_EDITABLES: return False, "Campo no editable.", num
    client = conectar_db()
    try:
        actual = buscar_por_dpi_y_numero(dpi, num)
        if not actual: return False, "El paquete ya no existe.", num
        valor = valor.strip()
        if campo in ("fecha_del", "fecha_al"):
            v = normalizar_fecha(valor)
            if not v: return False, "Fecha inválida. Usa DD/MM/AAAA o AAAA-MM-DD.", num
            if campo == "fecha_al" and actual.get("fecha_del") and v < actual["fecha_del"]:
                return False, "La fecha AL no puede ser anterior a la fecha DEL.", num
            if campo == "fecha_del" and actual.get("fecha_al") and v > actual["fecha_al"]:
                return False, "La fecha DEL no puede ser posterior a la fecha AL.", num
        elif campo == "numero_general":
            if valor.lower() in ("blanco", "null", ""): v = None
            else:
                v = int(valor)
                r = client.execute("SELECT COUNT(*) FROM paquetes WHERE numero_general = ? AND NOT (usuario_codigo = ? AND num_paquete = ?)", (v, dpi, num))
                if r.rows[0][0] > 0: return False, f"El N° General {v} ya está en uso por otro paquete.", num
        elif campo == "num_paquete":
            v = int(valor)
            if v != num and buscar_por_dpi_y_numero(dpi, v): return False, f"Ya existe el paquete N° {v} para este usuario.", num
        elif campo == "columna":
            v = valor.upper()
            if v not in tuple("ABCDEF"): return False, "La columna debe ser una letra de A a F.", num
        else:
            v = valor
        client.execute(f"UPDATE paquetes SET {campo} = ? WHERE usuario_codigo = ? AND num_paquete = ?", (v, dpi, num))
        nuevo_num = v if campo == "num_paquete" else num
        aviso = None
        if campo in ("estanteria", "fila", "columna", "pila"):
            act = buscar_por_dpi_y_numero(dpi, nuevo_num)
            ocup = paquete_en_ubicacion(act["estanteria"], act["fila"], act["columna"], act["pila"], excluir=(dpi, nuevo_num)) if act else None
            if ocup: aviso = f"⚠️ Esa ubicación también la ocupa el paquete N° {ocup[1]} de {ocup[0]}."
        return True, aviso, nuevo_num
    except ValueError:
        return False, "Ese campo requiere un número entero.", num
    except Exception as e:
        traceback.print_exc()
        return False, f"Error ({type(e).__name__}): {e}", num

def eliminar_paquete(dpi, num):
    """Mueve el paquete a la papelera (copia primero, borra después) y lo registra en la bitácora."""
    client = conectar_db()
    try:
        filas = _filas_evento("SELECT * FROM paquetes WHERE usuario_codigo = ? AND num_paquete = ?", (dpi, num))
        if not filas: return False
        fila = filas[0]
        mover_a_papelera("paquetes", fila, f"Paquete N° {num} · {dpi}")
        client.execute("DELETE FROM paquetes WHERE usuario_codigo = ? AND num_paquete = ?", (dpi, num))
        gen = fila.get("numero_general")
        bitacora("ELIMINAR", "paquete", f"{dpi} N° {num} · Gen {gen if gen is not None else 'en blanco'} → enviado a papelera")
        return True
    except Exception:
        traceback.print_exc()
        return False

def _actualizar_paquete(dpi, num, campo, valor):
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
def _insertar_evento(tipo, anio, num_paq, folio_ini, folio_fin, num_general, est, fil, col, pil, operador):
    client = conectar_db()
    try:
        num_general_val = None if str(num_general).lower() in ("blanco", "null", "") else int(num_general)
        client.execute("""
            INSERT INTO eventos (tipo_evento, anio, num_paquete, folio_inicio, folio_fin, numero_general, estanteria, fila, columna, pila, registrado_por, fecha_registro)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tipo, anio, num_paq, folio_ini, folio_fin, num_general_val, est, fil, col, pil, operador, ahora_gt()[:16]))
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

def _actualizar_evento_id(id_evento, campo, valor):
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
    """Mueve el paquete de evento a la papelera (copia primero, borra después) y lo registra en la bitácora."""
    try:
        fila = buscar_evento_por_id(id_evento)
        if not fila: return False
        mover_a_papelera("eventos", fila, f"{fila['tipo_evento']} {fila['anio']} · Paq {fila['num_paquete']} ({fila['folio_inicio']}-{fila['folio_fin']})")
        conectar_db().execute("DELETE FROM eventos WHERE id = ?", (id_evento,))
        bitacora("ELIMINAR", "evento", f"{fila['tipo_evento']} {fila['anio']} Paq {fila['num_paquete']} ({fila['folio_inicio']}-{fila['folio_fin']}) → enviado a papelera")
        return True
    except Exception:
        traceback.print_exc()
        return False

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
def _dibujar_etiqueta_enrol(pdf, y_inicio, dpi, fecha_del, fecha_al, num, num_gen, ubicacion):
    """Dibuja una etiqueta (ancha + angosta) con su borde superior en y_inicio (mm)."""
    def a_formato_mostrar(fecha):
        if not fecha or fecha == "?": return fecha
        try:
            partes = fecha.split("-")
            if len(partes) == 3: return f"{partes[2]}/{partes[1]}/{partes[0]}"
        except: pass
        return fecha
    del_mostrar, al_mostrar = a_formato_mostrar(fecha_del), a_formato_mostrar(fecha_al)
    num_str, gral = str(num), str(num_gen) if num_gen is not None else "-"
    margen_izq, ancho_disponible = 10, 215.9 - 20
    ancho_ancha, ancho_sep, ancho_angosta = ancho_disponible * 0.62, ancho_disponible * 0.06, ancho_disponible * 0.32
    x_ancha, x_sep, x_angosta, ancho_alto = margen_izq, margen_izq + ancho_ancha, margen_izq + ancho_ancha + ancho_sep, 70
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


def generar_pdf_etiqueta(dpi, fecha_del, fecha_al, num, num_gen, ubicacion):
    try: from fpdf import FPDF
    except ImportError: return None, "No tienes 'fpdf2'."
    try:
        pdf = FPDF(orientation="P", unit="mm", format="Letter"); pdf.add_page(); pdf.set_auto_page_break(auto=False)
        _dibujar_etiqueta_enrol(pdf, 15, dpi, fecha_del, fecha_al, num, num_gen, ubicacion)
        ruta = os.path.join(tempfile.gettempdir(), f"etiquetas_{dpi}_{num}.pdf"); pdf.output(ruta); return ruta, None
    except Exception as e: return None, str(e)

def _dibujar_etiqueta_evento(pdf, y_inicio, tipo, anio, num, folio_ini, folio_fin, num_gen, ubicacion):
    """Dibuja una etiqueta de evento (ancha + angosta) con su borde superior en y_inicio (mm)."""
    num_str, gral = str(num), str(num_gen) if num_gen is not None else "-"
    encabezado = f"{tipo.upper()} - {anio}"
    margen_izq, ancho_disponible = 10, 215.9 - 20
    ancho_ancha, ancho_sep, ancho_angosta = ancho_disponible * 0.62, ancho_disponible * 0.06, ancho_disponible * 0.32
    x_ancha, x_sep, x_angosta, alto = margen_izq, margen_izq + ancho_ancha, margen_izq + ancho_ancha + ancho_sep, 70
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


def generar_pdf_etiqueta_evento(tipo, anio, num, folio_ini, folio_fin, num_gen, ubicacion):
    try: from fpdf import FPDF
    except ImportError: return None, "No tienes 'fpdf2'."
    try:
        pdf = FPDF(orientation="P", unit="mm", format="Letter"); pdf.add_page(); pdf.set_auto_page_break(auto=False)
        _dibujar_etiqueta_evento(pdf, 15, tipo, anio, num, folio_ini, folio_fin, num_gen, ubicacion)
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
    teclado.append([InlineKeyboardButton("🗄️ MAPA DE ESTANTERÍAS", callback_data="mapa_inicio")])
    teclado.append([InlineKeyboardButton("📥 CARGA MASIVA (sin internet)", callback_data="accion_cargar")])
    if rol == "admin": teclado.append([InlineKeyboardButton("🔐 AUTORIZACIÓN", callback_data="menu_autorizacion")])
    if rol == "admin": teclado.append([InlineKeyboardButton("💾 BACKUP BASE DE DATOS", callback_data="accion_backup")])
    if rol == "admin": teclado.append([InlineKeyboardButton("🗑️ PAPELERA", callback_data="pap_lista"), InlineKeyboardButton("📜 BITÁCORA", callback_data="bit_menu")])
    teclado.append([InlineKeyboardButton("👤 MI INFORMACIÓN", callback_data="menu_info")])
    return InlineKeyboardMarkup(teclado)
    
async def editar_texto(query, texto, reply_markup=None, parse_mode=None):
    """Edita el mensaje del botón. Si es una foto o un archivo (no tiene texto editable), le quita los botones
    y envía el texto como mensaje nuevo."""
    msg = query.message
    if msg is not None and (msg.photo or msg.document or msg.caption):
        try: await msg.edit_reply_markup(reply_markup=None)
        except Exception: pass
        await msg.reply_text(texto, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    try:
        await query.edit_message_text(texto, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        if "no text in the message" in str(e).lower() and msg is not None:
            try: await msg.edit_reply_markup(reply_markup=None)
            except Exception: pass
            await msg.reply_text(texto, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            raise

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    mensaje = f"👋 *Hola, {usuario['nombre']}*\nTu rol es: `{usuario['rol']}`\nÁrea: `{usuario.get('area', 'enrolamiento').capitalize()}`\n\nSelecciona una categoría:"
    teclado = obtener_menu_principal(usuario)
    if update.callback_query: await editar_texto(update.callback_query, mensaje, reply_markup=teclado, parse_mode="Markdown")
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
            [InlineKeyboardButton("🏷️ Etiquetas en lote", callback_data="lote_enrol")],
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
            [InlineKeyboardButton("🏷️ Etiquetas en lote", callback_data="lote_ev")],
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
    else:
        await update.message.reply_text(formatear_mensaje(res), parse_mode="Markdown")
        await enviar_mapas_resultado(update.message, "E", [res])

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
    else:
        await update.message.reply_text(formatear_mensaje(resultado), parse_mode="Markdown")
        await enviar_mapas_resultado(update.message, "E", [resultado])

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
    await enviar_mapas_resultado(update.message, "E", resultados)

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
    await enviar_mapas_resultado(update.message, "E", resultados)

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
    await update.message.reply_text("2️⃣ Fecha DEL (AAAA-MM-DD o DD/MM/AAAA):")
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
    d = context.user_data
    if d.get("fecha_del") and fecha < d["fecha_del"]:
        await update.message.reply_text("⚠️ La fecha AL no puede ser anterior a la fecha DEL. Escribe la fecha AL de nuevo:"); return REG_AL
    d["fecha_al"] = fecha
    sug = siguiente_num_paquete_usuario(d["dpi"])
    d["sug_num"] = sug
    await update.message.reply_text(f"Usuario {d['dpi']}\n\nEl siguiente paquete sería el N° {sug}.\n¿Es correcto?",
                                    reply_markup=teclado_sugerencia("rsugnum", sug))
    return REG_NUM

async def _reg_pedir_ubicacion(update, context):
    d = context.user_data
    ref, ref_txt = None, ""
    ult = ultima_ubicacion_usuario(d["dpi"])
    if ult:
        est, fil, col, pil, num_ult = ult
        ref = (est, fil, col, pil)
        ref_txt = f"El último paquete de este usuario (N° {num_ult}) está en E{est}-F{fil}-{col}-P{pil}."
    else:
        g = ultima_ubicacion_general()
        if g:
            ref = g[:4]
            ref_txt = f"Este usuario aún no tiene paquetes. El último paquete registrado (N° {g[4]} de {g[5]}) está en E{g[0]}-F{g[1]}-{g[2]}-P{g[3]}."
    res = sugerir_ubicacion("paquetes", ref, paquete_en_ubicacion) if ref else None
    if not res:
        await _responder(update, f"Paquete N° {d['num_paquete']}.\n\n5️⃣ Estantería:")
        return REG_EST
    d["sug_ubic"], d["alt5"] = res["sug"], res["alt5"]
    texto = f"Paquete N° {d['num_paquete']}.\n\n{ref_txt}\nUbicación sugerida: {texto_ubicacion(res['sug'])}"
    if res["saltos"]: texto += f"\n(Se saltaron {res['saltos']} ubicación(es) ya ocupada(s).)"
    texto += "\n¿Usar esta ubicación?"
    extra = [InlineKeyboardButton(f"⬆️ Usar nivel {NIVELES_NORMALES + 1} (excepción)", callback_data="rsugubi_n5")] if res["alt5"] else None
    await _responder(update, texto, teclado_sugerencia("rsugubi", texto_ubicacion(res["sug"]), extra))
    return REG_UBIC

async def _reg_pedir_general(update, context):
    sug = siguiente_general_enrol()
    context.user_data["sug_gen"] = sug
    await _responder(update, f"El siguiente N° General correlativo sería {sug}.\n¿Es correcto?",
                     teclado_sugerencia("rsuggen", sug, extra=[InlineKeyboardButton("⬜ Dejar en blanco", callback_data="rsuggen_blanco")]))
    return REG_GENERAL

async def reg_num_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == "rsugnum_si":
        context.user_data["num_paquete"] = context.user_data["sug_num"]
        return await _reg_pedir_ubicacion(update, context)
    await query.edit_message_text("4️⃣ Escribe el número de paquete:")
    return REG_NUM

async def reg_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: num = int(update.message.text.strip())
    except: await update.message.reply_text("⚠️ Debe ser número:"); return REG_NUM
    d = context.user_data
    if buscar_por_dpi_y_numero(d["dpi"], num):
        await update.message.reply_text(f"⚠️ El usuario {d['dpi']} ya tiene el paquete N° {num}. Escribe otro número:"); return REG_NUM
    d["num_paquete"] = num
    return await _reg_pedir_ubicacion(update, context)

async def reg_ubic_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    d = context.user_data
    if query.data in ("rsugubi_si", "rsugubi_n5"):
        d["estanteria"], d["fila"], d["columna"], d["pila"] = d["alt5"] if query.data == "rsugubi_n5" else d["sug_ubic"]
        return await _reg_pedir_general(update, context)
    await query.edit_message_text("5️⃣ Estantería:")
    return REG_EST

async def reg_est(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["estanteria"] = update.message.text.strip()
    await update.message.reply_text("6️⃣ Fila:")
    return REG_FIL

async def reg_fil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["fila"] = update.message.text.strip()
    await update.message.reply_text("7️⃣ Columna (A a D; hasta F en las estanterías grandes):")
    return REG_COL

async def reg_col(update: Update, context: ContextTypes.DEFAULT_TYPE):
    col = update.message.text.strip().upper()
    if col not in list("ABCDEF"): await update.message.reply_text("⚠️ Debe ser una letra de A a F:"); return REG_COL
    context.user_data["columna"] = col
    await update.message.reply_text("8️⃣ Pila/Nivel:")
    return REG_PIL

async def reg_pil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pila"] = update.message.text.strip()
    return await _reg_pedir_general(update, context)

async def _reg_guardar(update, context, general):
    datos = context.user_data
    nuevo_gen, error = insertar_paquete(datos["dpi"], datos["num_paquete"], datos["fecha_del"], datos["fecha_al"], datos["estanteria"], datos["fila"], datos["columna"], datos["pila"], general)
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if error:
        await _responder(update, f"❌ {error}\n\nReintenta desde el menú o con /registrar", btn_volver)
    else:
        gen_texto = str(nuevo_gen) if nuevo_gen is not None else "En blanco"
        ubic = f"E{datos['estanteria']}-F{datos['fila']}-{datos['columna']}-P{datos['pila']}"
        await _responder(update, f"✅ Registrado\n📦 N°: {datos['num_paquete']}\n🔢 Gen: {gen_texto}\n📍 {ubic}\n👤 {datos['dpi']}",
                         teclado_post_registro_enrol(datos["dpi"], datos["num_paquete"]))
    context.user_data.clear()
    return ConversationHandler.END

async def reg_general_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == "rsuggen_otro":
        await query.edit_message_text("Escribe el Número General (número, 'auto' o 'blanco'):")
        return REG_GENERAL
    general = str(context.user_data["sug_gen"]) if query.data == "rsuggen_si" else "blanco"
    return await _reg_guardar(update, context, general)

async def reg_general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _reg_guardar(update, context, update.message.text.strip())

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
    else: await update.message.reply_text(f"✅ Registrado N° {num}", reply_markup=teclado_post_registro_enrol(dpi, num))
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
    return await _ev_pedir_ubicacion(update, context)

async def flujo_evento_general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    general = update.message.text.strip()
    if general.lower() not in ("blanco", "null", ""):
        try: n = int(general)
        except: await update.message.reply_text("⚠️ Escribe un número entero o 'blanco':"); return EV_GENERAL
        if buscar_evento_por_general(n):
            await update.message.reply_text(f"⚠️ El N° General {n} ya está en uso. Escribe otro (o 'blanco'):"); return EV_GENERAL
    context.user_data["ev_general"] = general
    return await _ev_pedir_ubicacion(update, context)

async def flujo_evento_est(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ev_est"] = update.message.text.strip()
    await update.message.reply_text("Ingresa la Fila:")
    return EV_FIL

async def flujo_evento_fil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ev_fil"] = update.message.text.strip()
    await update.message.reply_text("Ingresa la Columna (A a D; hasta F en las estanterías grandes):")
    return EV_COL

async def flujo_evento_col(update: Update, context: ContextTypes.DEFAULT_TYPE):
    col = update.message.text.strip().upper()
    if col not in list("ABCDEF"): await update.message.reply_text("⚠️ Debe ser una letra de A a F:"); return EV_COL
    context.user_data["ev_col"] = col
    await update.message.reply_text("Ingresa la Pila/Nivel:")
    return EV_PIL

async def flujo_evento_pil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ev_pil"] = update.message.text.strip()
    return await _ev_guardar(update, context)

async def _ev_guardar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    datos = context.user_data
    mensaje = update.callback_query.message if update.callback_query else update.message
    usuario = await verificar_acceso(update)
    if not usuario: return ConversationHandler.END
    pil = datos["ev_pil"]
    exito, error = insertar_evento(datos["ev_tipo"], datos["ev_anio"], datos["ev_num"], datos["ev_folio_ini"], datos["ev_folio_fin"], datos["ev_general"], datos["ev_est"], datos["ev_fil"], datos["ev_col"], pil, usuario["dpi_asignado"] or usuario["nombre"])
    if exito:
        gen_txt = f"`{datos['ev_general']}`" if datos['ev_general'].lower() not in ('blanco', 'null', '') else "_blanco_"
        coincide = buscar_evento_por_tap(datos["ev_tipo"], datos["ev_anio"], datos["ev_num"])
        nuevo_id = coincide[0]["id"] if coincide else 0
        await mensaje.reply_text(f"✅ *Evento Registrado*\n\nTipo: {datos['ev_tipo']}\nAño: {datos['ev_anio']}\nPaquete: {datos['ev_num']}\nGen: {gen_txt}\nEventos: {datos['ev_folio_ini']} al {datos['ev_folio_fin']}\nUbicación: E{datos['ev_est']}-F{datos['ev_fil']}-{datos['ev_col']}-P{pil}", parse_mode="Markdown", reply_markup=teclado_post_registro_evento(nuevo_id, datos["ev_tipo"], datos["ev_anio"]))
    else: await mensaje.reply_text(f"❌ Error: {error}")
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
        await enviar_mapas_resultado(update.message, "V", resultados)
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
    if res:
        await update.message.reply_text(texto_evento(res), reply_markup=btn_volver)
        await enviar_mapas_resultado(update.message, "V", [res])
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
    if accion in ("editar", "eliminar"):
        await enviar_mapas_resultado(mensaje, "V", [paquete])
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
        await enviar_mapas_resultado(update.message, "V", resultados)
    context.user_data.clear()
    return ConversationHandler.END

# --- ENROLAMIENTO: buscar por botones y luego editar / etiqueta / eliminar ---
TITULOS_ACCION_ENROL = {"editar": "✏️ Editar paquete", "etiqueta": "🏷️ Generar etiqueta PDF", "eliminar": "🗑️ Eliminar paquete"}
CAMPOS_ENROL_BOTONES = [("N° General", "numero_general"), ("Fecha DEL", "fecha_del"), ("Fecha AL", "fecha_al"),
                        ("N° de paquete", "num_paquete"), ("Estantería", "estanteria"), ("Fila", "fila"),
                        ("Columna", "columna"), ("Pila", "pila")]

def fecha_mostrar(f):
    try:
        a_, m_, d_ = str(f).split("-"); return f"{d_}/{m_}/{a_}"
    except Exception:
        return str(f)

def texto_paquete_enrol(r):
    gen = r.get("numero_general")
    return (f"📦 Paquete N° {r.get('num_paquete')}\n👤 {r.get('usuario_codigo')}\n🔢 Gen: {gen if gen is not None else 'En blanco'}\n"
            f"📅 {fecha_mostrar(r.get('fecha_del'))} al {fecha_mostrar(r.get('fecha_al'))}\n📍 {r.get('ubicacion_armada')}")

def resumen_paquete_enrol(r):
    gen = r.get("numero_general")
    return f"{r['usuario_codigo']} · N° {r['num_paquete']} · Gen {gen if gen is not None else '—'} · {fecha_mostrar(r['fecha_del'])}-{fecha_mostrar(r['fecha_al'])}"

def teclado_campos_enrol():
    filas = []
    for i in range(0, len(CAMPOS_ENROL_BOTONES), 2):
        filas.append([InlineKeyboardButton(t, callback_data=f"epcampo_{c}") for t, c in CAMPOS_ENROL_BOTONES[i:i + 2]])
    filas.append([InlineKeyboardButton("✅ Terminar", callback_data="menu_inicio")])
    return InlineKeyboardMarkup(filas)

async def enrol_sel_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"] if query.data != "enrol_etiqueta" else None)
    if not usuario: return ConversationHandler.END
    accion = query.data.replace("enrol_", "")
    context.user_data.clear()
    context.user_data["ep_accion"] = accion
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Por DPI y N° de paquete", callback_data="epmodo_dpinum")],
        [InlineKeyboardButton("🔢 Por N° General", callback_data="epmodo_general")],
        [InlineKeyboardButton("📅 Por DPI y fecha", callback_data="epmodo_fecha")],
        [InlineKeyboardButton("👥 Paquetes de un usuario", callback_data="epmodo_usuario")],
        [InlineKeyboardButton("🔙 Cancelar", callback_data="menu_inicio")]
    ])
    await query.edit_message_text(f"{TITULOS_ACCION_ENROL[accion]}\n\n¿Cómo quieres buscar el paquete?", reply_markup=teclado)
    return EP_MODO

EP_PROMPTS = {
    "dpinum":  "Escribe: <DPI> <N° de paquete>\nEjemplo: PCOTTON320 1",
    "general": "Escribe el N° General\nEjemplo: 41",
    "fecha":   "Escribe: <DPI> <fecha>\nEjemplo: PCOTTON320 15/03/2024",
    "usuario": "Escribe el DPI / código del usuario\nEjemplo: PCOTTON320",
}

async def enrol_sel_modo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    modo = query.data.replace("epmodo_", "")
    context.user_data["ep_modo"] = modo
    await query.edit_message_text(EP_PROMPTS[modo] + "\n\n(/cancelar para salir)")
    return EP_VALOR

async def _ep_continuar(mensaje, context, paquete, usuario_rol=None):
    d = context.user_data
    d["ep_dpi"], d["ep_num"] = paquete["usuario_codigo"], paquete["num_paquete"]
    accion = d.get("ep_accion", "editar")
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if accion == "etiqueta":
        await _enviar_etiqueta_paquete(mensaje, paquete)
        await mensaje.reply_text("¿Algo más?", reply_markup=btn_volver)
        d.clear(); return ConversationHandler.END
    if accion in ("editar", "eliminar"):
        await enviar_mapas_resultado(mensaje, "E", [paquete])
    if accion == "eliminar":
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Sí, eliminar", callback_data="epdel_si")],
            [InlineKeyboardButton("🔙 Cancelar", callback_data="menu_inicio")]
        ])
        await mensaje.reply_text(f"⚠️ ¿Eliminar este paquete?\n\n{texto_paquete_enrol(paquete)}\n\nEsta acción no se puede deshacer.", reply_markup=teclado)
        return EP_CONFIRMAR
    await mensaje.reply_text(f"✅ Paquete seleccionado:\n{texto_paquete_enrol(paquete)}\n\n¿Qué campo deseas modificar?", reply_markup=teclado_campos_enrol())
    return EP_CAMPO

async def _enviar_etiqueta_paquete(mensaje, res):
    dpi, num_real = res.get("usuario_codigo"), res.get("num_paquete")
    ruta, error = generar_pdf_etiqueta(dpi, res.get("fecha_del"), res.get("fecha_al"), num_real, res.get("numero_general"), res.get("ubicacion_armada"))
    if not ruta:
        await mensaje.reply_text(f"❌ Error al generar PDF: {error}"); return
    with open(ruta, "rb") as f:
        await mensaje.reply_document(document=f, filename=f"etiqueta_{dpi}_{num_real}_{datetime.now().strftime('%H%M%S')}.pdf",
                                     caption=f"🏷️ Etiqueta del Paquete N° {num_real}\n👤 {dpi}\n📅 {res.get('fecha_del')} al {res.get('fecha_al')}\n📍 {res.get('ubicacion_armada')}")
    os.remove(ruta)

async def enrol_sel_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return ConversationHandler.END
    texto = update.message.text.strip()
    partes = texto.split()
    modo = context.user_data.get("ep_modo", "dpinum")
    resultados, error = [], None
    try:
        if modo == "dpinum":
            if len(partes) != 2: error = "Escribe: <DPI> <N° de paquete>"
            else:
                r = buscar_por_dpi_y_numero(partes[0], int(partes[1])); resultados = [r] if r else []
        elif modo == "general":
            r = buscar_por_numero_general(int(texto)); resultados = [r] if r else []
        elif modo == "fecha":
            f = normalizar_fecha(partes[1]) if len(partes) == 2 else None
            if not f: error = "Escribe: <DPI> <fecha DD/MM/AAAA>"
            else: resultados = buscar_por_dpi_y_fecha(partes[0], f)
        elif modo == "usuario":
            if len(partes) != 1: error = "Escribe solo el DPI / código del usuario"
            else: resultados = buscar_por_usuario(partes[0])
    except ValueError:
        error = "El número no es válido."
    if error:
        await update.message.reply_text(f"⚠️ {error}\nInténtalo de nuevo o /cancelar:"); return EP_VALOR
    accion = context.user_data.get("ep_accion", "editar")
    if usuario["rol"] == "operador2" and accion in ("editar", "eliminar"):
        resultados = [r for r in resultados if r.get("usuario_codigo") == usuario["dpi_asignado"]]
    if not resultados:
        await update.message.reply_text("❌ No se encontró ningún paquete con esos datos. Intenta otro o /cancelar:"); return EP_VALOR
    if len(resultados) == 1:
        return await _ep_continuar(update.message, context, resultados[0])
    resultados = resultados[:40]
    botones = [[InlineKeyboardButton(resumen_paquete_enrol(r), callback_data=f"epsel_{r['usuario_codigo']}|{r['num_paquete']}")] for r in resultados]
    botones.append([InlineKeyboardButton("🔙 Cancelar", callback_data="menu_inicio")])
    await update.message.reply_text(f"Hay {len(resultados)} paquetes que coinciden. ¿Cuál eliges?", reply_markup=InlineKeyboardMarkup(botones))
    return EP_ELEGIR

async def enrol_sel_elegir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    dpi, num = query.data.replace("epsel_", "").rsplit("|", 1)
    paquete = buscar_por_dpi_y_numero(dpi, int(num))
    if not paquete:
        await query.edit_message_text("❌ Ese paquete ya no existe."); return ConversationHandler.END
    await query.edit_message_reply_markup(reply_markup=None)
    return await _ep_continuar(query.message, context, paquete)

EP_AYUDA_CAMPO = {
    "fecha_del": "Escribe la nueva fecha DEL (DD/MM/AAAA o AAAA-MM-DD):",
    "fecha_al": "Escribe la nueva fecha AL (DD/MM/AAAA o AAAA-MM-DD):",
    "columna": "Escribe la nueva columna (A, B, C o D):",
    "numero_general": "Escribe el nuevo N° General (o 'blanco' para dejarlo vacío):",
}

async def enrol_sel_campo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    campo = query.data.replace("epcampo_", "")
    context.user_data["ep_campo"] = campo
    nombre = dict((c, t) for t, c in CAMPOS_ENROL_BOTONES).get(campo, campo)
    await query.edit_message_text(f"Modificar: {nombre}\n\n" + EP_AYUDA_CAMPO.get(campo, f"Escribe el nuevo valor de {nombre}:"))
    return EP_NUEVO

async def enrol_sel_nuevo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return ConversationHandler.END
    d = context.user_data
    dpi, num, campo = d.get("ep_dpi"), d.get("ep_num"), d.get("ep_campo")
    if usuario["rol"] == "operador2" and dpi != usuario["dpi_asignado"]:
        await update.message.reply_text("🚫 Solo puedes editar paquetes de tu propio DPI."); return ConversationHandler.END
    ok, msg, nuevo_num = actualizar_paquete_clave(dpi, num, campo, update.message.text)
    if ok:
        d["ep_num"] = nuevo_num
        actual = buscar_por_dpi_y_numero(dpi, nuevo_num)
        texto = "✅ Campo actualizado."
        if msg: texto += f"\n{msg}"
        if actual: texto += f"\n\n{texto_paquete_enrol(actual)}"
        await update.message.reply_text(texto + "\n\n¿Quieres modificar otro campo?", reply_markup=teclado_campos_enrol())
    else:
        await update.message.reply_text(f"❌ {msg}\n\nElige el campo de nuevo:", reply_markup=teclado_campos_enrol())
    return EP_CAMPO

async def enrol_sel_eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return ConversationHandler.END
    d = context.user_data
    dpi, num = d.get("ep_dpi"), d.get("ep_num")
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if usuario["rol"] == "operador2" and dpi != usuario["dpi_asignado"]:
        await query.edit_message_text("🚫 Solo puedes eliminar paquetes de tu propio DPI.", reply_markup=btn_volver); return ConversationHandler.END
    if dpi is not None and buscar_por_dpi_y_numero(dpi, num) and eliminar_paquete(dpi, num):
        await query.edit_message_text(f"✅ Paquete N° {num} de {dpi} eliminado.", reply_markup=btn_volver)
    else:
        await query.edit_message_text("❌ No se pudo eliminar (¿ya no existe?).", reply_markup=btn_volver)
    d.clear()
    return ConversationHandler.END

# --- BACKUP DE LA BASE DE DATOS (solo administradores) ---
def generar_backup():
    """Copia TODAS las tablas de Turso a un .zip con: una base SQLite restaurable (.db) y un CSV por tabla.
    Devuelve (ruta_zip, resumen {tabla: filas}, carpeta_temporal)."""
    client = conectar_db()
    sello = datetime.now().strftime("%Y%m%d_%H%M%S")
    carpeta = tempfile.mkdtemp(prefix="backup_")
    ruta_db = os.path.join(carpeta, f"atestados_{sello}.db")
    ruta_zip = os.path.join(carpeta, f"backup_atestados_{sello}.zip")
    resumen = {}
    tablas = [(r[0], r[1]) for r in client.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY name").rows]
    indices = [r[0] for r in client.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").rows]
    local = sqlite3.connect(ruta_db)
    try:
        with zipfile.ZipFile(ruta_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for nombre, sql_crear in tablas:
                local.execute(sql_crear)
                res = client.execute(f'SELECT * FROM "{nombre}"')
                cols = _nombres_columnas(res)
                filas = [tuple(f) for f in res.rows]
                if filas:
                    columnas_sql = ", ".join(f'"{c}"' for c in cols)
                    marcas = ", ".join("?" for _ in cols)
                    local.executemany(f'INSERT INTO "{nombre}" ({columnas_sql}) VALUES ({marcas})', filas)
                resumen[nombre] = len(filas)
                buf = io.StringIO()
                escritor = csv.writer(buf)
                escritor.writerow(cols)
                escritor.writerows(filas)
                z.writestr(f"csv/{nombre}.csv", "\ufeff" + buf.getvalue())  # BOM: Excel lee bien los acentos
            for sql_indice in indices:
                try: local.execute(sql_indice)
                except Exception: pass
            local.commit()
            local.close()
            z.write(ruta_db, f"atestados_{sello}.db")
    finally:
        try: local.close()
        except Exception: pass
    return ruta_zip, resumen, carpeta

async def backup_bd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin"])
    if not usuario: return
    if update.callback_query: await update.callback_query.answer()
    mensaje = update.callback_query.message if update.callback_query else update.message
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    aviso = await mensaje.reply_text("⏳ Generando backup de la base de datos...")
    carpeta = None
    try:
        ruta_zip, resumen, carpeta = await asyncio.to_thread(generar_backup)
        detalle = "\n".join(f"• {t}: {n} registro(s)" for t, n in resumen.items())
        with open(ruta_zip, "rb") as f:
            await mensaje.reply_document(
                document=f, filename=os.path.basename(ruta_zip),
                caption=f"💾 Backup completo ({fecha_corta(ahora_gt())})\n\n{detalle}\n\n"
                        "Incluye la base SQLite (.db) y un CSV por tabla.\n"
                        "⚠️ Contiene datos personales: guárdalo en un lugar seguro y no lo reenvíes.")
        bitacora("BACKUP", "base de datos", ", ".join(f"{t}: {n}" for t, n in resumen.items()))
        await aviso.edit_text("✅ Backup generado.", reply_markup=btn_volver)
    except Exception as e:
        traceback.print_exc()
        await aviso.edit_text(f"❌ No se pudo generar el backup ({type(e).__name__}): {e}", reply_markup=btn_volver)
    finally:
        if carpeta: shutil.rmtree(carpeta, ignore_errors=True)

# --- Funciones con bitácora (envuelven a las originales) ---
def insertar_paquete(dpi, num, f_del, f_al, est, fil, col, pil, general=None):
    nuevo_gen, error = _insertar_paquete(dpi, num, f_del, f_al, est, fil, col, pil, general)
    if not error:
        gen = nuevo_gen if nuevo_gen is not None else "en blanco"
        bitacora("REGISTRAR", "paquete", f"{dpi} N° {num} · Gen {gen} · {fecha_mostrar(f_del)} al {fecha_mostrar(f_al)} · E{est}-F{fil}-{col}-P{pil}")
    return nuevo_gen, error

def actualizar_paquete_clave(dpi, num, campo, valor):
    antes = buscar_por_dpi_y_numero(dpi, num)
    ok, msg, nuevo_num = _actualizar_paquete_clave(dpi, num, campo, valor)
    if ok:
        despues = buscar_por_dpi_y_numero(dpi, nuevo_num)
        bitacora("EDITAR", "paquete", f"{dpi} N° {num}: {campo} {antes.get(campo) if antes else '?'} → {despues.get(campo) if despues else valor}")
    return ok, msg, nuevo_num

def actualizar_paquete(dpi, num, campo, valor):
    antes = buscar_por_dpi_y_numero(dpi, num)
    ok = _actualizar_paquete(dpi, num, campo, valor)
    if ok:
        bitacora("EDITAR", "paquete", f"{dpi} N° {num}: {campo} {antes.get(campo) if antes else '?'} → {valor}")
    return ok

def insertar_evento(tipo, anio, num_paq, folio_ini, folio_fin, num_general, est, fil, col, pil, operador):
    ok, error = _insertar_evento(tipo, anio, num_paq, folio_ini, folio_fin, num_general, est, fil, col, pil, operador)
    if ok:
        bitacora("REGISTRAR", "evento", f"{tipo} {anio} Paq {num_paq} ({folio_ini}-{folio_fin}) · Gen {num_general} · E{est}-F{fil}-{col}-P{pil}")
    return ok, error

def actualizar_evento_id(id_evento, campo, valor):
    antes = buscar_evento_por_id(id_evento)
    ok, error = _actualizar_evento_id(id_evento, campo, valor)
    if ok:
        despues = buscar_evento_por_id(id_evento)
        ref = f"{antes['tipo_evento']} {antes['anio']} Paq {antes['num_paquete']}" if antes else f"id {id_evento}"
        bitacora("EDITAR", "evento", f"{ref}: {campo} {antes.get(campo) if antes else '?'} → {despues.get(campo) if despues else valor}")
    return ok, error

# --- Actor: quién ejecuta cada acción ---
async def fijar_actor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u: return
    cache = _CACHE_ACTOR.get(u.id)
    if cache and time.time() - cache[1] < 120:
        datos = cache[0]
    else:
        usr = usuario_autorizado(u.id)
        datos = (u.id, usr["nombre"] if usr else u.full_name, usr["rol"] if usr else "sin_acceso")
        _CACHE_ACTOR[u.id] = (datos, time.time())
    ACTOR_ACTUAL.set(datos)

def fecha_corta(f):
    """'2026-11-12 14:30:05' -> '12/11 14:30'"""
    try:
        dia, hora = str(f).split(" ")
        a_, m_, d_ = dia.split("-")
        return f"{d_}/{m_} {hora[:5]}"
    except Exception:
        return str(f)

# --- PAPELERA (solo administradores) ---
def texto_papelera(tabla, d):
    gen = d.get("numero_general")
    gen_txt = gen if gen is not None else "En blanco"
    if tabla == "paquetes":
        return (f"📦 Paquete N° {d.get('num_paquete')}\n👤 {d.get('usuario_codigo')}\n🔢 Gen: {gen_txt}\n"
                f"📅 {fecha_mostrar(d.get('fecha_del'))} al {fecha_mostrar(d.get('fecha_al'))}\n"
                f"📍 E{d.get('estanteria')}-F{d.get('fila')}-{d.get('columna')}-P{d.get('pila')}")
    return (f"📖 {d.get('tipo_evento')} {d.get('anio')} · Paquete N° {d.get('num_paquete')}\n"
            f"Eventos: {d.get('folio_inicio')} al {d.get('folio_fin')}\n🔢 Gen: {gen_txt}\n"
            f"📍 E{d.get('estanteria')}-F{d.get('fila')}-{d.get('columna')}-P{d.get('pila')}")

def restaurar_de_papelera(id_pap):
    """Devuelve (ok, mensaje). Reinserta el registro si no choca con otro existente."""
    client = conectar_db()
    r = client.execute("SELECT tabla, datos, resumen FROM papelera WHERE id = ? AND restaurado = 0", (id_pap,))
    if not r.rows: return False, "Ese elemento ya no está en la papelera."
    tabla, datos_json, resumen = r.rows[0][0], r.rows[0][1], r.rows[0][2]
    if tabla not in ("paquetes", "eventos"): return False, "Tabla no soportada para restaurar."
    d = json.loads(datos_json)
    aviso = ""
    if tabla == "paquetes":
        existe = client.execute("SELECT COUNT(*) FROM paquetes WHERE usuario_codigo = ? AND num_paquete = ?", (d.get("usuario_codigo"), d.get("num_paquete")))
        if existe.rows[0][0] > 0:
            return False, f"No se puede restaurar: ya existe el paquete N° {d.get('num_paquete')} del usuario {d.get('usuario_codigo')}."
    else:
        existe = client.execute("SELECT COUNT(*) FROM eventos WHERE tipo_evento = ? AND anio = ? AND num_paquete = ?", (d.get("tipo_evento"), d.get("anio"), d.get("num_paquete")))
        if existe.rows[0][0] > 0:
            return False, f"No se puede restaurar: ya existe el paquete {d.get('num_paquete')} de {d.get('tipo_evento')} {d.get('anio')}."
    if d.get("numero_general") is not None:
        en_uso = client.execute(f"SELECT COUNT(*) FROM {tabla} WHERE numero_general = ?", (d["numero_general"],))
        if en_uso.rows[0][0] > 0:
            aviso = f"\n⚠️ El N° General {d['numero_general']} ya lo usa otro paquete; se restauró con el N° General en blanco."
            d["numero_general"] = None
    columnas_tabla = [c[1] for c in client.execute(f"PRAGMA table_info({tabla})").rows]
    columnas = [c for c in d.keys() if c in columnas_tabla and c != "id"]
    marcas = ", ".join("?" for _ in columnas)
    client.execute(f"INSERT INTO {tabla} ({', '.join(columnas)}) VALUES ({marcas})", tuple(d[c] for c in columnas))
    client.execute("UPDATE papelera SET restaurado = 1 WHERE id = ?", (id_pap,))
    bitacora("RESTAURAR", "paquete" if tabla == "paquetes" else "evento", f"{resumen} → restaurado desde la papelera")
    return True, f"Restaurado: {resumen}{aviso}"

async def papelera_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin"])
    if not usuario: return
    query = update.callback_query; await query.answer()
    accion = query.data
    client = conectar_db()
    btn_papelera = InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Ver papelera", callback_data="pap_lista")],
                                         [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if accion == "pap_lista":
        total = client.execute("SELECT COUNT(*) FROM papelera WHERE restaurado = 0").rows[0][0]
        filas = client.execute("SELECT id, resumen, fecha FROM papelera WHERE restaurado = 0 ORDER BY id DESC LIMIT 15").rows
        botones = [[InlineKeyboardButton(f"{fecha_corta(f[2])} · {f[1]}"[:60], callback_data=f"pap_ver_{f[0]}")] for f in filas]
        if filas: botones.append([InlineKeyboardButton("🧹 Vaciar papelera", callback_data="pap_vaciar")])
        botones.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")])
        if filas:
            texto = f"🗑️ PAPELERA ({total} elemento(s))\n\nToca uno para ver el detalle, restaurarlo o eliminarlo definitivamente."
            if total > 15: texto += "\nSe muestran los 15 más recientes."
        else:
            texto = "🗑️ La papelera está vacía."
        await query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(botones))
    elif accion.startswith("pap_ver_"):
        id_pap = int(accion.replace("pap_ver_", ""))
        r = client.execute("SELECT tabla, datos, eliminado_por, fecha FROM papelera WHERE id = ? AND restaurado = 0", (id_pap,))
        if not r.rows:
            await query.edit_message_text("Ese elemento ya no está en la papelera.", reply_markup=btn_papelera); return
        tabla, datos_json, quien, cuando = r.rows[0]
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("♻️ Restaurar", callback_data=f"pap_rest_{id_pap}")],
            [InlineKeyboardButton("❌ Eliminar definitivamente", callback_data=f"pap_borr_{id_pap}")],
            [InlineKeyboardButton("🔙 Volver a la papelera", callback_data="pap_lista")]])
        await query.edit_message_text(f"{texto_papelera(tabla, json.loads(datos_json))}\n\n🗑️ Eliminado por {quien} el {fecha_corta(cuando)}", reply_markup=teclado)
    elif accion.startswith("pap_rest_"):
        try: ok, msg = restaurar_de_papelera(int(accion.replace("pap_rest_", "")))
        except Exception as e:
            traceback.print_exc(); ok, msg = False, f"Error ({type(e).__name__}): {e}"
        await query.edit_message_text(("✅ " if ok else "❌ ") + msg, reply_markup=btn_papelera)
    elif accion.startswith("pap_borrsi_"):
        id_pap = int(accion.replace("pap_borrsi_", ""))
        r = client.execute("SELECT resumen FROM papelera WHERE id = ? AND restaurado = 0", (id_pap,))
        if r.rows:
            client.execute("DELETE FROM papelera WHERE id = ?", (id_pap,))
            bitacora("ELIMINAR DEFINITIVO", "papelera", r.rows[0][0])
            await query.edit_message_text(f"✅ Eliminado definitivamente: {r.rows[0][0]}", reply_markup=btn_papelera)
        else:
            await query.edit_message_text("Ese elemento ya no está en la papelera.", reply_markup=btn_papelera)
    elif accion.startswith("pap_borr_"):
        id_pap = int(accion.replace("pap_borr_", ""))
        teclado = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Sí, eliminar para siempre", callback_data=f"pap_borrsi_{id_pap}")],
                                        [InlineKeyboardButton("🔙 Cancelar", callback_data=f"pap_ver_{id_pap}")]])
        await query.edit_message_text("⚠️ Esto elimina el registro definitivamente y ya no se podrá restaurar. ¿Continuar?", reply_markup=teclado)
    elif accion == "pap_vaciar":
        teclado = InlineKeyboardMarkup([[InlineKeyboardButton("🧹 Sí, vaciar la papelera", callback_data="pap_vaciarsi")],
                                        [InlineKeyboardButton("🔙 Cancelar", callback_data="pap_lista")]])
        await query.edit_message_text("⚠️ Se eliminarán definitivamente TODOS los elementos de la papelera. ¿Continuar?", reply_markup=teclado)
    elif accion == "pap_vaciarsi":
        total = client.execute("SELECT COUNT(*) FROM papelera WHERE restaurado = 0").rows[0][0]
        client.execute("DELETE FROM papelera")
        bitacora("VACIAR PAPELERA", "papelera", f"{total} elemento(s) eliminados definitivamente")
        await query.edit_message_text(f"✅ Papelera vaciada ({total} elemento(s)).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]]))

# --- BITÁCORA (solo administradores) ---
def _linea_bitacora(f):
    # f = (fecha, usuario, rol, accion, entidad, detalle)
    return f"🕒 {fecha_corta(f[0])} · {f[1]}\n   {f[3]} {f[4]}: {f[5]}"

def _bloques_texto(titulo, lineas, limite=3800):
    bloques, actual = [], titulo + "\n\n"
    for l in lineas:
        if len(actual) + len(l) + 2 > limite:
            bloques.append(actual); actual = ""
        actual += l + "\n\n"
    bloques.append(actual)
    return bloques

SQL_ULTIMOS_BITACORA = "SELECT fecha, usuario, rol, accion, entidad, detalle FROM bitacora ORDER BY id DESC LIMIT 15"

async def bitacora_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin"])
    if not usuario: return
    query = update.callback_query; await query.answer()
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("📜 Bitácora", callback_data="bit_menu")],
                                       [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if query.data == "bit_menu":
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("🕒 Últimos 15 movimientos", callback_data="bit_ultimos")],
            [InlineKeyboardButton("🔎 Buscar en la bitácora", callback_data="bit_buscar")],
            [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
        await query.edit_message_text("📜 BITÁCORA\n\nRegistro de quién registró, editó, eliminó o restauró cada paquete.", reply_markup=teclado)
    elif query.data == "bit_ultimos":
        filas = conectar_db().execute(SQL_ULTIMOS_BITACORA).rows
        if not filas:
            await query.edit_message_text("📜 La bitácora aún no tiene movimientos.", reply_markup=btn_volver); return
        bloques = _bloques_texto("📜 Últimos movimientos", [_linea_bitacora(f) for f in filas])
        await query.edit_message_text(bloques[0], reply_markup=btn_volver if len(bloques) == 1 else None)
        for i, b in enumerate(bloques[1:], start=1):
            await query.message.reply_text(b, reply_markup=btn_volver if i == len(bloques) - 1 else None)

async def historial_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin"])
    if not usuario: return
    filas = conectar_db().execute(SQL_ULTIMOS_BITACORA).rows
    if not filas:
        await update.message.reply_text("📜 La bitácora aún no tiene movimientos."); return
    for b in _bloques_texto("📜 Últimos movimientos", [_linea_bitacora(f) for f in filas]):
        await update.message.reply_text(b)

async def bitacora_buscar_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin"])
    if not usuario: return ConversationHandler.END
    query = update.callback_query; await query.answer()
    await query.edit_message_text("🔎 Escribe lo que buscas: un DPI, el nombre de un usuario del bot, una acción (EDITAR, ELIMINAR, REGISTRAR, RESTAURAR) o un N°.\n\n(/cancelar para salir)")
    return BIT_TEXTO

async def bitacora_buscar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    patron = f"%{update.message.text.strip()}%"
    filas = conectar_db().execute("""SELECT fecha, usuario, rol, accion, entidad, detalle FROM bitacora
        WHERE usuario LIKE ? OR accion LIKE ? OR entidad LIKE ? OR detalle LIKE ? ORDER BY id DESC LIMIT 25""",
        (patron, patron, patron, patron)).rows
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("📜 Bitácora", callback_data="bit_menu")],
                                       [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if not filas:
        await update.message.reply_text("❌ No hay movimientos que coincidan.", reply_markup=btn_volver)
    else:
        bloques = _bloques_texto(f"🔎 Resultados ({len(filas)}, los más recientes primero)", [_linea_bitacora(f) for f in filas])
        for i, b in enumerate(bloques):
            await update.message.reply_text(b, reply_markup=btn_volver if i == len(bloques) - 1 else None)
    return ConversationHandler.END

# --- Etiquetas en lote (varias por hoja) ---
ETQ_POR_HOJA, ETQ_Y_INICIO, ETQ_Y_PASO = 3, 12, 84

def generar_pdf_lote_enrol(paquetes):
    try: from fpdf import FPDF
    except ImportError: return None, "No tienes 'fpdf2'."
    try:
        pdf = FPDF(orientation="P", unit="mm", format="Letter"); pdf.set_auto_page_break(auto=False)
        for i, r in enumerate(paquetes):
            pos = i % ETQ_POR_HOJA
            if pos == 0: pdf.add_page()
            ubic = f"E{r['estanteria']}-F{r['fila']}-{r['columna']}-P{r['pila']}"
            _dibujar_etiqueta_enrol(pdf, ETQ_Y_INICIO + pos * ETQ_Y_PASO, r["usuario_codigo"], r["fecha_del"], r["fecha_al"], r["num_paquete"], r.get("numero_general"), ubic)
        ruta = os.path.join(tempfile.gettempdir(), f"lote_etiquetas_{int(time.time())}.pdf"); pdf.output(ruta); return ruta, None
    except Exception as e: return None, str(e)

def generar_pdf_lote_evento(paquetes):
    try: from fpdf import FPDF
    except ImportError: return None, "No tienes 'fpdf2'."
    try:
        pdf = FPDF(orientation="P", unit="mm", format="Letter"); pdf.set_auto_page_break(auto=False)
        for i, r in enumerate(paquetes):
            pos = i % ETQ_POR_HOJA
            if pos == 0: pdf.add_page()
            ubic = f"E{r['estanteria']}-F{r['fila']}-{r['columna']}-P{r['pila']}"
            _dibujar_etiqueta_evento(pdf, ETQ_Y_INICIO + pos * ETQ_Y_PASO, r["tipo_evento"], r["anio"], r["num_paquete"], r["folio_inicio"], r["folio_fin"], r.get("numero_general"), ubic)
        ruta = os.path.join(tempfile.gettempdir(), f"lote_etiquetas_ev_{int(time.time())}.pdf"); pdf.output(ruta); return ruta, None
    except Exception as e: return None, str(e)

LOTE_MAX = 90  # 30 hojas

LT_MODOS = {
    "enrol": [("👤 Por usuario (DPI y rango de paquetes)", "usuario"), ("🔢 Por rango de N° General", "general")],
    "ev":    [("📦 Por Tipo, Año y rango de paquetes", "paq"), ("🔢 Por rango de N° General", "general")],
}
LT_PROMPTS = {
    ("enrol", "usuario"): "Escribe: <DPI> <desde> <hasta>\nEjemplo: PCOTTON320 15 21",
    ("enrol", "general"): "Escribe: <desde> <hasta> (N° General)\nEjemplo: 41 60",
    ("ev", "paq"):        "Escribe: <Tipo> <Año> <desde> <hasta> (N° de paquete)\nEjemplo: Nacimiento 2022 25 30",
    ("ev", "general"):    "Escribe: <desde> <hasta> (N° General)\nEjemplo: 41 60",
}

async def lote_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    usuario = await verificar_acceso(update)
    if not usuario: return ConversationHandler.END
    area = query.data.replace("lote_", "")  # enrol / ev
    context.user_data.clear()
    context.user_data["lt_area"] = area
    botones = [[InlineKeyboardButton(t, callback_data=f"ltmodo_{m}")] for t, m in LT_MODOS[area]]
    botones.append([InlineKeyboardButton("🔙 Cancelar", callback_data="menu_inicio")])
    await query.edit_message_text(f"🏷️ Etiquetas en lote ({'DPI' if area == 'enrol' else 'Eventos'})\n\nSe genera un solo PDF con 3 etiquetas por hoja (máximo {LOTE_MAX}).\n¿Cómo quieres elegir los paquetes?", reply_markup=InlineKeyboardMarkup(botones))
    return LT_MODO

async def lote_modo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    modo = query.data.replace("ltmodo_", "")
    context.user_data["lt_modo"] = modo
    area = context.user_data.get("lt_area", "enrol")
    await query.edit_message_text(LT_PROMPTS[(area, modo)] + "\n\n(/cancelar para salir)")
    return LT_VALOR

async def lote_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    area, modo = context.user_data.get("lt_area", "enrol"), context.user_data.get("lt_modo")
    partes = update.message.text.split()
    try:
        if (area, modo) == ("enrol", "usuario"):
            if len(partes) != 3: raise ValueError
            dpi, desde, hasta = partes[0], int(partes[1]), int(partes[2])
            filas = _filas_evento("SELECT * FROM paquetes WHERE usuario_codigo = ? AND num_paquete BETWEEN ? AND ? ORDER BY num_paquete", (dpi, desde, hasta))
            clave, descripcion = "num_paquete", f"{dpi} · paquetes {desde} al {hasta}"
        elif (area, modo) == ("ev", "paq"):
            tipo = normalizar_tipo(partes[0]) if len(partes) == 4 else None
            if not tipo: raise ValueError
            anio, desde, hasta = int(partes[1]), int(partes[2]), int(partes[3])
            filas = _filas_evento("SELECT * FROM eventos WHERE tipo_evento = ? AND anio = ? AND num_paquete BETWEEN ? AND ? ORDER BY num_paquete", (tipo, anio, desde, hasta))
            clave, descripcion = "num_paquete", f"{tipo} {anio} · paquetes {desde} al {hasta}"
        else:  # rango de N° General
            if len(partes) != 2: raise ValueError
            desde, hasta = int(partes[0]), int(partes[1])
            tabla = "paquetes" if area == "enrol" else "eventos"
            filas = _filas_evento(f"SELECT * FROM {tabla} WHERE numero_general BETWEEN ? AND ? ORDER BY numero_general", (desde, hasta))
            clave, descripcion = "numero_general", f"N° General {desde} al {hasta}"
    except ValueError:
        await update.message.reply_text("⚠️ Formato inválido.\n" + LT_PROMPTS[(area, modo)] + "\n\nInténtalo de nuevo o /cancelar:")
        return LT_VALOR
    if desde > hasta:
        await update.message.reply_text("⚠️ El número inicial no puede ser mayor que el final. Inténtalo de nuevo o /cancelar:"); return LT_VALOR
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if not filas:
        await update.message.reply_text("❌ No se encontró ningún paquete en ese rango.", reply_markup=btn_volver)
        context.user_data.clear(); return ConversationHandler.END
    if len(filas) > LOTE_MAX:
        await update.message.reply_text(f"⚠️ Son {len(filas)} paquetes; el máximo por lote es {LOTE_MAX}. Reduce el rango e inténtalo de nuevo:"); return LT_VALOR
    faltan = []
    if desde <= hasta and hasta - desde <= 500:
        encontrados = {f[clave] for f in filas if f.get(clave) is not None}
        faltan = [n for n in range(desde, hasta + 1) if n not in encontrados]
    aviso = await update.message.reply_text(f"⏳ Generando {len(filas)} etiqueta(s)...")
    ruta, error = await asyncio.to_thread(generar_pdf_lote_enrol if area == "enrol" else generar_pdf_lote_evento, filas)
    if not ruta:
        await aviso.edit_text(f"❌ Error al generar el PDF: {error}", reply_markup=btn_volver)
        context.user_data.clear(); return ConversationHandler.END
    caption = f"🏷️ Lote de {len(filas)} etiqueta(s)\n{descripcion}"
    if faltan: caption += f"\n⚠️ Sin paquete: {', '.join(str(n) for n in faltan[:20])}" + (" …" if len(faltan) > 20 else "")
    with open(ruta, "rb") as f:
        await update.message.reply_document(document=f, filename=f"lote_etiquetas_{datetime.now().strftime('%H%M%S')}.pdf", caption=caption)
    os.remove(ruta)
    await aviso.edit_text("✅ Lote generado.", reply_markup=btn_volver)
    bitacora("IMPRIMIR LOTE", "paquete" if area == "enrol" else "evento", f"{len(filas)} etiqueta(s) · {descripcion}")
    context.user_data.clear()
    return ConversationHandler.END

# --- Flujo rápido: opciones debajo del paquete recién registrado ---
def teclado_post_registro_enrol(dpi, num):
    rol = (ACTOR_ACTUAL.get() or (None, "", ""))[2]
    filas = [[InlineKeyboardButton("✏️ Editar", callback_data=f"pe_ed_{dpi}|{num}"),
              InlineKeyboardButton("🏷️ Imprimir etiqueta", callback_data=f"pe_pr_{dpi}|{num}")],
             [InlineKeyboardButton("➕ Registrar otro (mismo usuario)", callback_data=f"pe_otro_{dpi}")]]
    if rol != "operador2":
        filas.append([InlineKeyboardButton("👤 Registrar otro (otro usuario)", callback_data="enrol_registrar")])
    filas.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")])
    return InlineKeyboardMarkup(filas)

def teclado_post_registro_evento(id_evento, tipo, anio):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Editar", callback_data=f"ev_ed_{id_evento}"),
         InlineKeyboardButton("🏷️ Imprimir etiqueta", callback_data=f"ev_pr_{id_evento}")],
        [InlineKeyboardButton(f"➕ Registrar otro ({tipo} {anio})", callback_data=f"ev_otro_{tipo}|{anio}")],
        [InlineKeyboardButton("🆕 Otro tipo o año", callback_data="accion_registrar_evento")],
        [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])

async def post_imprimir_enrol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    query = update.callback_query; await query.answer()
    dpi, num = query.data.replace("pe_pr_", "").rsplit("|", 1)
    paquete = buscar_por_dpi_y_numero(dpi, int(num))
    if not paquete: await query.message.reply_text("❌ Ese paquete ya no existe."); return
    await _enviar_etiqueta_paquete(query.message, paquete)

async def post_editar_enrol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return ConversationHandler.END
    query = update.callback_query; await query.answer()
    dpi, num = query.data.replace("pe_ed_", "").rsplit("|", 1)
    if usuario["rol"] == "operador2" and dpi != usuario["dpi_asignado"]:
        await query.message.reply_text("🚫 Solo puedes editar paquetes de tu propio DPI."); return ConversationHandler.END
    paquete = buscar_por_dpi_y_numero(dpi, int(num))
    if not paquete: await query.message.reply_text("❌ Ese paquete ya no existe."); return ConversationHandler.END
    context.user_data.clear()
    context.user_data.update({"ep_accion": "editar", "ep_dpi": dpi, "ep_num": int(num)})
    await query.message.reply_text(f"✏️ Editar paquete:\n{texto_paquete_enrol(paquete)}\n\n¿Qué campo deseas modificar?", reply_markup=teclado_campos_enrol())
    return EP_CAMPO

async def registrar_otro_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return ConversationHandler.END
    query = update.callback_query; await query.answer()
    dpi = query.data.replace("pe_otro_", "")
    if usuario["rol"] == "operador2" and dpi != usuario["dpi_asignado"]:
        await query.message.reply_text("🚫 Solo puedes registrar paquetes de tu propio DPI."); return ConversationHandler.END
    context.user_data.clear()
    context.user_data["dpi"] = dpi
    await query.message.reply_text(f"📝 Nuevo paquete\n👤 Usuario: {dpi}\n\n2️⃣ Fecha DEL (AAAA-MM-DD o DD/MM/AAAA):")
    return REG_DEL

async def post_imprimir_evento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    query = update.callback_query; await query.answer()
    paquete = buscar_evento_por_id(int(query.data.replace("ev_pr_", "")))
    if not paquete: await query.message.reply_text("❌ Ese paquete ya no existe."); return
    await _enviar_etiqueta_evento(query.message, paquete)

async def post_editar_evento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return ConversationHandler.END
    query = update.callback_query; await query.answer()
    paquete = buscar_evento_por_id(int(query.data.replace("ev_ed_", "")))
    if not paquete: await query.message.reply_text("❌ Ese paquete ya no existe."); return ConversationHandler.END
    context.user_data.clear()
    context.user_data.update({"ev_accion": "editar", "edit_id": paquete["id"]})
    await query.message.reply_text(f"✏️ Editar paquete:\n{texto_evento(paquete)}\n\n¿Qué campo deseas editar?", reply_markup=teclado_campos_evento())
    return EV_EDIT_CAMPO

async def registrar_evento_otro_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return ConversationHandler.END
    query = update.callback_query; await query.answer()
    tipo, anio = query.data.replace("ev_otro_", "").rsplit("|", 1)
    anio = int(anio)
    context.user_data.clear()
    context.user_data["ev_tipo"], context.user_data["ev_anio"] = tipo, anio
    sug = siguiente_num_paquete(tipo, anio)
    context.user_data["sug_num"] = sug
    await query.message.reply_text(f"{tipo} {anio}\n\nEl siguiente paquete del año sería el N° {sug}.\n¿Es correcto?", reply_markup=teclado_sugerencia("sugnum", sug))
    return EV_NUM

# --- Reglas físicas de las estanterías y sugerencia de ubicación ---
NIVELES_NORMALES = 4          # niveles (pilas) por columna; en raras ocasiones caben 5
COLUMNAS_BASE = "ABCD"        # columnas normales de una estantería
COLUMNAS_POR_ESTANTERIA = {"paquetes": {}, "eventos": {}}  # estanterías con más columnas, ej.: {"paquetes": {"7": "ABCDEF"}}
FILA_SIN_TECHO = "0"          # la fila 0 no tiene techo: los niveles no tienen límite

def texto_ubicacion(u):
    return f"E{u[0]}-F{u[1]}-{u[2]}-P{u[3]}"

def columnas_estanteria(tabla, est):
    """Columnas disponibles: A-D por defecto; hasta F si la estantería lo tiene configurado o ya usa E/F."""
    if str(est) in COLUMNAS_POR_ESTANTERIA.get(tabla, {}): return COLUMNAS_POR_ESTANTERIA[tabla][str(est)]
    try:
        r = conectar_db().execute(f"SELECT MAX(UPPER(columna)) FROM {tabla} WHERE estanteria = ?", (est,))
        mayor = r.rows[0][0] if r.rows and r.rows[0][0] else "D"
    except Exception:
        mayor = "D"
    if len(str(mayor)) != 1 or str(mayor) not in "DEF": mayor = "D"
    return "ABCDEF"[: "ABCDEF".index(str(mayor)) + 1]

def siguiente_posicion(est, fila, col, pila, columnas):
    """Siguiente casilla: sube de nivel; si la columna se llena pasa a la siguiente; si la fila se llena, a la siguiente fila."""
    if not str(pila).isdigit(): return None
    n = int(pila)
    if str(fila) == FILA_SIN_TECHO or n < NIVELES_NORMALES:
        return (est, fila, col, str(n + 1))
    if col in columnas and columnas.index(col) + 1 < len(columnas):
        return (est, fila, columnas[columnas.index(col) + 1], "1")
    if str(fila).isdigit():
        return (est, str(int(fila) + 1), columnas[0], "1")
    return None

def sugerir_ubicacion(tabla, ref, ocupada):
    """ref = (est, fila, col, pila) del último paquete. Devuelve {'sug', 'alt5', 'saltos'} o None."""
    est, fila, col, pila = [str(x) for x in ref]
    columnas = columnas_estanteria(tabla, est)
    pos, saltos = (est, fila, col, pila), 0
    for _ in range(80):
        pos = siguiente_posicion(*pos, columnas)
        if pos is None: return None
        if not ocupada(*pos): break
        saltos += 1
    else:
        return None
    alt5 = None
    if fila != FILA_SIN_TECHO and pila.isdigit() and int(pila) == NIVELES_NORMALES:
        candidata = (est, fila, col, str(NIVELES_NORMALES + 1))
        if not ocupada(*candidata): alt5 = candidata
    return {"sug": pos, "alt5": alt5, "saltos": saltos}

def ultima_ubicacion_general():
    """Ubicación del último paquete de enrolamiento registrado (de cualquier usuario)."""
    r = conectar_db().execute("SELECT estanteria, fila, columna, pila, num_paquete, usuario_codigo FROM paquetes ORDER BY rowid DESC LIMIT 1")
    return tuple(r.rows[0]) if r.rows else None

def evento_en_ubicacion(est, fil, col, pil):
    r = conectar_db().execute("SELECT id FROM eventos WHERE estanteria = ? AND fila = ? AND columna = ? AND pila = ? LIMIT 1", (est, fil, col, pil))
    return bool(r.rows)

def ultima_ubicacion_evento(tipo, anio):
    """(est, fila, col, pila, texto_referencia) del último paquete del mismo tipo y año; si no hay, el último registrado."""
    r = conectar_db().execute("SELECT estanteria, fila, columna, pila, num_paquete FROM eventos WHERE tipo_evento = ? AND anio = ? ORDER BY num_paquete DESC LIMIT 1", (tipo, anio))
    if r.rows:
        f = r.rows[0]
        return (f[0], f[1], f[2], f[3], f"El último paquete de {tipo} {anio} (N° {f[4]}) está en E{f[0]}-F{f[1]}-{f[2]}-P{f[3]}.")
    r = conectar_db().execute("SELECT estanteria, fila, columna, pila, tipo_evento, anio, num_paquete FROM eventos ORDER BY rowid DESC LIMIT 1")
    if r.rows:
        f = r.rows[0]
        return (f[0], f[1], f[2], f[3], f"Aún no hay paquetes de {tipo} {anio}. El último registrado ({f[4]} {f[5]}, N° {f[6]}) está en E{f[0]}-F{f[1]}-{f[2]}-P{f[3]}.")
    return None

async def _ev_pedir_ubicacion(update, context):
    d = context.user_data
    ult = ultima_ubicacion_evento(d["ev_tipo"], d["ev_anio"])
    res = sugerir_ubicacion("eventos", ult[:4], evento_en_ubicacion) if ult else None
    if not res:
        await _responder(update, f"N° General: {d['ev_general']}\n\nIngresa la Estantería:")
        return EV_EST
    d["sug_ubic"], d["alt5"] = res["sug"], res["alt5"]
    texto = f"{ult[4]}\nUbicación sugerida: {texto_ubicacion(res['sug'])}"
    if res["saltos"]: texto += f"\n(Se saltaron {res['saltos']} ubicación(es) ya ocupada(s).)"
    texto += "\n¿Usar esta ubicación?"
    extra = [InlineKeyboardButton(f"⬆️ Usar nivel {NIVELES_NORMALES + 1} (excepción)", callback_data="evsugubi_n5")] if res["alt5"] else None
    await _responder(update, texto, teclado_sugerencia("evsugubi", texto_ubicacion(res["sug"]), extra))
    return EV_UBIC

async def ev_ubic_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    d = context.user_data
    if query.data == "evsugubi_otro":
        await query.edit_message_text("Ingresa la Estantería:")
        return EV_EST
    d["ev_est"], d["ev_fil"], d["ev_col"], d["ev_pil"] = d["alt5"] if query.data == "evsugubi_n5" else d["sug_ubic"]
    return await _ev_guardar(update, context)

# --- MAPA DE ESTANTERÍAS (una imagen por área: las estanterías de Enrolamiento y de Eventos son distintas) ---
FILAS_POR_ESTANTERIA = {"paquetes": {}, "eventos": {}}  # opcional: {"paquetes": {"5": 6}} dibuja filas 0 a 6 en la estantería 5 de Enrolamiento
AREAS_MAPA = {"E": ("paquetes", "Enrolamiento DPI"), "V": ("eventos", "Eventos SIRECI")}

def _fuente(tamano, negrita=False):
    from PIL import ImageFont
    nombre = "DejaVuSans-Bold.ttf" if negrita else "DejaVuSans.ttf"
    try: return ImageFont.truetype(nombre, tamano)
    except Exception: pass
    try: return ImageFont.load_default(size=tamano)
    except TypeError: return ImageFont.load_default()

def lista_estanterias(area):
    tabla = AREAS_MAPA[area][0]
    vistas = set()
    for r in conectar_db().execute(f"SELECT DISTINCT estanteria FROM {tabla}").rows:
        if r[0] is not None and str(r[0]).strip() != "": vistas.add(str(r[0]).strip())
    return sorted(vistas, key=lambda x: (0, int(x)) if x.isdigit() else (1, x))

def datos_estanteria(area, est):
    """Devuelve ({(fila, columna, nivel): [etiqueta, ...]}, cantidad_sin_ubicar) solo del área indicada."""
    ocupados, sin_ubicar = {}, 0
    if area == "E":
        filas = conectar_db().execute("SELECT usuario_codigo, num_paquete, fila, columna, pila FROM paquetes WHERE estanteria = ?", (est,)).rows
        registros = [(f"{str(r[0])[:7]} #{r[1]}", r[2], r[3], r[4]) for r in filas]
    else:
        filas = conectar_db().execute("SELECT tipo_evento, anio, num_paquete, fila, columna, pila FROM eventos WHERE estanteria = ?", (est,)).rows
        registros = [(f"{str(r[0])[:3]} {str(r[1])[-2:]} #{r[2]}", r[3], r[4], r[5]) for r in filas]
    for etiqueta, fila, col, pila in registros:
        fila, col, pila = str(fila).strip(), str(col).strip().upper(), str(pila).strip()
        if not (fila.isdigit() and pila.isdigit() and len(col) == 1 and col.isalpha()):
            sin_ubicar += 1; continue
        ocupados.setdefault((int(fila), col, int(pila)), []).append(etiqueta)
    return ocupados, sin_ubicar

def generar_mapa_estanteria(area, est, resaltar=None):
    """Dibuja la estantería. `resaltar` = {(fila, columna, nivel)} se marca con amarillo, borde rojo y flechas.
    Devuelve (bytes_png, resumen) o (None, None) si no hay datos."""
    from PIL import Image, ImageDraw
    tabla, nombre_area = AREAS_MAPA[area]
    resaltar = set(resaltar or [])
    ocupados, sin_ubicar = datos_estanteria(area, est)
    if not ocupados: return None, None
    max_fila = max(max(f for f, _, _ in ocupados), FILAS_POR_ESTANTERIA.get(tabla, {}).get(str(est), 0))
    filas = list(range(0, max_fila + 1))
    usadas = {c for _, c, _ in ocupados}
    if str(est) in COLUMNAS_POR_ESTANTERIA.get(tabla, {}): columnas = COLUMNAS_POR_ESTANTERIA[tabla][str(est)]
    else:
        mayor = max(usadas | {"D"})
        columnas = "ABCDEF"[: "ABCDEF".index(mayor) + 1] if mayor in "ABCDEF" else COLUMNAS_BASE
    columnas += "".join(sorted(c for c in usadas if c not in columnas))
    niveles = {}
    for f in filas:
        usados = [n for (ff, _, n) in ocupados if ff == f]
        niveles[f] = max(NIVELES_NORMALES, (max(usados) + 1) if (f == 0 and usados) else (max(usados) if usados else 0))
    ancho_celda, alto_caja, margen_izq, margen_sup, espacio = 178, 30, 100, 122, 14
    ancho = margen_izq + len(columnas) * ancho_celda + 20
    alto = margen_sup + sum(niveles[f] * alto_caja + espacio for f in filas) + 64
    img = Image.new("RGB", (ancho, alto), "white"); d = ImageDraw.Draw(img)
    f_tit, f_sub, f_txt, f_txt_b, f_peq = _fuente(24, True), _fuente(15), _fuente(14), _fuente(14, True), _fuente(12)
    ROJO = (213, 0, 0)
    color_area = (((187, 222, 251), (21, 101, 192)) if area == "E" else ((255, 224, 178), (230, 126, 0)))
    colores = {"libre": ((232, 245, 233), (129, 199, 132)), "ocupado": color_area, "conflicto": ((255, 205, 210), (198, 40, 40)),
               "resaltado": ((255, 235, 59), ROJO)}
    filas_res = {f for (f, c, n) in resaltar}; cols_res = {c for (f, c, n) in resaltar}
    libres = ocupados_n = conflictos = marcados = 0
    for i, c in enumerate(columnas):
        x = margen_izq + i * ancho_celda; xc = x + ancho_celda / 2
        en_res = c in cols_res
        d.text((xc, margen_sup - 42), f"Columna {c}", font=f_txt_b if en_res else f_txt, fill=ROJO if en_res else (40, 40, 40), anchor="ms")
        if en_res: d.polygon([(xc - 10, margen_sup - 34), (xc + 10, margen_sup - 34), (xc, margen_sup - 16)], fill=ROJO)
    y = margen_sup
    for f in filas:
        n = niveles[f]
        en_res = f in filas_res
        d.text((margen_izq - 34, y + n * alto_caja / 2), f"Fila {f}", font=f_txt_b if en_res else f_txt, fill=ROJO if en_res else (40, 40, 40), anchor="rm")
        if f == 0: d.text((margen_izq - 34, y + n * alto_caja / 2 + 16), "sin techo", font=f_peq, fill=(120, 120, 120), anchor="rm")
        for i, c in enumerate(columnas):
            x = margen_izq + i * ancho_celda
            for nivel in range(1, n + 1):
                top = y + (n - nivel) * alto_caja
                registros = ocupados.get((f, c, nivel), [])
                borde_ancho, fuente = 2, f_txt
                if not registros:
                    clave = "libre"
                    texto = f"N{nivel} (extra)" if (f > 0 and nivel > NIVELES_NORMALES) else f"N{nivel}"
                    if f > 0 and nivel <= NIVELES_NORMALES: libres += 1
                    fuente = f_peq
                else:
                    if len(registros) > 1: clave, texto = "conflicto", f"! {len(registros)} paquetes"; conflictos += 1
                    else: clave, texto = "ocupado", registros[0]; ocupados_n += 1
                    if (f, c, nivel) in resaltar:
                        clave, borde_ancho, fuente = "resaltado", 4, f_txt_b; marcados += 1
                        yc = top + alto_caja / 2
                        d.polygon([(margen_izq - 26, yc - 10), (margen_izq - 26, yc + 10), (margen_izq - 6, yc)], fill=ROJO)
                relleno, borde = colores[clave]
                d.rectangle([x + 3, top + 2, x + ancho_celda - 3, top + alto_caja - 2], fill=relleno, outline=borde, width=borde_ancho)
                d.text((x + ancho_celda / 2, top + alto_caja / 2), texto, font=fuente,
                       fill=(30, 30, 30) if clave != "libre" else (90, 140, 95), anchor="mm")
        y += n * alto_caja + espacio
    d.text((margen_izq, 14), f"Estantería {est} · {nombre_area}", font=f_tit, fill=(20, 20, 20))
    total_ocup = ocupados_n + conflictos
    d.text((margen_izq, 46), f"{total_ocup} nivel(es) ocupado(s) · {libres} libre(s) hasta el nivel {NIVELES_NORMALES} (sin contar la fila 0)", font=f_sub, fill=(70, 70, 70))
    leyenda = [("libre", "Libre"), ("ocupado", "Ocupado"), ("conflicto", "Ocupado 2+ veces")]
    if resaltar: leyenda.append(("resaltado", "Resultado de la búsqueda"))
    ly = y + 14
    for j, (clave, nombre) in enumerate(leyenda):
        lx = margen_izq + j * 150
        relleno, borde = colores[clave]
        d.rectangle([lx, ly, lx + 18, ly + 18], fill=relleno, outline=borde, width=3 if clave == "resaltado" else 2)
        d.text((lx + 26, ly + 9), nombre, font=f_peq, fill=(60, 60, 60), anchor="lm")
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return buf, {"ocupados": total_ocup, "libres": libres, "conflictos": conflictos, "sin_ubicar": sin_ubicar, "marcados": marcados}

async def enviar_mapas_resultado(mensaje, area, paquetes, max_mapas=3):
    """Después de una búsqueda: envía el dibujo de la estantería con el/los paquete(s) encontrado(s) resaltado(s).
    Nunca interrumpe la búsqueda si algo falla."""
    try:
        paquetes = [p for p in (paquetes or []) if p]
        grupos = {}
        for r in paquetes:
            grupos.setdefault(str(r.get("estanteria")).strip(), []).append(r)
        for i, (est, rs) in enumerate(grupos.items()):
            if i >= max_mapas:
                await mensaje.reply_text(f"ℹ️ Los resultados están en {len(grupos)} estanterías; te mostré las primeras {max_mapas}. Usa 🗄️ MAPA DE ESTANTERÍAS para ver las demás.")
                break
            resaltar, lineas = set(), []
            for r in rs:
                f, c, p = str(r.get("fila")).strip(), str(r.get("columna")).strip().upper(), str(r.get("pila")).strip()
                if f.isdigit() and p.isdigit() and len(c) == 1: resaltar.add((int(f), c, int(p)))
                desc = (f"N° {r.get('num_paquete')} ({r.get('usuario_codigo')})" if area == "E"
                        else f"{r.get('tipo_evento')} {r.get('anio')} · Paq {r.get('num_paquete')}")
                lineas.append(f"📍 {desc} → E{est}-F{f}-{c}-P{p}")
            if not resaltar: continue
            buf, resumen = await asyncio.to_thread(generar_mapa_estanteria, area, est, resaltar)
            if buf is None: continue
            caption = "\n".join(lineas[:12]) + (f"\n… y {len(lineas) - 12} más" if len(lineas) > 12 else "")
            await mensaje.reply_photo(photo=buf, caption=caption[:1000])
    except Exception:
        traceback.print_exc()

def teclado_areas_mapa():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Enrolamiento DPI", callback_data="mapa_area_E")],
        [InlineKeyboardButton("📖 Eventos SIRECI", callback_data="mapa_area_V")],
        [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])

def teclado_estanterias(area):
    est = lista_estanterias(area)
    botones, fila_ = [], []
    for e in est:
        fila_.append(InlineKeyboardButton(f"E{e}", callback_data=f"mapa_est_{area}_{e}"))
        if len(fila_) == 4: botones.append(fila_); fila_ = []
    if fila_: botones.append(fila_)
    botones.append([InlineKeyboardButton("⬅️ Cambiar de área", callback_data="mapa_inicio")])
    botones.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")])
    return InlineKeyboardMarkup(botones), est

async def _enviar_mapa(mensaje, area, est, como_archivo=False):
    nombre_area = AREAS_MAPA[area][1]
    buf, resumen = await asyncio.to_thread(generar_mapa_estanteria, area, est)
    btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Ver en alta calidad (archivo)", callback_data=f"mapa_doc_{area}_{est}")],
                                [InlineKeyboardButton("🗄️ Otra estantería", callback_data=f"mapa_area_{area}")],
                                [InlineKeyboardButton("⬅️ Cambiar de área", callback_data="mapa_inicio")],
                                [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if buf is None:
        await mensaje.reply_text(f"🗄️ La estantería {est} de {nombre_area} no tiene paquetes registrados todavía.", reply_markup=btn); return
    caption = f"🗄️ Estantería {est} · {nombre_area}: {resumen['ocupados']} ocupado(s) · {resumen['libres']} libre(s) hasta nivel {NIVELES_NORMALES}"
    if resumen["conflictos"]: caption += f"\n⚠️ {resumen['conflictos']} ubicación(es) con más de un paquete"
    if resumen["sin_ubicar"]: caption += f"\nℹ️ {resumen['sin_ubicar']} paquete(s) con ubicación incompleta no se dibujan"
    if como_archivo:
        await mensaje.reply_document(document=buf, filename=f"mapa_estanteria_{area}_{est}.png", caption=caption)
    else:
        await mensaje.reply_photo(photo=buf, caption=caption, reply_markup=btn)

async def mapa_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    query = update.callback_query; await query.answer()
    if query.data == "mapa_inicio":
        await editar_texto(query, "🗄️ MAPA DE ESTANTERÍAS\n\n¿De qué área quieres ver la estantería?", reply_markup=teclado_areas_mapa())
    elif query.data.startswith("mapa_area_"):
        area = query.data.replace("mapa_area_", "")
        if area not in AREAS_MAPA: return
        teclado, est = teclado_estanterias(area)
        texto = (f"🗄️ {AREAS_MAPA[area][1]}\n\nElige la estantería (también puedes usar /mapa {'dpi' if area == 'E' else 'eventos'} 5):"
                 if est else f"🗄️ {AREAS_MAPA[area][1]}: aún no hay paquetes con ubicación registrados.")
        await editar_texto(query, texto, reply_markup=teclado)
    elif query.data.startswith("mapa_est_"):
        area, est = query.data.replace("mapa_est_", "").split("_", 1)
        if area in AREAS_MAPA: await _enviar_mapa(query.message, area, est)
    elif query.data.startswith("mapa_doc_"):
        area, est = query.data.replace("mapa_doc_", "").split("_", 1)
        if area in AREAS_MAPA: await _enviar_mapa(query.message, area, est, como_archivo=True)

async def mapa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    args = [a.strip() for a in (context.args or [])]
    area = None
    if args:
        if args[0].lower() in ("e", "dpi", "enrolamiento"): area = "E"
        elif args[0].lower() in ("v", "ev", "evento", "eventos"): area = "V"
    if area and len(args) >= 2:
        await _enviar_mapa(update.message, area, args[1]); return
    if area:
        teclado, est = teclado_estanterias(area)
        await update.message.reply_text(f"🗄️ {AREAS_MAPA[area][1]}\n\nElige la estantería:" if est else f"🗄️ {AREAS_MAPA[area][1]}: aún no hay paquetes con ubicación registrados.", reply_markup=teclado); return
    await update.message.reply_text("🗄️ MAPA DE ESTANTERÍAS\n\n¿De qué área quieres ver la estantería?\n(También: /mapa dpi 5  o  /mapa eventos 5)", reply_markup=teclado_areas_mapa())

# --- CARGA MASIVA: sube los registros guardados sin internet (y corrige los que ya existen) ---
MAX_LINEAS_CARGA = 300

# Campos que la carga masiva PUEDE actualizar en un paquete que ya existe. La clave del paquete (usuario/tipo-año y N° de paquete)
# y el N° General NO se modifican por esta vía (el N° General es el que ya está en las etiquetas; se cambia solo con Editar).
CAMPOS_ACT_E = (("fecha_del", "DEL"), ("fecha_al", "AL"), ("estanteria", "estantería"), ("fila", "fila"), ("columna", "columna"), ("pila", "pila"))
CAMPOS_ACT_V = (("folio_inicio", "DEL"), ("folio_fin", "AL"), ("estanteria", "estantería"), ("fila", "fila"), ("columna", "columna"), ("pila", "pila"))

def url_offline():
    base = os.environ.get("RENDER_EXTERNAL_URL", "https://bot-atestados.onrender.com").rstrip("/")
    return base + "/offline/"

def texto_ayuda_carga():
    return ("📥 CARGA MASIVA\n\n"
            "Pega aquí los registros que guardaste sin internet, o sube el archivo .txt / .csv. Una línea por paquete:\n\n"
            "DPI:\nE;usuario;fecha DEL;fecha AL;paquete;estantería;fila;columna;pila;general\n\n"
            "Eventos:\nV;tipo;año;paquete;DEL;AL;estantería;fila;columna;pila;general\n\n"
            "• Las fechas pueden ir como AAAA-MM-DD o DD/MM/AAAA.\n"
            "• El N° General vacío se asigna solo, en orden correlativo.\n"
            "• Si un paquete ya existe y la línea trae datos distintos (fechas, rango DEL-AL o ubicación), te propongo ACTUALIZARLO. El N° General nunca se modifica por aquí.\n"
            "• Antes de guardar te muestro una revisión y confirmas.\n\n"
            f"Formulario para registrar sin internet (ábrelo una vez con internet y quedará disponible):\n{url_offline()}\n\n"
            "(/cancelar para salir)")

def _i(x):
    try: return int(x)
    except Exception: return x

def _norm(campo, v):
    if v is None: return ""
    v = str(v).strip()
    if campo in ("fila", "pila", "folio_inicio", "folio_fin") and v.isdigit(): return str(int(v))
    if campo == "columna": return v.upper()
    return v

def _cambios(actual, nuevo, campos):
    """Lista [(campo, nombre, valor_actual, valor_nuevo)] de lo que cambia."""
    out = []
    for campo, nombre in campos:
        a, n = _norm(campo, actual.get(campo)), _norm(campo, nuevo.get(campo))
        if a != n: out.append((campo, nombre, a, n))
    return out

def _texto_cambios(cambios):
    return "; ".join(f"{nombre} {a or '—'} → {n}" for _, nombre, a, n in cambios)

def _estado_existente():
    """Carga de una vez lo que ya existe, para validar sin hacer una consulta por línea."""
    client = conectar_db()
    ex = {"paq": {}, "gen_e": set(), "ubic_e": set(), "ev": {}, "gen_v": set(), "ubic_v": set()}
    for r in client.execute("SELECT usuario_codigo, num_paquete, numero_general, estanteria, fila, columna, pila, fecha_del, fecha_al FROM paquetes").rows:
        ex["paq"][(str(r[0]).strip().lower(), _i(r[1]))] = {"dpi": str(r[0]).strip(), "numero_general": r[2], "estanteria": r[3], "fila": r[4], "columna": r[5], "pila": r[6], "fecha_del": r[7], "fecha_al": r[8]}
        if r[2] is not None: ex["gen_e"].add(_i(r[2]))
        ex["ubic_e"].add((str(r[3]).strip(), _i(r[4]), str(r[5]).strip().upper(), _i(r[6])))
    for r in client.execute("SELECT tipo_evento, anio, num_paquete, numero_general, estanteria, fila, columna, pila, folio_inicio, folio_fin FROM eventos").rows:
        ex["ev"][(str(r[0]), _i(r[1]), _i(r[2]))] = {"numero_general": r[3], "estanteria": r[4], "fila": r[5], "columna": r[6], "pila": r[7], "folio_inicio": r[8], "folio_fin": r[9]}
        if r[3] is not None: ex["gen_v"].add(_i(r[3]))
        ex["ubic_v"].add((str(r[4]).strip(), _i(r[5]), str(r[6]).strip().upper(), _i(r[7])))
    return ex

def parsear_carga(texto, usuario):
    """Devuelve (validos, errores, avisos, reasignables, actualizables, sin_cambios).
    validos = paquetes nuevos; reasignables = nuevos con N° General repetido; actualizables = ya existen y cambian algo."""
    ex = _estado_existente()
    validos, errores, avisos, reasignables, actualizables, sin_cambios = [], [], [], [], [], []
    vistos = {"paq": set(), "gen_e": set(), "ubic_e": set(), "ev": set(), "gen_v": set(), "ubic_v": set()}
    def entero(valor, nombre):
        if not re.fullmatch(r"\d+", valor or ""): raise ValueError(f"{nombre} debe ser un número")
        return int(valor)
    for nro, cruda in enumerate(texto.splitlines(), start=1):
        linea = cruda.strip().lstrip("\ufeff")
        if not linea or linea.startswith("#"): continue
        partes = [p.strip() for p in re.split(r"[;|\t]", linea)]
        if len(partes) == 1: partes = [p.strip() for p in linea.split(",")]
        t = partes[0].upper()
        gen_repetido = None
        try:
            if t == "E":
                if len(partes) < 9: raise ValueError("faltan datos (se esperan 9 o 10 campos)")
                dpi, f_del, f_al, paq, est, fila, col, pila = partes[1:9]
                gen_txt = partes[9] if len(partes) > 9 else ""
                if not dpi: raise ValueError("falta el usuario")
                if usuario["rol"] == "operador2" and dpi != usuario["dpi_asignado"]:
                    raise ValueError("solo puedes cargar paquetes de tu propio DPI")
                f_del, f_al = normalizar_fecha(f_del), normalizar_fecha(f_al)
                if not f_del or not f_al: raise ValueError("fecha inválida")
                if f_al < f_del: raise ValueError("la fecha AL es anterior a la fecha DEL")
                paq, fila, pila = entero(paq, "el paquete"), entero(fila, "la fila"), entero(pila, "la pila")
                col = col.upper()
                if col not in tuple("ABCDEF"): raise ValueError("la columna debe ser A-F")
                if not est: raise ValueError("falta la estantería")
                clave = (dpi.lower(), paq)
                if clave in vistos["paq"]: raise ValueError(f"el paquete {paq} de {dpi} está repetido en el archivo")
                vistos["paq"].add(clave)
                ubic = (est, fila, col, pila)
                if clave in ex["paq"]:  # ya existe: ¿hay algo que corregir?
                    actual = ex["paq"][clave]
                    nuevo = {"fecha_del": f_del, "fecha_al": f_al, "estanteria": est, "fila": str(fila), "columna": col, "pila": str(pila)}
                    cambios = _cambios(actual, nuevo, CAMPOS_ACT_E)
                    if not cambios:
                        sin_cambios.append((nro, f"{dpi} N° {paq} ya está igual")); continue
                    propia = (str(actual["estanteria"]).strip(), _i(actual["fila"]), str(actual["columna"]).strip().upper(), _i(actual["pila"]))
                    if ubic != propia and (ubic in ex["ubic_e"] or ubic in vistos["ubic_e"]):
                        avisos.append((nro, f"la nueva ubicación E{est}-F{fila}-{col}-P{pila} ya está ocupada"))
                    vistos["ubic_e"].add(ubic)
                    if gen_txt.isdigit() and actual.get("numero_general") is not None and int(gen_txt) != int(actual["numero_general"]):
                        avisos.append((nro, f"el N° General no se modifica por aquí (usa Editar si quieres cambiarlo)"))
                    actualizables.append({"nro": nro, "tipo": "E", "dpi": actual["dpi"], "paq": paq, "cambios": cambios})
                    continue
                if gen_txt.lower() in ("", "auto"): gen = "auto"
                elif gen_txt.lower() in ("blanco", "null"): gen = None
                else:
                    gen = entero(gen_txt, "el N° General")
                    if gen in ex["gen_e"] or gen in vistos["gen_e"]:
                        gen_repetido, gen = gen, "auto"
                    else: vistos["gen_e"].add(gen)
                if ubic in ex["ubic_e"] or ubic in vistos["ubic_e"]:
                    avisos.append((nro, f"la ubicación E{est}-F{fila}-{col}-P{pila} ya está ocupada"))
                vistos["ubic_e"].add(ubic)
                (validos if gen_repetido is None else reasignables).append({"gen_rep": gen_repetido, "nro": nro, "tipo": "E", "dpi": dpi, "del": f_del, "al": f_al, "paq": paq, "est": est, "fila": str(fila), "col": col, "pila": str(pila), "gen": gen})
            elif t == "V":
                if len(partes) < 10: raise ValueError("faltan datos (se esperan 10 u 11 campos)")
                tipo_txt, anio, paq, ini, fin, est, fila, col, pila = partes[1:10]
                gen_txt = partes[10] if len(partes) > 10 else ""
                tipo = normalizar_tipo(tipo_txt)
                if not tipo: raise ValueError(f"tipo de evento no reconocido ({tipo_txt})")
                anio, paq, ini, fin = entero(anio, "el año"), entero(paq, "el paquete"), entero(ini, "el evento DEL"), entero(fin, "el evento AL")
                if not 2000 <= anio <= 2100: raise ValueError("año fuera de rango")
                if fin < ini: raise ValueError("el evento AL es menor que el DEL")
                fila, pila = entero(fila, "la fila"), entero(pila, "la pila")
                col = col.upper()
                if col not in tuple("ABCDEF"): raise ValueError("la columna debe ser A-F")
                if not est: raise ValueError("falta la estantería")
                clave = (tipo, anio, paq)
                if clave in vistos["ev"]: raise ValueError(f"el paquete {paq} de {tipo} {anio} está repetido en el archivo")
                vistos["ev"].add(clave)
                ubic = (est, fila, col, pila)
                if clave in ex["ev"]:  # ya existe: ¿hay algo que corregir?
                    actual = ex["ev"][clave]
                    nuevo = {"folio_inicio": ini, "folio_fin": fin, "estanteria": est, "fila": str(fila), "columna": col, "pila": str(pila)}
                    cambios = _cambios(actual, nuevo, CAMPOS_ACT_V)
                    if not cambios:
                        sin_cambios.append((nro, f"{tipo} {anio} Paq {paq} ya está igual")); continue
                    propia = (str(actual["estanteria"]).strip(), _i(actual["fila"]), str(actual["columna"]).strip().upper(), _i(actual["pila"]))
                    if ubic != propia and (ubic in ex["ubic_v"] or ubic in vistos["ubic_v"]):
                        avisos.append((nro, f"la nueva ubicación E{est}-F{fila}-{col}-P{pila} ya está ocupada"))
                    vistos["ubic_v"].add(ubic)
                    if gen_txt.isdigit() and actual.get("numero_general") is not None and int(gen_txt) != int(actual["numero_general"]):
                        avisos.append((nro, "el N° General no se modifica por aquí (usa Editar si quieres cambiarlo)"))
                    actualizables.append({"nro": nro, "tipo": "V", "evento": tipo, "anio": anio, "paq": paq, "cambios": cambios})
                    continue
                if gen_txt.lower() in ("", "auto"): gen = "auto"
                elif gen_txt.lower() in ("blanco", "null"): gen = None
                else:
                    gen = entero(gen_txt, "el N° General")
                    if gen in ex["gen_v"] or gen in vistos["gen_v"]:
                        gen_repetido, gen = gen, "auto"
                    else: vistos["gen_v"].add(gen)
                if ubic in ex["ubic_v"] or ubic in vistos["ubic_v"]:
                    avisos.append((nro, f"la ubicación E{est}-F{fila}-{col}-P{pila} ya está ocupada"))
                vistos["ubic_v"].add(ubic)
                (validos if gen_repetido is None else reasignables).append({"gen_rep": gen_repetido, "nro": nro, "tipo": "V", "evento": tipo, "anio": anio, "paq": paq, "ini": ini, "fin": fin, "est": est, "fila": str(fila), "col": col, "pila": str(pila), "gen": gen})
            else:
                raise ValueError("la línea debe empezar con E (DPI) o V (eventos)")
        except ValueError as e:
            errores.append((nro, str(e)))
    return validos, errores, avisos, reasignables, actualizables, sin_cambios

def ejecutar_carga(validos, usuario):
    """Inserta cada línea nueva. Devuelve (cargados, fallos [(n° de línea, motivo)], asignados [(n° de línea, N° General, repetido)])."""
    ok, fallos, asignados = 0, [], []
    for it in validos:
        try:
            if it["tipo"] == "E":
                general = "auto" if it["gen"] == "auto" else ("blanco" if it["gen"] is None else str(it["gen"]))
                nuevo_gen, error = insertar_paquete(it["dpi"], it["paq"], it["del"], it["al"], it["est"], it["fila"], it["col"], it["pila"], general)
            else:
                if it["gen"] == "auto": general = str(siguiente_general())
                elif it["gen"] is None: general = "blanco"
                else: general = str(it["gen"])
                exito, error = insertar_evento(it["evento"], it["anio"], it["paq"], it["ini"], it["fin"], general, it["est"], it["fila"], it["col"], it["pila"], usuario["dpi_asignado"] or usuario["nombre"])
                if exito: error = None
            if error: fallos.append((it["nro"], error))
            else:
                ok += 1
                if it["gen"] == "auto":
                    asignados.append((it["nro"], nuevo_gen if it["tipo"] == "E" else int(general), it.get("gen_rep")))
        except Exception as e:
            traceback.print_exc()
            fallos.append((it["nro"], f"{type(e).__name__}: {e}"))
    if ok or fallos:
        bitacora("CARGA MASIVA", "paquetes", f"{ok} registro(s) cargados, {len(fallos)} con error")
    return ok, fallos, asignados

def ejecutar_actualizacion(actualizables, usuario):
    """Corrige los paquetes que ya existen (solo los campos que cambian). Cada campo queda en la bitácora.
    Devuelve (actualizados, fallos [(n° de línea, motivo)])."""
    ok, fallos = 0, []
    for it in actualizables:
        try:
            errores_item = []
            if it["tipo"] == "E":
                actual = buscar_por_dpi_y_numero(it["dpi"], it["paq"])
                if not actual:
                    fallos.append((it["nro"], "el paquete ya no existe")); continue
                nuevos = {c: n for c, _, _, n in it["cambios"]}
                orden = [c for c, _, _, _ in it["cambios"]]
                # Si la nueva fecha DEL queda después de la fecha AL actual, se cambia primero la AL
                if "fecha_del" in nuevos and "fecha_al" in nuevos and nuevos["fecha_del"] > (actual.get("fecha_al") or ""):
                    orden = ["fecha_al"] + [c for c in orden if c != "fecha_al"]
                for campo in orden:
                    ok_c, msg, _ = actualizar_paquete_clave(it["dpi"], it["paq"], campo, str(nuevos[campo]))
                    if not ok_c: errores_item.append(f"{campo}: {msg}")
            else:
                filas = buscar_evento_por_tap(it["evento"], it["anio"], it["paq"])
                if not filas:
                    fallos.append((it["nro"], "el paquete ya no existe")); continue
                for campo, _, _, nuevo in it["cambios"]:
                    ok_c, err = actualizar_evento_id(filas[0]["id"], campo, str(nuevo))
                    if not ok_c: errores_item.append(f"{campo}: {err}")
            if errores_item: fallos.append((it["nro"], "; ".join(errores_item)))
            else: ok += 1
        except Exception as e:
            traceback.print_exc()
            fallos.append((it["nro"], f"{type(e).__name__}: {e}"))
    if ok or fallos:
        bitacora("ACTUALIZACIÓN MASIVA", "paquetes", f"{ok} paquete(s) actualizados, {len(fallos)} con error")
    return ok, fallos

async def cargar_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return ConversationHandler.END
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(texto_ayuda_carga())
    else:
        await update.message.reply_text(texto_ayuda_carga())
    return CG_DATOS

def _lista_corta(items, formato, maximo):
    lineas = [formato(i) for i in items[:maximo]]
    if len(items) > maximo: lineas.append(f"… y {len(items) - maximo} más")
    return "\n".join(lineas)

async def cargar_recibir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return ConversationHandler.END
    mensaje = update.message
    if mensaje.document:
        if mensaje.document.file_size and mensaje.document.file_size > 1_000_000:
            await mensaje.reply_text("⚠️ El archivo es muy grande (máximo 1 MB). Divídelo e inténtalo de nuevo:"); return CG_DATOS
        archivo = await mensaje.document.get_file()
        datos = await archivo.download_as_bytearray()
        try: texto = bytes(datos).decode("utf-8-sig")
        except UnicodeDecodeError: texto = bytes(datos).decode("latin-1")
    else:
        texto = mensaje.text or ""
    if len([l for l in texto.splitlines() if l.strip()]) > MAX_LINEAS_CARGA:
        await mensaje.reply_text(f"⚠️ Máximo {MAX_LINEAS_CARGA} líneas por carga. Divide el archivo e inténtalo de nuevo:"); return CG_DATOS
    aviso = await mensaje.reply_text("⏳ Revisando los registros...")
    validos, errores, avisos, reasignables, actualizables, sin_cambios = await asyncio.to_thread(parsear_carga, texto, usuario)
    context.user_data.update({"cg_validos": validos, "cg_reasignables": reasignables, "cg_actualizables": actualizables})
    n_e = sum(1 for v in validos if v["tipo"] == "E"); n_v = len(validos) - n_e
    lineas = [f"📥 Revisión de la carga\n\n✅ Nuevos: {len(validos)} (DPI: {n_e}, Eventos: {n_v})\n🔄 Ya existen y cambian: {len(actualizables)}\n⏸️ Ya existen y están iguales: {len(sin_cambios)}\n🔁 Con N° General repetido: {len(reasignables)}\n❌ Con error: {len(errores)}\n⚠️ Avisos: {len(avisos)}"]
    if actualizables:
        def f_act(it):
            ref = f"{it['dpi']} N° {it['paq']}" if it["tipo"] == "E" else f"{it['evento']} {it['anio']} Paq {it['paq']}"
            return f"• Línea {it['nro']}: {ref} → {_texto_cambios(it['cambios'])}"
        lineas.append("Se actualizarían (solo con tu confirmación):\n" + _lista_corta(actualizables, f_act, 15))
    if reasignables:
        lineas.append("N° General repetido (puedes asignarles uno automático):\n" + _lista_corta(reasignables, lambda it: f"• Línea {it['nro']}: el N° General {it['gen_rep']} ya está en uso", 15))
    if errores:
        lineas.append("Errores (no se procesarán):\n" + _lista_corta(errores, lambda e: f"• Línea {e[0]}: {e[1]}", 15))
    if avisos:
        lineas.append("Avisos:\n" + _lista_corta(avisos, lambda a: f"• Línea {a[0]}: {a[1]}", 10))
    texto_revision = "\n\n".join(lineas)
    botones = []
    if validos: botones.append([InlineKeyboardButton(f"✅ Cargar {len(validos)} nuevo(s)", callback_data="cg_si")])
    if actualizables: botones.append([InlineKeyboardButton(f"🔄 Solo actualizar {len(actualizables)} existente(s)", callback_data="cg_upd")])
    if validos and actualizables: botones.append([InlineKeyboardButton(f"📥 Todo: {len(validos)} nuevo(s) + {len(actualizables)} actualización(es)", callback_data="cg_todo")])
    if reasignables: botones.append([InlineKeyboardButton(f"🔁 Cargar {len(validos) + len(reasignables)} nuevo(s) (N° General automático a los repetidos)", callback_data="cg_auto")])
    botones.append([InlineKeyboardButton("🔙 Cancelar", callback_data="menu_inicio")])
    if validos or reasignables or actualizables:
        texto_revision += "\n\nPuedes enviar un texto corregido para repetir la revisión, o elegir una opción."
        estado = CG_CONFIRMAR
    else:
        texto_revision += "\n\nNo hay nada que cargar ni actualizar. Corrige y vuelve a enviar el texto."
        estado = CG_DATOS
    teclado = InlineKeyboardMarkup(botones)
    bloques = _bloques_texto("", [texto_revision])
    await aviso.edit_text(bloques[0].strip(), reply_markup=teclado if len(bloques) == 1 else None)
    for i, b in enumerate(bloques[1:], start=1):
        await mensaje.reply_text(b.strip(), reply_markup=teclado if i == len(bloques) - 1 else None)
    return estado

async def cargar_confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return ConversationHandler.END
    accion = query.data
    nuevos = list(context.user_data.get("cg_validos") or [])
    actualizar = list(context.user_data.get("cg_actualizables") or [])
    if accion == "cg_auto": nuevos += list(context.user_data.get("cg_reasignables") or [])
    if accion == "cg_upd": nuevos = []
    if accion in ("cg_si", "cg_auto"): actualizar = []
    nuevos.sort(key=lambda it: it["nro"]); actualizar.sort(key=lambda it: it["nro"])
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])
    if not nuevos and not actualizar:
        await query.edit_message_text("No hay registros para cargar.", reply_markup=btn_volver); return ConversationHandler.END
    await query.edit_message_text(f"⏳ Procesando {len(nuevos)} nuevo(s) y {len(actualizar)} actualización(es)...")
    texto, hubo_fallos = "", False
    if nuevos:
        ok, fallos, asignados = await asyncio.to_thread(ejecutar_carga, nuevos, usuario)
        hubo_fallos = hubo_fallos or bool(fallos)
        texto += f"{'✅' if not fallos else '⚠️'} Nuevos guardados: {ok} de {len(nuevos)}."
        if fallos: texto += "\nNo se guardaron:\n" + _lista_corta(fallos, lambda e: f"• Línea {e[0]}: {e[1]}", 15)
        if asignados:
            texto += "\n\n🔢 N° General asignados automáticamente:\n" + _lista_corta(asignados, lambda a: f"• Línea {a[0]} → {a[1]}" + (f" (el {a[2]} estaba repetido)" if a[2] else ""), 20)
    if actualizar:
        ok_u, fallos_u = await asyncio.to_thread(ejecutar_actualizacion, actualizar, usuario)
        hubo_fallos = hubo_fallos or bool(fallos_u)
        texto += ("\n\n" if texto else "") + f"{'🔄' if not fallos_u else '⚠️'} Actualizados: {ok_u} de {len(actualizar)}."
        if fallos_u: texto += "\nNo se actualizaron:\n" + _lista_corta(fallos_u, lambda e: f"• Línea {e[0]}: {e[1]}", 15)
    if not hubo_fallos: texto += "\n\nYa puedes vaciar la lista en el formulario sin internet."
    await query.edit_message_text(texto, reply_markup=btn_volver)
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

# --- Formulario para registrar SIN internet (se instala en el celular y funciona sin conexión) ---
OFFLINE_HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#1565c0">
<link rel="manifest" href="/offline/manifest.json">
<link rel="icon" href="/offline/icon.png">
<title>Registro sin internet</title>
<style>
  :root { --bg:#f4f6fa; --card:#fff; --tx:#1b1f24; --bd:#cfd6df; --az:#1565c0; --gr:#2e7d32; --rj:#c62828; --am:#ef6c00; }
  @media (prefers-color-scheme: dark) { :root { --bg:#10151b; --card:#1b232c; --tx:#e8ecf1; --bd:#33414f; --az:#64a8ff; --gr:#66bb6a; --rj:#ef6f6c; --am:#ffb74d; } }
  * { box-sizing: border-box; }
  body { margin:0; padding:12px 12px 90px; background:var(--bg); color:var(--tx); font:16px/1.4 system-ui, sans-serif; }
  h1 { font-size:20px; margin:4px 0 8px; }
  h2 { font-size:17px; margin:20px 0 8px; }
  #estado { padding:8px 12px; border-radius:8px; font-size:14px; margin-bottom:10px; }
  .on { background:#e8f5e9; color:#1b5e20; } .off { background:#fff3e0; color:#e65100; }
  .tabs { display:flex; gap:8px; margin-bottom:10px; }
  .tabs button { flex:1; padding:10px; border:2px solid var(--az); background:transparent; color:var(--az); border-radius:10px; font-weight:600; font-size:15px; }
  .tabs button.act { background:var(--az); color:#fff; }
  .card { background:var(--card); border:1px solid var(--bd); border-radius:12px; padding:12px; margin-bottom:10px; }
  label { display:block; font-size:13px; margin:8px 0 2px; opacity:.85; }
  input, select { width:100%; padding:10px; font-size:16px; border:1px solid var(--bd); border-radius:8px; background:var(--bg); color:var(--tx); }
  .fila2 { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  button.btn { width:100%; padding:13px; margin-top:10px; border:0; border-radius:10px; font-size:16px; font-weight:600; background:var(--az); color:#fff; }
  button.sec { background:transparent; color:var(--az); border:2px solid var(--az); }
  button.pel { background:transparent; color:var(--rj); border:2px solid var(--rj); }
  .item { display:flex; gap:8px; align-items:center; justify-content:space-between; }
  .item small { display:block; opacity:.8; }
  .item button { border:1px solid var(--bd); background:transparent; color:var(--tx); border-radius:8px; padding:8px 10px; font-size:16px; }
  .aviso { color:var(--am); font-size:13px; }
  .error { color:var(--rj); font-size:14px; margin-top:8px; min-height:18px; }
  .ok { color:var(--gr); font-size:14px; }
  textarea { width:100%; min-height:90px; font:13px monospace; background:var(--bg); color:var(--tx); border:1px solid var(--bd); border-radius:8px; padding:8px; }
  .nota { font-size:13px; opacity:.8; }
  .barra { display:flex; gap:8px; margin-bottom:8px; }
  button.chico { margin-top:0; padding:9px; font-size:14px; }
  .item input[type=checkbox] { width:22px; height:22px; flex:0 0 auto; }
  .item .cuerpo { flex:1; min-width:0; }
  .item .acc { display:flex; gap:6px; flex:0 0 auto; }

  /* ---- Etiquetas (misma medida que el PDF del bot: 3 por hoja carta) ---- */
  #zonaImpresion { display:none; }
  .hoja { width:215.9mm; height:279.4mm; padding:12mm 10mm 0; box-sizing:border-box; break-after:page; page-break-after:always; overflow:hidden; }
  .hoja:last-child { break-after:auto; page-break-after:auto; }
  .etq-fila { display:flex; width:195.9mm; height:70mm; margin-bottom:14mm; }
  .etq { position:relative; height:70mm; border:.8mm solid #000; box-sizing:border-box; color:#000; background:#fff; font-family:Helvetica, Arial, sans-serif; overflow:hidden; }
  .etq.ancha { width:121.5mm; } .etq.angosta { width:62.7mm; }
  .corte { width:11.8mm; position:relative; }
  .corte::after { content:""; position:absolute; left:50%; top:0; bottom:0; border-left:.5mm dashed #000; }
  .etq .hd { position:absolute; left:0; right:0; top:0; text-align:center; font-weight:bold; background:#ddd; border-bottom:.8mm solid #000; white-space:nowrap; overflow:hidden; -webkit-print-color-adjust:exact; print-color-adjust:exact; }
  .etq .del, .etq .al, .etq .no, .etq .gr, .etq .ub { position:absolute; left:0; right:0; white-space:nowrap; }
  .etq .ln1, .etq .ln2 { position:absolute; left:0; right:0; height:0; border-top:.8mm solid #000; }
  .ancha .hd { height:12mm; line-height:11mm; font-size:20pt; }
  .ancha .del { top:14mm; padding-left:3mm; font-size:16pt; font-weight:bold; }
  .ancha .al { top:22mm; padding-left:3mm; font-size:16pt; font-weight:bold; }
  .ancha .ln1 { top:30mm; } .ancha .ln2 { top:58mm; }
  .ancha .no { top:33mm; text-align:center; font-size:38pt; font-weight:bold; line-height:18mm; }
  .ancha .gr { top:52mm; text-align:right; padding-right:3mm; font-size:9pt; color:#555; }
  .ancha .ub { top:60mm; text-align:center; font-size:14pt; }
  .angosta .hd { height:10mm; line-height:9mm; font-size:11pt; }
  .angosta .del { top:12mm; text-align:center; font-size:12pt; font-weight:bold; }
  .angosta .al { top:18mm; text-align:center; font-size:12pt; font-weight:bold; }
  .angosta .ln1 { top:25mm; } .angosta .ln2 { top:52mm; }
  .angosta .no { top:28mm; text-align:center; font-size:30pt; font-weight:bold; line-height:15mm; }
  .angosta .gr { top:45mm; text-align:right; padding-right:2mm; font-size:8pt; color:#555; }
  .angosta .ub { top:54mm; text-align:center; font-size:10pt; }
  @media print {
    @page { size:letter portrait; margin:0; }
    html, body { background:#fff !important; color:#000 !important; padding:0 !important; margin:0 !important; }
    body > *:not(#zonaImpresion) { display:none !important; }
    #zonaImpresion { display:block; }
  }
</style>
</head>
<body>
<h1>📦 Registro sin internet</h1>
<div id="estado" class="on">Conectado</div>

<div class="tabs">
  <button id="tabE" class="act">Enrolamiento DPI</button>
  <button id="tabV">Eventos SIRECI</button>
</div>

<div class="card">
  <div id="form"></div>
  <div id="error" class="error"></div>
  <button class="btn" id="agregar">➕ Guardar en el celular</button>
  <button class="btn sec" id="cancelarEdicion" style="display:none">Cancelar edición</button>
  <p class="nota">Los datos se guardan en este celular. Al tener internet, envíalos al bot con el botón de abajo.</p>
</div>
<div id="guardado"></div>

<h2>Pendientes de enviar (<span id="n">0</span>)</h2>
<div class="barra"><button class="btn sec chico" id="selTodo">Seleccionar todas</button><button class="btn sec chico" id="selNada">Ninguna</button></div>
<div id="lista"></div>

<div class="card">
  <button class="btn" id="imprimirSel">🏷️ Imprimir etiquetas seleccionadas (0)</button>
  <p class="nota">Se imprimen 3 etiquetas por hoja carta (ancha + angosta). En la vista de impresión elige tu impresora o «Guardar como PDF». Si dejaste el N° General vacío, la etiqueta mostrará «-».</p>
</div>

<div class="card">
  <button class="btn" id="compartir">📤 Enviar al bot (compartir)</button>
  <button class="btn sec" id="copiar">📋 Copiar texto</button>
  <button class="btn sec" id="descargar">💾 Descargar archivo</button>
  <div id="msg" class="ok"></div>
  <p class="nota">En Telegram, abre el bot, usa <b>/cargar</b> y pega el texto (o sube el archivo). Cuando el bot confirme la carga, vuelve aquí y pulsa «Vaciar lista». Si corriges un dato (por ejemplo el nivel) de un paquete que ya subiste, vuelve a enviarlo: el bot te propondrá actualizarlo.</p>
  <button class="btn pel" id="vaciar">🗑️ Vaciar lista (ya enviada)</button>
</div>

<div id="zonaImpresion"></div>

<script>
/*PURAS*/
const NIVELES = 4;
function siguienteUbic(o) {
  const p = parseInt(o.pila, 10);
  if (isNaN(p)) return { est: o.est, fila: o.fila, col: o.col, pila: o.pila };
  if (String(o.fila) === '0' || p < NIVELES) return { est: o.est, fila: o.fila, col: o.col, pila: String(p + 1) };
  const cols = (o.col === 'E' || o.col === 'F') ? 'ABCDEF' : 'ABCD';
  const i = cols.indexOf(o.col);
  if (i >= 0 && i + 1 < cols.length) return { est: o.est, fila: o.fila, col: cols[i + 1], pila: '1' };
  return { est: o.est, fila: String((parseInt(o.fila, 10) || 0) + 1), col: 'A', pila: '1' };
}
function limpio(v) { return String(v === undefined || v === null ? '' : v).replace(/[;\n\r\t|]/g, ' ').trim(); }
function linea(it) {
  const d = it.datos;
  if (it.tipo === 'E') return ['E', d.usuario, d.del, d.al, d.paq, d.est, d.fila, d.col, d.pila, d.gen || ''].map(limpio).join(';');
  return ['V', d.tipo, d.anio, d.paq, d.ini, d.fin, d.est, d.fila, d.col, d.pila, d.gen || ''].map(limpio).join(';');
}
function sugerirSiguiente(tipo, d) {
  const u = siguienteUbic(d);
  const gen = /^\d+$/.test(String(d.gen || '').trim()) ? String(parseInt(d.gen, 10) + 1) : '';
  if (tipo === 'E') return { usuario: d.usuario, paq: String((parseInt(d.paq, 10) || 0) + 1), est: u.est, fila: u.fila, col: u.col, pila: u.pila, gen: gen };
  return { tipo: d.tipo, anio: d.anio, paq: String((parseInt(d.paq, 10) || 0) + 1), ini: String((parseInt(d.fin, 10) || 0) + 1), est: u.est, fila: u.fila, col: u.col, pila: u.pila, gen: gen };
}
function validar(tipo, d) {
  const entero = (v) => /^\d+$/.test(String(v || '').trim());
  if (!String(d.est || '').trim()) return 'Falta la estantería.';
  if (!entero(d.fila)) return 'La fila debe ser un número (0 o más).';
  if (!/^[A-Fa-f]$/.test(String(d.col || '').trim())) return 'La columna debe ser una letra de A a F.';
  if (!entero(d.pila) || parseInt(d.pila, 10) < 1) return 'La pila / nivel debe ser un número (1 o más).';
  if (!entero(d.paq) || parseInt(d.paq, 10) < 1) return 'El N° de paquete debe ser un número.';
  if (String(d.gen || '').trim() && !entero(d.gen)) return 'El N° General debe ser un número o quedar vacío.';
  if (tipo === 'E') {
    if (!String(d.usuario || '').trim()) return 'Falta el usuario (DPI o código).';
    if (!d.del || !d.al) return 'Faltan las fechas DEL y AL.';
    if (d.al < d.del) return 'La fecha AL no puede ser anterior a la fecha DEL.';
  } else {
    if (!d.tipo) return 'Falta el tipo de evento.';
    if (!entero(d.anio) || String(d.anio).length !== 4) return 'El año debe tener 4 dígitos.';
    if (!entero(d.ini) || !entero(d.fin)) return 'Los números de evento DEL y AL deben ser números.';
    if (parseInt(d.fin, 10) < parseInt(d.ini, 10)) return 'El evento AL no puede ser menor que el DEL.';
  }
  return '';
}
function esc(v) { return String(v === undefined || v === null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
function fechaDMA(f) { const p = String(f || '').split('-'); return p.length === 3 ? p[2] + '/' + p[1] + '/' + p[0] : String(f || ''); }
function etiquetaHTML(it) {
  const d = it.datos, gen = d.gen ? esc(d.gen) : '-';
  const ubic = 'E' + esc(d.est) + '-F' + esc(d.fila) + '-' + esc(String(d.col).toUpperCase()) + '-P' + esc(d.pila);
  let enc, del, al;
  if (it.tipo === 'E') { enc = esc(d.usuario); del = esc(fechaDMA(d.del)); al = esc(fechaDMA(d.al)); }
  else { enc = esc(String(d.tipo).toUpperCase() + ' - ' + d.anio); del = esc(d.ini); al = esc(d.fin); }
  const no = 'No. ' + esc(d.paq);
  const cuerpo = (clase, grLabel, ubLabel) => '<div class="etq ' + clase + '"><div class="hd">' + enc + '</div><div class="del">DEL: ' + del + '</div><div class="al">AL: ' + al + '</div><div class="ln1"></div><div class="no">' + no + '</div><div class="gr">' + grLabel + gen + '</div><div class="ln2"></div><div class="ub">' + ubLabel + ubic + '</div></div>';
  return '<div class="etq-fila">' + cuerpo('ancha', 'Gral. N° ', 'Ubicación: ') + '<div class="corte"></div>' + cuerpo('angosta', 'Gral. ', 'Ubic: ') + '</div>';
}
function hojasHTML(lista) {
  let out = '';
  for (let i = 0; i < lista.length; i += 3) out += '<div class="hoja">' + lista.slice(i, i + 3).map(etiquetaHTML).join('') + '</div>';
  return out;
}
/*FIN_PURAS*/

const KEY = 'atestados_pendientes_v1';
const KEY_BOR = 'atestados_borradores_v1';
const CAMPOS = {
  E: [
    { id: 'usuario', et: 'Usuario (DPI o código)', t: 'text' },
    { id: 'del', et: 'Fecha DEL', t: 'date', mitad: true }, { id: 'al', et: 'Fecha AL', t: 'date', mitad: true },
    { id: 'paq', et: 'N° de paquete', t: 'number', mitad: true }, { id: 'gen', et: 'N° General (vacío = lo asigna el bot)', t: 'number', mitad: true, ph: 'automático' },
    { id: 'est', et: 'Estantería', t: 'text', mitad: true }, { id: 'fila', et: 'Fila', t: 'number', mitad: true },
    { id: 'col', et: 'Columna', t: 'select', op: ['A', 'B', 'C', 'D', 'E', 'F'], mitad: true }, { id: 'pila', et: 'Pila / nivel', t: 'number', mitad: true }
  ],
  V: [
    { id: 'tipo', et: 'Tipo de evento', t: 'select', op: ['Nacimiento', 'Matrimonio', 'Defuncion', 'Divorcio', 'Negativa'] },
    { id: 'anio', et: 'Año', t: 'number', mitad: true }, { id: 'paq', et: 'N° de paquete', t: 'number', mitad: true },
    { id: 'ini', et: 'Evento DEL', t: 'number', mitad: true }, { id: 'fin', et: 'Evento AL', t: 'number', mitad: true },
    { id: 'gen', et: 'N° General (vacío = lo asigna el bot)', t: 'number', ph: 'automático' },
    { id: 'est', et: 'Estantería', t: 'text', mitad: true }, { id: 'fila', et: 'Fila', t: 'number', mitad: true },
    { id: 'col', et: 'Columna', t: 'select', op: ['A', 'B', 'C', 'D', 'E', 'F'], mitad: true }, { id: 'pila', et: 'Pila / nivel', t: 'number', mitad: true }
  ]
};
let tipo = 'E';
let items = leer(KEY, []);
let borradores = leer(KEY_BOR, { E: {}, V: {} });
let editando = null;

function leer(k, def) { try { const v = JSON.parse(localStorage.getItem(k)); return v === null ? def : v; } catch (e) { return def; } }
function guardar() { try { localStorage.setItem(KEY, JSON.stringify(items)); localStorage.setItem(KEY_BOR, JSON.stringify(borradores)); } catch (e) { mostrarError('No se pudo guardar en el celular (almacenamiento lleno o bloqueado).'); } }
const $ = (id) => document.getElementById(id);
function mostrarError(t) { $('error').textContent = t || ''; }
function msg(t) { $('msg').textContent = t || ''; }

function dibujarForm() {
  const b = borradores[tipo] || {};
  let html = '', abierto = false;
  CAMPOS[tipo].forEach((c) => {
    if (c.mitad && !abierto) { html += '<div class="fila2">'; abierto = true; }
    if (!c.mitad && abierto) { html += '</div>'; abierto = false; }
    const v = b[c.id] !== undefined ? b[c.id] : '';
    let campo;
    if (c.t === 'select') campo = '<select id="c_' + c.id + '">' + c.op.map((o) => '<option' + (o === v ? ' selected' : '') + '>' + o + '</option>').join('') + '</select>';
    else campo = '<input id="c_' + c.id + '" type="' + c.t + '"' + (c.t === 'number' ? ' inputmode="numeric" min="0"' : '') + (c.ph ? ' placeholder="' + c.ph + '"' : '') + ' value="' + String(v).replace(/"/g, '&quot;') + '" autocomplete="off">';
    html += '<div><label for="c_' + c.id + '">' + c.et + '</label>' + campo + '</div>';
  });
  if (abierto) html += '</div>';
  $('form').innerHTML = html;
}
function leerForm() {
  const d = {};
  CAMPOS[tipo].forEach((c) => { d[c.id] = ($('c_' + c.id).value || '').trim(); });
  d.col = (d.col || '').toUpperCase();
  return d;
}
function cambiarTab(t) {
  borradores[tipo] = leerForm();
  tipo = t; editando = null; $('guardado').innerHTML = '';
  $('tabE').classList.toggle('act', t === 'E'); $('tabV').classList.toggle('act', t === 'V');
  $('cancelarEdicion').style.display = 'none'; $('agregar').textContent = '➕ Guardar en el celular';
  mostrarError(''); dibujarForm();
}
function claveItem(it) { const d = it.datos; return it.tipo === 'E' ? 'E|' + d.usuario.toLowerCase() + '|' + parseInt(d.paq, 10) : 'V|' + d.tipo + '|' + d.anio + '|' + parseInt(d.paq, 10); }
function claveUbic(it) { const d = it.datos; return [d.est, parseInt(d.fila, 10), d.col.toUpperCase(), parseInt(d.pila, 10)].join('|'); }
function dibujarLista() {
  $('n').textContent = items.length;
  const seleccionadas = items.filter((it) => it.sel !== false).length;
  $('imprimirSel').textContent = '🏷️ Imprimir etiquetas seleccionadas (' + seleccionadas + ')';
  const claves = {}, ubics = {}, gens = {};
  const claveGen = (it) => (String(it.datos.gen || '').trim() ? it.tipo + '|' + parseInt(it.datos.gen, 10) : '');
  items.forEach((it) => { claves[claveItem(it)] = (claves[claveItem(it)] || 0) + 1; ubics[claveUbic(it)] = (ubics[claveUbic(it)] || 0) + 1; if (claveGen(it)) gens[claveGen(it)] = (gens[claveGen(it)] || 0) + 1; });
  $('lista').innerHTML = items.length ? '' : '<p class="nota">Aún no hay registros pendientes.</p>';
  items.forEach((it, i) => {
    const d = it.datos, av = [];
    if (claves[claveItem(it)] > 1) av.push('⚠️ Paquete repetido en la lista');
    if (ubics[claveUbic(it)] > 1) av.push('⚠️ Misma ubicación que otro pendiente');
    if (claveGen(it) && gens[claveGen(it)] > 1) av.push('⚠️ N° General repetido en la lista');
    const titulo = it.tipo === 'E' ? '👤 ' + d.usuario + ' · Paq ' + d.paq : '📖 ' + d.tipo + ' ' + d.anio + ' · Paq ' + d.paq;
    const detalle = it.tipo === 'E' ? d.del + ' → ' + d.al : 'Eventos ' + d.ini + '-' + d.fin;
    const div = document.createElement('div'); div.className = 'card item';
    div.innerHTML = '<input type="checkbox" data-s="' + i + '"' + (it.sel !== false ? ' checked' : '') + ' aria-label="Incluir en la impresión"><div class="cuerpo"><b>' + esc(titulo) + '</b><small>' + esc(detalle) + ' · E' + esc(d.est) + '-F' + esc(d.fila) + '-' + esc(d.col) + '-P' + esc(d.pila) + (d.gen ? ' · Gen ' + esc(d.gen) : ' · Gen automático') + '</small>' + av.map((a) => '<span class="aviso">' + a + '</span>').join('<br>') + '</div><div class="acc"><button data-p="' + i + '" title="Imprimir etiqueta">🏷️</button><button data-e="' + i + '">✏️</button><button data-b="' + i + '">🗑️</button></div>';
    $('lista').appendChild(div);
  });
}
function agregar() {
  const d = leerForm(), err = validar(tipo, d);
  if (err) { mostrarError(err); return; }
  if (String(d.gen || '').trim()) {
    const otro = items.findIndex((it, i) => i !== editando && it.tipo === tipo && String(it.datos.gen || '').trim() !== '' && parseInt(it.datos.gen, 10) === parseInt(d.gen, 10));
    if (otro >= 0) { mostrarError('El N° General ' + d.gen + ' ya lo usaste en otro pendiente (Paq ' + items[otro].datos.paq + '). Cambia el número o déjalo vacío para que lo asigne el bot.'); return; }
  }
  mostrarError(''); msg('');
  let indice;
  if (editando !== null) { items[editando] = { tipo, datos: d, ts: items[editando].ts, sel: items[editando].sel }; indice = editando; editando = null; }
  else { items.push({ tipo, datos: d, ts: Date.now(), sel: true }); indice = items.length - 1; }
  borradores[tipo] = sugerirSiguiente(tipo, d);
  guardar(); $('cancelarEdicion').style.display = 'none'; $('agregar').textContent = '➕ Guardar en el celular';
  dibujarForm(); dibujarLista();
  $('guardado').innerHTML = '<div class="card"><span class="ok">✅ Guardado en el celular.</span><button class="btn" id="impUltima">🏷️ Imprimir la etiqueta de este paquete</button></div>';
  $('impUltima').onclick = () => imprimir([items[indice]]);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function imprimir(lista) {
  lista = lista.filter(Boolean);
  if (!lista.length) { msg('Selecciona al menos un registro para imprimir.'); return; }
  msg(''); $('zonaImpresion').innerHTML = hojasHTML(lista);
  setTimeout(() => window.print(), 150);
}
function textoExport() { return items.map(linea).join('\n'); }
async function copiar() {
  const t = textoExport(); if (!t) { msg('No hay registros pendientes.'); return; }
  try { await navigator.clipboard.writeText(t); msg('✅ Copiado. Pégalo en el bot con /cargar.'); }
  catch (e) { const ta = document.createElement('textarea'); ta.value = t; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); msg('✅ Copiado. Pégalo en el bot con /cargar.'); } catch (e2) { msg('No se pudo copiar; usa «Descargar archivo».'); } ta.remove(); }
}
async function compartir() {
  const t = textoExport(); if (!t) { msg('No hay registros pendientes.'); return; }
  if (navigator.share) { try { await navigator.share({ text: t }); msg('Si lo enviaste al bot, usa /cargar y pega el mensaje.'); return; } catch (e) { if (e && e.name === 'AbortError') return; } }
  copiar();
}
function descargar() {
  const t = textoExport(); if (!t) { msg('No hay registros pendientes.'); return; }
  const f = new Date(), p = (n) => String(n).padStart(2, '0');
  const nombre = 'pendientes_' + f.getFullYear() + p(f.getMonth() + 1) + p(f.getDate()) + '_' + p(f.getHours()) + p(f.getMinutes()) + '.txt';
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([t], { type: 'text/plain' })); a.download = nombre; document.body.appendChild(a); a.click(); a.remove();
  msg('✅ Archivo descargado. Súbelo al bot después de /cargar.');
}
function vaciar() {
  if (!items.length) return;
  if (confirm('¿Vaciar la lista? Hazlo solo si el bot ya confirmó la carga.')) { items = []; guardar(); dibujarLista(); msg(''); }
}
function estadoRed() { const on = navigator.onLine; const e = $('estado'); e.className = on ? 'on' : 'off'; e.textContent = on ? 'Conectado: ya puedes enviar los pendientes al bot' : 'Sin internet: guarda aquí los registros y envíalos después'; }

$('tabE').onclick = () => cambiarTab('E'); $('tabV').onclick = () => cambiarTab('V');
$('imprimirSel').onclick = () => imprimir(items.filter((it) => it.sel !== false));
$('selTodo').onclick = () => { items.forEach((it) => { it.sel = true; }); guardar(); dibujarLista(); };
$('selNada').onclick = () => { items.forEach((it) => { it.sel = false; }); guardar(); dibujarLista(); };
$('agregar').onclick = agregar; $('copiar').onclick = copiar; $('compartir').onclick = compartir; $('descargar').onclick = descargar; $('vaciar').onclick = vaciar;
$('cancelarEdicion').onclick = () => { editando = null; $('cancelarEdicion').style.display = 'none'; $('agregar').textContent = '➕ Guardar en el celular'; mostrarError(''); dibujarForm(); };
$('lista').onclick = (ev) => {
  const bp = ev.target.getAttribute('data-p');
  if (bp !== null) { imprimir([items[+bp]]); return; }
  const be = ev.target.getAttribute('data-e'), bb = ev.target.getAttribute('data-b');
  if (be !== null) { const it = items[+be]; if (it.tipo !== tipo) cambiarTab(it.tipo); borradores[tipo] = Object.assign({}, it.datos); editando = +be; dibujarForm(); $('cancelarEdicion').style.display = 'block'; $('agregar').textContent = '💾 Guardar cambios'; window.scrollTo({ top: 0, behavior: 'smooth' }); }
  if (bb !== null && confirm('¿Eliminar este registro pendiente?')) { items.splice(+bb, 1); guardar(); dibujarLista(); }
};
$('lista').onchange = (ev) => { const s = ev.target.getAttribute('data-s'); if (s !== null) { items[+s].sel = ev.target.checked; guardar(); dibujarLista(); } };
window.addEventListener('online', estadoRed); window.addEventListener('offline', estadoRed);
estadoRed(); dibujarForm(); dibujarLista();
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/offline/sw.js', { scope: '/offline/' }).catch(function () {});
</script>
</body>
</html>
"""

OFFLINE_SW = """
const C = 'atestados-offline-v1';
const ARCHIVOS = ['/offline/', '/offline/manifest.json', '/offline/icon.png'];
self.addEventListener('install', function (e) { e.waitUntil(caches.open(C).then(function (c) { return c.addAll(ARCHIVOS); }).then(function () { return self.skipWaiting(); })); });
self.addEventListener('activate', function (e) { e.waitUntil(caches.keys().then(function (ks) { return Promise.all(ks.filter(function (k) { return k !== C; }).map(function (k) { return caches.delete(k); })); }).then(function () { return self.clients.claim(); })); });
self.addEventListener('fetch', function (e) {
  if (e.request.method !== 'GET') return;
  e.respondWith(fetch(e.request).then(function (r) { var cp = r.clone(); caches.open(C).then(function (c) { c.put(e.request, cp); }); return r; })
    .catch(function () { return caches.match(e.request).then(function (r) { return r || caches.match('/offline/'); }); }));
});
"""

@app_web.route('/offline')
@app_web.route('/offline/')
def offline_pagina():
    r = Response(OFFLINE_HTML, mimetype="text/html"); r.headers["Cache-Control"] = "no-cache"; return r

@app_web.route('/offline/manifest.json')
def offline_manifest():
    datos = {"name": "Registro sin internet - Atestados", "short_name": "Atestados", "start_url": "/offline/", "scope": "/offline/",
             "display": "standalone", "background_color": "#f4f6fa", "theme_color": "#1565c0",
             "icons": [{"src": "/offline/icon.png", "sizes": "192x192", "type": "image/png", "purpose": "any"}]}
    return Response(json.dumps(datos), mimetype="application/manifest+json")

@app_web.route('/offline/sw.js')
def offline_sw():
    r = Response(OFFLINE_SW, mimetype="application/javascript")
    r.headers["Service-Worker-Allowed"] = "/offline/"; r.headers["Cache-Control"] = "no-cache"; return r

@app_web.route('/offline/icon.png')
def offline_icono():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (192, 192), (21, 101, 192)); d = ImageDraw.Draw(img)
    d.rounded_rectangle([34, 52, 158, 150], radius=10, fill=(255, 255, 255))
    d.rectangle([34, 52, 158, 76], fill=(187, 222, 251))
    d.text((96, 114), "AT", font=_fuente(44, True), fill=(21, 101, 192), anchor="mm")
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return Response(buf.getvalue(), mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})

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
    inicializar_papelera_bitacora()
    
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(TypeHandler(Update, fijar_actor), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("cargar", cargar_inicio), CallbackQueryHandler(cargar_inicio, pattern="^accion_cargar$")],
        states={
            CG_DATOS: [MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.Document.ALL, cargar_recibir)],
            CG_CONFIRMAR: [CallbackQueryHandler(cargar_confirmar, pattern="^cg_(si|auto|upd|todo)$"),
                           MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.Document.ALL, cargar_recibir)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar), CallbackQueryHandler(cancelar_a_menu, pattern="^menu_inicio$")],
        per_message=False,
    ))
    app.add_handler(CommandHandler("mapa", mapa_cmd))
    app.add_handler(CallbackQueryHandler(mapa_cb, pattern="^mapa_"))
    app.add_handler(CommandHandler("backup", backup_bd))
    app.add_handler(CommandHandler("historial", historial_cmd))
    app.add_handler(CallbackQueryHandler(papelera_cb, pattern="^pap_"))
    app.add_handler(CallbackQueryHandler(bitacora_cb, pattern="^bit_(menu|ultimos)$"))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(bitacora_buscar_inicio, pattern="^bit_buscar$")],
        states={BIT_TEXTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, bitacora_buscar_texto)]},
        fallbacks=[CommandHandler("cancelar", cancelar), CallbackQueryHandler(cancelar_a_menu, pattern="^menu_inicio$")],
        per_message=False,
    ))
    app.add_handler(CallbackQueryHandler(backup_bd, pattern="^accion_backup$"))
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
        entry_points=[CommandHandler("registrar", registrar_inicio), CallbackQueryHandler(registrar_inicio_cb, pattern="^enrol_registrar$"), CallbackQueryHandler(registrar_otro_cb, pattern="^pe_otro_")],
        states={
            REG_DPI: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_dpi)],
            REG_DEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_del)],
            REG_AL: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_al)],
            REG_NUM: [CallbackQueryHandler(reg_num_cb, pattern="^rsugnum_"),
                      MessageHandler(filters.TEXT & ~filters.COMMAND, reg_num)],
            REG_UBIC: [CallbackQueryHandler(reg_ubic_cb, pattern="^rsugubi_")],
            REG_EST: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_est)],
            REG_FIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_fil)],
            REG_COL: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_col)],
            REG_PIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_pil)],
            REG_GENERAL: [CallbackQueryHandler(reg_general_cb, pattern="^rsuggen_"),
                          MessageHandler(filters.TEXT & ~filters.COMMAND, reg_general)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar), CallbackQueryHandler(cancelar_a_menu, pattern="^menu_inicio$")],
        per_message=False,
    )
    app.add_handler(conv_registro)

    # 2. Flujo de Registro Eventos
    conv_evento = ConversationHandler(
        entry_points=[CallbackQueryHandler(inicio_registrar_evento, pattern="^accion_registrar_evento$"), CallbackQueryHandler(registrar_evento_otro_cb, pattern="^ev_otro_")],
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
            EV_UBIC: [CallbackQueryHandler(ev_ubic_cb, pattern="^evsugubi_")],
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
        entry_points=[CallbackQueryHandler(inicio_editar_evento, pattern="^accion_(editar|etiqueta|eliminar)_evento$"), CallbackQueryHandler(post_editar_evento, pattern="^ev_ed_")],
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
        entry_points=[CallbackQueryHandler(enrol_inicio, pattern="^enrol_(buscar|general|fecha|usuario)$")],
        states={ENROL_ARGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, enrol_recibir)]},
        fallbacks=[CommandHandler("cancelar", cancelar), CallbackQueryHandler(cancelar_a_menu, pattern="^menu_inicio$")],
        per_message=False,
    )
    app.add_handler(conv_enrol)

    conv_enrol_sel = ConversationHandler(
        entry_points=[CallbackQueryHandler(enrol_sel_inicio, pattern="^enrol_(editar|etiqueta|eliminar)$"), CallbackQueryHandler(post_editar_enrol, pattern="^pe_ed_")],
        states={
            EP_MODO: [CallbackQueryHandler(enrol_sel_modo, pattern="^epmodo_")],
            EP_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, enrol_sel_valor)],
            EP_ELEGIR: [CallbackQueryHandler(enrol_sel_elegir, pattern="^epsel_")],
            EP_CAMPO: [CallbackQueryHandler(enrol_sel_campo, pattern="^epcampo_")],
            EP_NUEVO: [MessageHandler(filters.TEXT & ~filters.COMMAND, enrol_sel_nuevo)],
            EP_CONFIRMAR: [CallbackQueryHandler(enrol_sel_eliminar, pattern="^epdel_si$")],
        },
        fallbacks=[CommandHandler("cancelar", cancelar), CallbackQueryHandler(cancelar_a_menu, pattern="^menu_inicio$")],
        per_message=False,
    )
    app.add_handler(conv_enrol_sel)
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(lote_inicio, pattern="^lote_(enrol|ev)$")],
        states={
            LT_MODO: [CallbackQueryHandler(lote_modo, pattern="^ltmodo_")],
            LT_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, lote_valor)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar), CallbackQueryHandler(cancelar_a_menu, pattern="^menu_inicio$")],
        per_message=False,
    ))
    app.add_handler(CallbackQueryHandler(post_imprimir_enrol, pattern="^pe_pr_"))
    app.add_handler(CallbackQueryHandler(post_imprimir_evento, pattern="^ev_pr_"))
    app.add_handler(CallbackQueryHandler(enrol_total, pattern="^enrol_total$"))
    app.add_handler(CallbackQueryHandler(totales_anio, pattern="^accion_totales_anio$"))

    # 6. MANEJADOR DE BOTONES DEL MENÚ
    app.add_handler(CallbackQueryHandler(botones_menu, pattern="^menu_"))
    
    print("✅ Bot activo. Presiona /start en Telegram.")
    app.add_error_handler(manejar_error)
    app.run_polling(drop_pending_updates=True)
