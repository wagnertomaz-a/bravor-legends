#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HISTORICO DE PARTIDAS - Bravor Legends (fase 2, 18/11/2026)

Servico pequeno e SEPARADO do servidor de jogo. So usa o Python que ja vem no servidor (sem instalar nada).
Guarda so numeros/textos curtos de cada partida num banco SQLite (um arquivo). A arte da tela de MVP fica no jogo:
o jogo pede os numeros e desenha a tela.

Quem grava: SO o servidor de jogo (na mesma maquina), com o "segredo" (arquivo /opt/bravor-historico/segredo).
Quem le: o jogo do jogador, com o login dele (token do Supabase).

Rotas (o Caddy manda https://sp.bravorlegends.com.br/api/... para ca, sem o "/api"):
  GET  /saude                     -> {"ok": true}
  GET  /historico                 -> ultimas partidas da conta do token (Authorization: Bearer <token Supabase>)
  GET  /partida/<id>              -> numeros completos de uma partida (para a tela de MVP)
  POST /interno/conta             -> {"token"} -> {"uid","nick"}   (so o servidor de jogo; com X-Segredo)
  POST /interno/partida           -> grava uma partida              (so o servidor de jogo; com X-Segredo)

Uso:  python3 historico.py   (variaveis: HIST_PORTA=9090, HIST_BANCO, HIST_SEGREDO_ARQ, HIST_SUPABASE_URL, HIST_SUPABASE_CHAVE)
"""
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTA = int(os.environ.get("HIST_PORTA", "9090"))
BANCO = os.environ.get("HIST_BANCO", "/opt/bravor-historico/historico.db")
SEGREDO_ARQ = os.environ.get("HIST_SEGREDO_ARQ", "/opt/bravor-historico/segredo")
SUPABASE_URL = os.environ.get("HIST_SUPABASE_URL", "https://ardlwpwmuntnwkkersuw.supabase.co")
SUPABASE_CHAVE = os.environ.get("HIST_SUPABASE_CHAVE", "sb_publishable_rJdpQcRmuBKGYBkJ3bDJDw_Ay4SA4xU")
ORIGENS = ["https://jogar.bravorlegends.com.br", "http://localhost", "http://127.0.0.1"]
LISTA_MAX = 30
CORPO_MAX = 64 * 1024

_trava = threading.Lock()
_cache_contas = {}   # token -> (validade, uid, nick)


def segredo():
    try:
        with open(SEGREDO_ARQ, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def abrir_banco():
    pasta = os.path.dirname(BANCO)
    if pasta:
        os.makedirs(pasta, exist_ok=True)
    con = sqlite3.connect(BANCO, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS partidas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data INTEGER NOT NULL,           -- unix (segundos)
            duracao REAL NOT NULL,           -- segundos de jogo
            vencedor INTEGER NOT NULL,       -- 0 azul, 1 vermelho
            sala TEXT,
            dados TEXT NOT NULL              -- JSON com os 6 jogadores (numeros da tela de MVP)
        );
        CREATE TABLE IF NOT EXISTS jogadores (
            partida_id INTEGER NOT NULL,
            uid TEXT NOT NULL,               -- conta (Supabase); so jogadores logados entram aqui
            lado INTEGER NOT NULL,
            time INTEGER NOT NULL,
            venceu INTEGER NOT NULL,
            champ TEXT NOT NULL,
            titulo TEXT NOT NULL,            -- MVP, SVP ou ""
            abates INTEGER, mortes INTEGER, assist INTEGER,
            dano REAL, nota REAL
        );
        CREATE INDEX IF NOT EXISTS jog_uid ON jogadores(uid, partida_id DESC);
    """)
    return con


DB = None


def conta_do_token(token):
    """Confere o token no Supabase (com cache de 10 min). Devolve (uid, nick) ou (None, None)."""
    token = (token or "").strip()
    if not token or len(token) > 4096:
        return None, None
    if os.environ.get("HIST_TESTE") == "1" and token.startswith("teste:"):
        return token[6:40], "Teste"   # so nos testes no PC (variavel HIST_TESTE=1); no servidor nao existe
    agora = time.time()
    c = _cache_contas.get(token)
    if c and c[0] > agora:
        return c[1], c[2]
    req = urllib.request.Request(SUPABASE_URL + "/auth/v1/user",
                                 headers={"apikey": SUPABASE_CHAVE, "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            u = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError):
        return None, None
    uid = str(u.get("id", ""))
    if not uid:
        return None, None
    meta = u.get("user_metadata") or {}
    nick = str(meta.get("nick") or meta.get("full_name") or meta.get("name") or "").strip()[:12]
    if len(_cache_contas) > 5000:
        _cache_contas.clear()
    _cache_contas[token] = (agora + 600, uid, nick)
    return uid, nick


def _num(v, padrao=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return padrao


def gravar_partida(d):
    jogadores = d.get("jogadores")
    if not isinstance(jogadores, list) or len(jogadores) != 6:
        return None
    vencedor = int(_num(d.get("vencedor"), -1))
    if vencedor not in (0, 1):
        return None
    limpos = []
    for j in jogadores:
        if not isinstance(j, dict):
            return None
        limpos.append({
            "lado": int(_num(j.get("lado"))), "time": int(_num(j.get("time"))),
            "uid": str(j.get("uid") or "")[:64], "nome": str(j.get("nome") or "")[:16],
            "bot": bool(j.get("bot")), "champ": re.sub(r"[^a-z_]", "", str(j.get("champ") or ""))[:16],
            "spell": re.sub(r"[^a-z_]", "", str(j.get("spell") or ""))[:16],
            "abates": int(_num(j.get("abates"))), "mortes": int(_num(j.get("mortes"))), "assist": int(_num(j.get("assist"))),
            "dano": round(_num(j.get("dano")), 1), "defesa": round(_num(j.get("defesa")), 1),
            "util": round(_num(j.get("util")), 2), "nota": round(_num(j.get("nota")), 2),
            "titulo": str(j.get("titulo") or "") if str(j.get("titulo") or "") in ("MVP", "SVP") else "",
        })
    dados = {"vencedor": vencedor, "duracao": round(_num(d.get("duracao")), 1), "jogadores": limpos}
    with _trava:
        cur = DB.execute("INSERT INTO partidas (data, duracao, vencedor, sala, dados) VALUES (?, ?, ?, ?, ?)",
                         (int(time.time()), dados["duracao"], vencedor, str(d.get("sala") or "")[:8],
                          json.dumps(dados, separators=(",", ":"))))
        pid = cur.lastrowid
        for j in limpos:
            if j["uid"]:
                DB.execute("INSERT INTO jogadores VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (pid, j["uid"], j["lado"], j["time"], 1 if j["time"] == vencedor else 0, j["champ"],
                            j["titulo"], j["abates"], j["mortes"], j["assist"], j["dano"], j["nota"]))
        DB.commit()
    return pid


def lista_da_conta(uid):
    with _trava:
        linhas = DB.execute("""
            SELECT j.partida_id, p.data, p.duracao, j.venceu, j.champ, j.titulo, j.abates, j.mortes, j.assist, j.dano, j.nota, j.lado
            FROM jogadores j JOIN partidas p ON p.id = j.partida_id
            WHERE j.uid = ? ORDER BY j.partida_id DESC LIMIT ?""", (uid, LISTA_MAX)).fetchall()
        tot = DB.execute("SELECT COUNT(*), SUM(venceu), SUM(titulo = 'MVP') FROM jogadores WHERE uid = ?", (uid,)).fetchone()
    lista = [{"id": l[0], "data": l[1], "duracao": l[2], "venceu": bool(l[3]), "champ": l[4], "titulo": l[5],
              "abates": l[6], "mortes": l[7], "assist": l[8], "dano": l[9], "nota": l[10], "lado": l[11]} for l in linhas]
    return {"partidas": lista, "total": tot[0] or 0, "vitorias": tot[1] or 0, "mvps": tot[2] or 0}


def partida(pid):
    with _trava:
        l = DB.execute("SELECT id, data, dados FROM partidas WHERE id = ?", (pid,)).fetchone()
    if not l:
        return None
    d = json.loads(l[2])
    d["id"] = l[0]
    d["data"] = l[1]
    for j in d.get("jogadores", []):
        j.pop("uid", None)   # quem jogou nao sai daqui: so o nome/nick
    return d


class Pedido(BaseHTTPRequestHandler):
    server_version = "BravorHistorico/1"

    def log_message(self, fmt, *args):   # log curto
        print("[historico] %s %s" % (self.address_string(), fmt % args), flush=True)

    def _cors(self):
        o = self.headers.get("Origin", "")
        if any(o == x or o.startswith(x + ":") for x in ORIGENS):
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")

    def _json(self, codigo, obj):
        b = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(b)

    def _interno_ok(self):
        # so o servidor de jogo: pedido direto (nao veio pela internet via Caddy) e com o segredo certo
        if self.headers.get("X-Forwarded-For") or self.headers.get("X-Forwarded-Host"):
            return False
        s = segredo()
        return bool(s) and self.headers.get("X-Segredo", "") == s

    def _corpo(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0 or n > CORPO_MAX:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return None

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        cam = self.path.split("?")[0].rstrip("/")
        if cam in ("", "/saude"):
            return self._json(200, {"ok": True})
        if cam == "/historico":
            auth = self.headers.get("Authorization", "")
            uid, _nick = conta_do_token(auth[7:] if auth.startswith("Bearer ") else "")
            if not uid:
                return self._json(401, {"erro": "login"})
            return self._json(200, lista_da_conta(uid))
        m = re.fullmatch(r"/partida/(\d{1,12})", cam)
        if m:
            d = partida(int(m.group(1)))
            return self._json(200, d) if d else self._json(404, {"erro": "nao achei"})
        return self._json(404, {"erro": "rota"})

    def do_POST(self):
        cam = self.path.split("?")[0].rstrip("/")
        if not cam.startswith("/interno/") or not self._interno_ok():
            return self._json(403, {"erro": "proibido"})
        d = self._corpo()
        if not isinstance(d, dict):
            return self._json(400, {"erro": "corpo"})
        if cam == "/interno/conta":
            uid, nick = conta_do_token(str(d.get("token", "")))
            return self._json(200, {"uid": uid or "", "nick": nick or ""})
        if cam == "/interno/partida":
            pid = gravar_partida(d)
            return self._json(200, {"id": pid}) if pid else self._json(400, {"erro": "dados"})
        return self._json(404, {"erro": "rota"})


def main():
    global DB
    DB = abrir_banco()
    srv = ThreadingHTTPServer(("127.0.0.1", PORTA), Pedido)
    print("[historico] ouvindo em 127.0.0.1:%d  banco=%s  segredo=%s" % (PORTA, BANCO, "ok" if segredo() else "FALTANDO"), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
