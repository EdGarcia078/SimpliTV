# Seguridad de SimpliTV

SimpliTV está diseñado como una aplicación privada de televisión doméstica. Su configuración predeterminada prioriza que pueda usarse directamente dentro de una LAN mediante `http://IP_LOCAL:8000`, sin exigir un dominio, Cloudflare ni un reverse proxy.

Este documento explica el modelo de seguridad y qué cambia cuando una instalación se expone fuera de la red doméstica.

## Primer inicio

Cuando la tabla de usuarios está vacía, SimpliTV crea automáticamente:

- usuario: `admin`
- contraseña: `admin123`

Estas credenciales existen para que una instalación nueva o una base de datos reiniciada pueda administrarse sin usar una CLI. El administrador queda marcado para **cambio obligatorio de contraseña** y no puede utilizar normalmente el reproductor, el panel administrativo ni los streams hasta establecer una contraseña nueva.

No expongas una instalación nueva a Internet antes de completar ese primer cambio.

## Usuarios

- No existe registro público.
- Solo un administrador puede crear usuarios, restablecer contraseñas y gestionar grupos/permisos.
- No existe recuperación de contraseña por correo. Un espectador que pierda su contraseña debe contactar con el administrador.
- Las contraseñas nuevas requieren al menos 12 caracteres y se almacenan con bcrypt.

## Sesiones

Las cookies de sesión son `HttpOnly` y `SameSite=Lax`.

Los tokens nuevos no se almacenan en texto plano en SQLite: el navegador conserva el token aleatorio y la base de datos guarda únicamente su SHA-256. Las sesiones antiguas creadas por versiones previas se migran automáticamente al formato hasheado cuando vuelven a utilizarse.

Una sesión se renueva durante el uso normal, pero existe una expiración absoluta para evitar que la misma credencial pueda renovarse indefinidamente. Cambiar una contraseña revoca las demás sesiones del usuario.

## Instalación doméstica por HTTP

El uso siguiente está soportado oficialmente:

```text
TV / móvil / PC
      │
      └── red local ──> http://192.168.x.x:8000
```

Por defecto `SECURE_COOKIES=false`, porque una cookie marcada `Secure` no funcionaría sobre HTTP local.

HTTP no cifra el tráfico. Este modo presupone una red doméstica de confianza. No publiques directamente el puerto 8000 del router hacia Internet utilizando HTTP.

## Exposición a Internet

Para acceso remoto se recomienda colocar SimpliTV detrás de HTTPS:

```text
Internet
   ↓
HTTPS / reverse proxy o túnel
   ↓
SimpliTV
```

En ese escenario configura al menos:

```env
SECURE_COOKIES=true
ENABLE_HSTS=true
```

HSTS solo debe activarse cuando el acceso de los usuarios sea exclusivamente HTTPS.

Cloudflare Tunnel/Access, Caddy, Nginx, Traefik u otras soluciones pueden utilizarse, pero ninguna forma parte obligatoria de SimpliTV.

## Proxies confiables

SimpliTV no confía por defecto en `X-Forwarded-For` ni en cabeceras equivalentes enviadas por clientes. Esto evita que un atacante falsifique su dirección para eludir controles como el rate limiting del login.

Si utilizas un proxy que controlas, añade únicamente la dirección o red de ese proxy a `TRUSTED_PROXIES`:

```env
TRUSTED_PROXIES=127.0.0.1,::1
```

Admite IPs y redes CIDR separadas por comas. No incluyas redes de clientes normales ni valores genéricos como `0.0.0.0/0`.

En un despliegue público también puedes limitar los valores aceptados del encabezado `Host` con, por ejemplo:

```env
ALLOWED_HOSTS=tv.example.com
```

Déjalo vacío en instalaciones LAN, donde IPs locales, `localhost` y nombres mDNS pueden variar.

## Autorización

Los permisos se vuelven a comprobar en servidor. Ocultar un botón en el frontend no concede ni retira acceso.

Las rutas privadas validan, según corresponda:

- sesión válida y usuario activo;
- cambio obligatorio de contraseña completado;
- rol de administrador;
- pertenencia a grupos;
- acceso al canal;
- bloqueos personales;
- preferencia de contenido sensible;
- existencia actual del canal y del medio.

Conocer un ID de episodio o llamar manualmente a `/api/stream/{id}` no evita estas comprobaciones. Los streams largos revalidan periódicamente la autorización y se interrumpen si la sesión o el permiso dejan de ser válidos.

## Acciones administrativas

Todo `/api/admin` requiere rol `admin`, incluido el sistema de actualización. El updater conserva su comportamiento normal de Git y no está disponible para espectadores o visitantes sin sesión.

## Protección del navegador

SimpliTV aplica, entre otras medidas:

- política same-origin para operaciones que modifican estado;
- rechazo de solicitudes CSRF cross-site de navegadores;
- Content Security Policy restrictiva;
- `X-Frame-Options: DENY` / `frame-ancestors 'none'` contra clickjacking;
- `X-Content-Type-Options: nosniff`;
- política de referrer restrictiva;
- Permissions Policy para APIs no utilizadas;
- ausencia de CORS permisivo;
- documentación OpenAPI deshabilitada por defecto.

La protección CSRF está pensada para navegadores. Clientes administrativos intencionales como `curl` no dependen de `Origin`, pero siguen necesitando una sesión/autenticación válida y todos los permisos del servidor.

## Archivos y biblioteca

SimpliTV no sirve el repositorio ni la base de datos como contenido estático. `.env`, `.git`, SQLite y archivos internos no deben estar publicados por el servidor web.

El streaming resuelve la ruta del archivo y exige que pertenezca a la raíz de medios configurada. Los nombres de archivo no se interpolan en comandos de shell para FFmpeg/FFprobe y la configuración YAML se carga con funciones seguras.

Mantén `.env`, la base SQLite y cualquier clave privada con permisos del sistema operativo apropiados para la cuenta que ejecuta SimpliTV.

## Rate limiting del login

Los intentos fallidos se limitan temporalmente por combinación de usuario/origen y también mediante un umbral mayor por origen. Los contadores viven en memoria: reiniciar el proceso los limpia. Es una defensa contra fuerza bruta básica, no un servicio anti-DDoS.

Para una instancia pública, un reverse proxy o proveedor perimetral puede añadir controles de abuso adicionales.

## API de desarrollo

`/docs`, `/redoc` y `/openapi.json` están deshabilitados de forma predeterminada. Para desarrollo local pueden habilitarse con:

```env
ENABLE_API_DOCS=true
```

No es necesario habilitarlos para usar SimpliTV normalmente.

## Contenido protegido por derechos de autor

Los controles de SimpliTV reducen el riesgo de acceso anónimo o accidental a una biblioteca privada, pero no conceden derechos de distribución sobre los archivos almacenados. Un usuario autorizado para reproducir un vídeo recibe sus datos y técnicamente puede capturarlos; SimpliTV no pretende implementar DRM.

La responsabilidad sobre licencias y derechos del contenido depende de cómo y dónde se utilice el software.

## Recomendaciones para una instancia pública

1. Cambia inmediatamente `admin/admin123` en el primer inicio.
2. Usa HTTPS y `SECURE_COOKIES=true`.
3. Activa HSTS únicamente cuando HTTPS sea exclusivo.
4. No hagas port-forward directo del puerto 8000 sobre HTTP.
5. Mantén Ubuntu/Linux, Python y dependencias actualizados.
6. Limita `TRUSTED_PROXIES` a proxies que realmente controles.
7. Haz copias de seguridad de SQLite y de la configuración portable de la biblioteca.
8. Revisa los logs después de cambios de despliegue o autenticación.

## Reporte de vulnerabilidades

Si descubres una vulnerabilidad, evita publicar credenciales, tokens, bases de datos o detalles explotables de una instalación real. Cuando el repositorio ofrezca un canal privado de reporte (por ejemplo, Security Advisories), úsalo antes de abrir un issue público con detalles sensibles.

## Dependencias

Las versiones mínimas de dependencias de seguridad se mantienen en `requirements.txt`. Al actualizar una instalación existente, usa `pip install --upgrade -r requirements.txt` para que un paquete antiguo que todavía satisfacía requisitos previos no permanezca instalado.
