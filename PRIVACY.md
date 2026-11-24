# Política de Privacidad

RsHub se ejecuta de forma local en tu ordenador y funciona únicamente con los datos de tu propio sistema. **No requiere cuentas de usuario, no incluye telemetría ni analíticas, y no contiene publicidad**. En ningún momento recopila ni envía información sobre ti, sobre tu equipo o sobre las tareas que realizas.

Toda la información y estado que maneja (configuración, registros locales, resultados de análisis, historial de pruebas de rendimiento y tareas programadas) se almacena exclusivamente en `%APPDATA%\RsHub` y `%LOCALAPPDATA%\RsHub` en tu disco. Las copias de seguridad de cambios que afectan a todo el equipo (servicios, claves del registro del sistema, cuarentena del desinstalador) se guardan en `%ProgramData%\RsHub`. Al desinstalar la aplicación, el asistente te ofrece la opción de eliminar todos estos datos de forma definitiva.

## Cuándo se conecta RsHub a Internet

Las únicas conexiones que realiza RsHub son para funciones indispensables y explícitas. Ninguna transmite datos personales:

| Cuándo | Destino | Propósito | Cómo desactivarlo |
|---|---|---|---|
| Al abrirse y periódicamente en segundo plano | `api.github.com` (releases del repositorio) | Comprobar si existe una nueva versión de RsHub. Solo consulta la versión más reciente en GitHub. | *Configuración > Actualizaciones > Buscar actualizaciones automáticamente* |
| Cuando hay una versión disponible o pulsas "Actualizar ahora" | `github.com` (releases del repositorio) | Descarga el instalador oficial o versión portable verificando su firma de integridad criptográfica. | *Configuración > Actualizaciones* (desmarcar opciones de actualización automática) |
| Cada 2 segundos mientras la ventana o el widget están visibles | `1.1.1.1` (Cloudflare), puerto 443 | Medir la latencia (ping) de tu conexión para las estadísticas en vivo. Solo abre y cierra la conexión inmediatamente, sin enviar datos. | *Configuración > General > Medir la latencia de la conexión* |
| Al abrir el Hangar de Aplicaciones | `api.winstall.app`, `raw.githubusercontent.com` y `google.com/s2/favicons` | Obtener los iconos de los programas del catálogo. Solo viaja el identificador del software. | No abrir el catálogo del Hangar de Aplicaciones |
| Al instalar o actualizar programas | Servidores oficiales de `winget` y repositorios oficiales de cada programa | Descarga e instalación directa de los paquetes solicitados por el usuario. | No iniciar instalaciones o actualizaciones |
| Al buscar o instalar controladores | Servidores oficiales de Windows Update (Microsoft) | Gestionado directamente por Windows, idéntico a usar la app de Configuración de Windows. | No iniciar búsquedas de controladores |
| Únicamente al pulsar "mostrar" junto a la IP pública en el Monitor en Vivo | `api.ipify.org` | Resolver y mostrar tu dirección IP pública. | No pulsar sobre el botón de mostrar IP |
| Únicamente al realizar la prueba de red en Benchmark | `speed.cloudflare.com` | Medir la velocidad de subida y bajada. | Nunca se ejecuta automáticamente, solo por acción explícita del usuario |
| Al pulsar "buscar en la web" en el Desinstalador | Abre tu navegador web predeterminado en `google.com` | Facilitar la búsqueda de guías de desinstalación específicas en tu navegador. | No pulsar en buscar en la web |

El instalador estándar no crea tareas programadas ocultas. Todo el tráfico y operaciones son transparentes y auditables directamente en el código fuente.

## Seguridad de tu sistema

RsHub lee la información de tu PC para presentártela en pantalla (hardware, controladores, programas, procesos, conexiones de red y elementos de arranque). Todo cambio en el sistema **solicita confirmación previa** y, siempre que es posible, genera un punto de restauración o copia de seguridad para poder revertirse con total tranquilidad.

## Preguntas y soporte

Puedes abrir un issue o consulta directamente en:  
https://github.com/Ruiz-RS/RsHub/issues
