import os
import tempfile
import threading
import libsql_client
from flask import Flask
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
(EV_TIPO, EV_ANIO, EV_NUM, EV_FOLIO_INI, EV_FOLIO_FIN, EV_EST, EV_FIL, EV_COL, EV_PIL) = range(20, 29)
(EV_BUS_FOLIO,) = range(30, 31)

# ==========================================
# CONEXIÓN A LA BASE DE DATOS (TURSO)
# ==========================================
def conectar_db():
    url_https = TURSO_URL.replace("libsql://", "https://")
    return libsql_client.create_client_sync(url_https, auth_token=TURSO_TOKEN)

# ==========================================
# INICIALIZACIÓN DE TABLAS
# ==========================================
def inicializar_tabla_telegram():
    client = conectar_db()
    # Agregamos la columna 'area' (por defecto enrolamiento)
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
    
    # Intentar agregar la columna si la tabla ya existía de antes (por si acaso)
    try:
        client.execute("ALTER TABLE usuarios_telegram ADD COLUMN area TEXT DEFAULT 'enrolamiento'")
    except:
        pass # Si ya existe, no hace nada

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
            estanteria TEXT,
            fila TEXT,
            columna TEXT,
            pila TEXT,
            registrado_por TEXT,
            fecha_registro TEXT,
            UNIQUE(tipo_evento, anio, num_paquete)
        )
    """)

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
            return {
                "telegram_id": fila[0],
                "nombre": fila[1],
                "rol": fila[2],
                "area": fila[3] if fila[3] else "enrolamiento",
                "dpi_asignado": fila[4]
            }
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
    est = datos.get("estanteria", "")
    fil = datos.get("fila", "")
    col = datos.get("columna", "")
    pil = datos.get("pila", "")
    datos["ubicacion_armada"] = f"E{est}-F{fil}-{col}-P{pil}"
    return datos

def buscar_por_dpi_y_numero(dpi, num_paquete):
    client = conectar_db()
    columnas = obtener_columnas()
    res = client.execute("SELECT * FROM paquetes WHERE usuario_codigo = ? AND num_paquete = ?", (dpi, num_paquete))
    if res.rows:
        return formatear_paquete(res.rows[0], columnas)
    return None

def buscar_por_numero_general(num_general):
    client = conectar_db()
    columnas = obtener_columnas()
    res = client.execute("SELECT * FROM paquetes WHERE numero_general = ?", (num_general,))
    if res.rows:
        return formatear_paquete(res.rows[0], columnas)
    return None

def buscar_por_usuario(dpi):
    client = conectar_db()
    columnas = obtener_columnas()
    res = client.execute("SELECT * FROM paquetes WHERE usuario_codigo = ? ORDER BY num_paquete", (dpi,))
    return [formatear_paquete(f, columnas) for f in res.rows]

def buscar_por_dpi_y_fecha(dpi, fecha):
    client = conectar_db()
    columnas = obtener_columnas()
    res = client.execute(
        "SELECT * FROM paquetes WHERE usuario_codigo = ? AND fecha_del <= ? AND fecha_al >= ? ORDER BY num_paquete",
        (dpi, fecha, fecha)
    )
    return [formatear_paquete(f, columnas) for f in res.rows]

def contar_paquetes():
    client = conectar_db()
    res = client.execute("SELECT COUNT(*) FROM paquetes")
    return res.rows[0][0]

def usuario_existe(dpi):
    try:
        client = conectar_db()
        res = client.execute("SELECT COUNT(*) FROM usuarios WHERE dpi = ?", (dpi,))
        return res.rows[0][0] > 0
    except:
        return False

def insertar_paquete(dpi, num, f_del, f_al, est, fil, col, pil, general=None):
    client = conectar_db()
    try:
        if general is None or str(general).lower() == "auto":
            res = client.execute("SELECT MAX(numero_general) FROM paquetes")
            max_gen = res.rows[0][0] or 0
            nuevo_gen = max_gen + 1
        elif str(general).lower() in ("blanco", "null", ""):
            nuevo_gen = None
        else:
            try:
                nuevo_gen = int(general)
                res = client.execute("SELECT COUNT(*) FROM paquetes WHERE numero_general = ?", (nuevo_gen,))
                if res.rows[0][0] > 0:
                    return (None, f"El número general {nuevo_gen} ya está en uso")
            except ValueError:
                return (None, f"'{general}' no es un número válido")
        
        client.execute("""
            INSERT INTO paquetes 
            (usuario_codigo, num_paquete, fecha_del, fecha_al, estanteria, fila, columna, pila, activo, numero_general)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (dpi, num, f_del, f_al, est, fil, col, pil, nuevo_gen))
        return (nuevo_gen, None)
    except Exception as e:
        return (None, f"Error: {e}")

def eliminar_paquete(dpi, num):
    client = conectar_db()
    client.execute("DELETE FROM paquetes WHERE usuario_codigo = ? AND num_paquete = ?", (dpi, num))
    return True

def actualizar_paquete(dpi, num, campo, valor):
    campos_validos = ["fecha_del", "fecha_al", "estanteria", "fila", "columna", "pila", "numero_general"]
    if campo not in campos_validos:
        return False
    client = conectar_db()
    try:
        if campo == "numero_general":
            if valor.lower() in ("blanco", "null", ""):
                valor_final = None
            else:
                valor_final = int(valor)
        else:
            valor_final = valor
        
        client.execute(f"UPDATE paquetes SET {campo} = ? WHERE usuario_codigo = ? AND num_paquete = ?",
                       (valor_final, dpi, num))
        return True
    except Exception as e:
        print(f"Error actualizar: {e}")
        return False

def agregar_usuario(dpi, nombre):
    client = conectar_db()
    try:
        client.execute("INSERT INTO usuarios (dpi, nombre) VALUES (?, ?)", (dpi, nombre))
        return True
    except:
        return False

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
def insertar_evento(tipo, anio, num_paq, folio_ini, folio_fin, est, fil, col, pil, operador):
    client = conectar_db()
    try:
        client.execute("""
            INSERT INTO eventos (tipo_evento, anio, num_paquete, folio_inicio, folio_fin, estanteria, fila, columna, pila, registrado_por, fecha_registro)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tipo, anio, num_paq, folio_ini, folio_fin, est, fil, col, pil, operador, datetime.now().strftime("%Y-%m-%d %H:%M")))
        return True, None
    except Exception as e:
        return False, str(e)

def buscar_evento_por_folio(folio):
    client = conectar_db()
    res = client.execute("""
        SELECT * FROM eventos 
        WHERE folio_inicio <= ? AND folio_fin >= ?
    """, (folio, folio))
    if res.rows:
        columnas = [col[0] for col in res.description]
        return dict(zip(columnas, res.rows[0]))
    return None

# ==========================================
# UTILIDADES
# ==========================================
def normalizar_fecha(fecha_str):
    fecha_str = fecha_str.replace("/", "-")
    partes = fecha_str.split("-")
    if len(partes) != 3:
        return None
    try:
        if len(partes[0]) == 4:
            a, m, d = int(partes[0]), int(partes[1]), int(partes[2])
        else:
            d, m, a = int(partes[0]), int(partes[1]), int(partes[2])
        return f"{a:04d}-{m:02d}-{d:02d}"
    except ValueError:
        return None

def formatear_mensaje(datos, encabezado="📦 *Paquete encontrado*"):
    num = datos.get("num_paquete", "?")
    num_gen = datos.get("numero_general")
    num_gen_texto = f"`{num_gen}`" if num_gen is not None else "_En blanco_"
    dpi = datos.get("usuario_codigo", "?")
    f_del = datos.get("fecha_del", "?")
    f_al = datos.get("fecha_al", "?")
    ubicacion = datos.get("ubicacion_armada", "?")
    return (
        f"{encabezado}\n\n"
        f"📦 N°: *{num}*\n"
        f"🔢 General: {num_gen_texto}\n"
        f"👤 Código: `{dpi}`\n"
        f"📅 Rango: {f_del} al {f_al}\n"
        f"📍 *Ubicación:* `{ubicacion}`"
    )

def generar_pdf_etiqueta(dpi, fecha_del, fecha_al, num, num_gen, ubicacion):
    try:
        from fpdf import FPDF
    except ImportError:
        return None, "No tienes 'fpdf2'."
    
    def a_formato_mostrar(fecha):
        if not fecha or fecha == "?": return fecha
        try:
            partes = fecha.split("-")
            if len(partes) == 3: return f"{partes[2]}/{partes[1]}/{partes[0]}"
        except: pass
        return fecha
    
    del_mostrar = a_formato_mostrar(fecha_del)
    al_mostrar = a_formato_mostrar(fecha_al)
    num_str = str(num)
    gral = str(num_gen) if num_gen is not None else "-"
    
    try:
        pdf = FPDF(orientation="P", unit="mm", format="Letter")
        pdf.add_page()
        pdf.set_auto_page_break(auto=False)
        
        margen_izq = 10
        margen_sup = 15
        ancho_disponible = 215.9 - 20
        ancho_ancha = ancho_disponible * 0.62
        ancho_sep = ancho_disponible * 0.06
        ancho_angosta = ancho_disponible * 0.32
        
        x_ancha = margen_izq
        x_sep = x_ancha + ancho_ancha
        x_angosta = x_sep + ancho_sep
        y_inicio = margen_sup
        ancho_alto = 70
        
        # ETIQUETA ANCHA
        pdf.set_draw_color(0, 0, 0)
        pdf.set_line_width(0.8)
        pdf.rect(x_ancha, y_inicio, ancho_ancha, ancho_alto)
        
        pdf.set_fill_color(221, 221, 221)
        pdf.rect(x_ancha, y_inicio, ancho_ancha, 12, style="F")
        pdf.set_xy(x_ancha, y_inicio + 2)
        pdf.set_font("Helvetica", "B", 20)
        pdf.cell(ancho_ancha, 8, dpi, align="C")
        
        pdf.set_line_width(0.8)
        pdf.line(x_ancha, y_inicio + 12, x_ancha + ancho_ancha, y_inicio + 12)
        
        pdf.set_xy(x_ancha + 3, y_inicio + 14)
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(ancho_ancha - 6, 8, f"DEL: {del_mostrar}", align="L")
        
        pdf.set_xy(x_ancha + 3, y_inicio + 22)
        pdf.cell(ancho_ancha - 6, 8, f"AL: {al_mostrar}", align="L")
        
        pdf.line(x_ancha, y_inicio + 30, x_ancha + ancho_ancha, y_inicio + 30)
        
        pdf.set_xy(x_ancha, y_inicio + 33)
        pdf.set_font("Helvetica", "B", 38)
        pdf.cell(ancho_ancha, 18, f"No. {num_str}", align="C")
        
        pdf.set_xy(x_ancha + 3, y_inicio + 52)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(85, 85, 85)
        pdf.cell(ancho_ancha - 6, 5, f"Gral. N° {gral}", align="R")
        pdf.set_text_color(0, 0, 0)
        
        pdf.line(x_ancha, y_inicio + 58, x_ancha + ancho_ancha, y_inicio + 58)
        
        pdf.set_xy(x_ancha, y_inicio + 60)
        pdf.set_font("Helvetica", "", 14)
        pdf.cell(ancho_ancha, 8, f"Ubicación: {ubicacion}", align="C")
        
        # LÍNEA DE CORTE
        pdf.set_line_width(0.5)
        y_actual = y_inicio
        while y_actual < y_inicio + ancho_alto:
            pdf.line(x_sep + ancho_sep / 2, y_actual, x_sep + ancho_sep / 2, y_actual + 2)
            y_actual += 4
        
        # ETIQUETA ANGOSTA
        pdf.set_line_width(0.8)
        pdf.rect(x_angosta, y_inicio, ancho_angosta, ancho_alto)
        
        pdf.set_fill_color(221, 221, 221)
        pdf.rect(x_angosta, y_inicio, ancho_angosta, 10, style="F")
        pdf.set_xy(x_angosta, y_inicio + 1.5)
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(ancho_angosta, 7, dpi, align="C")
        
        pdf.line(x_angosta, y_inicio + 10, x_angosta + ancho_angosta, y_inicio + 10)
        
        pdf.set_xy(x_angosta, y_inicio + 12)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(ancho_angosta, 6, f"DEL: {del_mostrar}", align="C")
        
        pdf.set_xy(x_angosta, y_inicio + 18)
        pdf.cell(ancho_angosta, 6, f"AL: {al_mostrar}", align="C")
        
        pdf.line(x_angosta, y_inicio + 25, x_angosta + ancho_angosta, y_inicio + 25)
        
        pdf.set_xy(x_angosta, y_inicio + 28)
        pdf.set_font("Helvetica", "B", 30)
        pdf.cell(ancho_angosta, 15, f"No. {num_str}", align="C")
        
        pdf.set_xy(x_angosta, y_inicio + 45)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(85, 85, 85)
        pdf.cell(ancho_angosta - 2, 5, f"Gral. {gral}", align="R")
        pdf.set_text_color(0, 0, 0)
        
        pdf.line(x_angosta, y_inicio + 52, x_angosta + ancho_angosta, y_inicio + 52)
        
        pdf.set_xy(x_angosta, y_inicio + 54)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(ancho_angosta, 6, f"Ubic: {ubicacion}", align="C")
        
        ruta = os.path.join(tempfile.gettempdir(), f"etiquetas_{dpi}_{num}.pdf")
        pdf.output(ruta)
        return ruta, None
    except Exception as e:
        return None, str(e)

# ==========================================
# VERIFICACIÓN DE ACCESO
# ==========================================
async def verificar_acceso(update, roles_permitidos=None):
    user_id = update.effective_user.id
    usuario = usuario_autorizado(user_id)
    
    if not usuario:
        msg = "🚫 *No estás autorizado*\n\nContacta al administrador."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg, parse_mode="Markdown")
        return None
    
    if roles_permitidos and usuario["rol"] not in roles_permitidos:
        msg = f"🚫 *No tienes permiso*\n\nTu rol es: `{usuario['rol']}`"
        if update.callback_query:
            await update.callback_query.answer("No tienes permiso para esto.", show_alert=True)
        else:
            await update.message.reply_text(msg, parse_mode="Markdown")
        return None
    
    return usuario

# ==========================================
# INTERFAZ GRÁFICA (MENÚS ADAPTADOS POR ÁREA)
# ==========================================
def obtener_menu_principal(usuario):
    rol = usuario["rol"]
    area = usuario.get("area", "enrolamiento")
    
    teclado = []
    
    # Si es admin, ve AMBOS menús (Enrolamiento y Registro)
    if rol == "admin":
        teclado.append([InlineKeyboardButton("🔍 CONSULTAS DPI (Enrolamiento)", callback_data="menu_consulta_enrol")])
        teclado.append([InlineKeyboardButton("✏️ EDICIÓN DPI (Enrolamiento)", callback_data="menu_edicion_enrol")])
        teclado.append([InlineKeyboardButton("📖 CONSULTAS EVENTOS (Registro)", callback_data="menu_consulta_reg")])
        teclado.append([InlineKeyboardButton("✏️ EDICIÓN EVENTOS (Registro)", callback_data="menu_edicion_reg")])
    
    # Si es operador u otro, solo ve el menú de su área asignada
    else:
        if area == "enrolamiento":
            teclado.append([InlineKeyboardButton("🔍 CONSULTAS DPI", callback_data="menu_consulta_enrol")])
            teclado.append([InlineKeyboardButton("✏️ EDICIÓN DPI", callback_data="menu_edicion_enrol")])
        else:
            teclado.append([InlineKeyboardButton("📖 CONSULTAS EVENTOS", callback_data="menu_consulta_reg")])
            teclado.append([InlineKeyboardButton("✏️ EDICIÓN EVENTOS", callback_data="menu_edicion_reg")])
    
    if rol in ["admin", "operador1"]:
        teclado.append([InlineKeyboardButton("👥 GESTIÓN", callback_data="menu_gestion")])
    if rol == "admin":
        teclado.append([InlineKeyboardButton("🔐 AUTORIZACIÓN", callback_data="menu_autorizacion")])
        
    teclado.append([InlineKeyboardButton("👤 MI INFORMACIÓN", callback_data="menu_info")])
    return InlineKeyboardMarkup(teclado)
    
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    mensaje = (
        f"👋 *Hola, {usuario['nombre']}*\n"
        f"Tu rol es: `{usuario['rol']}`\n"
        f"Área: `{usuario.get('area', 'enrolamiento').capitalize()}`\n\n"
        "Selecciona una categoría:"
    )
    teclado = obtener_menu_principal(usuario)
    if update.callback_query:
        await update.callback_query.edit_message_text(mensaje, reply_markup=teclado, parse_mode="Markdown")
    else:
        await update.message.reply_text(mensaje, reply_markup=teclado, parse_mode="Markdown")
        
async def botones_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    usuario = await verificar_acceso(update)
    if not usuario: return
    accion = query.data
    btn_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]])

    if accion == "menu_inicio":
        await start(update, context)
    elif accion == "menu_info":
        dpi_info = f"\n👤 DPI asignado: `{usuario['dpi_asignado']}`" if usuario['dpi_asignado'] else ""
        texto = (f"👤 *Tu información*\n\nNombre: {usuario['nombre']}\nRol: `{usuario['rol']}`\nÁrea: `{usuario.get('area', 'enrolamiento')}`\nID: `{usuario['telegram_id']}`{dpi_info}")
        await query.edit_message_text(texto, reply_markup=btn_volver, parse_mode="Markdown")
        
    # --- MENÚS DE ENROLAMIENTO ---
    elif accion == "menu_consulta_enrol":
        texto = ("🔍 *CONSULTAS DPI*\n\nComandos:\n`/buscar <DPI> <N°>`\n`/fecha <DPI> <fecha>`\n`/general <N°>`\n`/usuario <DPI>`\n`/total`")
        await query.edit_message_text(texto, reply_markup=btn_volver, parse_mode="Markdown")
    elif accion == "menu_edicion_enrol":
        texto = ("✏️ *EDICIÓN DPI*\n\nComandos:\n`/registrar` (paso a paso)\n`/editar <DPI> <N°> <campo> <valor>`\n`/eliminar <DPI> <N°>`\n`/etiqueta <DPI> <N°>`")
        await query.edit_message_text(texto, reply_markup=btn_volver, parse_mode="Markdown")
        
    # --- MENÚS DE EVENTOS (REGISTRO CIVIL) ---
    elif accion == "menu_consulta_reg":
        btn_reg = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Buscar por Folio", callback_data="accion_buscar_folio")],
            [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]
        ])
        await query.edit_message_text("📖 *CONSULTAS DE EVENTOS*\n\nSelecciona cómo buscar:", reply_markup=btn_reg, parse_mode="Markdown")
    elif accion == "menu_edicion_reg":
        btn_reg = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Registrar Evento", callback_data="accion_registrar_evento")],
            [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_inicio")]
        ])
        await query.edit_message_text("✏️ *EDICIÓN DE EVENTOS*\n\nSelecciona una acción:", reply_markup=btn_reg, parse_mode="Markdown")

    # --- OTROS ---
    elif accion == "menu_gestion":
        if usuario["rol"] not in ["admin", "operador1"]:
            await query.answer("No tienes permiso para ver esto.", show_alert=True)
            return
        texto = ("👥 *GESTIÓN*\n\n`/usuarios`\n`/agregar_usuario <DPI> <Nombre>`\n`/eliminar_usuario <DPI>`")
        await query.edit_message_text(texto, reply_markup=btn_volver, parse_mode="Markdown")
    elif accion == "menu_autorizacion":
        if usuario["rol"] != "admin":
            await query.answer("Solo el admin puede ver esto.", show_alert=True)
            return
        texto = ("🔐 *AUTORIZACIÓN*\n\n`/autorizar <ID> <Nombre> <Rol> <Área> [DPI]`\nEj: `/autorizar 123 Juan operador1 enrolamiento`\n`/lista_autorizados`")
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
        await update.message.reply_text("⚠️ Uso: `/autorizar <ID> <nombre> <rol> <area> [dpi]`\nÁreas: `enrolamiento` o `registro`", parse_mode="Markdown")
        return
    try: telegram_id = int(context.args[0])
    except: 
        await update.message.reply_text("⚠️ El ID debe ser número.")
        return
    nombre, rol, area = context.args[1], context.args[2].lower(), context.args[3].lower()
    dpi = context.args[4] if len(context.args) > 4 else None
    exito, error = agregar_autorizado(telegram_id, nombre, rol, area, dpi)
    if exito: await update.message.reply_text(f"✅ Autorizado: {nombre} como {rol} en {area}", parse_mode="Markdown")
    else: await update.message.reply_text(f"❌ {error}")

async def lista_autorizados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin"])
    if not usuario: return
    lista = listar_autorizados()
    mensaje = "👥 *Usuarios autorizados:*\n\n"
    for tg_id, nombre, rol, area, dpi, activo in lista:
        estado = "✅" if activo == 1 else "❌"
        mensaje += f"{estado} *{nombre}* | `{rol}` | `{area}` | ID: `{tg_id}`\n"
    await update.message.reply_text(mensaje, parse_mode="Markdown")

async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Uso: `/buscar <DPI> <N°>`", parse_mode="Markdown")
        return
    dpi = context.args[0].strip()
    try: num = int(context.args[1])
    except: 
        await update.message.reply_text("⚠️ Número inválido.")
        return
    res = buscar_por_dpi_y_numero(dpi, num)
    if not res: await update.message.reply_text(f"❌ No se encontró `{dpi}` N° {num}.", parse_mode="Markdown")
    else: await update.message.reply_text(formatear_mensaje(res), parse_mode="Markdown")

async def etiqueta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Uso: `/etiqueta <DPI> <N°>`", parse_mode="Markdown")
        return
    dpi = context.args[0].strip()
    try: num = int(context.args[1])
    except: 
        await update.message.reply_text("⚠️ Número inválido.")
        return
    try:
        res = buscar_por_dpi_y_numero(dpi, num)
        if not res:
            res = buscar_por_numero_general(num)
            if res and res.get("usuario_codigo") != dpi:
                res = None 
        if not res: 
            await update.message.reply_text(f"❌ No se encontró N° {num} para el DPI `{dpi}`.", parse_mode="Markdown")
            return
        num_real = res.get("num_paquete")
        ruta, error = generar_pdf_etiqueta(dpi, res.get("fecha_del"), res.get("fecha_al"), num_real, res.get("numero_general"), res.get("ubicacion_armada"))
        if ruta:
            with open(ruta, "rb") as f:
                caption = (f"🏷️ *Etiquetas del Paquete N° {num_real}*\n\n👤 DPI: `{dpi}`\n📅 {res.get('fecha_del')} al {res.get('fecha_al')}\n📍 `{res.get('ubicacion_armada')}`\n\n📄 *Formato:* PDF (listo para imprimir)\n📐 *Tamaño:* Carta\n🏷️ *Contiene:* Etiqueta ancha + angosta")
                await update.message.reply_document(document=f, filename=f"etiqueta_{dpi}_{num_real}.pdf", caption=caption, parse_mode="Markdown")
            os.remove(ruta)
        else: 
            await update.message.reply_text(f"❌ Error al generar PDF: {error}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error inesperado: {str(e)}")

async def total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    total = contar_paquetes()
    await update.message.reply_text(f"📊 Total de paquetes DPI: *{total}*", parse_mode="Markdown")

async def general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Uso: `/general <número>`", parse_mode="Markdown")
        return
    try: num_gen = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ El número debe ser entero.")
        return
    resultado = buscar_por_numero_general(num_gen)
    if not resultado:
        await update.message.reply_text(f"❌ No se encontró el general *{num_gen}*.", parse_mode="Markdown")
        return
    await update.message.reply_text(formatear_mensaje(resultado), parse_mode="Markdown")

async def fecha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Uso: `/fecha <DPI> <AAAA-MM-DD>`", parse_mode="Markdown")
        return
    dpi = context.args[0].strip()
    fecha_norm = normalizar_fecha(context.args[1].strip())
    if not fecha_norm:
        await update.message.reply_text("⚠️ Fecha inválida.", parse_mode="Markdown")
        return
    resultados = buscar_por_dpi_y_fecha(dpi, fecha_norm)
    if not resultados:
        await update.message.reply_text(f"❌ No hay paquetes del DPI `{dpi}` en esa fecha.", parse_mode="Markdown")
        return
    mensaje = f"📅 *Paquetes del DPI* `{dpi}` que incluyen *{fecha_norm}*\n\n"
    for r in resultados:
        gen = r.get('numero_general')
        gen_txt = f"`{gen}`" if gen is not None else "_blanco_"
        mensaje += f"📦 N° *{r.get('num_paquete')}* (Gen: {gen_txt})\n   📍 `{r.get('ubicacion_armada')}`\n\n"
        if len(mensaje) > 3800:
            await update.message.reply_text(mensaje, parse_mode="Markdown")
            mensaje = ""
    if mensaje:
        await update.message.reply_text(mensaje, parse_mode="Markdown")

async def usuario_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update)
    if not usuario: return
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Uso: `/usuario <DPI>`", parse_mode="Markdown")
        return
    dpi = context.args[0].strip()
    if usuario["rol"] == "operador2" and dpi != usuario["dpi_asignado"]:
        await update.message.reply_text(f"🚫 Solo puedes consultar tu propio DPI: `{usuario['dpi_asignado']}`", parse_mode="Markdown")
        return
    resultados = buscar_por_usuario(dpi)
    if not resultados:
        await update.message.reply_text(f"❌ No hay paquetes para el DPI `{dpi}`.", parse_mode="Markdown")
        return
    mensaje = f"👤 *Paquetes del usuario* `{dpi}`\n📦 Total: *{len(resultados)}*\n\n"
    for r in resultados:
        gen = r.get('numero_general')
        gen_txt = f"`{gen}`" if gen is not None else "_blanco_"
        mensaje += f"📦 N° *{r.get('num_paquete')}* (Gen: {gen_txt}) → `{r.get('ubicacion_armada')}`\n   📅 {r.get('fecha_del')} al {r.get('fecha_al')}\n\n"
        if len(mensaje) > 3800:
            await update.message.reply_text(mensaje, parse_mode="Markdown")
            mensaje = ""
    if mensaje:
        await update.message.reply_text(mensaje, parse_mode="Markdown")

async def eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Uso: `/eliminar <DPI> <N°>`", parse_mode="Markdown")
        return
    dpi = context.args[0]
    if usuario["rol"] == "operador2" and dpi != usuario["dpi_asignado"]:
        await update.message.reply_text("🚫 Solo puedes eliminar paquetes de tu propio DPI.")
        return
    try: num = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ El número debe ser entero.")
        return
    resultado = buscar_por_dpi_y_numero(dpi, num)
    if not resultado:
        await update.message.reply_text(f"❌ No se encontró el paquete {num} del DPI `{dpi}`.")
        return
    if eliminar_paquete(dpi, num):
        await update.message.reply_text(f"✅ Paquete N° {num} del DPI `{dpi}` eliminado.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ No se pudo eliminar.")

async def editar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return
    if len(context.args) != 4:
        await update.message.reply_text("⚠️ Uso: `/editar <DPI> <N°> <campo> <valor>`\nCampos: `fecha_del`, `fecha_al`, `estanteria`, `fila`, `columna`, `pila`, `numero_general`", parse_mode="Markdown")
        return
    dpi = context.args[0]
    if usuario["rol"] == "operador2" and dpi != usuario["dpi_asignado"]:
        await update.message.reply_text("🚫 Solo puedes editar paquetes de tu propio DPI.")
        return
    try: num = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ El número debe ser entero.")
        return
    campo = context.args[2]
    valor = context.args[3]
    if actualizar_paquete(dpi, num, campo, valor):
        await update.message.reply_text(f"✅ Campo `{campo}` actualizado a `{valor}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ No se pudo actualizar.")

async def agregar_usuario_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1"])
    if not usuario: return
    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Uso: `/agregar_usuario <DPI> <Nombre>`", parse_mode="Markdown")
        return
    dpi = context.args[0]
    nombre = " ".join(context.args[1:])
    if agregar_usuario(dpi, nombre):
        await update.message.reply_text(f"✅ Usuario agregado: `{dpi}` - {nombre}", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ El DPI `{dpi}` ya existe.", parse_mode="Markdown")

async def usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1"])
    if not usuario: return
    lista = listar_usuarios()
    if not lista:
        await update.message.reply_text("No hay usuarios.")
        return
    mensaje = "👥 *Usuarios (tabla usuarios):*\n\n"
    for dpi, nombre in lista:
        total_paq = contar_paquetes_de_usuario(dpi)
        mensaje += f"• `{dpi}` - {nombre} ({total_paq} paq.)\n"
        if len(mensaje) > 3800:
            await update.message.reply_text(mensaje, parse_mode="Markdown")
            mensaje = ""
    if mensaje:
        await update.message.reply_text(mensaje, parse_mode="Markdown")

async def eliminar_usuario_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1"])
    if not usuario: return
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Uso: `/eliminar_usuario <DPI>`", parse_mode="Markdown")
        return
    dpi = context.args[0]
    total_paq = contar_paquetes_de_usuario(dpi)
    if total_paq > 0:
        await update.message.reply_text(f"⚠️ El usuario tiene {total_paq} paquetes. Elimínalos primero.", parse_mode="Markdown")
        return
    if eliminar_usuario(dpi):
        await update.message.reply_text(f"✅ Usuario `{dpi}` eliminado.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ No se encontró.")

# ==========================================
# FLUJOS DE REGISTRO (ENROLAMIENTO)
# ==========================================
async def registrar_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usuario = await verificar_acceso(update, ["admin", "operador1", "operador2"])
    if not usuario: return ConversationHandler.END
    if usuario["rol"] == "operador2":
        context.user_data["dpi"] = usuario["dpi_asignado"]
        await update.message.reply_text(f"📝 *Registro*\n👤 DPI: `{usuario['dpi_asignado']}`\n\n1️⃣ Fecha *DEL*:", parse_mode="Markdown")
        return REG_DEL
    if context.args and len(context.args) >= 8:
        return await registrar_directo(update, context, usuario)
    context.user_data.clear()
    await update.message.reply_text("📝 *Registro paso a paso*\n\n1️⃣ Ingresa el *DPI/código* del usuario:", parse_mode="Markdown")
    return REG_DPI

async def reg_dpi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    codigo = update.message.text.strip()
    if not codigo:
        await update.message.reply_text("⚠️ Intenta de nuevo:")
        return REG_DPI
    if not usuario_existe(codigo):
        await update.message.reply_text(f"❌ El código `{codigo}` no está registrado.\nIntenta otro o /cancelar:", parse_mode="Markdown")
        return REG_DPI
    context.user_data["dpi"] = codigo
    await update.message.reply_text("2️⃣ Fecha *DEL* (AAAA-MM-DD o DD/MM/AAAA):")
    return REG_DEL

async def reg_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fecha = normalizar_fecha(update.message.text.strip())
    if not fecha:
        await update.message.reply_text("⚠️ Fecha inválida. Usa AAAA-MM-DD:")
        return REG_DEL
    context.user_data["fecha_del"] = fecha
    await update.message.reply_text("3️⃣ Fecha *AL*:")
    return REG_AL

async def reg_al(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fecha = normalizar_fecha(update.message.text.strip())
    if not fecha:
        await update.message.reply_text("⚠️ Fecha inválida:")
        return REG_AL
    context.user_data["fecha_al"] = fecha
    await update.message.reply_text("4️⃣ *Número de paquete*:")
    return REG_NUM

async def reg_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: num = int(update.message.text.strip())
    except:
        await update.message.reply_text("⚠️ Debe ser número:")
        return REG_NUM
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
    if col not in ["A", "B", "C", "D"]:
        await update.message.reply_text("⚠️ Debe ser A, B, C o D:")
        return REG_COL
    context.user_data["columna"] = col
    await update.message.reply_text("8️⃣ *Pila/Nivel*:")
    return REG_PIL

async def reg_pil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pila"] = update.message.text.strip()
    await update.message.reply_text("9️⃣ *Número general* (número, 'auto' o 'blanco'):")
    return REG_GENERAL

async def reg_general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    general = update.message.text.strip()
    datos = context.user_data
    nuevo_gen, error = insertar_paquete(datos["dpi"], datos["num_paquete"], datos["fecha_del"], datos["fecha_al"], datos["estanteria"], datos["fila"], datos["columna"], datos["pila"], general)
    if error:
        await update.message.reply_text(f"❌ {error}\n\nReintenta con /registrar")
        context.user_data.clear()
        return ConversationHandler.END
    gen_texto = f"`{nuevo_gen}`" if nuevo_gen is not None else "_En blanco_"
    await update.message.reply_text(f"✅ *Registrado*\n📦 N°: {datos['num_paquete']}\n🔢 Gen: {gen_texto}\n👤 `{datos['dpi']}`", parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def registrar_directo(update: Update, context: ContextTypes.DEFAULT_TYPE, usuario):
    if len(context.args) not in (8, 9): return ConversationHandler.END
    dpi = context.args[0]
    try:
        num = int(context.args[1])
        f_del = normalizar_fecha(context.args[2])
        f_al = normalizar_fecha(context.args[3])
        est, fil, col, pil = context.args[4:8]
    except: return ConversationHandler.END
    general = context.args[8] if len(context.args) == 9 else None
    nuevo_gen, error = insertar_paquete(dpi, num, f_del, f_al, est, fil, col, pil, general)
    if error: await update.message.reply_text(f"❌ {error}")
    else: await update.message.reply_text(f"✅ Registrado N° {num}")
    return ConversationHandler.END

# ==========================================
# FLUJOS DE EVENTOS (REGISTRO CIVIL)
# ==========================================
async def inicio_registrar_evento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("👶 Nacimiento", callback_data="tipo_Nacimiento")],
        [InlineKeyboardButton("💍 Matrimonio", callback_data="tipo_Matrimonio")],
        [InlineKeyboardButton("⚰️ Defunción", callback_data="tipo_Defuncion")],
        [InlineKeyboardButton("🔙 Cancelar", callback_data="menu_inicio")]
    ])
    await query.edit_message_text("📝 *Registrar Evento*\n\nSelecciona el tipo:", reply_markup=teclado, parse_mode="Markdown")
    return EV_TIPO

async def flujo_evento_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tipo = query.data.replace("tipo_", "")
    context.user_data["ev_tipo"] = tipo
    await query.edit_message_text(f"Tipo: *{tipo}*\n\nIngresa el *Año* (ej: 2023):", parse_mode="Markdown")
    return EV_ANIO

async def flujo_evento_anio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: anio = int(update.message.text.strip())
    except:
        await update.message.reply_text("⚠️ Debe ser un año válido:")
        return EV_ANIO
    context.user_data["ev_anio"] = anio
    await update.message.reply_text("Ingresa el *Número de Paquete*:")
    return EV_NUM

async def flujo_evento_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: num = int(update.message.text.strip())
    except:
        await update.message.reply_text("⚠️ Debe ser un número entero:")
        return EV_NUM
    context.user_data["ev_num"] = num
    await update.message.reply_text("Ingresa el *Folio de Inicio* (ej: 59495):")
    return EV_FOLIO_INI

async def flujo_evento_folio_ini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: folio = int(update.message.text.strip())
    except:
        await update.message.reply_text("⚠️ Debe ser un número entero:")
        return EV_FOLIO_INI
    context.user_data["ev_folio_ini"] = folio
    await update.message.reply_text("Ingresa el *Folio Final* (ej: 59497):")
    return EV_FOLIO_FIN

async def flujo_evento_folio_fin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: folio = int(update.message.text.strip())
    except:
        await update.message.reply_text("⚠️ Debe ser un número entero:")
        return EV_FOLIO_FIN
    context.user_data["ev_folio_fin"] = folio
    await update.message.reply_text("Ingresa la *Estantería*:")
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
    if col not in ["A", "B", "C", "D"]:
        await update.message.reply_text("⚠️ Debe ser A, B, C o D:")
        return EV_COL
    context.user_data["ev_col"] = col
    await update.message.reply_text("Ingresa la *Pila/Nivel*:")
    return EV_PIL

async def flujo_evento_pil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pil = update.message.text.strip()
    datos = context.user_data
    usuario = await verificar_acceso(update)
    
    exito, error = insertar_evento(
        datos["ev_tipo"], datos["ev_anio"], datos["ev_num"], 
        datos["ev_folio_ini"], datos["ev_folio_fin"], 
        datos["ev_est"], datos["ev_fil"], datos["ev_col"], pil,
        usuario["dpi_asignado"] or usuario["nombre"]
    )
    
    if exito:
        await update.message.reply_text(f"✅ *Evento Registrado*\n\nTipo: {datos['ev_tipo']}\nAño: {datos['ev_anio']}\nPaquete: {datos['ev_num']}\nFolios: {datos['ev_folio_ini']} al {datos['ev_folio_fin']}\nUbicación: E{datos['ev_est']}-F{datos['ev_fil']}-{datos['ev_col']}-P{pil}", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Error: {error}")
    
    context.user_data.clear()
    return ConversationHandler.END

async def inicio_buscar_folio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🔍 *Buscar por Folio*\n\nEscribe el número de folio (ej: 59496):", parse_mode="Markdown")
    return EV_BUS_FOLIO

async def flujo_buscar_folio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: folio = int(update.message.text.strip())
    except:
        await update.message.reply_text("⚠️ Debe ser un número entero:")
        return EV_BUS_FOLIO
    
    res = buscar_evento_por_folio(folio)
    if res:
        mensaje = (f"📖 *Evento Encontrado*\n\n"
                   f"Tipo: {res['tipo_evento']}\n"
                   f"Año: {res['anio']}\n"
                   f"Paquete N°: {res['num_paquete']}\n"
                   f"Rango: {res['folio_inicio']} al {res['folio_fin']}\n"
                   f"Ubicación: E{res['estanteria']}-F{res['fila']}-{res['columna']}-P{res['pila']}")
        await update.message.reply_text(mensaje, parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ No se encontró ningún paquete con el folio {folio}.")
    return ConversationHandler.END

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelado.")
    return ConversationHandler.END

# ==========================================
# MAIN Y SERVIDOR WEB PARA RENDER
# ==========================================
app_web = Flask(__name__)

@app_web.route('/')
def health_check():
    return "Bot is running!"

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
    
   # app.add_handler(CallbackQueryHandler(botones_menu))
    
    # Flujo de Registro Enrolamiento
    conv_registro = ConversationHandler(
        entry_points=[CommandHandler("registrar", registrar_inicio)],
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
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_message=False,
    )
    app.add_handler(conv_registro)

    # Flujo de Registro Eventos
    conv_evento = ConversationHandler(
        entry_points=[CallbackQueryHandler(inicio_registrar_evento, pattern="^accion_registrar_evento$")],
        states={
            EV_TIPO: [CallbackQueryHandler(flujo_evento_tipo, pattern="^tipo_")],
            EV_ANIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_anio)],
            EV_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_num)],
            EV_FOLIO_INI: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_folio_ini)],
            EV_FOLIO_FIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_folio_fin)],
            EV_EST: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_est)],
            EV_FIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_fil)],
            EV_COL: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_col)],
            EV_PIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_evento_pil)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_message=False,
    )
    app.add_handler(conv_evento)

    # Flujo de Búsqueda por Folio
    conv_bus_folio = ConversationHandler(
        entry_points=[CallbackQueryHandler(inicio_buscar_folio, pattern="^accion_buscar_folio$")],
        states={
            EV_BUS_FOLIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, flujo_buscar_folio)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        per_message=False,
    )
    app.add_handler(conv_bus_folio)
    
    print("✅ Bot activo. Presiona /start en Telegram.")
    app.run_polling()
