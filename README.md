# SimpliTV

SimpliTV es un sistema privado de televisión web lineal basado en una biblioteca multimedia local. Los espectadores disfrutan de una experiencia continua tipo televisión tradicional, con canales programados y sin depender de un catálogo bajo demanda (VOD).

Este proyecto está optimizado para ejecutarse eficientemente en hardware de recursos modestos (como laptops antiguas o servidores locales ligeros) mediante **Direct Play con HTTP Range Requests**, evitando la transcodificación innecesaria en tiempo real.

---

## 🚀 Requisitos Previos

- Python 3.10+
- `ffmpeg` y `ffprobe` (instalados en el sistema para la extracción de metadatos)

---

## 🛠️ Instalación y Configuración

1. **Crear y activar el entorno virtual**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Instalar dependencias**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Colocar la biblioteca de canales**:
   La jerarquía canónica es `media/<CANAL>/Series/...` y `media/<CANAL>/Movies/...`:
   ```text
   media/
   └── Canal 1/
       ├── channel.yaml
       ├── Series/
       │   └── JoJo/
       │       ├── series.yaml
       │       └── Season 1/
       │           ├── S01E01 - Dio the Invader.mp4
       │           └── S01E02 - A Letter from the Past.mp4
       └── Movies/
           ├── Akira.mp4
           └── Harry Potter/
               ├── franchise.yaml
               └── Harry Potter and the Philosopher's Stone.mp4
   ```

   El escáner crea automáticamente `channel.yaml`, `Series/`, `Movies/`, `series.yaml` y `franchise.yaml` cuando falten. También se admite la jerarquía simplificada `media/<CANAL>/<SERIE>/<TEMPORADA>/<EPISODIO>` para bibliotecas existentes. Al actualizar desde una instalación antigua, la carpeta raíz `anime/` se migra automáticamente a `media/` cuando es posible.

## Configuración portable de canales

La configuración vive junto al contenido para que copiar/exportar una carpeta de canal conserve su comportamiento. SQLite sigue siendo un índice/estado operativo del servidor, pero las reglas de programación de los canales portables se leen del filesystem. El panel de administración es la interfaz normal para editar estas reglas; no es necesario escribir YAML manualmente.

### `series.yaml`

Se crea automáticamente en cada serie con estos valores:

```yaml
version: 1
episodes_per_airing: 1
start_episode:
  mode: any
playback:
  mode: random
selection_weight: 1
```

- `episodes_per_airing`: cantidad de episodios consecutivos cuando se selecciona la serie.
- `start_episode.mode`: `any`, `odd` o `even`; limita el episodio inicial de un bloque aleatorio y el primer inicio de una serie secuencial sin historial.
- `playback.mode: random`: cada nuevo bloque elige un episodio inicial según `start_episode`.
- `playback.mode: sequential`: cuando la serie vuelve a ser seleccionada continúa desde el episodio posterior al último emitido. El progreso es estado operativo del servidor y no se escribe en `series.yaml`.
- `selection_weight`: peso relativo de esta serie frente a otras series elegibles. Por ejemplo, pesos 3 y 1 producen aproximadamente una relación 75/25.
- Dentro de un bloque, los episodios siguientes siempre continúan cronológicamente.

### `franchise.yaml`

Cada subcarpeta dentro de `Movies/` recibe automáticamente:

```yaml
version: 1
name: Harry Potter
playback:
  mode: random
selection_weight: 1
```

- `name`: nombre visible portable de la franquicia.
- `playback.mode: random`: elige una película aleatoria dentro de la franquicia.
- `playback.mode: sequential`: continúa después de la última película emitida usando el orden por nombre/ruta de archivo. Para sagas con un orden estricto se recomiendan prefijos como `01 -`, `02 -`, etc.
- `selection_weight`: peso de la franquicia frente a otras franquicias y películas sueltas elegibles.

### `channel.yaml`

El archivo se crea automáticamente por canal. El horario usa ventanas; una ventana nunca corta el vídeo actual, sino que limita la siguiente selección cuando termina.

```yaml
version: 1
name: Canal 1
sensitive_content: false
schedule:
  default:
    - series
    - movies
  default_weights:
    series: 80
    movies: 20
  slots:
    - start: "20:00"
      end: "02:00"
      days:
        - friday
        - saturday
      programming:
        series:
          mode: off
          items: []
        movies:
          mode: only
          franchises:
            - Movies/Harry Potter
          movies:
            - Movies/Akira.mp4
      weights:
        series: 1
        movies: 1
loose_movie_weights:
  Movies/Akira.mp4: 3
```

#### Horarios y días

- La primera franja coincidente tiene prioridad.
- Si ninguna franja coincide, se usa `schedule.default` y `schedule.default_weights`.
- `days` admite `monday`, `tuesday`, `wednesday`, `thursday`, `friday`, `saturday` y `sunday`.
- En una franja que cruza medianoche, los días representan el día en que **empieza** la franja. Por ejemplo, `friday 22:00 -> 02:00` también cubre el sábado entre 00:00 y 02:00.

#### Programación de una franja

Cada tipo de contenido usa **una sola regla mutuamente excluyente** dentro de `programming`, evitando combinaciones contradictorias. Los modos disponibles son:

- `off`: no emitir ese tipo de contenido durante la franja.
- `all`: permitir todo el contenido de ese tipo.
- `only`: permitir únicamente los elementos seleccionados.
- `except`: permitir todo excepto los elementos seleccionados.

Para Series, `items` contiene rutas relativas como `Series/JoJo`. Para Películas, `franchises` contiene rutas de franquicias como `Movies/Harry Potter` y `movies` contiene películas sueltas como `Movies/Akira.mp4`.

Ejemplos:

```yaml
programming:
  series:
    mode: only
    items:
      - Series/JoJo
  movies:
    mode: off
    franchises: []
    movies: []
```

significa **solo JoJo y ninguna película**. En cambio:

```yaml
programming:
  series:
    mode: except
    items:
      - Series/JoJo
  movies:
    mode: all
    franchises: []
    movies: []
```

significa **todas las series excepto JoJo y todas las películas**.

Las rutas son relativas a la carpeta del canal y no dependen del nombre visible. Los `channel.yaml` antiguos con `series_include`, `series_exclude`, `franchise_include` y `movie_include` se migran automáticamente al nuevo modelo cuando se leen.

#### Pesos

Los pesos son relativos y no tienen que sumar 100:

- `schedule.default_weights` controla Series vs Películas fuera de franjas.
- `slots[].weights` controla Series vs Películas dentro de esa franja.
- `series.yaml -> selection_weight` controla una serie frente a otras series.
- `franchise.yaml -> selection_weight` controla una franquicia frente a otras opciones de películas.
- `channel.yaml -> loose_movie_weights` controla películas sueltas individuales.

`80/20`, `8/2` y `4/1` expresan la misma proporción. Un peso solo interviene cuando existen al menos dos candidatos que compiten en ese nivel.

### Tolerancia a contenido eliminado

Si una franja contiene una selección que ya no encuentra medios (por ejemplo, se borró una serie incluida), el selector registra un warning y relaja primero únicamente esa selección manteniendo el tipo `series`/`movies`. Si tampoco existe contenido del tipo permitido, utiliza otro contenido disponible para evitar dejar el canal sin señal.

### Administración visual

En **Administración → Gestión de Canales → Administrar** se puede configurar sin editar archivos:

- nombre y contenido sensible del canal;
- Series/Películas predeterminadas y sus pesos;
- franjas horarias, días de semana y franjas que cruzan medianoche;
- filtros de inclusión/exclusión de series por franja;
- franquicias y películas sueltas específicas por franja;
- pesos Series vs Películas por franja;
- episodios por emisión, inicio `any`/`odd`/`even`, modo `random`/`sequential` y peso de cada serie;
- nombre, modo `random`/`sequential` y peso de cada franquicia;
- peso individual de cada película suelta.

Cada guardado valida y escribe atómicamente el archivo correspondiente (`channel.yaml`, `series.yaml` o `franchise.yaml`). SQLite no conserva una segunda copia de estas reglas. Por ello, exportar la carpeta completa de un canal, una serie o una franquicia conserva la configuración que se veía en el panel.

---

## 📡 Ejecución del Servidor

Para permitir el acceso desde cualquier dispositivo de tu red local (LAN) y desde el propio equipo:

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- **Interfaz Web del Reproductor**: `http://localhost:8000` (o `http://<IP_LOCAL>:8000` desde el móvil, Smart TV o tablet)
- **Documentación de la API (Swagger UI)**: `http://localhost:8000/docs`
- **Documentación alternativa (ReDoc)**: `http://localhost:8000/redoc`

---

## 🎮 Controles del Reproductor Web

- **Pantalla Completa**: Tecla `F` o clic en el botón superior derecho.
- **Silenciar / Activar Sonido**: Tecla `M` o clic en la pantalla.
- **Pausar / Reanudar**: Tecla `Espacio`.
- **Siguiente Episodio**: Tecla `N` (o automático al terminar el episodio).
- **Mostrar/Ocultar OSD**: Tecla `I` o mover el ratón.

---

## 🧪 Pruebas Automatizadas

Para ejecutar la suite de pruebas:

```bash
source .venv/bin/activate
pytest -v
```

## ⚡ Optimización de biblioteca

El panel de administración incluye un optimizador de almacenamiento basado en el estado actual de los archivos:

- **Analizar biblioteca** recorre el filesystem y consulta cada vídeo con `ffprobe`; no escribe flags de optimización en SQLite ni añade metadatos especiales a los archivos.
- El perfil inicial usa **HEVC/H.265**, **CRF 24**, preset `medium` y una resolución máxima de **1920×1080**. Nunca aumenta la resolución.
- Solo se procesa un archivo cuando el análisis estima una reducción útil de espacio. Los HEVC que ya cumplen un bitrate razonable se conservan.
- Los archivos `.webm` y `.avi` se omiten para no cambiar su ruta/contenedor al usar HEVC.
- Cada conversión se escribe primero como `*.optimizing.*`; ese temporal es ignorado por el scanner y el watcher.
- Antes del encode se vuelve a comprobar que el archivo no sea el episodio actual ni el siguiente programado de ningún canal.
- El resultado se valida con `ffprobe`. Se descarta si cambia demasiado la duración, supera 1080p, no es HEVC, no reduce al menos un 8% el tamaño o presenta cualquier error.
- Solo después de validar se hace `os.replace()` sobre el original. Si el proceso falla o se interrumpe, el original permanece intacto.
- Los trabajos y análisis viven únicamente en memoria. Tras reiniciar, basta con analizar de nuevo: el filesystem vuelve a determinar qué falta por optimizar.

API administrativa:

```text
POST /api/admin/library/optimization/analyze
GET  /api/admin/library/optimization/analysis
POST /api/admin/library/optimization
GET  /api/admin/library/optimization/{job_id}
```

## Normalización de compatibilidad a MP4

El panel de administración incluye una herramienta independiente de la optimización HEVC para normalizar la biblioteca a un formato reproducible y predecible:

- destino canónico: **MP4 + H.264 + audio AAC/MP3 + subtítulos `mov_text`**;
- si el vídeo H.264 y el audio ya son compatibles, FFmpeg hace remux sin recodificar esos streams;
- codecs incompatibles se transcodifican a H.264/AAC;
- subtítulos de texto (SRT/ASS/SSA/WebVTT, etc.) se migran a `mov_text`;
- subtítulos de imagen no migrables de forma segura bloquean ese archivo y conservan el original;
- el resultado se escribe primero como `*.converting.mp4`, se valida con FFprobe y solo entonces reemplaza al original;
- los archivos en reproducción o programados inmediatamente después se omiten por seguridad;
- al finalizar se reescanea la biblioteca para actualizar las rutas `.mkv`/`.avi`/etc. a `.mp4`.

La normalización de compatibilidad y la optimización de tamaño son procesos distintos y no pueden ejecutarse simultáneamente.
