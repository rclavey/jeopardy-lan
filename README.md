# Jeopardy LAN Setup

This project runs a local Jeopardy game using Flask and Socket.IO.

1. Run the server with `python3 jeopardy.py`.
2. The console will display the server address, e.g. `http://192.168.1.50:5050`.
   Share this address with players on the same Wi-Fi/LAN.
3. Players visit `http://<server-ip>:5050/` and the host visits
   `http://<server-ip>:5050/host`.

Use the displayed IP instead of `127.0.0.1` so other devices can connect.

**Troubleshooting:** If your browser says it can't establish a secure connection,
make sure the address begins with `http://` (not `https://`). Some browsers
automatically try HTTPS which this simple local server does not support.
