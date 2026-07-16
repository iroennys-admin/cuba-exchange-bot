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

1. Hablá con el bot en Telegram: [@eltoquecubabot](https://t.me/eltoquecubabot)
2. Enviá `/start` para registrarte
3. Usá `/tasas` para ver las cotizaciones del día
4. Usá `/sub` para recibir actualizaciones automáticas

## 🛠️ Despliegue

### Requisitos

- Python 3.10+
- Un token de bot de Telegram (de [@BotFather](https://t.me/BotFather))

### 🖥️ GitHub Codespaces (desarrollo)

[![Open in Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/Iroennys/cuba-exchange-bot)

1. Creá un **secret** en tu repositorio: `Settings → Secrets and variables → Codespaces` → `BOT_TOKEN` con el token de tu bot
2. Abrí el repositorio en Codespaces
3. Se instala solo y arranca el bot

> ⚠️ Codespaces se apaga a los 30 min de inactividad. No es ideal para un bot 24/7.

> 🐙 El bot ahora incluye un **modo GitHub** completo con `/github`. Configurá tu token con `/settoken`.

### ⚡ Un clic + token

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/iroennys-admin/cuba-exchange-bot)
[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template?template=https://github.com/iroennys-admin/cuba-exchange-bot)
[![Deploy to Koyeb](https://www.koyeb.com/static/images/deploy/button.svg)](https://app.koyeb.com/deploy?name=cuba-exchange-bot&type=docker&repository=iroennys-admin/cuba-exchange-bot&branch=master&env%5BBOT_TOKEN%5D=&env%5BZEN_API_KEY%5D=)

Render, Railway y Koyeb te piden **solo el token** y el bot arranca solo. Sin servidores, sin config.

### 🆓 Más opciones gratis 24/7

| Plataforma | Free tier | Setup |
|------------|-----------|-------|
| [Fly.io](https://fly.io) | 3 apps siempre activas | `fly launch` desde el repo |
| [Koyeb](https://koyeb.com) | 1 app siempre activa | Conectar repo, poner `BOT_TOKEN` |
| [Oracle Cloud](https://oracle.com/cloud/free) | VM ARM 4C/24GB **permanente** | SSH, instalación manual |

### 📱 Termux (Android)

```bash
pkg install python tmux
pip install -r requirements.txt
export BOT_TOKEN="tu_token_aqui"
termux-wake-lock
tmux new -s cubabot
python bot.py
# Ctrl+B, D para desacoplar
```

### 🐧 systemd (Linux)

```ini
[Unit]
Description=Cuba Exchange Rate Bot
After=network.target

[Service]
Type=simple
User=tu_usuario
WorkingDirectory=/path/to/cuba-exchange-bot
Environment="BOT_TOKEN=tu_token_aqui"
ExecStart=/usr/bin/python3 bot.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

## 📦 Estructura del proyecto

```
cuba-exchange-bot/
├── bot.py              # Bot principal (scraper + bot + scheduler + GitHub)
├── database.py         # SQLite — usuarios, tokens, suscriptores
├── github_client.py    # Cliente GitHub API
├── requirements.txt    # Dependencias
├── README.md           # Esta documentación
├── .gitignore
├── koyeb.yaml          # Config Koyeb
├── rates.json          # Últimas tasas (auto-generado)
└── subscribers.json    # IDs de suscriptores legacy (auto-generado)
```

## 🔧 Cómo funciona

1. Cada 30 minutos el bot consulta el canal público @eltoquecom en Telegram
2. Extrae las tasas usando expresiones regulares sobre el texto del mensaje
3. Compara con la última tasa conocida (almacenada en `rates.json`)
4. Si cambió, notifica a todos los suscriptores
5. Cada día a las 9:00 UTC envía un resumen a todos los suscriptores

## 📄 Licencia

MIT
