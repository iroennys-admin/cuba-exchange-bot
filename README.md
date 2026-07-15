# 🇨🇺 Cuba Exchange Rate Bot

Bot de Telegram que monitoriza el tipo de cambio informal del dólar (USD),
euro (EUR) y MLC en Cuba, publicado por **El Toque** (@eltoquecom).

**Creado por [Iroennys](https://github.com/Iroennys) — Telegram: [@nautaii](https://t.me/nautaii)**

## ✨ Funcionalidades

- **/tasas** — Muestra las cotizaciones actuales (USD, EUR, MLC → CUP)
- **/moneda USD|EUR|MLC** — Cotización específica de una moneda
- **/sub** — Activa notificaciones diarias y alertas de cambio
- **/unsub** — Desactiva notificaciones
- **/ayuda** — Mensaje de bienvenida y ayuda
- **Notificación diaria** — Resumen automático a las 9:00 UTC
- **Alerta de cambio** — Notificación cuando alguna tasa se modifica

## 🚀 Cómo usarlo

1. Hablá con el bot en Telegram (cuando esté desplegado)
2. Enviá `/start` para registrarte
3. Usá `/tasas` para ver las cotizaciones del día
4. Usá `/sub` para recibir actualizaciones automáticas

## 🛠️ Instalación y despliegue

### Requisitos

- Python 3.10+
- Un token de bot de Telegram (de [@BotFather](https://t.me/BotFather))

### Instalación

```bash
# Clonar el repositorio
git clone https://github.com/Iroennys/cuba-exchange-bot.git
cd cuba-exchange-bot

# Instalar dependencias
pip install -r requirements.txt

# Configurar el token
export BOT_TOKEN="tu_token_aqui"

# Ejecutar
python bot.py
```

### Despliegue en Termux (Android)

```bash
# Instalar dependencias
pkg install python
pip install -r requirements.txt

# Configurar token
echo 'export BOT_TOKEN="tu_token_aqui"' >> ~/.bashrc
source ~/.bashrc

# Mantener el dispositivo despierto
termux-wake-lock

# Ejecutar en sesión persistente (tmux)
pkg install tmux
tmux new -s cubabot
python bot.py
# Ctrl+B, D para desacoplar
```

### Despliegue con systemd (Linux)

```ini
[Unit]
Description=Cuba Exchange Rate Bot
After=network.target

[Service]
Type=simple
User=tu_usuario
WorkingDirectory=/path/to/cuba-exchange-bot
Environment="BOT_TOKEN=tu_token_aqui"
ExecStart=/usr/bin/python3 /path/to/cuba-exchange-bot/bot.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

## 📦 Estructura del proyecto

```
cuba-exchange-bot/
├── bot.py              # Bot principal (scraper + bot + scheduler)
├── requirements.txt    # Dependencias
├── README.md           # Esta documentación
├── .gitignore
├── rates.json          # Últimas tasas (auto-generado)
└── subscribers.json    # IDs de suscriptores (auto-generado)
```

## 🔧 Cómo funciona

1. Cada 30 minutos el bot consulta el canal público @eltoquecom en Telegram
2. Extrae las tasas usando expresiones regulares sobre el texto del mensaje
3. Compara con la última tasa conocida (almacenada en `rates.json`)
4. Si cambió, notifica a todos los suscriptores
5. Cada día a las 9:00 UTC envía un resumen a todos los suscriptores

## 📄 Licencia

MIT
