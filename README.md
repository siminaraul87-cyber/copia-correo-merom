# Copia de correo Merom a Gmail

Proyecto sencillo con Python 3.12 e IMAPClient. Revisa las carpetas del buzón
de Merom en solo lectura y copia a Gmail los mensajes recientes que todavía no
existen allí.

## Qué hace

- Se conecta por SSL a **mail.merom.eu:993** y a **imap.gmail.com:993**.
- Revisa todas las carpetas de origen, incluida la Papelera. Omite Borradores y
  Spam/Junk.
- Busca mensajes desde los últimos 3 días por defecto. La búsqueda IMAP usa la
  fecha inclusiva **SINCE**.
- Lee el mensaje con **BODY.PEEK[]** y selecciona cada carpeta de origen con
  **readonly=True**.
- Comprueba el **Message-ID** con la búsqueda Gmail **X-GM-RAW** en la carpeta
  con atributo **\\All**. Si ya existe, no lo vuelve a copiar.
- Usa **APPEND** para guardar el mensaje original completo en **MEROM**, conserva
  **INTERNALDATE** y añade etiquetas para reflejar su carpeta de origen. Los
  mensajes de la bandeja de entrada reciben también la etiqueta Recibidos de
  Gmail.
- Si un mensaje falla, registra el tipo de error y continúa con los demás. Los
  registros no imprimen direcciones, asuntos, identificadores ni contenido del
  correo.
- Un mensaje sin un **Message-ID** válido se registra como error y se omite,
  porque no se puede comprobar su existencia previa en Gmail.
- Ejecuta la copia cada 10 minutos y permite lanzarla manualmente con otro
  número de días.
- El workflow mensual confirma la fecha en
  **.github/actividad-mensual.txt** para mantener actividad en el repositorio.

## Preparar el repositorio

1. Crea en GitHub un repositorio **público** nuevo.
2. Copia el contenido de esta carpeta al repositorio y súbelo desde tu equipo.
   Este proyecto no publica nada por sí solo al crearlo.
3. En GitHub, abre **Settings → Secrets and variables → Actions** y crea estos
   cuatro *repository secrets*:

   | Nombre | Valor |
   | --- | --- |
   | ORIGEN_USUARIO | Usuario del buzón de origen |
   | ORIGEN_PASSWORD | Contraseña del buzón de origen |
   | GMAIL_USUARIO | Dirección de tu cuenta Gmail |
   | GMAIL_APP_PASSWORD | Contraseña de aplicación de Gmail |

   Los valores se leen solo desde esas variables de entorno. No los escribas
   en archivos del repositorio.

## Contraseña de aplicación de Gmail

La cuenta de Google debe tener activada la
[verificación en 2 pasos](https://myaccount.google.com/signinoptions/two-step-verification).
Después, crea una contraseña de aplicación en
[Contraseñas de aplicaciones de Google](https://myaccount.google.com/apppasswords)
y guarda su valor como GMAIL_APP_PASSWORD.
El script elimina los espacios de agrupación que pueda mostrar Google.

Google indica que las contraseñas de aplicación requieren la verificación en
2 pasos y que algunas cuentas administradas por una organización pueden no
ofrecer esta opción. Consulta la
[ayuda oficial de Google](https://support.google.com/accounts/answer/185833).

## Primera copia

1. Abre la pestaña **Actions** del repositorio y selecciona **Copiar correo a
   Gmail**.
2. Pulsa **Run workflow**.
3. En el campo **dias**, escribe **3650** para revisar aproximadamente los
   últimos diez años, y lanza el workflow.

La primera ejecución puede tardar según la cantidad y el tamaño de los mensajes.
Si hace falta, puedes volver a lanzarla con **3650**: los mensajes que ya estén
en Gmail se omiten mediante su **Message-ID**.

Las ejecuciones programadas usan **dias=3**. GitHub ejecuta los cron en UTC y
puede retrasarlos durante periodos de alta carga.
