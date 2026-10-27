import os
import tempfile
import sqlite3
import threading
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.ext import ConversationHandler, MessageHandler, filters

# ==========================================
# CONFIGURACIÓN
# ==========================================
TOKEN = "8802170768:AAEVgJr0fBFvSX3Ft0k0-LLbOUlBYEjJTH0"
DB_NAME = "atestados_backup_1791223653484.db"

# Estados para el ConversationHandler de /registrar
(REG_DPI, REG_DEL, REG_AL, REG_NUM, REG_EST, REG_FIL, REG_COL, REG_PIL, REG_GENERAL) = range(9)

# ==========================================
# FUNCIONES DE BASE DE DATOS
# ==========================================

def obtener_columnas():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(paquetes)")
    columnas = [col[1] for col in cursor.fetchall()]
    conn.close()
    return columnas

def formatear_paquete(fila, columnas):
    """Convierte una fila en diccionario con ubicación armada."""
    datos = dict(zip(columnas, fila))
    est = datos.get("estanteria", "")
    fil = datos.get("fila", "")
    col = datos.get("columna", "")
    pil = datos.get("pila", "")
    datos["ubicacion_armada"] = f"E{est}-F{fil}-{col}-P{pil}"
    return datos

# --- CONSULTAS ---
def buscar_por_dpi_y_numero(dpi, num_paquete):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    columnas = obtener_columnas()
    cursor.execute("SELECT * FROM paquetes WHERE usuario_codigo = ? AND num_paquete = ?", (dpi, num_paquete))
    fila = cursor.fetchone()
    conn.close()
    return formatear_paquete(fila, columnas) if fila else None

def buscar_por_numero_general(num_general):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    columnas = obtener_columnas()
    cursor.execute("SELECT * FROM paquetes WHERE numero_general = ?", (num_general,))
    fila = cursor.fetchone()
    conn.close()
    return formatear_paquete(fila, columnas) if fila else None

def buscar_por_usuario(dpi):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    columnas = obtener_columnas()
    cursor.execute("SELECT * FROM paquetes WHERE usuario_codigo = ? ORDER BY num_paquete", (dpi,))
    filas = cursor.fetchall()
    conn.close()
    return [formatear_paquete(f, columnas) for f in filas]

def buscar_por_dpi_y_fecha(dpi, fecha):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    columnas = obtener_columnas()
    cursor.execute(
        "SELECT * FROM paquetes WHERE usuario_codigo = ? AND fecha_del <= ? AND fecha_al >= ? ORDER BY num_paquete",
        (dpi, fecha, fecha)
    )
    filas = cursor.fetchall()
    conn.close()
    return [formatear_paquete(f, columnas) for f in filas]

def contar_paquetes():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM paquetes")
    total = cursor.fetchone()[0]
    conn.close()
    return total

# --- ESCRITURA ---
def usuario_existe(dpi):
    """Verifica si el DPI existe en la tabla usuarios."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM usuarios WHERE dpi = ?", (dpi,))
        existe = cursor.fetchone()[0] > 0
        conn.close()
        return existe
    except:
        return False

def insertar_paquete(dpi, num, f_del, f_al, est, fil, col, pil, general=None):
    """
    Inserta un paquete.
    general puede ser:
      - None: se genera automáticamente
      - "auto": se genera automáticamente
      - "blanco" o "null": se guarda como NULL
      - número entero: se usa ese valor
    Devuelve: (nuevo_general, mensaje_error)
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        if general is None or str(general).lower() == "auto":
            cursor.execute("SELECT MAX(numero_general) FROM paquetes")
            max_gen = cursor.fetchone()[0] or 0
            nuevo_gen = max_gen + 1
        elif str(general).lower() in ("blanco", "null", ""):
            nuevo_gen = None
        else:
            try:
                nuevo_gen = int(general)
                cursor.execute("SELECT COUNT(*) FROM paquetes WHERE numero_general = ?", (nuevo_gen,))
                if cursor.fetchone()[0] > 0:
                    return (None, f"El número general {nuevo_gen} ya está en uso")
            except ValueError:
                return (None, f"'{general}' no es un número válido")
        
        cursor.execute("""
            INSERT INTO paquetes 
            (usuario_codigo, num_paquete, fecha_del, fecha_al, estanteria, fila, columna, pila, activo, numero_general)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (dpi, num, f_del, f_al, est, fil, col, pil, nuevo_gen))
        conn.commit()
        return (nuevo_gen, None)
    except sqlite3.IntegrityError:
        return (None, "Ya existe un paquete con ese número para ese DPI")
    except Exception as e:
        return (None, f"Error: {e}")
    finally:
        conn.close()

def eliminar_paquete(dpi, num):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM paquetes WHERE usuario_codigo = ? AND num_paquete = ?", (dpi, num))
    filas = cursor.rowcount
    conn.commit()
    conn.close()
    return filas > 0

def actualizar_paquete(dpi, num, campo, valor):
    """Actualiza un campo específico de un paquete."""
    campos_validos = ["fecha_del", "fecha_al", "estanteria", "fila", "columna", "pila", "numero_general"]
    if campo not in campos_validos:
        return False
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        if campo == "numero_general":
            if valor.lower() in ("blanco", "null", ""):
                valor_final = None
            else:
                valor_final = int(valor)
        else:
            valor_final = valor
        
        cursor.execute(f"UPDATE paquetes SET {campo} = ? WHERE usuario_codigo = ? AND num_paquete = ?",
                       (valor_final, dpi, num))
        filas = cursor.rowcount
        conn.commit()
        conn.close()
        return filas > 0
    except Exception as e:
        conn.close()
        print(f"Error actualizar: {e}")
        return False

# --- USUARIOS ---
def agregar_usuario(dpi, nombre):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO usuarios (dpi, nombre) VALUES (?, ?)", (dpi, nombre))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def listar_usuarios():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT dpi, nombre FROM usuarios ORDER BY nombre")
    usuarios = cursor.fetchall()
    conn.close()
    return usuarios

def eliminar_usuario(dpi):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM usuarios WHERE dpi = ?", (dpi,))
    filas = cursor.rowcount
    conn.commit()
    conn.close()
    return filas > 0

def contar_paquetes_de_usuario(dpi):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM paquetes WHERE usuario_codigo = ?", (dpi,))
    total = cursor.fetchone()[0]
    conn.close()
    return total

# ==========================================
# UTILIDADES
# ==========================================
def normalizar_fecha(fecha_str):
    """Convierte DD/MM/AAAA a AAAA-MM-DD. Devuelve None si es inválido."""
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
    
def generar_html_etiqueta(dpi, fecha_del, fecha_al, num, num_gen, ubicacion):
    """
    Genera el HTML de la pareja de etiquetas (ancha + angosta) idénticas.
    Adaptado del código Java original para mantener el mismo diseño.
    """
    # Formatear fecha para mostrar (si viene en AAAA-MM-DD, la pasamos a DD/MM/AAAA)
    def a_formato_mostrar(fecha):
        if not fecha or fecha == "?":
            return fecha
        try:
            partes = fecha.split("-")
            if len(partes) == 3:
                return f"{partes[2]}/{partes[1]}/{partes[0]}"
        except:
            pass
        return fecha
    
    del_mostrar = a_formato_mostrar(fecha_del)
    al_mostrar = a_formato_mostrar(fecha_al)
    num_str = str(num)
    gral = str(num_gen) if num_gen is not None else "-"
    
    # HTML de la pareja de etiquetas (ancha + angosta)
    etiquetas_html = (
        "<div style='display:flex;align-items:stretch;margin-bottom:14px;'>"
        
        # ETIQUETA ANCHA (62% del ancho)
        "<table style='border:3px solid #000;border-collapse:collapse;width:62%;font-family:sans-serif;'>"
        f"<tr><td style='background:#ddd;text-align:center;padding:10px;font-size:22pt;font-weight:bold;letter-spacing:3px;border-bottom:3px solid #000;'>{dpi}</td></tr>"
        f"<tr><td style='padding:8px 12px;font-size:18pt;font-weight:bold;'>DEL: {del_mostrar}</td></tr>"
        f"<tr><td style='padding:8px 12px;font-size:18pt;font-weight:bold;border-bottom:3px solid #000;'>AL: {al_mostrar}</td></tr>"
        f"<tr><td style='padding:12px;text-align:center;font-size:40pt;font-weight:bold;'>No. {num_str}</td></tr>"
        f"<tr><td style='padding:4px;text-align:right;font-size:10pt;color:#555;'>Gral. N° {gral}</td></tr>"
        f"<tr><td style='padding:8px;text-align:center;font-size:16pt;border-top:3px solid #000;'>Ubicación: {ubicacion}</td></tr>"
        "</table>"
        
        # LÍNEA DE CORTE (dashes)
        "<div style='width:24px;border-left:2px dashed #000;margin:0 8px;'></div>"
        
        # ETIQUETA ANGOSTA (32% del ancho)
        "<table style='border:3px solid #000;border-collapse:collapse;width:32%;font-family:sans-serif;'>"
        f"<tr><td style='background:#ddd;text-align:center;padding:8px 4px;font-size:16pt;font-weight:bold;letter-spacing:1px;border-bottom:3px solid #000;'>{dpi}</td></tr>"
        f"<tr><td style='padding:6px 4px;text-align:center;font-size:15pt;font-weight:bold;'>DEL: {del_mostrar}</td></tr>"
        f"<tr><td style='padding:6px 4px;text-align:center;font-size:15pt;font-weight:bold;border-bottom:3px solid #000;'>AL: {al_mostrar}</td></tr>"
        f"<tr><td style='padding:10px 4px;text-align:center;font-size:34pt;font-weight:bold;'>No. {num_str}</td></tr>"
        f"<tr><td style='padding:2px 4px;text-align:right;font-size:9pt;color:#555;'>Gral. {gral}</td></tr>"
        f"<tr><td style='padding:6px 4px;text-align:center;font-size:12pt;border-top:3px solid #000;'>Ubic: {ubicacion}</td></tr>"
        "</table>"
        
        "</div>"
    )
    
    # HTML completo con tamaño Carta y el diseño de dos etiquetas por hoja
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Etiquetas Paquete {num}</title>
<style>
    @page {{
        size: letter;
        margin: 10mm;
    }}
    body {{
        font-family: sans-serif;
        margin: 0;
        padding: 10px;
        background-color: white;
    }}
    /* Estilo para que al imprimir se vea una pareja por hoja */
    .pareja {{
        page-break-after: always;
    }}
    .pareja:last-child {{
        page-break-after: auto;
    }}
</style>
</head>
<body>
    <div class="pareja">
        {etiquetas_html}
    </div>
</body>
</html>"""
    
    # Guardar el archivo
    ruta = os.path.join(tempfile.gettempdir(), f"etiquetas_{dpi}_{num}.html")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(html)
    return ruta

def generar_pdf_etiqueta(dpi, fecha_del, fecha_al, num, num_gen, ubicacion):
    """
    Genera un PDF con la pareja de etiquetas (ancha + angosta).
    Devuelve la ruta del archivo PDF generado.
    """
    try:
        from fpdf import FPDF
    except ImportError:
        return None, "No tienes instalada 'fpdf2'. Abre Menu → Pip en Pydroid 3 y busca 'fpdf2'."
    
    # Formatear fechas
    def a_formato_mostrar(fecha):
        if not fecha or fecha == "?":
            return fecha
        try:
            partes = fecha.split("-")
            if len(partes) == 3:
                return f"{partes[2]}/{partes[1]}/{partes[0]}"
        except:
            pass
        return fecha
    
    del_mostrar = a_formato_mostrar(fecha_del)
    al_mostrar = a_formato_mostrar(fecha_al)
    num_str = str(num)
    gral = str(num_gen) if num_gen is not None else "-"
    
    # Crear PDF tamaño Carta (Letter) en milímetros
    # Carta: 215.9 x 279.4 mm
    pdf = FPDF(orientation="P", unit="mm", format="Letter")
    pdf.add_page()
    pdf.set_auto_page_break(auto=False)
    
    # Márgenes
    margen_izq = 10
    margen_sup = 15
    ancho_disponible = 215.9 - 20  # 195.9 mm
    
    # Distribución: 62% ancha, 6% separación, 32% angosta
    ancho_ancha = ancho_disponible * 0.62
    ancho_sep = ancho_disponible * 0.06
    ancho_angosta = ancho_disponible * 0.32
    
    # Posiciones
    x_ancha = margen_izq
    x_sep = x_ancha + ancho_ancha
    x_angosta = x_sep + ancho_sep
    y_inicio = margen_sup
    
    # ==================== ETIQUETA ANCHA (62%) ====================
    ancho_alto_ancha = 70  # Altura total de la etiqueta ancha
    
    # Borde exterior grueso
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.8)
    pdf.rect(x_ancha, y_inicio, ancho_ancha, ancho_alto_ancha)
    
    # Encabezado gris con DPI
    pdf.set_fill_color(221, 221, 221)
    pdf.rect(x_ancha, y_inicio, ancho_ancha, 12, style="F")
    pdf.set_xy(x_ancha, y_inicio + 2)
    pdf.set_font("Arial", "B", 20)
    pdf.cell(ancho_ancha, 8, dpi, align="C")
    
    # Línea inferior del encabezado
    pdf.set_line_width(0.8)
    pdf.line(x_ancha, y_inicio + 12, x_ancha + ancho_ancha, y_inicio + 12)
    
    # Fecha DEL
    pdf.set_xy(x_ancha + 3, y_inicio + 14)
    pdf.set_font("Arial", "B", 16)
    pdf.cell(ancho_ancha - 6, 8, f"DEL: {del_mostrar}", align="L")
    
    # Fecha AL
    pdf.set_xy(x_ancha + 3, y_inicio + 22)
    pdf.cell(ancho_ancha - 6, 8, f"AL: {al_mostrar}", align="L")
    
    # Línea después de fechas
    pdf.set_line_width(0.8)
    pdf.line(x_ancha, y_inicio + 30, x_ancha + ancho_ancha, y_inicio + 30)
    
    # Número GRANDE
    pdf.set_xy(x_ancha, y_inicio + 33)
    pdf.set_font("Arial", "B", 38)
    pdf.cell(ancho_ancha, 18, f"No. {num_str}", align="C")
    
    # Número general (pequeño, a la derecha)
    pdf.set_xy(x_ancha + 3, y_inicio + 52)
    pdf.set_font("Arial", "", 9)
    pdf.set_text_color(85, 85, 85)
    pdf.cell(ancho_ancha - 6, 5, f"Gral. N° {gral}", align="R")
    pdf.set_text_color(0, 0, 0)
    
    # Línea antes de ubicación
    pdf.set_line_width(0.8)
    pdf.line(x_ancha, y_inicio + 58, x_ancha + ancho_ancha, y_inicio + 58)
    
    # Ubicación
    pdf.set_xy(x_ancha, y_inicio + 60)
    pdf.set_font("Arial", "", 14)
    pdf.cell(ancho_ancha, 8, f"Ubicación: {ubicacion}", align="C")
    
    # ==================== LÍNEA DE CORTE (dashes) ====================
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.5)
    # Dibujar línea punteada vertical
    y_actual = y_inicio
    while y_actual < y_inicio + ancho_alto_ancha:
        pdf.line(x_sep + ancho_sep / 2, y_actual, x_sep + ancho_sep / 2, y_actual + 2)
        y_actual += 4
    
    # ==================== ETIQUETA ANGOSTA (32%) ====================
    ancho_alto_angosta = 70
    
    # Borde exterior grueso
    pdf.set_line_width(0.8)
    pdf.rect(x_angosta, y_inicio, ancho_angosta, ancho_alto_angosta)
    
    # Encabezado gris con DPI
    pdf.set_fill_color(221, 221, 221)
    pdf.rect(x_angosta, y_inicio, ancho_angosta, 10, style="F")
    pdf.set_xy(x_angosta, y_inicio + 1.5)
    pdf.set_font("Arial", "B", 14)
    pdf.cell(ancho_angosta, 7, dpi, align="C")
    
    # Línea inferior del encabezado
    pdf.set_line_width(0.8)
    pdf.line(x_angosta, y_inicio + 10, x_angosta + ancho_angosta, y_inicio + 10)
    
    # Fecha DEL
    pdf.set_xy(x_angosta, y_inicio + 12)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(ancho_angosta, 6, f"DEL: {del_mostrar}", align="C")
    
    # Fecha AL
    pdf.set_xy(x_angosta, y_inicio + 18)
    pdf.cell(ancho_angosta, 6, f"AL: {al_mostrar}", align="C")
    
    # Línea después de fechas
    pdf.set_line_width(0.8)
    pdf.line(x_angosta, y_inicio + 25, x_angosta + ancho_angosta, y_inicio + 25)
    
    # Número GRANDE
    pdf.set_xy(x_angosta, y_inicio + 28)
    pdf.set_font("Arial", "B", 30)
    pdf.cell(ancho_angosta, 15, f"No. {num_str}", align="C")
    
    # Número general (pequeño)
    pdf.set_xy(x_angosta, y_inicio + 45)
    pdf.set_font("Arial", "", 8)
    pdf.set_text_color(85, 85, 85)
    pdf.cell(ancho_angosta - 2, 5, f"Gral. {gral}", align="R")
    pdf.set_text_color(0, 0, 0)
    
    # Línea antes de ubicación
    pdf.set_line_width(0.8)
    pdf.line(x_angosta, y_inicio + 52, x_angosta + ancho_angosta, y_inicio + 52)
    
    # Ubicación
    pdf.set_xy(x_angosta, y_inicio + 54)
    pdf.set_font("Arial", "", 10)
    pdf.cell(ancho_angosta, 6, f"Ubic: {ubicacion}", align="C")
    
    # Guardar PDF
    ruta = os.path.join(tempfile.gettempdir(), f"etiquetas_{dpi}_{num}.pdf")
    pdf.output(ruta)
    return ruta, None
            
# ==========================================
# COMANDOS DE CONSULTA
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "👋 *Bienvenido al Buscador de Atestados*\n\n"
        "📋 *CONSULTAS:*\n"
        "🔍 `/buscar <DPI> <N°>` - Por DPI y número\n"
        "📅 `/fecha <DPI> <fecha>` - Por DPI y fecha\n"
        "🔢 `/general <N°>` - Por número general\n"
        "👤 `/usuario <DPI>` - Todos los paquetes de un DPI\n"
        "📊 `/total` - Total de paquetes\n\n"
        "✏️ *EDICIÓN:*\n"
        "➕ `/registrar <DPI> <N°> <DEL> <AL> <E> <F> <C> <P> [general]`\n"
        "🗑️ `/eliminar <DPI> <N°>`\n"
        "✏️ `/editar <DPI> <N°> <campo> <valor>`\n"
        "🏷️ `/etiqueta <DPI> <N°>` - Genera la etiqueta\n\n"
        "👥 *USUARIOS:*\n"
        "➕ `/agregar_usuario <DPI> <Nombre>`\n"
        "📋 `/usuarios`\n"
        "🗑️ `/eliminar_usuario <DPI>`\n\n"
        "❓ `/help` - Muestra esta ayuda"
    )
    await update.message.reply_text(mensaje, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = contar_paquetes()
    await update.message.reply_text(f"📊 Total de paquetes en la BD: *{total}*", parse_mode="Markdown")

async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Uso: `/buscar <DPI> <N°>`", parse_mode="Markdown")
        return
    dpi = context.args[0].strip()
    try:
        num = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ El número debe ser entero.")
        return
    resultado = buscar_por_dpi_y_numero(dpi, num)
    if not resultado:
        await update.message.reply_text(f"❌ No se encontró el paquete N° *{num}* para el DPI `{dpi}`.", parse_mode="Markdown")
        return
    await update.message.reply_text(formatear_mensaje(resultado), parse_mode="Markdown")

async def fecha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Uso: `/fecha <DPI> <AAAA-MM-DD>`", parse_mode="Markdown")
        return
    dpi = context.args[0].strip()
    fecha_norm = normalizar_fecha(context.args[1].strip())
    if not fecha_norm:
        await update.message.reply_text("⚠️ Fecha inválida. Usa `AAAA-MM-DD` o `DD/MM/AAAA`.", parse_mode="Markdown")
        return
    resultados = buscar_por_dpi_y_fecha(dpi, fecha_norm)
    if not resultados:
        await update.message.reply_text(f"❌ No hay paquetes del DPI `{dpi}` que incluyan la fecha *{fecha_norm}*.", parse_mode="Markdown")
        return
    encabezado = f"📅 *Paquetes del DPI* `{dpi}` que incluyen *{fecha_norm}*\n📦 Total: *{len(resultados)}*\n\n"
    mensaje = encabezado
    for r in resultados:
        gen = r.get('numero_general')
        gen_txt = f"`{gen}`" if gen is not None else "_blanco_"
        mensaje += f"📦 N° *{r.get('num_paquete')}* (Gen: {gen_txt})\n"
        mensaje += f"   📅 {r.get('fecha_del')} al {r.get('fecha_al')}\n"
        mensaje += f"   📍 `{r.get('ubicacion_armada')}`\n\n"
        if len(mensaje) > 3800:
            await update.message.reply_text(mensaje, parse_mode="Markdown")
            mensaje = ""
    if mensaje:
        await update.message.reply_text(mensaje, parse_mode="Markdown")

async def general(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Uso: `/general <número>`", parse_mode="Markdown")
        return
    try:
        num_gen = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ El número debe ser entero.")
        return
    resultado = buscar_por_numero_general(num_gen)
    if not resultado:
        await update.message.reply_text(f"❌ No se encontró el paquete con número general *{num_gen}*.", parse_mode="Markdown")
        return
    await update.message.reply_text(formatear_mensaje(resultado), parse_mode="Markdown")

async def usuario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Uso: `/usuario <DPI>`", parse_mode="Markdown")
        return
    dpi = context.args[0].strip()
    resultados = buscar_por_usuario(dpi)
    if not resultados:
        await update.message.reply_text(f"❌ No hay paquetes para el DPI `{dpi}`.", parse_mode="Markdown")
        return
    encabezado = f"👤 *Paquetes del usuario* `{dpi}`\n📦 Total: *{len(resultados)}*\n\n"
    mensaje = encabezado
    for r in resultados:
        gen = r.get('numero_general')
        gen_txt = f"`{gen}`" if gen is not None else "_blanco_"
        mensaje += f"📦 N° *{r.get('num_paquete')}* (Gen: {gen_txt}) → `{r.get('ubicacion_armada')}`\n"
        mensaje += f"   📅 {r.get('fecha_del')} al {r.get('fecha_al')}\n\n"
        if len(mensaje) > 3800:
            await update.message.reply_text(mensaje, parse_mode="Markdown")
            mensaje = ""
    if mensaje:
        await update.message.reply_text(mensaje, parse_mode="Markdown")

# ==========================================
# COMANDOS DE EDICIÓN
# ==========================================
async def registrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) not in (8, 9):
        await update.message.reply_text(
            "⚠️ Uso: `/registrar <DPI> <N°> <DEL> <AL> <E> <F> <C> <P> [general]`\n\n"
            "El último parámetro es *opcional*:\n"
            "• Sin ponerlo → genera automáticamente\n"
            "• `auto` → genera automáticamente\n"
            "• `blanco` → lo deja en blanco (NULL)\n"
            "• Un número → usa ese número general\n\n"
            "Ejemplos:\n"
            "`/registrar 2205534751215 50 2024-01-01 2024-02-01 5 4 A 1`\n"
            "`/registrar 2205534751215 50 2024-01-01 2024-02-01 5 4 A 1 blanco`",
            parse_mode="Markdown"
        )
        return
    
    dpi = context.args[0]
    try:
        num = int(context.args[1])
        f_del = normalizar_fecha(context.args[2])
        f_al = normalizar_fecha(context.args[3])
        if not f_del or not f_al:
            raise ValueError("fecha inválida")
        est, fil, col, pil = context.args[4], context.args[5], context.args[6], context.args[7]
    except ValueError:
        await update.message.reply_text("⚠️ Verifica el número y las fechas (AAAA-MM-DD o DD/MM/AAAA).")
        return
    
    general = context.args[8] if len(context.args) == 9 else None
    
    if not usuario_existe(dpi):
        await update.message.reply_text(
            f"❌ El DPI `{dpi}` no está registrado.\n"
            f"Agrégalo primero con `/agregar_usuario {dpi} Nombre Completo`",
            parse_mode="Markdown"
        )
        return
    
    nuevo_gen, error = insertar_paquete(dpi, num, f_del, f_al, est, fil, col, pil, general)
    
    if error:
        await update.message.reply_text(f"❌ {error}", parse_mode="Markdown")
        return
    
    gen_texto = f"`{nuevo_gen}`" if nuevo_gen is not None else "_En blanco (sin asignar)_"
    
    await update.message.reply_text(
        f"✅ *Paquete registrado*\n\n"
        f"📦 N°: {num}\n"
        f"🔢 General: {gen_texto}\n"
        f"👤 DPI: `{dpi}`\n"
        f"📅 {f_del} al {f_al}\n"
        f"📍 E{est}-F{fil}-{col}-P{pil}",
        parse_mode="Markdown"
    )

async def eliminar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Uso: `/eliminar <DPI> <N°>`", parse_mode="Markdown")
        return
    dpi = context.args[0]
    try:
        num = int(context.args[1])
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
    if len(context.args) != 4:
        await update.message.reply_text(
            "⚠️ Uso: `/editar <DPI> <N°> <campo> <valor>`\n\n"
            "Campos válidos:\n"
            "`fecha_del`, `fecha_al`, `estanteria`, `fila`, `columna`, `pila`, `numero_general`\n\n"
            "Para dejar el general en blanco:\n"
            "`/editar 2205534751215 1 numero_general blanco`",
            parse_mode="Markdown"
        )
        return
    dpi = context.args[0]
    try:
        num = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ El número debe ser entero.")
        return
    campo = context.args[2]
    valor = context.args[3]
    
    if actualizar_paquete(dpi, num, campo, valor):
        await update.message.reply_text(f"✅ Campo `{campo}` actualizado a `{valor}` en el paquete {num}.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ No se pudo actualizar. Verifica el campo y que el paquete exista.")

async def etiqueta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Genera y envía la etiqueta del paquete (PDF o HTML)."""
    if len(context.args) != 2:
        await update.message.reply_text(
            "⚠️ Uso: `/etiqueta <DPI> <N°>`\n"
            "Ejemplo: `/etiqueta 2205534751215 1`\n\n"
            "El bot te enviará un archivo listo para imprimir.",
            parse_mode="Markdown"
        )
        return
    
    dpi = context.args[0].strip()
    try:
        num = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ El número debe ser entero.")
        return
    
    resultado = buscar_por_dpi_y_numero(dpi, num)
    if not resultado:
        await update.message.reply_text(
            f"❌ No se encontró el paquete N° *{num}* del DPI `{dpi}`.",
            parse_mode="Markdown"
        )
        return
    
    # Datos del paquete
    f_del = resultado.get("fecha_del", "?")
    f_al = resultado.get("fecha_al", "?")
    num_gen = resultado.get("numero_general")
    ubicacion = resultado.get("ubicacion_armada", "?")
    
    # Intentar generar PDF primero
    ruta_pdf, error_pdf = generar_pdf_etiqueta(dpi, f_del, f_al, num, num_gen, ubicacion)
    
    if ruta_pdf:
        # Enviar PDF
        try:
            with open(ruta_pdf, "rb") as archivo:
                await update.message.reply_document(
                    document=archivo,
                    filename=f"etiquetas_{dpi}_{num}.pdf",
                    caption=(
                        f"🏷️ *Etiquetas del Paquete N° {num}*\n\n"
                        f"👤 DPI: `{dpi}`\n"
                        f"📅 {f_del} al {f_al}\n"
                        f"📍 `{ubicacion}`\n\n"
                        f"📄 *Formato:* PDF (listo para imprimir)\n"
                        f"📐 *Tamaño:* Carta\n"
                        f"🏷️ *Contiene:* Etiqueta ancha + angosta"
                    ),
                    parse_mode="Markdown"
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Error al enviar PDF: {e}")
        finally:
            try:
                os.remove(ruta_pdf)
            except:
                pass
    else:
        # Si falla el PDF, enviar HTML como respaldo
        await update.message.reply_text(
            f"⚠️ No se pudo generar PDF: {error_pdf}\n"
            f"Generando HTML como alternativa...",
        )
        try:
            ruta_html = generar_html_etiqueta(dpi, f_del, f_al, num, num_gen, ubicacion)
            with open(ruta_html, "rb") as archivo:
                await update.message.reply_document(
                    document=archivo,
                    filename=f"etiquetas_{dpi}_{num}.html",
                    caption=(
                        f"🏷️ *Etiquetas del Paquete N° {num}* (HTML)\n\n"
                        f"👤 DPI: `{dpi}`\n"
                        f"📅 {f_del} al {f_al}\n"
                        f"📍 `{ubicacion}`\n\n"
                        f"💡 Abre el HTML y usa *Compartir → Imprimir*."
                    ),
                    parse_mode="Markdown"
                )
            os.remove(ruta_html)
        except Exception as e:
            await update.message.reply_text(f"❌ Error al enviar HTML: {e}")

# ==========================================
# COMANDOS DE USUARIOS
# ==========================================
async def agregar_usuario_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Uso: `/agregar_usuario <DPI> <Nombre>`", parse_mode="Markdown")
        return
    dpi = context.args[0]
    nombre = " ".join(context.args[1:])
    if agregar_usuario(dpi, nombre):
        await update.message.reply_text(f"✅ Usuario agregado:\n`{dpi}` - {nombre}", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ El DPI `{dpi}` ya existe.", parse_mode="Markdown")

async def usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lista = listar_usuarios()
    if not lista:
        await update.message.reply_text("No hay usuarios registrados.")
        return
    mensaje = "👥 *Usuarios registrados:*\n\n"
    for dpi, nombre in lista:
        total_paq = contar_paquetes_de_usuario(dpi)
        mensaje += f"• `{dpi}` - {nombre} ({total_paq} paquetes)\n"
        if len(mensaje) > 3800:
            await update.message.reply_text(mensaje, parse_mode="Markdown")
            mensaje = ""
    if mensaje:
        await update.message.reply_text(mensaje, parse_mode="Markdown")

async def eliminar_usuario_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Uso: `/eliminar_usuario <DPI>`", parse_mode="Markdown")
        return
    dpi = context.args[0]
    total_paq = contar_paquetes_de_usuario(dpi)
    if total_paq > 0:
        await update.message.reply_text(
            f"⚠️ El usuario `{dpi}` tiene *{total_paq} paquetes* registrados.\n"
            f"Elimina primero sus paquetes o reasígnalos.",
            parse_mode="Markdown"
        )
        return
    if eliminar_usuario(dpi):
        await update.message.reply_text(f"✅ Usuario `{dpi}` eliminado.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ No se encontró el usuario `{dpi}`.")

app_web = Flask(__name__)

@app_web.route('/')
def health_check():
    return "Bot is running!"

def ejecutar_bot():
    # Esta función contiene tu main() original
    print("🤖 Iniciando bot...")
    app = Application.builder().token(TOKEN).build()
    # ... (aquí van todos tus app.add_handler...)
    # (Copia exactamente los mismos handlers que tenías en tu main)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("total", total))
    app.add_handler(CommandHandler("buscar", buscar))
    app.add_handler(CommandHandler("fecha", fecha))
    app.add_handler(CommandHandler("general", general))
    app.add_handler(CommandHandler("usuario", usuario))
    app.add_handler(CommandHandler("registrar", registrar))
    app.add_handler(CommandHandler("eliminar", eliminar))
    app.add_handler(CommandHandler("editar", editar))
    app.add_handler(CommandHandler("etiqueta", etiqueta))
    app.add_handler(CommandHandler("agregar_usuario", agregar_usuario_cmd))
    app.add_handler(CommandHandler("usuarios", usuarios))
    app.add_handler(CommandHandler("eliminar_usuario", eliminar_usuario_cmd))
    
    print("✅ Bot activo. Escuchando mensajes...")
    app.run_polling()

if __name__ == "__main__":
    # 1. Iniciar el bot en un hilo separado para que no bloquee a Flask
    hilo_bot = threading.Thread(target=ejecutar_bot)
    hilo_bot.daemon = True
    hilo_bot.start()
    
    # 2. Iniciar el servidor web de Flask (Render usará esto para saber que estás vivo)
    # Render asigna el puerto automáticamente en la variable de entorno PORT
    import os
    puerto = int(os.environ.get("PORT", 8080))
    app_web.run(host="0.0.0.0", port=puerto)