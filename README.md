# Cobros con Mercado Pago (Checkout Pro + webhooks)

API en **Python + FastAPI + SQLite** que integra cobros con **Mercado Pago** usando la API REST oficial con
`httpx` (sin SDK):

- Crea una **orden** y su **preferencia de Checkout Pro** y devuelve el **link de pago** (`init_point`).
- Recibe las **notificaciones (webhooks)**, valida la **firma `x-signature`** (HMAC-SHA256), consulta el pago a
  la API y actualiza la orden: `pendiente` / `aprobada` / `rechazada` / `reembolsada` (+ `en_revision`).
- Es **idempotente**: la misma notificación dos veces no duplica nada (ni eventos ni avisos).
- **Reembolso total** desde la API.
- **Aviso al dueño** cuando se aprueba un pago: webhook propio (Slack, Discord, n8n, Zapier...) y/o email SMTP.
- **Simulador local** de Mercado Pago: checkout falso + notificaciones firmadas, para probar todo sin cuenta.

## Qué resuelve

El problema típico: "la gente paga por Mercado Pago pero yo me entero mirando la app y confirmo a mano".
Con esto la web genera el link de pago, y cuando Mercado Pago aprueba el pago la orden queda **aprobada sola**
y el dueño recibe un aviso. La página de "gracias" no decide nada: el estado lo fija el webhook después de
consultar a Mercado Pago, así que no se puede falsificar cambiando la URL.

## Pedidos reales de Workana que cubre

- "Integrar Mercado Pago en mi web / tienda (Checkout Pro)".
- "Necesito generar links de pago de Mercado Pago desde mi sistema".
- "Confirmar pagos automáticamente con webhooks de Mercado Pago" / "las notificaciones IPN no me funcionan".
- "Cuando alguien paga quiero que me llegue un mail / mensaje" o "que se habilite el acceso al curso".
- "Backend para venta de entradas / turnos / cursos con cobro por Mercado Pago".
- "Migrar del SDK viejo / IPN a webhooks con validación de firma".
- "Hacer reembolsos desde mi panel sin entrar a Mercado Pago".

## Probarlo en 1 minuto (sin cuenta ni claves)

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt     # Linux/Mac: .venv/bin/python
.venv/Scripts/python.exe demo.py
```

`demo.py` recorre todo contra el simulador: crea una orden, la paga, llega la notificación firmada, se
repite la notificación (no duplica), se rechaza una firma falsa y se hace el reembolso.

En el navegador:

```bash
.venv/Scripts/python.exe -m app
```

Abrir http://127.0.0.1:8000 → "Crear orden de prueba y pagar" → elegir aprobado / rechazado / pendiente en el
checkout simulado. La documentación interactiva está en http://127.0.0.1:8000/docs.

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/salud` | Chequeo de salud (modo y base de datos) |
| POST | `/ordenes` | Crea orden + preferencia y devuelve `link_pago` |
| GET | `/ordenes?estado=&limite=&desde=` | Lista órdenes (más nuevas primero) |
| GET | `/ordenes/{id}` | Ve una orden con ítems e historial de eventos |
| POST | `/ordenes/{id}/reembolso` | Reembolso total (solo órdenes aprobadas) |
| POST | `/webhooks/mercadopago` | Recibe las notificaciones de Mercado Pago |
| GET | `/pago/resultado` | Página simple de vuelta del checkout (back_urls) |
| * | `/simulador/...` | Solo en `MP_MODO=simulador`: checkout falso y pagos simulados |

Si `ADMIN_API_KEY` está definida, `/ordenes` exige el encabezado `X-API-Key`. El webhook es público y lo
protege la firma.

Ejemplo:

```bash
POST /ordenes
{"items": [{"titulo": "Remera talle M", "cantidad": 2, "precio_unitario": "1500.00"}],
 "comprador_email": "cliente@correo.com"}

201 -> {"id": "db86af29d3584b5d", "estado": "pendiente", "total": 3000.0, "moneda": "ARS",
        "link_pago": "https://www.mercadopago.com.ar/checkout/v1/redirect?pref_id=...", ...}
```

## Cómo funciona el webhook

1. Mercado Pago hace `POST /webhooks/mercadopago?data.id=<pago>&type=payment` con los encabezados
   `x-signature: ts=...,v1=...` y `x-request-id`.
2. Se arma el manifest `id:<data.id>;request-id:<x-request-id>;ts:<ts>;` y se compara el HMAC-SHA256 con
   `MP_WEBHOOK_SECRET` (comparación en tiempo constante). Firma mala o faltante → **401**.
3. Si la notificación (por su `id`) ya se procesó → **200 `duplicada`** sin tocar nada.
4. Se consulta `GET /v1/payments/{id}` (nunca se confía en el cuerpo de la notificación) y se busca la orden por
   `external_reference`.
5. Se actualiza la orden en una transacción, registrando la notificación con clave única (aguanta dos
   notificaciones iguales en paralelo).
6. Si pasó a `aprobada`, se manda el aviso al dueño (una sola vez).

Casos cubiertos: API de Mercado Pago caída → **503** (Mercado Pago reintenta); pago inexistente (las
notificaciones de prueba del panel) → 200 para que no reintente; un rechazo tardío de otro intento no pisa una
orden aprobada; si el monto pagado es menor al total o la moneda no coincide, la orden queda **`en_revision`**
y no se avisa; un reembolso hecho desde el panel de Mercado Pago llega por webhook y deja la orden
`reembolsada`.

## Instalar y configurar

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
copy .env.example .env      # Linux/Mac: cp .env.example .env
```

Variables principales (todas están explicadas en `.env.example`):

| Variable | Para qué |
|---|---|
| `MP_MODO` | `simulador`, `sandbox` o `produccion` |
| `MP_ACCESS_TOKEN` | Access token de la aplicación de Mercado Pago |
| `MP_WEBHOOK_SECRET` | Clave secreta de Webhooks (obligatoria en producción) |
| `BASE_URL_PUBLICA` | URL pública de esta API (para `notification_url` y las páginas de vuelta) |
| `URL_EXITO` / `URL_FALLO` / `URL_PENDIENTE` | A dónde vuelve el comprador (la web del cliente) |
| `MONEDA`, `NOMBRE_TIENDA` | Moneda de la cuenta y texto del resumen de tarjeta |
| `ADMIN_API_KEY` | Protege `/ordenes` |
| `AVISO_WEBHOOK_URL`, `SMTP_*`, `AVISO_EMAIL_PARA` | Avisos al dueño |

## Credenciales de prueba (sandbox), paso a paso

Los nombres de los menús de Mercado Pago cambian cada tanto; la idea es siempre la misma.

1. Entrar con la cuenta de Mercado Pago del cliente (o una propia) a **Mercado Pago Developers →
   Tus integraciones** (https://www.mercadopago.com.ar/developers/panel/app).
2. **Crear aplicación**: tipo "Pagos online", producto **Checkout Pro**.
3. En la aplicación, ir a **Cuentas de prueba** y crear dos: un **vendedor** y un **comprador** (mismo país).
4. Entrar (en una ventana de incógnito) con el usuario **vendedor de prueba**, crear ahí una aplicación y copiar
   su **Access Token** (de "Credenciales de producción" de esa cuenta de prueba; en cuentas viejas también sirve
   el de "Credenciales de prueba", que empieza con `TEST-`). Eso va en `MP_ACCESS_TOKEN` con `MP_MODO=sandbox`.
5. **Webhooks**: en la aplicación → **Webhooks → Configurar notificaciones** → URL de modo prueba
   `https://<tu-url-publica>/webhooks/mercadopago`, evento **Pagos**. Al guardar aparece la **clave secreta** →
   `MP_WEBHOOK_SECRET`. El botón "Simular" manda una notificación con un pago inventado (la API responde 200
   `pago_no_encontrado`, es lo esperado).
6. Para que Mercado Pago llegue a tu PC, exponer el puerto con un túnel, por ejemplo
   `cloudflared tunnel --url http://localhost:8000` o `ngrok http 8000`, y poner esa URL en `BASE_URL_PUBLICA`.
7. Pagar el `link_pago` logueado con el **comprador de prueba** y una tarjeta de prueba (por ejemplo Mastercard
   `5031 7557 3453 0604`, CVV `123`, vencimiento `11/30`). El **nombre del titular** define el resultado:
   `APRO` aprobado, `OTHE` rechazado, `CONT` pendiente. Las tarjetas vigentes están en la documentación de
   Checkout Pro ("Tarjetas de prueba").

Para producción: cambiar a `MP_MODO=produccion`, usar las credenciales de producción de la cuenta real del
cliente, configurar el webhook de modo producción (tiene su propia clave secreta) y definir `ADMIN_API_KEY`.

## Pruebas

```bash
.venv/Scripts/python.exe -m pytest
```

87 pruebas, sin internet ni claves: Mercado Pago se reemplaza por el simulador (que es un transporte `httpx`
falso) o por transportes `httpx.MockTransport` que devuelven errores a pedido. Cubren firma, idempotencia,
concurrencia, errores de la API, montos, reembolsos, avisos (webhook y SMTP falsos) y configuración.

## Desplegar

- **Render / Railway / Fly.io**: comando de inicio `python -m app` con `HOST=0.0.0.0` y `PORT` del servicio.
  Montar un disco/volumen persistente y apuntar `DB_PATH` ahí (si no, la base se borra en cada deploy).
- **Docker**: `docker build -t pagos .` y `docker run -p 8000:8000 --env-file .env -v pagos-datos:/datos pagos`
  (en ese `.env` poner `HOST=0.0.0.0` y `DB_PATH=/datos/pagos.db`).
- **VPS**: `uvicorn app.main:crear_app --factory --host 127.0.0.1 --port 8000` detrás de Nginx con HTTPS
  (Mercado Pago exige HTTPS en la `notification_url` de producción), como servicio de systemd.
- Una sola instancia alcanza para una tienda chica/mediana. Para varias instancias, pasar a PostgreSQL.

## Estructura

```
app/
  config.py       variables de entorno y validación
  mercadopago.py  cliente REST (preferencias, pagos, reembolsos) + mapeo de estados
  firma.py        validación de x-signature
  servicio.py     lógica: crear orden, procesar notificación, reembolsar
  db.py           SQLite: ordenes, items, notificaciones, eventos
  avisos.py       aviso al dueño por webhook y SMTP
  simulador.py    Mercado Pago falso + checkout simulado
  main.py         API FastAPI
demo.py           demo de 1 minuto
tests/            pruebas (pytest)
```

## CÓMO ADAPTARLO A UN CLIENTE

| # | Qué cambiar | Dónde | Tiempo |
|---|---|---|---|
| 1 | Credenciales, moneda, nombre de la tienda, URLs de vuelta | `.env` | 15 min |
| 2 | Campos de la orden que manda la web (talle, DNI, dirección, id de usuario...) | `OrdenEntrada` en `app/main.py`, tabla `ordenes` en `app/db.py`, `crear_orden` en `app/servicio.py` | 30-60 min |
| 3 | Precios desde su catálogo en vez de confiar en el precio que manda el front | `crear_orden` en `app/servicio.py` (buscar el precio por id de producto) | 30-90 min |
| 4 | Qué pasa cuando se aprueba (habilitar curso, descontar stock, mandar entrada) | `procesar_notificacion` en `app/servicio.py`, donde se calcula `aviso` | 1-3 h |
| 5 | Texto y canal de los avisos (mail con su marca, WhatsApp vía webhook de n8n) | `app/avisos.py` | 30 min |
| 6 | Extras de la preferencia: cuotas máximas, excluir medios de pago, vencimiento del link | `armar_preferencia` en `app/servicio.py` (`payment_methods`, `expires`, `expiration_date_to`) | 20 min |
| 7 | Conectar la web del cliente (botón "Pagar" que llama a `POST /ordenes` y redirige a `link_pago`) | su front / backend | 30-60 min |
| 8 | Webhook en el panel, prueba de punta a punta en sandbox con tarjetas de prueba | panel de Mercado Pago | 30-45 min |
| 9 | Deploy con HTTPS y volumen para la base | ver "Desplegar" | 30-60 min |
| 10 | Pruebas nuevas para lo que se agregó | `tests/` | 30 min |

Total típico: **2-6 horas** según cuánto haya que tocar del paso 4.

Antes de entregar: correr `pytest`, probar en sandbox un pago aprobado, uno rechazado y un reembolso, y
revisar que `.env` no esté en el repositorio.

## Qué preguntarle al cliente

1. ¿De qué país es la cuenta de Mercado Pago y en qué moneda cobra? ¿Ya tiene la aplicación creada en
   Mercado Pago Developers?
2. ¿Dónde está la web (WordPress, a medida, Tiendanube...) y quién llama a la API? ¿Hay un hosting con HTTPS para
   el backend o lo despliego yo?
3. ¿Qué vende y de dónde salen los precios (catálogo fijo, base de datos, lo carga a mano)?
4. ¿Qué tiene que pasar exactamente cuando un pago se aprueba (mail, habilitar acceso, descontar stock)?
5. ¿Cómo quiere enterarse de los pagos: mail, Slack/Discord, WhatsApp vía n8n/Zapier, un panel?
6. ¿Acepta pagos en efectivo (Rapipago/Pago Fácil) que quedan pendientes unos días? ¿Limita cuotas?
7. ¿Necesita reembolsos parciales o alcanza con el total?
8. ¿Me puede dar acceso a una cuenta de prueba o compartir las credenciales de prueba (nunca las de producción
   por chat)?

## Licencia

MIT - markbackend. Hecho con ayuda de herramientas de IA, revisado y probado.
