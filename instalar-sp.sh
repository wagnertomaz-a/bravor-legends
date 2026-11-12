#!/bin/bash
# Instala o servidor do Bravor Legends num Ubuntu 24.04 (Vultr Sao Paulo).
# Uso:  curl -fsSL https://jogar.bravorlegends.com.br/instalar-sp.sh | bash -s sp.bravorlegends.com.br
# O servidor e o mesmo jogo do site (index.pck), rodando sem tela, atras de HTTPS (Caddy).
set -euo pipefail

DOMINIO="${1:-sp.bravorlegends.com.br}"
SITE="https://jogar.bravorlegends.com.br"
GODOT_ZIP="https://github.com/godotengine/godot/releases/download/4.7.2-stable/Godot_v4.7.2-stable_linux.x86_64.zip"
PASTA=/opt/bravor
PORTA=9080

if [ "$(id -u)" -ne 0 ]; then
  echo "Rode como root (no console da Vultr voce ja e root)."; exit 1
fi

echo "== 1/6 Pacotes =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y unzip curl ca-certificates caddy

echo "== 2/6 Usuario e pasta =="
id bravor >/dev/null 2>&1 || useradd --system --home "$PASTA" --shell /usr/sbin/nologin bravor
mkdir -p "$PASTA"

echo "== 3/6 Godot (sem tela) =="
if [ ! -x "$PASTA/godot" ]; then
  curl -fL "$GODOT_ZIP" -o /tmp/godot.zip
  unzip -o /tmp/godot.zip -d /tmp/godot_unzip
  mv /tmp/godot_unzip/Godot_v4.7.2-stable_linux.x86_64 "$PASTA/godot"
  chmod +x "$PASTA/godot"
  rm -rf /tmp/godot.zip /tmp/godot_unzip
fi

echo "== 4/6 Comando de atualizar =="
cat > /usr/local/bin/atualizar-bravor <<EOF
#!/bin/bash
# Baixa a versao mais nova do jogo (a mesma do site) e reinicia o servidor.
set -euo pipefail
curl -fsSL "$SITE/index.pck?t=\$(date +%s)" -o $PASTA/index.pck.novo
TAM=\$(stat -c %s $PASTA/index.pck.novo)
if [ "\$TAM" -lt 1000000 ]; then echo "Download suspeito (\$TAM bytes). Nada foi trocado."; exit 1; fi
mv $PASTA/index.pck.novo $PASTA/index.pck
chown bravor:bravor $PASTA/index.pck
systemctl restart bravor
echo "Servidor atualizado e reiniciado (\$TAM bytes)."
EOF
chmod +x /usr/local/bin/atualizar-bravor
/usr/local/bin/atualizar-bravor || true   # o servico ainda nao existe na 1a vez; o passo 5 cria

echo "== 5/6 Servico do jogo =="
chown -R bravor:bravor "$PASTA"
cat > /etc/systemd/system/bravor.service <<EOF
[Unit]
Description=Bravor Legends - servidor
After=network-online.target
Wants=network-online.target

[Service]
User=bravor
WorkingDirectory=$PASTA
ExecStart=$PASTA/godot --headless --main-pack $PASTA/index.pck res://servidor.tscn -- --port=$PORTA
Restart=always
RestartSec=2
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now bravor
systemctl restart bravor

echo "== 6/6 HTTPS (Caddy) para $DOMINIO =="
cat > /etc/caddy/Caddyfile <<EOF
$DOMINIO {
	reverse_proxy 127.0.0.1:$PORTA
}
EOF
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 22/tcp; ufw allow 80/tcp; ufw allow 443/tcp
fi
systemctl enable caddy
systemctl restart caddy

sleep 3
echo
echo "================ PRONTO ================"
systemctl is-active bravor && echo "Servidor do jogo: ligado"
journalctl -u bravor -n 5 --no-pager || true
echo
echo "IP deste servidor: $(curl -4 -s ifconfig.me || echo 'veja no painel da Vultr')"
echo "Agora crie no Cloudflare o registro DNS:  $DOMINIO  ->  esse IP  (nuvem CINZA, 'somente DNS')."
echo "O certificado HTTPS e emitido sozinho quando o DNS comecar a apontar para ca."
echo "Ver logs ao vivo:  journalctl -u bravor -f"
echo "Atualizar o jogo:  atualizar-bravor"
