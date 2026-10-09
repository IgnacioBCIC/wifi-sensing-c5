# WiFi Sensing C5

Sistema de sensado Wi-Fi basado en CSI utilizando tres receptores ESP32-C5, un servidor Linux como punto de acceso y backend de procesamiento en Python. El proyecto permite capturar y validar CSI, administrar los receptores por MQTT/OTA, entrenar un autoencoder one-class híbrido para presencia humana y operar el sistema desde una interfaz web Flask.

## Arquitectura

```text
Servidor Linux / AP Wi-Fi
├── MQTT Mosquitto
├── Generador UDP WSEN
├── Controlador de captura
├── Receptor y validación CSI
├── Autoencoder híbrido
├── API/Web Flask
└── Servidor OTA
        │
        ├── ESP32-C5 rx01
        ├── ESP32-C5 rx02
        └── ESP32-C5 rx03
```

Los probes UDP se envían a los tres receptores. Cada ESP32-C5 captura CSI asociado al enlace Wi-Fi y devuelve las muestras al servidor, donde se validan, procesan y utilizan para inferencia o generación de datasets.

## Estructura

```text
controller/   Control, captura, recepción y procesamiento CSI
firmware/     Firmware ESP32-C5
models/       Modelos y normalizadores
mosquitto/    Configuración MQTT
ota-server/   Binarios OTA conservados
scripts/      Operación, captura, entrenamiento y despliegue
web/          Interfaz Flask
```

## Requisitos principales

- Linux con NetworkManager.
- Python 3 y entorno virtual `.venv`.
- Docker y Docker Compose.
- ESP-IDF 6.1.x para compilar firmware.
- NumPy y PyTorch para el autoencoder.
- Tres ESP32-C5 configurados como `rx01`, `rx02` y `rx03`.

## Configuración

Crear el archivo local de variables de entorno:

```bash
cp .env.example .env
```

Completar credenciales MQTT, datos de los receptores, interfaz Wi-Fi, perfiles de banda y parámetros OTA. El archivo `.env` no debe subirse al repositorio.

## Servicios base

Levantar Mosquitto y el servidor OTA:

```bash
docker compose up -d
```

## Firmware

Compilar un receptor:

```bash
./scripts/build_rx.sh rx01 2.0.3
```

Flashear por USB:

```bash
./scripts/flash_rx.sh rx01 2.0.3
```

Actualizar por OTA:

```bash
python scripts/ota_rx.py rx01 2.0.3
```

Consultar estado:

```bash
python scripts/status_rx.py
```

## Captura CSI

Cambiar de banda:

```bash
./scripts/set_band.sh 2.4g
./scripts/set_band.sh 5g
```

Ejecutar el flujo de experimento:

```bash
./scripts/experiment.sh
```

Los parámetros generales de captura se encuentran en:

```text
controller/experiment.json
```

## Autoencoder híbrido

El modelo incluido actualmente corresponde a 2.4 GHz:

```text
models/ae_presence/ae_24g_hybrid_v1/
```

Utiliza dos ramas:

- **Spectral:** detecta cambios relativos en la forma espectral del CSI.
- **Delta:** detecta variaciones temporales entre muestras consecutivas.

La decisión final considera presencia cuando al menos una rama supera su umbral calibrado.

Capturar una sesión EMPTY:

```bash
python scripts/ae_capture_session.py \
  --condition empty \
  --minutes 3 \
  --band 2.4g \
  --name empty_24g_01
```

Entrenar el modelo híbrido:

```bash
python scripts/ae_train_hybrid.py \
  --band 2.4g \
  --name ae_24g_hybrid_v1
```

Ejecutar inferencia en vivo:

```bash
python scripts/ae_live.py \
  --model models/ae_presence/ae_24g_hybrid_v1 \
  --band 2.4g
```

La calibración de umbrales se realiza con `scripts/ae_calibrate_hybrid.py` utilizando capturas EMPTY.

## Interfaz web

Instalar dependencias web:

```bash
./scripts/setup_web.sh
```

Ejecutar:

```bash
./scripts/run_web.sh
```

La web permite controlar capturas, consultar estado de los receptores, modificar la configuración del experimento, ejecutar inferencia y realizar calibración del modelo híbrido.
