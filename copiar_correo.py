#!/usr/bin/env python3
"""Copia mensajes IMAP a Gmail sin modificar el buzón de origen."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from typing import Any

from imapclient import IMAPClient


# Servidores de correo. Los usuarios y contraseñas siempre vienen de Secrets.
ORIGEN_SERVIDOR = "mail.merom.eu"
GMAIL_SERVIDOR = "imap.gmail.com"
PUERTO_IMAP = 993
ETIQUETA_BASE = "MEROM"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def normalizar(texto: str) -> str:
    """Pasa texto a minúsculas y elimina acentos para comparar carpetas."""
    texto = unicodedata.normalize("NFKD", texto)
    return "".join(letra for letra in texto if not unicodedata.combining(letra)).casefold()


def texto_imap(valor: Any) -> str:
    """Convierte a texto nombres y atributos devueltos por IMAPClient."""
    if isinstance(valor, bytes):
        return valor.decode("utf-8", errors="replace")
    return str(valor)


def componentes_carpeta(nombre: str, separador: str | None) -> list[str]:
    """Separa los componentes de una ruta IMAP."""
    return nombre.split(separador) if separador and separador in nombre else [nombre]


def es_carpeta_excluida(flags: tuple[Any, ...], nombre: str, separador: str | None) -> bool:
    """Omite únicamente Borradores y sus subcarpetas."""
    atributos = {texto_imap(flag).casefold() for flag in flags}
    if r"\drafts" in atributos:
        return True

    partes = componentes_carpeta(nombre, separador)
    nombres_excluidos = {"borrador", "borradores", "draft", "drafts"}
    return any(normalizar(parte.strip()) in nombres_excluidos for parte in partes)


def es_bandeja_entrada(flags: tuple[Any, ...], nombre: str) -> bool:
    """Reconoce la bandeja de entrada por su atributo IMAP o su nombre."""
    atributos = {texto_imap(flag).casefold() for flag in flags}
    return r"\inbox" in atributos or normalizar(nombre) in {
        "inbox",
        "bandeja de entrada",
    }


def es_carpeta_spam(
    flags: tuple[Any, ...], nombre: str, separador: str | None
) -> bool:
    """Reconoce Spam/Junk por atributo especial o por nombre."""
    atributos = {texto_imap(flag).casefold() for flag in flags}
    if atributos.intersection({r"\junk", r"\spam"}):
        return True

    nombres_spam = {
        "spam",
        "junk",
        "correo no deseado",
        "correo no deseados",
        "correo no deseada",
        "correo basura",
    }
    return any(
        normalizar(parte.strip()) in nombres_spam
        for parte in componentes_carpeta(nombre, separador)
    )


def es_carpeta_papelera(
    flags: tuple[Any, ...], nombre: str, separador: str | None
) -> bool:
    """Reconoce la Papelera por atributo especial o por nombre."""
    atributos = {texto_imap(flag).casefold() for flag in flags}
    if r"\trash" in atributos:
        return True

    nombres_papelera = {
        "papelera",
        "trash",
        "deleted",
        "deleted items",
        "deleted messages",
        "elementos eliminados",
    }
    return any(
        normalizar(parte.strip()) in nombres_papelera
        for parte in componentes_carpeta(nombre, separador)
    )


def etiqueta_origen(
    flags: tuple[Any, ...], nombre: str, separador: str | None
) -> list[str]:
    """Devuelve las etiquetas que corresponden a la carpeta de origen."""
    atributos = {texto_imap(flag).casefold() for flag in flags}
    partes = componentes_carpeta(nombre, separador)
    ultimo_nombre = normalizar(partes[-1].strip())
    inbox = es_bandeja_entrada(flags, nombre)

    etiquetas = [ETIQUETA_BASE]
    if inbox:
        # X-GM-LABELS permite poner el mismo mensaje también en Recibidos.
        etiquetas.append(r"\Inbox")

    enviados = r"\sent" in atributos or ultimo_nombre in {
        "enviado",
        "enviados",
        "sent",
        "sent items",
        "sent messages",
        "elementos enviados",
    }
    spam = es_carpeta_spam(flags, nombre, separador)
    papelera = es_carpeta_papelera(flags, nombre, separador)

    if spam:
        etiquetas.append(f"{ETIQUETA_BASE}/Spam")
    elif enviados:
        etiquetas.append(f"{ETIQUETA_BASE}/Enviados")
    elif papelera:
        etiquetas.append(f"{ETIQUETA_BASE}/Papelera")
    elif not inbox:
        ruta = nombre.replace(separador, "/") if separador else nombre
        ruta = ruta.strip("/")
        if ruta:
            etiquetas.append(f"{ETIQUETA_BASE}/{ruta}")

    return etiquetas


def obtener_id_mensaje(mensaje: bytes) -> str | None:
    """Lee Message-ID sin alterar los bytes originales del correo."""
    cabeceras = BytesParser(policy=policy.default).parsebytes(
        mensaje, headersonly=True
    )
    valor = cabeceras.get("Message-ID")
    if not valor:
        return None

    identificador = str(valor).strip()
    if identificador.startswith("<") and identificador.endswith(">"):
        identificador = identificador[1:-1].strip()

    # Evita valores malformados y consultas Gmail con caracteres de control.
    if (
        not identificador
        or "@" not in identificador
        or re.search(r"[\s<>\x00-\x1f\x7f]", identificador)
    ):
        return None
    return identificador


def obtener_cabecera_message_id(datos: dict[Any, Any]) -> bytes:
    """Extrae la cabecera que se pidió con BODY.PEEK[HEADER.FIELDS]."""
    for clave, valor in datos.items():
        nombre = texto_imap(clave).upper().replace(".PEEK", "")
        if (
            "HEADER.FIELDS" in nombre
            and "MESSAGE-ID" in nombre
            and isinstance(valor, bytes)
        ):
            return valor
    return b""


def obtener_cuerpo_y_fecha(datos: dict[Any, Any]) -> tuple[bytes, datetime]:
    """Extrae BODY.PEEK[] e INTERNALDATE de la respuesta FETCH."""
    cuerpo: bytes | None = None
    fecha: datetime | None = None

    for clave, valor in datos.items():
        nombre = texto_imap(clave).upper().replace(".PEEK", "")
        if nombre == "BODY[]" and isinstance(valor, bytes):
            cuerpo = valor
        elif nombre == "INTERNALDATE" and isinstance(valor, datetime):
            fecha = valor

    if cuerpo is None or fecha is None:
        raise ValueError("La respuesta IMAP no incluyó BODY.PEEK[] o INTERNALDATE")
    return cuerpo, fecha


def buscar_carpeta_todos(gmail: IMAPClient) -> tuple[str, set[str]]:
    """Encuentra \\All y devuelve también los nombres de carpetas existentes."""
    carpetas = gmail.list_folders()
    nombres = {nombre for _flags, _separador, nombre in carpetas}
    for flags, _separador, nombre in carpetas:
        if any(texto_imap(flag).casefold() == r"\all" for flag in flags):
            return nombre, nombres
    raise RuntimeError("Gmail no anunció una carpeta IMAP con el atributo \\All")


def asegurar_etiquetas(
    gmail: IMAPClient, etiquetas: list[str], carpetas_existentes: set[str]
) -> None:
    """Crea las etiquetas de Gmail que todavía no existen."""
    necesarias: set[str] = {ETIQUETA_BASE}
    for etiqueta in etiquetas:
        if etiqueta == ETIQUETA_BASE or etiqueta == r"\Inbox":
            continue
        partes = etiqueta.split("/")
        if partes[0] == ETIQUETA_BASE:
            for indice in range(1, len(partes) + 1):
                necesarias.add("/".join(partes[:indice]))

    # El orden garantiza que exista MEROM antes de sus etiquetas hijas.
    for etiqueta in sorted(necesarias, key=lambda item: (item.count("/"), item)):
        if etiqueta not in carpetas_existentes:
            gmail.create_folder(etiqueta)
            carpetas_existentes.add(etiqueta)


def validar_secretos() -> dict[str, str]:
    """Lee las cuatro credenciales exclusivamente del entorno."""
    nombres = (
        "ORIGEN_USUARIO",
        "ORIGEN_PASSWORD",
        "GMAIL_USUARIO",
        "GMAIL_APP_PASSWORD",
    )
    valores = {nombre: os.environ.get(nombre, "") for nombre in nombres}
    # Google muestra la contraseña de aplicación agrupada con espacios;
    # esos espacios son solo de formato y se quitan antes de autenticar.
    valores["GMAIL_APP_PASSWORD"] = valores["GMAIL_APP_PASSWORD"].replace(" ", "")
    faltantes = [nombre for nombre, valor in valores.items() if not valor]
    if faltantes:
        raise RuntimeError("Faltan Secrets requeridos: " + ", ".join(faltantes))
    return valores


def procesar_mensaje(
    origen: IMAPClient,
    gmail: IMAPClient,
    uid: int,
    flags: tuple[Any, ...],
    separador: str | None,
    nombre: str,
    carpetas_gmail: set[str],
    copiados_en_esta_ejecucion: set[str],
) -> tuple[str, bool]:
    """Copia un mensaje nuevo y devuelve el resultado y si usó ID sintético."""
    respuesta_cabecera = origen.fetch(
        [uid], ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"]
    )
    cabecera = obtener_cabecera_message_id(respuesta_cabecera[uid])
    message_id = obtener_id_mensaje(cabecera)
    mensaje_para_gmail: bytes | None = None
    fecha_interna: datetime | None = None
    id_sintetico = False

    if not message_id:
        # El hash se calcula sobre los bytes originales, antes de añadir la
        # única cabecera sintética que tendrá la copia en Gmail.
        respuesta = origen.fetch([uid], ["BODY.PEEK[]", "INTERNALDATE"])
        mensaje_original, fecha_interna = obtener_cuerpo_y_fecha(respuesta[uid])
        hash_mensaje = hashlib.sha256(mensaje_original).hexdigest()
        message_id = f"{hash_mensaje}@copia.merom"
        mensaje_para_gmail = (
            f"Message-ID: <{message_id}>\r\n".encode("ascii") + mensaje_original
        )
        id_sintetico = True

    consulta = f"rfc822msgid:{message_id}"
    uids_existentes = gmail.gmail_search(consulta)
    if uids_existentes:
        # Si esta ejecución ya copió el mismo mensaje desde otra carpeta,
        # aplica la etiqueta adicional sin crear otra copia.
        if message_id in copiados_en_esta_ejecucion:
            etiquetas = etiqueta_origen(flags, nombre, separador)
            asegurar_etiquetas(gmail, etiquetas, carpetas_gmail)
            extras = [
                etiqueta for etiqueta in etiquetas if etiqueta != ETIQUETA_BASE
            ]
            if extras:
                gmail.add_gmail_labels(uids_existentes, extras)
            return "ubicacion_adicional", id_sintetico
        return "duplicado", id_sintetico

    if mensaje_para_gmail is None:
        # Para IDs válidos, el cuerpo completo solo se pide tras comprobar Gmail.
        respuesta = origen.fetch([uid], ["BODY.PEEK[]", "INTERNALDATE"])
        mensaje_para_gmail, fecha_interna = obtener_cuerpo_y_fecha(respuesta[uid])

    etiquetas = etiqueta_origen(flags, nombre, separador)
    asegurar_etiquetas(gmail, etiquetas, carpetas_gmail)

    # APPEND conserva el mensaje original, salvo el único header sintético
    # en los casos que carecen de un Message-ID válido.
    gmail.append(ETIQUETA_BASE, mensaje_para_gmail, msg_time=fecha_interna)
    copiados_en_esta_ejecucion.add(message_id)

    # Localiza el UID creado y agrega etiquetas al mismo mensaje.
    uids_gmail = gmail.gmail_search(consulta)
    if not uids_gmail:
        raise RuntimeError("Gmail no encontró el mensaje recién añadido")
    etiquetas_extra = [
        etiqueta for etiqueta in etiquetas if etiqueta != ETIQUETA_BASE
    ]
    if etiquetas_extra:
        gmail.add_gmail_labels(uids_gmail, etiquetas_extra)
    return "copiado", id_sintetico


def revisar_carpeta(
    origen: IMAPClient,
    gmail: IMAPClient,
    carpeta: tuple[tuple[Any, ...], str | None, str],
    desde: Any,
    carpetas_gmail: set[str],
    copiados_en_esta_ejecucion: set[str],
    estadisticas: dict[str, int],
    procesados: set[int] | None = None,
    detalle: tuple[int, int] | None = None,
) -> None:
    """Revisa una carpeta; permite omitir UIDs ya vistos en la vigilancia."""
    flags, separador, nombre = carpeta
    try:
        # El origen se vuelve a seleccionar en solo lectura en cada pasada.
        origen.select_folder(nombre, readonly=True)
        uids = origen.search(["SINCE", desde])
    except Exception as error:
        estadisticas["errores"] += 1
        logging.error(
            "No se pudo revisar una carpeta de origen (%s).",
            type(error).__name__,
        )
        return

    if detalle is not None:
        indice, total = detalle
        logging.info(
            "Carpeta %d/%d: %d mensajes recientes.",
            indice,
            total,
            len(uids),
        )

    for uid in uids:
        if procesados is not None and uid in procesados:
            continue
        try:
            resultado, id_sintetico = procesar_mensaje(
                origen,
                gmail,
                uid,
                flags,
                separador,
                nombre,
                carpetas_gmail,
                copiados_en_esta_ejecucion,
            )
            if resultado == "copiado":
                estadisticas["copiados"] += 1
                if id_sintetico:
                    estadisticas["ids_sinteticos"] += 1
            elif resultado == "duplicado":
                estadisticas["duplicados"] += 1
            else:
                estadisticas["ubicaciones_adicionales"] += 1
            if procesados is not None:
                procesados.add(uid)
        except Exception as error:
            estadisticas["errores"] += 1
            logging.error(
                "Error al procesar un mensaje de origen (%s); se continúa.",
                type(error).__name__,
            )


def copiar(dias: int, secretos: dict[str, str]) -> int:
    """Copia los mensajes recientes; devuelve el número de errores."""
    desde = datetime.now(timezone.utc).date() - timedelta(days=dias - 1)
    estadisticas = {
        "errores": 0,
        "copiados": 0,
        "duplicados": 0,
        "ubicaciones_adicionales": 0,
        "ids_sinteticos": 0,
    }
    copiados_en_esta_ejecucion: set[str] = set()

    with (
        IMAPClient(ORIGEN_SERVIDOR, port=PUERTO_IMAP, ssl=True) as origen,
        IMAPClient(GMAIL_SERVIDOR, port=PUERTO_IMAP, ssl=True) as gmail,
    ):
        # Mantiene el huso horario de INTERNALDATE al pasarlo a Gmail.
        origen.normalise_times = False
        try:
            origen.login(secretos["ORIGEN_USUARIO"], secretos["ORIGEN_PASSWORD"])
            logging.info("Login en info@merom.eu correcto.")
        except Exception:
            logging.error("Fallo al entrar en info@merom.eu: revisa ORIGEN_USUARIO y ORIGEN_PASSWORD.")
            raise
        try:
            gmail.login(secretos["GMAIL_USUARIO"], secretos["GMAIL_APP_PASSWORD"])
            logging.info("Login en Gmail correcto.")
        except Exception:
            logging.error("Fallo al entrar en Gmail: revisa GMAIL_USUARIO y que GMAIL_APP_PASSWORD sea la contraseña de aplicación (16 letras), no la normal.")
            raise

        carpeta_todos, carpetas_gmail = buscar_carpeta_todos(gmail)
        # El origen siempre queda en solo lectura; Gmail se abre para poder
        # añadir etiquetas al mensaje que acabamos de copiar.
        gmail.select_folder(carpeta_todos, readonly=False)

        carpetas = []
        for flags, separador, nombre in origen.list_folders():
            atributos = {texto_imap(flag).casefold() for flag in flags}
            if r"\noselect" in atributos:
                continue
            if es_carpeta_excluida(flags, nombre, separador):
                continue
            carpetas.append((flags, separador, nombre))

        carpetas_vigiladas = [
            carpeta
            for carpeta in carpetas
            if es_bandeja_entrada(carpeta[0], carpeta[2])
            or es_carpeta_spam(carpeta[0], carpeta[2], carpeta[1])
            or es_carpeta_papelera(carpeta[0], carpeta[2], carpeta[1])
        ]
        procesados_vigilados = {carpeta[2]: set() for carpeta in carpetas_vigiladas}

        logging.info("Carpetas de origen que se revisarán: %d", len(carpetas))
        for indice_carpeta, carpeta in enumerate(carpetas, start=1):
            procesados = procesados_vigilados.get(carpeta[2])
            revisar_carpeta(
                origen,
                gmail,
                carpeta,
                desde,
                carpetas_gmail,
                copiados_en_esta_ejecucion,
                estadisticas,
                procesados=procesados,
                detalle=(indice_carpeta, len(carpetas)),
            )

        if carpetas_vigiladas:
            logging.info(
                "Vigilancia adicional de bandeja de entrada, Spam y Papelera "
                "durante 8 minutos."
            )
            siguiente_pasada = time.monotonic()
            for numero_pasada in range(1, 9):
                siguiente_pasada += 60
                espera = siguiente_pasada - time.monotonic()
                if espera > 0:
                    time.sleep(espera)
                logging.info("Pasada de vigilancia %d/8.", numero_pasada)
                for carpeta in carpetas_vigiladas:
                    revisar_carpeta(
                        origen,
                        gmail,
                        carpeta,
                        desde,
                        carpetas_gmail,
                        copiados_en_esta_ejecucion,
                        estadisticas,
                        procesados=procesados_vigilados[carpeta[2]],
                    )

    logging.info(
        "Resumen: copiados=%d, IDs sintéticos=%d, ubicaciones adicionales=%d, "
        "ya existentes=%d, errores=%d.",
        estadisticas["copiados"],
        estadisticas["ids_sinteticos"],
        estadisticas["ubicaciones_adicionales"],
        estadisticas["duplicados"],
        estadisticas["errores"],
    )
    return estadisticas["errores"]


def argumentos() -> argparse.Namespace:
    """Lee el número de días desde la línea de comandos."""
    parser = argparse.ArgumentParser(
        description="Copia mensajes IMAP recientes a Gmail."
    )
    parser.add_argument(
        "--dias",
        type=int,
        default=3,
        help="Días recientes que se revisan (por defecto: 3).",
    )
    args = parser.parse_args()
    if args.dias < 1:
        parser.error("--dias debe ser un número mayor que cero.")
    return args


def main() -> int:
    args = argumentos()
    try:
        secretos = validar_secretos()
        errores = copiar(args.dias, secretos)
        return 1 if errores else 0
    except Exception as error:
        # No se imprimen mensajes del servidor que pudieran incluir datos
        # privados; basta con el tipo de error para consultar el log.
        logging.error("La ejecución no pudo completarse (%s).", type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
