import os
import re
import json
import traceback
import threading
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import anthropic
from dotenv import dotenv_values


def extraer_json_robusto(raw):
    """
    Extrae JSON de la respuesta de Claude manejando todos los formatos típicos:
    - JSON puro
    - JSON dentro de ```json ... ```
    - JSON con texto explicativo antes/después
    Levanta ValueError descriptivo si no encuentra JSON parseable.
    """
    if not raw or not raw.strip():
        raise ValueError('Respuesta de Claude vacía')

    raw = raw.strip()

    # Caso 1: markdown fence ```json ... ``` o ``` ... ```
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))

    # Caso 2: buscar primer { y último } balanceados
    start = raw.find('{')
    end = raw.rfind('}') + 1
    if start == -1 or end <= start:
        raise ValueError(f'No se detectó JSON en la respuesta. Inicio: {raw[:300]!r}')

    return json.loads(raw[start:end])

# === API Key resolution (producción primero, .env como fallback dev) ===
def _get_env_var(key, fallback=''):
    """Lee primero de os.environ (producción/Render), luego de .env (desarrollo local)."""
    val = os.environ.get(key, '').strip()
    if val:
        return val
    env_path = Path(__file__).parent / '.env'
    if env_path.exists():
        _env = dotenv_values(dotenv_path=env_path)
        return (_env.get(key) or fallback).strip()
    return fallback

# === ANTHROPIC (Claude API) ===
API_KEY = _get_env_var('ANTHROPIC_API_KEY')
if API_KEY:
    print(f'✅ ANTHROPIC_API_KEY cargada (longitud: {len(API_KEY)} chars, prefijo: {API_KEY[:12]}...)')
else:
    print('❌ ANTHROPIC_API_KEY no encontrada — el endpoint /api/diagnostico fallará')

# === PANCAKE CRM v2 — integración para persistencia de leads ===
# KILL-SWITCH: PANCAKE_ENABLED='true' activa la integración. Default 'false' por seguridad.
# Mientras no confirmemos el método de auth correcto de Pancake CRM v2 (api_key vs access_token
# vs header), mantenemos la integración desactivada para no degradar el diagnóstico.
# Los leads quedan en logs estructurados de Render aunque Pancake esté off.
PANCAKE_ENABLED = _get_env_var('PANCAKE_ENABLED', 'false').lower() == 'true'
PANCAKE_API_KEY = _get_env_var('PANCAKE_API_KEY')
# API v1 del POS (patrón validado en CDA Motocesar; probado en este shop el 2026-07-12):
#   POST {base}/shops/{shop_id}/crm/{tabla}/records?api_key=KEY
# La v2 (crm.pancake.vn/api/v2/workspace/…) creaba el registro pero IGNORABA los campos
# (las claves en minúscula no coinciden con los slugs reales de la tabla) → contactos vacíos.
PANCAKE_SHOP_ID = _get_env_var('PANCAKE_SHOP_ID', '1022053221')  # shop de Potencia en Pancake POS
PANCAKE_TABLE_NAME = _get_env_var('PANCAKE_TABLE_NAME', 'Contact')
PANCAKE_API_URL = _get_env_var(
    'PANCAKE_API_URL',
    f'https://pos.pages.fm/api/v1/shops/{PANCAKE_SHOP_ID}/crm/{PANCAKE_TABLE_NAME}/records'
)
# Timeout corto: si Pancake no responde en X seg, abortamos y NO bloqueamos al user.
PANCAKE_TIMEOUT_SEG = int(_get_env_var('PANCAKE_TIMEOUT_SEG', '5'))

if PANCAKE_ENABLED and PANCAKE_API_KEY:
    print(f'✅ PANCAKE habilitado → POST {PANCAKE_API_URL} (timeout {PANCAKE_TIMEOUT_SEG}s)')
elif PANCAKE_ENABLED and not PANCAKE_API_KEY:
    print('⚠️  PANCAKE_ENABLED=true pero PANCAKE_API_KEY vacía — no se enviarán leads')
else:
    print('🔌 PANCAKE desactivado (PANCAKE_ENABLED!=true). Leads solo en logs.')

# === GOOGLE DRIVE — archivo de resultados + PDF por cliente ===
# Webhook de Apps Script (ver drive_webhook.gs) desplegado en la cuenta de
# Potencia. Mismo patrón kill-switch que Pancake: si está off o falla, el
# diagnóstico sigue funcionando igual (los resultados ya quedan en logs y CRM).
DRIVE_ENABLED = _get_env_var('DRIVE_ENABLED', 'false').lower() == 'true'
DRIVE_WEBHOOK_URL = _get_env_var('DRIVE_WEBHOOK_URL')
DRIVE_WEBHOOK_TOKEN = _get_env_var('DRIVE_WEBHOOK_TOKEN')
# Timeout más generoso que Pancake: el PDF pesa varios MB y Apps Script tarda.
DRIVE_TIMEOUT_SEG = int(_get_env_var('DRIVE_TIMEOUT_SEG', '30'))
# Tope del PDF en base64 (~15 MB reales). Los reportes normales pesan 1-4 MB.
DRIVE_PDF_MAX_B64 = 20 * 1024 * 1024

if DRIVE_ENABLED and DRIVE_WEBHOOK_URL and DRIVE_WEBHOOK_TOKEN:
    print(f'✅ DRIVE habilitado → webhook Apps Script (timeout {DRIVE_TIMEOUT_SEG}s)')
elif DRIVE_ENABLED:
    print('⚠️  DRIVE_ENABLED=true pero falta DRIVE_WEBHOOK_URL o DRIVE_WEBHOOK_TOKEN')
else:
    print('🔌 DRIVE desactivado (DRIVE_ENABLED!=true).')

app = Flask(__name__, static_folder='.')
# CORS explícito: el frontend (potenciaempresarial.site) llama directo a este backend
# (potencia-api.onrender.com) — es cross-origin. Declaramos methods y headers permitidos
# para que el preflight OPTIONS pase sin problemas.
CORS(app, resources={r"/api/*": {
    "origins": "*",
    "methods": ["GET", "POST", "OPTIONS"],
    "allow_headers": ["Content-Type"],
}})
# Tope global de request: protege /api/pdf de payloads absurdos (PDF normal: 1-4 MB).
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024
client = anthropic.Anthropic(api_key=API_KEY) if API_KEY else None


def enviar_lead_a_pancake(datos, resultado):
    """
    Envía el lead capturado a Pancake CRM.
    Diseño defensivo: si falla, hace log pero NO interrumpe la respuesta al usuario.
    Timeout de 10 seg para no bloquear.

    Retorna: (ok: bool, mensaje: str)
    """
    if not PANCAKE_ENABLED:
        return False, 'PANCAKE_ENABLED=false (kill-switch activo)'
    if not PANCAKE_API_KEY:
        return False, 'PANCAKE_API_KEY no configurada'

    # === Payload mapeado: campos del diagnóstico → campos del CRM ===
    nombre_completo = (datos.get('nombre') or '').strip()
    partes = nombre_completo.split(' ', 1)
    primer_nombre = partes[0] if partes else ''
    apellido = partes[1] if len(partes) > 1 else ''

    score = resultado.get('score', 0)
    nivel = resultado.get('nivel', 'Sin clasificar')

    # Notas estructuradas del diagnóstico (para que el equipo comercial tenga contexto)
    notas = f"""DIAGNÓSTICO WEB EXPRESS — {datetime.now().strftime('%Y-%m-%d %H:%M')}

📊 SCORE: {score}/100 — {nivel}

🏢 EMPRESA: {datos.get('empresa', 'N/A')}
📍 Industria: {datos.get('industria', 'N/A')}
👥 Empleados: {datos.get('empleados', 'N/A')}
💰 Facturación: {datos.get('facturacion', 'N/A')}

🎯 OBJETIVO: {datos.get('objetivo', 'N/A')}
⚠️ DESAFÍO: {datos.get('desafio', 'N/A')}

🌐 PRESENCIA DIGITAL:
  - Web: {datos.get('tienePaginaWeb', 'N/A')}
  - Redes: {', '.join(datos.get('redesSociales', [])) or 'Ninguna'}
  - CRM: {datos.get('gestionLeads', 'N/A')}

⚙️ AUTOMATIZACIÓN:
  - Tiene: {datos.get('tieneAutomatizaciones', 'N/A')}
  - Horas manuales: {datos.get('horasManuales', 'N/A')}

📣 MARKETING:
  - Publicidad: {datos.get('tienePublicidad', 'N/A')}
  - Presupuesto: {datos.get('presupuestoMarketing', 'N/A')}
  - Canal ventas: {datos.get('canalVentas', 'N/A')}

🎁 TOP OPORTUNIDAD SUGERIDA:
{resultado.get('oportunidades', [{}])[0].get('titulo', 'N/A') if resultado.get('oportunidades') else 'N/A'}
"""

    # Slugs REALES de la tabla Contact del shop 1022053221, confirmados con
    # GET /crm/tables + POST de prueba exitoso (2026-07-12): Name (título,
    # obligatorio), Phone, Email, Note. TODA la info del diagnóstico va en 'Note'.
    payload = {
        'Name': nombre_completo or '(Sin nombre)',
        'Email': datos.get('correo', ''),
        'Phone': datos.get('telefono', ''),  # opcional, casi nunca lo capturamos hoy
        'Note': notas,
    }

    # URL completa configurable. La api_key se añade aquí para no logguearla.
    url = f'{PANCAKE_API_URL}?api_key={PANCAKE_API_KEY}'

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'PotencIA-Diagnostico/1.0',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=PANCAKE_TIMEOUT_SEG) as resp:
            response_data = resp.read().decode('utf-8')
            print(f'✅ Lead enviado a Pancake CRM (HTTP {resp.status}): {response_data[:200]}', flush=True)
            return True, f'OK (HTTP {resp.status})'
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:500]
        print(f'❌ Pancake CRM rechazó el lead (HTTP {e.code}): {body}', flush=True)
        return False, f'HTTP {e.code}: {body}'
    except urllib.error.URLError as e:
        print(f'❌ No se pudo conectar a Pancake CRM: {e.reason}', flush=True)
        return False, f'URLError: {e.reason}'
    except Exception as e:
        print(f'❌ Error inesperado enviando a Pancake CRM: {e}', flush=True)
        return False, f'Exception: {e}'


def _post_a_drive(payload):
    """
    POST al webhook de Apps Script. Apps Script responde con redirect 302 a
    script.googleusercontent.com — urllib lo sigue solo y ahí viene el JSON
    {ok, mensaje}. Diseño defensivo igual que Pancake: nunca interrumpe.

    Retorna: (ok: bool, mensaje: str)
    """
    if not DRIVE_ENABLED:
        return False, 'DRIVE_ENABLED=false (kill-switch activo)'
    if not DRIVE_WEBHOOK_URL or not DRIVE_WEBHOOK_TOKEN:
        return False, 'DRIVE_WEBHOOK_URL/TOKEN no configurados'

    payload = dict(payload)
    payload['token'] = DRIVE_WEBHOOK_TOKEN
    try:
        req = urllib.request.Request(
            DRIVE_WEBHOOK_URL,
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=DRIVE_TIMEOUT_SEG) as resp:
            body = resp.read().decode('utf-8', errors='replace')
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return False, f'respuesta no-JSON de Apps Script: {body[:200]}'
        if data.get('ok'):
            return True, data.get('mensaje', 'OK')
        return False, f'Apps Script rechazó: {data.get("mensaje", body[:200])}'
    except urllib.error.HTTPError as e:
        return False, f'HTTP {e.code}: {e.read().decode("utf-8", errors="replace")[:300]}'
    except urllib.error.URLError as e:
        return False, f'URLError: {e.reason}'
    except Exception as e:
        return False, f'Exception: {e}'


def carpeta_cliente(datos, momento):
    """Nombre determinístico de la subcarpeta en Drive para este diagnóstico.
    El frontend recibe este mismo string y lo usa al subir el PDF, así ambos
    archivos caen en la misma carpeta (Apps Script hace find-or-create)."""
    empresa = re.sub(r'[/\\\n\r]+', ' ', (datos.get('empresa') or 'Sin empresa')).strip()
    return f"{momento.strftime('%Y-%m-%d %H%M')} — {empresa}"[:120]


def enviar_resultados_a_drive(datos, resultado, carpeta):
    """Archiva en Drive el JSON completo + un resumen legible del diagnóstico."""
    oportunidades = resultado.get('oportunidades') or []
    lineas_oport = '\n'.join(
        f"{i}. **{o.get('titulo', 'N/A')}** — {o.get('impacto', '?')} · {o.get('plazo', '?')} · ROI: {o.get('roiEstimado', '?')}\n   {o.get('descripcion', '')}"
        for i, o in enumerate(oportunidades, 1)
    ) or 'N/A'

    resumen_md = f"""# Diagnóstico Web Express — {datos.get('empresa', 'N/A')}

**Fecha:** {datetime.now().strftime('%Y-%m-%d %H:%M')}
**Contacto:** {datos.get('nombre', 'N/A')} · {datos.get('correo', 'N/A')} · {datos.get('telefono') or 'sin teléfono'}

## Score: {resultado.get('score', 0)}/100 — {resultado.get('nivel', 'Sin clasificar')}

{resultado.get('descripcionNivel', '')}

| Dimensión | Puntos |
|---|---|
| Presencia Digital | {(resultado.get('scoreDetalle') or {}).get('presenciaDigital', '?')}/25 |
| Automatización | {(resultado.get('scoreDetalle') or {}).get('automatizacion', '?')}/25 |
| Datos & Decisiones | {(resultado.get('scoreDetalle') or {}).get('datosDecisiones', '?')}/25 |
| Marketing IA | {(resultado.get('scoreDetalle') or {}).get('marketingIA', '?')}/25 |

## Perfil de la empresa

- **Industria:** {datos.get('industria', 'N/A')} · **Empleados:** {datos.get('empleados', 'N/A')} · **Facturación:** {datos.get('facturacion', 'N/A')}
- **Objetivo del año:** {datos.get('objetivo', 'N/A')}
- **Mayor desafío:** {datos.get('desafio', 'N/A')}
- **Web:** {datos.get('tienePaginaWeb', 'N/A')} · **Gestión de leads:** {datos.get('gestionLeads', 'N/A')}
- **Automatizaciones:** {datos.get('tieneAutomatizaciones', 'N/A')} · **Horas manuales/sem:** {datos.get('horasManuales', 'N/A')}
- **Publicidad:** {datos.get('tienePublicidad', 'N/A')} · **Presupuesto mkt:** {datos.get('presupuestoMarketing', 'N/A')}

## Top oportunidades sugeridas

{lineas_oport}

---
_Los datos completos (respuestas + reporte IA) están en `resultado.json` de esta misma carpeta._
"""

    payload = {
        'accion': 'resultado',
        'carpeta': carpeta,
        'resultado': {
            'capturado': datetime.now().isoformat(),
            'respuestas_formulario': datos,
            'reporte_ia': resultado,
        },
        'resumen_md': resumen_md,
    }
    return _post_a_drive(payload)


@app.route('/health')
def health():
    """Endpoint de salud — Render lo usa para verificar que la app está viva."""
    return jsonify({'status': 'ok', 'service': 'PotencIA API'})


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory('.', filename)


@app.route('/api/pdf', methods=['POST'])
def recibir_pdf():
    """
    Recibe el PDF del reporte (base64) generado por html2pdf.js en el navegador
    y lo reenvía a Drive vía el webhook de Apps Script. El PDF solo existe
    client-side, por eso el navegador es quien lo aporta.
    """
    if not (DRIVE_ENABLED and DRIVE_WEBHOOK_URL and DRIVE_WEBHOOK_TOKEN):
        return jsonify({'ok': False, 'error': 'Archivo en Drive desactivado'}), 503

    data = request.get_json(silent=True) or {}
    carpeta = (data.get('carpeta') or '').strip()
    pdf_b64 = data.get('pdf_base64') or ''
    filename = (data.get('filename') or 'reporte.pdf').strip()

    if not carpeta or not pdf_b64:
        return jsonify({'ok': False, 'error': 'Faltan carpeta o pdf_base64'}), 400
    if len(pdf_b64) > DRIVE_PDF_MAX_B64:
        print(f'⚠️  /api/pdf rechazado por tamaño: {len(pdf_b64)} chars b64 ({carpeta})', flush=True)
        return jsonify({'ok': False, 'error': 'PDF demasiado grande'}), 413
    # Solo nombres de archivo simples (sin rutas) y siempre .pdf
    filename = re.sub(r'[/\\\n\r]+', '_', filename)[:150]
    if not filename.lower().endswith('.pdf'):
        filename += '.pdf'

    def _enviar_pdf_background(carpeta_s, filename_s, pdf_s):
        try:
            ok, msg = _post_a_drive({
                'accion': 'pdf',
                'carpeta': carpeta_s,
                'filename': filename_s,
                'pdf_base64': pdf_s,
            })
            print(f'📁 Drive PDF (async): {"✅" if ok else "⚠️"} {msg} [{carpeta_s}]', flush=True)
        except Exception as e:
            print(f'⚠️  Excepción subiendo PDF a Drive: {e}', flush=True)

    threading.Thread(
        target=_enviar_pdf_background,
        args=(carpeta, filename, pdf_b64),
        daemon=True,
        name='drive-pdf-async',
    ).start()
    return jsonify({'ok': True, 'mensaje': 'PDF en camino a Drive'}), 202


@app.route('/api/diagnostico', methods=['POST'])
def diagnostico():
    if client is None:
        return jsonify({
            'ok': False,
            'error': 'Configuración del servidor incompleta — falta ANTHROPIC_API_KEY. Contacta al administrador.'
        }), 500

    datos = request.get_json()

    redes = ', '.join(datos.get('redesSociales', [])) or 'Ninguna'
    herramientas = ', '.join(datos.get('herramientas', [])) or 'Ninguna'
    procesos = ', '.join(datos.get('procesosManuales', [])) or 'No especificado'

    prompt = f"""Eres un consultor senior de transformación digital e inteligencia artificial de PotencIA Empresarial.
Tu tarea es analizar el diagnóstico de una empresa y generar un reporte ejecutivo profesional en formato JSON estricto.

DATOS DEL DIAGNÓSTICO:
- Empresa: {datos.get('empresa')}
- Industria: {datos.get('industria')}
- Empleados: {datos.get('empleados')}
- Facturación mensual aprox: {datos.get('facturacion')}
- Representante: {datos.get('nombre')}
- Correo: {datos.get('correo')}

PRESENCIA DIGITAL:
- Tiene página web: {datos.get('tienePaginaWeb')}
- Redes sociales activas: {redes}
- Herramientas de gestión: {herramientas}
- Gestión de leads: {datos.get('gestionLeads')}

OPERACIONES:
- Horas semanales en tareas manuales: {datos.get('horasManuales')}
- Procesos más manuales: {procesos}
- Tiene automatizaciones: {datos.get('tieneAutomatizaciones')}
- Descripción automatizaciones: {datos.get('descripcionAutomatizaciones') or 'Ninguna'}

MARKETING Y VENTAS:
- Invierte en publicidad digital: {datos.get('tienePublicidad')}
- Presupuesto mensual marketing: {datos.get('presupuestoMarketing')}
- Cómo mide resultados: {datos.get('mideResultados')}
- Canal principal de ventas: {datos.get('canalVentas')}

METAS:
- Objetivo principal: {datos.get('objetivo')}
- Mayor desafío: {datos.get('desafio')}

Genera el análisis ÚNICAMENTE como JSON puro (sin markdown, sin explicaciones, solo el objeto JSON):

{{
  "score": <número entre 0 y 100>,
  "nivel": "<uno de: Inicial | En Desarrollo | Intermedio | Avanzado | Líder Digital>",
  "descripcionNivel": "<2 oraciones sobre el estado actual de la empresa>",
  "scoreDetalle": {{
    "presenciaDigital": <0-25>,
    "automatizacion": <0-25>,
    "datosDecisiones": <0-25>,
    "marketingIA": <0-25>
  }},
  "fortalezas": [
    "<fortaleza 1 concreta>",
    "<fortaleza 2 concreta>",
    "<fortaleza 3 concreta>"
  ],
  "oportunidades": [
    {{
      "titulo": "<nombre de la oportunidad>",
      "descripcion": "<qué se implementa y cómo>",
      "impacto": "<Alto | Medio>",
      "plazo": "<30 días | 60 días | 90 días>",
      "roiEstimado": "<porcentaje o descripción cuantificable del retorno>",
      "herramientasSugeridas": ["<herramienta 1>", "<herramienta 2>"]
    }},
    {{
      "titulo": "<nombre>",
      "descripcion": "<descripción>",
      "impacto": "<Alto | Medio>",
      "plazo": "<30 días | 60 días | 90 días>",
      "roiEstimado": "<ROI>",
      "herramientasSugeridas": ["<herramienta>"]
    }},
    {{
      "titulo": "<nombre>",
      "descripcion": "<descripción>",
      "impacto": "<Alto | Medio>",
      "plazo": "<30 días | 60 días | 90 días>",
      "roiEstimado": "<ROI>",
      "herramientasSugeridas": ["<herramienta>"]
    }}
  ],
  "planAccion": {{
    "mes1": {{
      "titulo": "Fundación Digital",
      "acciones": ["<acción 1>", "<acción 2>", "<acción 3>"]
    }},
    "mes2": {{
      "titulo": "Implementación IA",
      "acciones": ["<acción 1>", "<acción 2>", "<acción 3>"]
    }},
    "mes3": {{
      "titulo": "Optimización y Escala",
      "acciones": ["<acción 1>", "<acción 2>", "<acción 3>"]
    }}
  }},
  "mensajeFinal": "<2-3 oraciones motivadoras y específicas para esta empresa>"
}}"""

    inicio = datetime.now()
    raw = ''  # para tener referencia si falla el parsing

    try:
        message = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=2000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = message.content[0].text.strip()
        resultado = extraer_json_robusto(raw)

        # === LOG ESTRUCTURADO — cada diagnóstico queda registrado en Render Logs ===
        duracion = (datetime.now() - inicio).total_seconds()
        log_lead = {
            'timestamp': inicio.isoformat(),
            'tipo': 'DIAGNOSTICO_NUEVO',
            'lead': {
                'nombre': datos.get('nombre'),
                'correo': datos.get('correo'),
                'empresa': datos.get('empresa'),
                'industria': datos.get('industria'),
                'empleados': datos.get('empleados'),
                'facturacion': datos.get('facturacion'),
            },
            'resultado': {
                'score': resultado.get('score'),
                'nivel': resultado.get('nivel'),
            },
            'metrica_tecnica': {
                'duracion_seg': round(duracion, 2),
                'tokens_input': message.usage.input_tokens,
                'tokens_output': message.usage.output_tokens,
                'costo_usd_aprox': round((message.usage.input_tokens * 3 + message.usage.output_tokens * 15) / 1_000_000, 4),
            }
        }
        print(f'📊 LEAD CAPTURADO: {json.dumps(log_lead, ensure_ascii=False)}', flush=True)

        # === ENVIAR A PANCAKE CRM EN BACKGROUND (POE-N-01 §2: no bloquear ruta crítica) ===
        # Guarda dura: si el kill-switch está off, NO lanzamos thread (cero overhead).
        # El lead ya quedó en logs estructurados de Render (LEAD CAPTURADO arriba).
        if PANCAKE_ENABLED and PANCAKE_API_KEY:
            def _enviar_pancake_background(datos_snapshot, resultado_snapshot):
                try:
                    crm_ok, crm_msg = enviar_lead_a_pancake(datos_snapshot, resultado_snapshot)
                    print(f'🔗 Pancake CRM (async): {"✅" if crm_ok else "⚠️"} {crm_msg}', flush=True)
                except Exception as crm_err:
                    print(f'⚠️  Excepción en integración Pancake async (lead en logs): {crm_err}', flush=True)

            threading.Thread(
                target=_enviar_pancake_background,
                args=(datos, resultado),
                daemon=True,
                name='pancake-crm-async',
            ).start()

        # === ARCHIVAR RESULTADOS EN GOOGLE DRIVE (background, mismo patrón) ===
        # 'carpeta' viaja también al frontend: cuando el navegador genere el PDF
        # lo sube a /api/pdf con este mismo nombre y cae en la misma subcarpeta.
        carpeta = carpeta_cliente(datos, inicio)
        if DRIVE_ENABLED and DRIVE_WEBHOOK_URL:
            def _enviar_drive_background(datos_snapshot, resultado_snapshot, carpeta_snapshot):
                try:
                    drv_ok, drv_msg = enviar_resultados_a_drive(datos_snapshot, resultado_snapshot, carpeta_snapshot)
                    print(f'📁 Drive resultados (async): {"✅" if drv_ok else "⚠️"} {drv_msg}', flush=True)
                except Exception as drv_err:
                    print(f'⚠️  Excepción archivando en Drive (lead en logs): {drv_err}', flush=True)

            threading.Thread(
                target=_enviar_drive_background,
                args=(datos, resultado, carpeta),
                daemon=True,
                name='drive-resultados-async',
            ).start()

        return jsonify({'ok': True, 'resultado': resultado, 'carpeta': carpeta})

    # === MANEJO DE ERRORES GRANULAR (POE-N-01 §5: logging proactivo) ===
    # Cada tipo de error registra contexto completo en logs y devuelve un
    # mensaje user-friendly al frontend (no jerga técnica).
    except anthropic.APIStatusError as e:
        # Errores HTTP del API de Anthropic (rate limit, content policy, etc.)
        print(f'❌ ANTHROPIC API ERROR para {datos.get("correo", "?")}: '
              f'status={e.status_code} message={e.message}', flush=True)
        msg_user = ('El servicio de IA está temporalmente saturado o rechazó la solicitud. '
                    'Intenta de nuevo en 1 minuto.')
        return jsonify({'ok': False, 'error': msg_user, 'codigo': 'anthropic_api_error'}), 503

    except anthropic.APIConnectionError as e:
        # Problema de conexión con Anthropic
        print(f'❌ ANTHROPIC CONNECTION ERROR para {datos.get("correo", "?")}: {e}', flush=True)
        msg_user = 'No pudimos conectar con el servicio de IA. Intenta de nuevo en unos segundos.'
        return jsonify({'ok': False, 'error': msg_user, 'codigo': 'anthropic_conn_error'}), 503

    except json.JSONDecodeError as e:
        # Claude devolvió algo que no es JSON parseable
        print(f'❌ JSON PARSE ERROR para {datos.get("correo", "?")}: {e}', flush=True)
        print(f'   RAW (primeros 500 chars): {raw[:500]!r}', flush=True)
        msg_user = ('Hubo un problema procesando la respuesta de IA. '
                    'Intenta de nuevo (suele resolverse al reintentar).')
        return jsonify({'ok': False, 'error': msg_user, 'codigo': 'json_parse_error'}), 500

    except ValueError as e:
        # extraer_json_robusto levantó ValueError (no encontró JSON)
        print(f'❌ NO JSON IN CLAUDE RESPONSE para {datos.get("correo", "?")}: {e}', flush=True)
        print(f'   RAW (primeros 500 chars): {raw[:500]!r}', flush=True)
        msg_user = 'La respuesta de IA llegó incompleta. Intenta de nuevo.'
        return jsonify({'ok': False, 'error': msg_user, 'codigo': 'incomplete_response'}), 500

    except Exception as e:
        # Cualquier otro error — log COMPLETO con traceback para debug post-mortem
        print(f'❌ ERROR INESPERADO para {datos.get("correo", "?")}: '
              f'{type(e).__name__}: {e}', flush=True)
        print(f'   Traceback completo:\n{traceback.format_exc()}', flush=True)
        msg_user = ('Hubo un error inesperado generando el diagnóstico. '
                    'Por favor intenta de nuevo. Si persiste, contáctanos.')
        return jsonify({'ok': False, 'error': msg_user, 'codigo': 'unexpected_error'}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    print(f'✅ PotencIA Empresarial - Servidor en http://localhost:{port}')
    print(f'📊 Diagnóstico en http://localhost:{port}/diagnostico.html')
    app.run(host='0.0.0.0', port=port, debug=False)
