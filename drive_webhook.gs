/**
 * PotencIA — Webhook de Google Drive para el Diagnóstico Web Express
 * ====================================================================
 * Recibe (vía POST) los resultados y el PDF de cada diagnóstico y los
 * guarda en una carpeta de Drive, con una subcarpeta por cliente:
 *
 *   📁 Diagnósticos Web (carpeta raíz que tú eliges)
 *      📁 2026-07-12 1030 — Empresa X
 *         📄 resultado.json   (JSON completo del diagnóstico)
 *         📄 resumen.md       (resumen legible para humanos)
 *         📄 Diagnostico_IA_...pdf (el PDF que ve el cliente)
 *
 * CÓMO DESPLEGARLO (una sola vez, ~5 minutos):
 *   1. Entra a https://script.google.com con gerencia@potenciaempresarial.site
 *      y crea un proyecto nuevo ("Webhook Diagnóstico Drive").
 *   2. Pega TODO este archivo en Code.gs (reemplaza lo que haya).
 *   3. Crea en Drive la carpeta destino (ej. "Diagnósticos Web"), ábrela y
 *      copia el ID de la URL (drive.google.com/drive/folders/<ESTE_ID>).
 *      Pégalo abajo en CARPETA_RAIZ_ID.
 *   4. Pega el TOKEN secreto abajo (el mismo que va en Render como
 *      DRIVE_WEBHOOK_TOKEN).
 *   5. Implementar → Nueva implementación → tipo "Aplicación web":
 *        - Ejecutar como: Yo (gerencia@...)
 *        - Acceso: Cualquier persona
 *      Autoriza los permisos y copia la URL /exec resultante.
 *   6. En Render, agrega las variables:
 *        DRIVE_ENABLED=true
 *        DRIVE_WEBHOOK_URL=<la URL /exec>
 *        DRIVE_WEBHOOK_TOKEN=<el mismo token del paso 4>
 *
 * Seguridad: solo acepta peticiones que traigan el token correcto.
 * Los archivos quedan en TU Drive, con tu cuenta como propietaria.
 */

var CARPETA_RAIZ_ID = 'PEGA_AQUI_EL_ID_DE_LA_CARPETA';
var TOKEN = 'PEGA_AQUI_EL_TOKEN_SECRETO';

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);

    if (!body.token || body.token !== TOKEN) {
      return respuesta(false, 'token inválido');
    }
    if (!body.carpeta) {
      return respuesta(false, 'falta el nombre de carpeta del cliente');
    }

    var raiz = DriveApp.getFolderById(CARPETA_RAIZ_ID);
    var carpeta = obtenerOCrearCarpeta(raiz, String(body.carpeta).substring(0, 120));

    if (body.accion === 'resultado') {
      // JSON completo (para análisis posterior / dashboard)
      carpeta.createFile('resultado.json',
        JSON.stringify(body.resultado || {}, null, 2), 'application/json');
      // Resumen legible (para abrir y leer directo en Drive)
      if (body.resumen_md) {
        carpeta.createFile('resumen.md', body.resumen_md, 'text/markdown');
      }
      return respuesta(true, 'resultado guardado en ' + carpeta.getName());
    }

    if (body.accion === 'pdf') {
      if (!body.pdf_base64) return respuesta(false, 'falta pdf_base64');
      var bytes = Utilities.base64Decode(body.pdf_base64);
      var blob = Utilities.newBlob(bytes, 'application/pdf',
        body.filename || 'reporte.pdf');
      carpeta.createFile(blob);
      return respuesta(true, 'pdf guardado en ' + carpeta.getName());
    }

    return respuesta(false, 'acción desconocida: ' + body.accion);
  } catch (err) {
    return respuesta(false, 'error: ' + String(err));
  }
}

/** Busca la subcarpeta por nombre dentro de la raíz; si no existe la crea. */
function obtenerOCrearCarpeta(raiz, nombre) {
  var existentes = raiz.getFoldersByName(nombre);
  if (existentes.hasNext()) return existentes.next();
  return raiz.createFolder(nombre);
}

/** Apps Script siempre responde HTTP 200; el ok/mensaje va en el JSON. */
function respuesta(ok, mensaje) {
  return ContentService
    .createTextOutput(JSON.stringify({ ok: ok, mensaje: mensaje }))
    .setMimeType(ContentService.MimeType.JSON);
}
