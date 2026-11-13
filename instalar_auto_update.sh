#!/bin/bash
# Roda UMA vez no console da Vultr. Depois o servidor se atualiza sozinho quando o site publica um index.pck novo.
set -e
cat > /usr/local/bin/bravor-auto-update <<'EOS'
#!/bin/bash
URL="https://jogar.bravorlegends.com.br/index.pck"
ARQ=/var/lib/bravor-auto.sig
SIG=$(curl -fsSI "$URL" | tr -d '\r' | grep -iE '^(etag|content-length|last-modified):' | sort | md5sum | cut -d' ' -f1)
[ -z "$SIG" ] && exit 0
if [ ! -f "$ARQ" ]; then echo "$SIG" > "$ARQ"; exit 0; fi
if [ "$SIG" != "$(cat $ARQ)" ]; then
  echo "$(date) pck mudou, atualizando"
  sleep 90   # espera o GitHub Pages terminar de propagar
  atualizar-bravor && echo "$SIG" > "$ARQ"
fi
EOS
chmod +x /usr/local/bin/bravor-auto-update
cat > /etc/systemd/system/bravor-auto-update.service <<'EOS'
[Unit]
Description=Atualiza servidor Bravor quando o site muda
[Service]
Type=oneshot
ExecStart=/usr/local/bin/bravor-auto-update
EOS
cat > /etc/systemd/system/bravor-auto-update.timer <<'EOS'
[Unit]
Description=Confere atualizacao do Bravor a cada 2 min
[Timer]
OnBootSec=60
OnUnitActiveSec=120
[Install]
WantedBy=timers.target
EOS
systemctl daemon-reload
systemctl enable --now bravor-auto-update.timer
/usr/local/bin/bravor-auto-update || true
echo "OK: auto-update instalado. Logs: journalctl -u bravor-auto-update"
