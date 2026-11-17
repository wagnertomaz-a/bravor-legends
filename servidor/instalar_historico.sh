#!/bin/bash
# INSTALA O HISTORICO DE PARTIDAS no servidor da Vultr (Bravor Legends, 18/11/2026).
# Rodar como root no console da Vultr:
#   curl -fsSL https://jogar.bravorlegends.com.br/servidor/instalar_historico.sh | bash
# Pode rodar de novo quantas vezes quiser (atualiza o programa e nao apaga o banco).
#  - programa:  /opt/bravor-historico/historico.py   (servico "bravor-historico", porta 9090 so por dentro)
#  - banco:     /opt/bravor-historico/historico.db   (backup todo dia as 4h em /opt/bravor-historico/backups, 14 dias)
#  - Caddy:     https://sp.bravorlegends.com.br/api/... -> historico (o jogo continua igual na mesma porta de sempre)
set -e
SITE="https://jogar.bravorlegends.com.br/servidor"
DIR=/opt/bravor-historico
CF=/etc/caddy/Caddyfile

echo "== 1/5 programa"
command -v python3 >/dev/null || { apt-get update -qq && apt-get install -y -qq python3; }
mkdir -p "$DIR/backups"
curl -fsSL "$SITE/historico.py?v=$(date +%s)" -o "$DIR/historico.py.novo"
python3 -m py_compile "$DIR/historico.py.novo"
mv "$DIR/historico.py.novo" "$DIR/historico.py"
if [ ! -s "$DIR/segredo" ]; then
  head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40 > "$DIR/segredo"
fi
chmod 755 "$DIR"; chmod 644 "$DIR/historico.py" "$DIR/segredo"

echo "== 2/5 servico"
cat > /etc/systemd/system/bravor-historico.service <<EOF
[Unit]
Description=Bravor Legends - historico de partidas
After=network.target

[Service]
WorkingDirectory=$DIR
Environment=HIST_PORTA=9090
Environment=HIST_BANCO=$DIR/historico.db
Environment=HIST_SEGREDO_ARQ=$DIR/segredo
ExecStart=/usr/bin/python3 $DIR/historico.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable bravor-historico >/dev/null 2>&1
systemctl restart bravor-historico

echo "== 3/5 backup diario"
cat > "$DIR/backup.py" <<'EOF'
import sqlite3, time, os, glob
d = "/opt/bravor-historico"
destino = os.path.join(d, "backups", "historico-%s.db" % time.strftime("%Y%m%d"))
src = sqlite3.connect(os.path.join(d, "historico.db"))
dst = sqlite3.connect(destino)
src.backup(dst)
dst.close(); src.close()
for f in sorted(glob.glob(os.path.join(d, "backups", "historico-*.db")))[:-14]:
    os.remove(f)
EOF
echo "0 4 * * * root /usr/bin/python3 $DIR/backup.py" > /etc/cron.d/bravor-historico

echo "== 4/5 Caddy (/api -> historico)"
if [ -f "$CF" ] && ! grep -q "127.0.0.1:9090" "$CF"; then
  cp "$CF" "$CF.antes-historico"
  python3 - "$CF" <<'EOF'
import re, sys
f = sys.argv[1]
linhas = open(f, encoding="utf-8").read().split("\n")
for i, l in enumerate(linhas):
    if re.match(r"^\s*(https?://)?sp\.bravorlegends\.com\.br[^{]*\{\s*$", l):
        linhas[i+1:i+1] = ["\thandle_path /api/* {", "\t\treverse_proxy 127.0.0.1:9090", "\t}"]
        open(f, "w", encoding="utf-8").write("\n".join(linhas))
        print("Caddyfile: rota /api adicionada")
        break
else:
    print("ATENCAO: nao achei o bloco 'sp.bravorlegends.com.br {' no Caddyfile; nada mudou")
EOF
  if caddy validate --config "$CF" --adapter caddyfile >/dev/null 2>&1; then
    systemctl reload caddy
  else
    echo "ATENCAO: o Caddyfile novo deu erro; voltei o antigo"
    cp "$CF.antes-historico" "$CF"
  fi
else
  echo "Caddy ja estava com a rota /api (ou sem Caddyfile)"
fi

echo "== 5/5 conferindo"
sleep 2
echo -n "historico (por dentro): "; curl -s http://127.0.0.1:9090/saude || echo "FALHOU"
echo
echo -n "historico (pela internet): "; curl -s https://sp.bravorlegends.com.br/api/saude || echo "FALHOU"
echo
systemctl is-active bravor-historico
echo "Pronto. Agora rode: atualizar-bravor"
