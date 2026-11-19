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
  POST /acesso                    -> o jogo avisa que esta aberto (ao abrir e a cada 5 min): {"aparelho","plat"}
                                     + Authorization do login (opcional). Sem IP: so um codigo aleatorio do aparelho.
  GET  /painel                    -> PAINEL do Wagner (pagina): jogadores por dia, horarios, contas
  GET  /painel/dados?dias=14      -> numeros do painel (cabecalho X-Painel = senha do arquivo painel_senha)

Uso:  python3 historico.py   (variaveis: HIST_PORTA=9090, HIST_BANCO, HIST_SEGREDO_ARQ, HIST_SUPABASE_URL, HIST_SUPABASE_CHAVE,
      HIST_PAINEL_ARQ (senha do painel), HIST_SUPABASE_ADMIN_ARQ (opcional: chave service_role para listar TODOS os cadastros))
"""
import hmac
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
PAINEL_ARQ = os.environ.get("HIST_PAINEL_ARQ", "/opt/bravor-historico/painel_senha")
ADMIN_ARQ = os.environ.get("HIST_SUPABASE_ADMIN_ARQ", "/opt/bravor-historico/supabase_admin")
FUSO = -3 * 3600            # horario de Brasilia (sem horario de verao)
PING_MIN = 240              # o mesmo aparelho so conta de novo depois de 4 min (o jogo avisa a cada 5)
SESSAO_NOVA = 1800          # 30 min sem aviso = nova sessao
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
        CREATE TABLE IF NOT EXISTS acessos (
            ts INTEGER NOT NULL,             -- unix (segundos)
            dia TEXT NOT NULL,               -- AAAA-MM-DD no horario de Brasilia
            hora INTEGER NOT NULL,           -- 0..23 no horario de Brasilia
            aparelho TEXT NOT NULL,          -- codigo aleatorio guardado no aparelho (nao e IP nem nada pessoal)
            uid TEXT NOT NULL DEFAULT '',    -- conta (Supabase) quando logado
            nick TEXT NOT NULL DEFAULT '',
            plat TEXT NOT NULL DEFAULT '',   -- web / android / windows ...
            inicio INTEGER NOT NULL DEFAULT 0  -- 1 = comeco de uma sessao (abriu o jogo)
        );
        CREATE INDEX IF NOT EXISTS ac_dia ON acessos(dia);
        CREATE INDEX IF NOT EXISTS ac_ap ON acessos(aparelho, ts);
        CREATE INDEX IF NOT EXISTS ac_ts ON acessos(ts);
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


# ---------------------------------------------------------------
# ACESSOS (painel do Wagner): quem abriu o jogo, por dia e por hora
_ult_ping = {}   # aparelho -> ultimo aviso (unix)


def _dia_hora(ts):
    t = time.gmtime(ts + FUSO)
    return time.strftime("%Y-%m-%d", t), t.tm_hour


def registrar_acesso(aparelho, plat, uid, nick):
    aparelho = (aparelho or "").lower()
    if not re.fullmatch(r"[0-9a-f]{16,40}", aparelho):
        return False
    plat = re.sub(r"[^a-z0-9_]", "", (plat or "").lower())[:12]
    agora = int(time.time())
    with _trava:
        ult = _ult_ping.get(aparelho)
        if ult is None:
            l = DB.execute("SELECT MAX(ts) FROM acessos WHERE aparelho = ?", (aparelho,)).fetchone()
            ult = l[0] if l and l[0] else 0
        if agora - ult < PING_MIN:
            return True   # aviso repetido: ja contou
        inicio = 1 if agora - ult > SESSAO_NOVA else 0
        dia, hora = _dia_hora(agora)
        DB.execute("INSERT INTO acessos (ts, dia, hora, aparelho, uid, nick, plat, inicio) VALUES (?,?,?,?,?,?,?,?)",
                   (agora, dia, hora, aparelho, uid or "", (nick or "")[:16], plat, inicio))
        DB.commit()
        if len(_ult_ping) > 50000:
            _ult_ping.clear()
        _ult_ping[aparelho] = agora
    return True


def _ler_arq(caminho):
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


_cache_cadastros = [0.0, None]


def cadastros_supabase():
    """Lista de TODOS os cadastros (so se existir a chave service_role no arquivo supabase_admin). Cache de 2 min."""
    chave = _ler_arq(ADMIN_ARQ)
    if not chave:
        return None
    if _cache_cadastros[1] is not None and time.time() - _cache_cadastros[0] < 120:
        return _cache_cadastros[1]
    lista = []
    pagina = 1
    while pagina <= 20:
        req = urllib.request.Request(SUPABASE_URL + "/auth/v1/admin/users?page=%d&per_page=500" % pagina,
                                     headers={"apikey": chave, "Authorization": "Bearer " + chave})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError, OSError):
            return {"erro": "nao consegui falar com o Supabase (confira a chave em supabase_admin)"}
        us = d.get("users", []) if isinstance(d, dict) else d
        for u in us:
            meta = u.get("user_metadata") or {}
            lista.append({"email": u.get("email") or "", "criado": u.get("created_at") or "",
                          "ultimo": u.get("last_sign_in_at") or "",
                          "nick": str(meta.get("nick") or meta.get("full_name") or meta.get("name") or "")[:20],
                          "via": ((u.get("app_metadata") or {}).get("provider") or "")})
        if len(us) < 500:
            break
        pagina += 1
    lista.sort(key=lambda x: x["criado"], reverse=True)
    res = {"total": len(lista), "lista": lista[:300]}
    _cache_cadastros[0] = time.time()
    _cache_cadastros[1] = res
    return res


def painel_dados(dias):
    dias = max(1, min(int(dias), 90))
    agora = int(time.time())
    hoje, hora_agora = _dia_hora(agora)
    lista_dias = [_dia_hora(agora - 86400 * k)[0] for k in range(dias - 1, -1, -1)]
    ini = lista_dias[0]
    with _trava:
        por_dia = {l[0]: l[1:] for l in DB.execute(
            "SELECT dia, COUNT(DISTINCT aparelho), COUNT(DISTINCT NULLIF(uid, '')), SUM(inicio) FROM acessos WHERE dia >= ? GROUP BY dia",
            (ini,)).fetchall()}
        novos = dict(DB.execute(
            "SELECT d, COUNT(*) FROM (SELECT MIN(dia) d FROM acessos GROUP BY aparelho) WHERE d >= ? GROUP BY d",
            (ini,)).fetchall())
        hora_hoje = dict(DB.execute("SELECT hora, COUNT(DISTINCT aparelho) FROM acessos WHERE dia = ? GROUP BY hora",
                                    (hoje,)).fetchall())
        hora_per = dict(DB.execute(
            "SELECT hora, COUNT(DISTINCT aparelho || dia) FROM acessos WHERE dia >= ? GROUP BY hora", (ini,)).fetchall())
        online = DB.execute("SELECT COUNT(DISTINCT aparelho) FROM acessos WHERE ts >= ?", (agora - 360,)).fetchone()[0]
        plats = DB.execute("SELECT plat, COUNT(DISTINCT aparelho) FROM acessos WHERE dia >= ? GROUP BY plat ORDER BY 2 DESC",
                           (ini,)).fetchall()
        tot_ap = DB.execute("SELECT COUNT(DISTINCT aparelho), COUNT(DISTINCT NULLIF(uid, '')) FROM acessos").fetchone()
        contas = DB.execute("""
            SELECT a.uid, (SELECT b.nick FROM acessos b WHERE b.uid = a.uid AND b.nick != '' ORDER BY b.ts DESC LIMIT 1),
                   MIN(a.ts), MAX(a.ts), COUNT(DISTINCT a.dia), SUM(a.inicio) FROM acessos a
            WHERE a.uid != '' GROUP BY a.uid ORDER BY MAX(a.ts) DESC LIMIT 200""").fetchall()
        part = dict(DB.execute(
            "SELECT strftime('%Y-%m-%d', data + ?, 'unixepoch'), COUNT(*) FROM partidas WHERE data >= ? GROUP BY 1",
            (FUSO, agora - 86400 * (dias + 1))).fetchall())
        ultimos = DB.execute("""
            SELECT ts, nick, plat, uid != '' FROM acessos WHERE inicio = 1 ORDER BY ts DESC LIMIT 25""").fetchall()
    dia_l = []
    for d in lista_dias:
        v = por_dia.get(d, (0, 0, 0))
        dia_l.append({"dia": d, "jogadores": v[0] or 0, "contas": v[1] or 0, "sessoes": v[2] or 0,
                      "novos": novos.get(d, 0), "partidas": part.get(d, 0)})
    hj = dia_l[-1]
    return {
        "agora": agora, "hoje": hoje, "hora_agora": hora_agora, "dias": dias,
        "online": online, "hoje_jogadores": hj["jogadores"], "hoje_contas": hj["contas"], "hoje_novos": hj["novos"],
        "hoje_sessoes": hj["sessoes"], "hoje_partidas": hj["partidas"],
        "total_aparelhos": tot_ap[0] or 0, "total_contas": tot_ap[1] or 0,
        "por_dia": dia_l,
        "por_hora_hoje": [hora_hoje.get(h, 0) for h in range(24)],
        "por_hora_media": [round(hora_per.get(h, 0) / float(dias), 2) for h in range(24)],
        "plataformas": [{"plat": p[0] or "?", "jogadores": p[1]} for p in plats],
        "contas": [{"nick": c[1] or "(sem nick)", "id": c[0][:8], "primeiro": c[2], "ultimo": c[3], "dias": c[4],
                    "sessoes": c[5]} for c in contas],
        "ultimos": [{"ts": u[0], "nick": u[1] or "", "plat": u[2], "conta": bool(u[3])} for u in ultimos],
        "cadastros": cadastros_supabase(),
    }


def _pagina_painel():
    return PAINEL_HTML.encode("utf-8")


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
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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
        if cam == "/painel":
            b = _pagina_painel()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex")
            self.end_headers()
            self.wfile.write(b)
            return
        if cam == "/painel/dados":
            senha = _ler_arq(PAINEL_ARQ)
            dada = self.headers.get("X-Painel", "")
            if not senha or not hmac.compare_digest(senha.encode("utf-8"), dada.encode("utf-8")):
                time.sleep(0.6)   # freia chutes de senha
                return self._json(401, {"erro": "senha"})
            q = self.path.split("?")[1] if "?" in self.path else ""
            mm = re.search(r"dias=(\d{1,3})", q)
            return self._json(200, painel_dados(int(mm.group(1)) if mm else 14))
        return self._json(404, {"erro": "rota"})

    def do_POST(self):
        cam = self.path.split("?")[0].rstrip("/")
        if cam == "/acesso":
            # o jogo avisa que esta aberto: conta 1 jogador (aparelho); com login, junta a conta
            d = self._corpo()
            if not isinstance(d, dict):
                return self._json(400, {"erro": "corpo"})
            auth = self.headers.get("Authorization", "")
            uid, nick = ("", "")
            if auth.startswith("Bearer "):
                uid, nick = conta_do_token(auth[7:])
            if not nick:
                nick = re.sub(r"[\x00-\x1f]", "", str(d.get("nick") or ""))[:16]
            ok = registrar_acesso(str(d.get("aparelho") or ""), str(d.get("plat") or ""), uid or "", nick or "")
            return self._json(200 if ok else 400, {"ok": ok})
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


PAINEL_HTML = r'''<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Painel Bravor</title>
<style>
:root{--bg:#070b1a;--card:#0f1630;--borda:#24305a;--txt:#e8ecf7;--suave:#94a0c4;--ouro:#ffc83d;--azul:#3fb6ff;--verde:#5be38a}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;padding:16px;border-bottom:1px solid var(--borda)}
h1{margin:0;font-size:20px}h1 b{color:var(--ouro)}h2{font-size:16px;margin:0 0 10px}
main{max-width:1100px;margin:0 auto;padding:16px;display:grid;gap:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--borda);border-radius:12px;padding:12px 14px}
.card .n{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums}.card .r{color:var(--suave);font-size:13px}
.card.ao .n{color:var(--verde)}
.bloco{background:var(--card);border:1px solid var(--borda);border-radius:12px;padding:14px;overflow-x:auto}
svg text{fill:var(--suave);font-size:11px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #1b2547;white-space:nowrap}
th{color:var(--suave);font-weight:600}
select,button,input{background:#141d3d;color:var(--txt);border:1px solid var(--borda);border-radius:8px;padding:7px 10px;font:inherit}
button{cursor:pointer}.leg{display:flex;gap:14px;color:var(--suave);font-size:13px;margin-bottom:6px}.leg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
#entrar{max-width:360px;margin:60px auto;display:grid;gap:10px}.erro{color:#ff7a6a}.nota{color:var(--suave);font-size:13px}
</style></head><body>
<header><h1><b>Bravor Legends</b> · Painel</h1>
<div><select id="dias"><option value="7">7 dias</option><option value="14" selected>14 dias</option><option value="30">30 dias</option></select>
<button id="atualizar">Atualizar</button> <span id="quando" class="nota"></span></div></header>
<div id="entrar" hidden><h2>Senha do painel</h2><input id="senha" type="password" autocomplete="current-password">
<button id="ok">Entrar</button><div id="msg" class="erro"></div>
<div class="nota">A senha aparece no fim do instalador no servidor (arquivo /opt/bravor-historico/painel_senha).</div></div>
<main id="tudo" hidden>
<div class="cards">
<div class="card ao"><div class="n" id="c_online">-</div><div class="r">jogando agora</div></div>
<div class="card"><div class="n" id="c_hoje">-</div><div class="r">jogadores hoje</div></div>
<div class="card"><div class="n" id="c_novos">-</div><div class="r">novos hoje</div></div>
<div class="card"><div class="n" id="c_contas">-</div><div class="r">contas logadas hoje</div></div>
<div class="card"><div class="n" id="c_part">-</div><div class="r">partidas online hoje</div></div>
<div class="card"><div class="n" id="c_total">-</div><div class="r">jogadores desde o início</div></div>
</div>
<div class="bloco"><h2>Jogadores diferentes por dia</h2>
<div class="leg"><span><i style="background:var(--azul)"></i>já jogaram antes</span><span><i style="background:var(--ouro)"></i>novos</span></div>
<svg id="g_dias" width="100%" height="230"></svg></div>
<div class="bloco"><h2>Horários (jogadores por hora)</h2>
<div class="leg"><span><i style="background:var(--azul)"></i>hoje</span><span><i style="background:var(--ouro)"></i>média do período</span></div>
<svg id="g_horas" width="100%" height="210"></svg></div>
<div class="bloco"><h2>Aparelhos</h2><div id="plats"></div></div>
<div class="bloco"><h2>Últimas entradas no jogo</h2><table id="t_ult"></table></div>
<div class="bloco"><h2>Contas que entraram no jogo</h2><table id="t_contas"></table></div>
<div class="bloco"><h2>Cadastros</h2><div id="cad"></div></div>
</main>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let senha='';try{senha=localStorage.getItem('painel_senha')||''}catch(e){}
const dm=d=>d.slice(8,10)+'/'+d.slice(5,7);
const hm=ts=>{const d=new Date(ts*1000);return d.toLocaleString('pt-BR',{timeZone:'America/Sao_Paulo',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})};
const dataIso=s=>s?new Date(s).toLocaleString('pt-BR',{timeZone:'America/Sao_Paulo',day:'2-digit',month:'2-digit',year:'2-digit',hour:'2-digit',minute:'2-digit'}):'-';
function svgBarras(el,itens,opc){
  const W=el.clientWidth||700,H=el.clientHeight||220,m={l:30,r:8,t:16,b:24},w=W-m.l-m.r,h=H-m.t-m.b;
  const max=Math.max(4,Math.ceil(Math.max(...itens.map(i=>Math.max(i.a+(i.b||0),i.linha||0)))/4)*4);   // escala em numeros inteiros
  const passo=w/itens.length,bw=Math.max(3,passo*0.62);let s='';
  for(let k=0;k<=4;k++){const y=m.t+h-h*k/4,v=max*k/4;s+=`<line x1="${m.l}" x2="${W-m.r}" y1="${y}" y2="${y}" stroke="#1b2547"/><text x="${m.l-5}" y="${y+4}" text-anchor="end">${v}</text>`}
  itens.forEach((it,i)=>{const x=m.l+i*passo+(passo-bw)/2;const ha=h*it.a/max,hb=h*(it.b||0)/max;
    s+=`<rect x="${x}" y="${m.t+h-ha}" width="${bw}" height="${ha}" rx="2" fill="${it.cor||'#3fb6ff'}"><title>${esc(it.dica)}</title></rect>`;
    if(hb>0)s+=`<rect x="${x}" y="${m.t+h-ha-hb}" width="${bw}" height="${hb}" rx="2" fill="#ffc83d"><title>${esc(it.dica)}</title></rect>`;
    if(opc.valor&&(it.a+(it.b||0))>0)s+=`<text x="${x+bw/2}" y="${m.t+h-ha-hb-4}" text-anchor="middle" style="fill:#e8ecf7">${it.a+(it.b||0)}</text>`;
    if(i%opc.cada===0)s+=`<text x="${x+bw/2}" y="${H-6}" text-anchor="middle">${esc(it.rot)}</text>`});
  if(itens.some(i=>i.linha!=null)){const pts=itens.map((it,i)=>`${m.l+i*passo+passo/2},${m.t+h-h*(it.linha||0)/max}`).join(' ');
    s+=`<polyline points="${pts}" fill="none" stroke="#ffc83d" stroke-width="2"/>`}
  el.innerHTML=s}
let ult=null;
function desenhar(d){ult=d;
  $('#c_online').textContent=d.online;$('#c_hoje').textContent=d.hoje_jogadores;$('#c_novos').textContent=d.hoje_novos;
  $('#c_contas').textContent=d.hoje_contas;$('#c_part').textContent=d.hoje_partidas;$('#c_total').textContent=d.total_aparelhos;
  $('#quando').textContent='atualizado '+hm(d.agora);
  const cada=d.dias>14?3:1;
  svgBarras($('#g_dias'),d.por_dia.map(x=>({a:x.jogadores-x.novos,b:x.novos,rot:dm(x.dia),
    dica:`${dm(x.dia)}: ${x.jogadores} jogadores (${x.novos} novos), ${x.contas} contas, ${x.sessoes} entradas, ${x.partidas} partidas online`})),{valor:true,cada});
  svgBarras($('#g_horas'),d.por_hora_hoje.map((v,h)=>({a:v,linha:d.por_hora_media[h],rot:h+'h',cor:h===d.hora_agora?'#5be38a':'#3fb6ff',
    dica:`${h}h: ${v} hoje · média ${d.por_hora_media[h]}`})),{valor:false,cada:2});
  const tp=Math.max(1,...d.plataformas.map(p=>p.jogadores));
  $('#plats').innerHTML=d.plataformas.length?d.plataformas.map(p=>`<div style="display:flex;gap:10px;align-items:center;margin:4px 0"><span style="width:90px">${esc(p.plat)}</span><span style="height:12px;border-radius:6px;background:#3fb6ff;width:${Math.max(4,240*p.jogadores/tp)}px"></span><span>${p.jogadores}</span></div>`).join(''):'<span class="nota">nada ainda</span>';
  $('#t_ult').innerHTML='<tr><th>quando</th><th>nome</th><th>aparelho</th><th>conta</th></tr>'+d.ultimos.map(u=>`<tr><td>${hm(u.ts)}</td><td>${esc(u.nick||'-')}</td><td>${esc(u.plat)}</td><td>${u.conta?'sim':'não'}</td></tr>`).join('');
  $('#t_contas').innerHTML=d.contas.length?'<tr><th>nick</th><th>conta</th><th>primeira vez</th><th>última vez</th><th>dias</th><th>entradas</th></tr>'+d.contas.map(c=>`<tr><td>${esc(c.nick)}</td><td>${esc(c.id)}…</td><td>${hm(c.primeiro)}</td><td>${hm(c.ultimo)}</td><td>${c.dias}</td><td>${c.sessoes}</td></tr>`).join(''):'<tr><td class="nota">Nenhuma conta logada entrou ainda.</td></tr>';
  const cd=d.cadastros;
  if(!cd)$('#cad').innerHTML='<span class="nota">Para ver TODOS os cadastros (até quem nunca jogou), coloque a chave <b>service_role</b> do Supabase no arquivo /opt/bravor-historico/supabase_admin do servidor. Enquanto isso, a tabela de cima mostra as contas que já entraram no jogo.</span>';
  else if(cd.erro)$('#cad').innerHTML=`<span class="erro">${esc(cd.erro)}</span>`;
  else $('#cad').innerHTML=`<div style="margin-bottom:8px"><b style="font-size:22px">${cd.total}</b> <span class="nota">cadastros no total</span></div><table><tr><th>cadastro</th><th>nick</th><th>e-mail</th><th>via</th><th>último login</th></tr>`+cd.lista.map(u=>`<tr><td>${dataIso(u.criado)}</td><td>${esc(u.nick||'-')}</td><td>${esc(u.email)}</td><td>${esc(u.via)}</td><td>${dataIso(u.ultimo)}</td></tr>`).join('')+'</table>'}
async function carregar(){
  if(!senha){$('#entrar').hidden=false;$('#tudo').hidden=true;return}
  try{const r=await fetch('painel/dados?dias='+$('#dias').value,{headers:{'X-Painel':senha},cache:'no-store'});
    if(r.status===401){senha='';try{localStorage.removeItem('painel_senha')}catch(e){};$('#msg').textContent='Senha errada.';$('#entrar').hidden=false;$('#tudo').hidden=true;return}
    const d=await r.json();$('#entrar').hidden=true;$('#tudo').hidden=false;desenhar(d)}
  catch(e){$('#quando').textContent='sem conexão com o servidor'}}
$('#ok').onclick=()=>{senha=$('#senha').value.trim();try{localStorage.setItem('painel_senha',senha)}catch(e){};$('#msg').textContent='';carregar()};
$('#senha').addEventListener('keydown',e=>{if(e.key==='Enter')$('#ok').click()});
$('#atualizar').onclick=carregar;$('#dias').onchange=carregar;
window.addEventListener('resize',()=>{if(ult)desenhar(ult)});
setInterval(()=>{if(senha&&!document.hidden)carregar()},60000);
carregar();
</script></body></html>'''


def main():
    global DB
    DB = abrir_banco()
    srv = ThreadingHTTPServer(("127.0.0.1", PORTA), Pedido)
    print("[historico] ouvindo em 127.0.0.1:%d  banco=%s  segredo=%s" % (PORTA, BANCO, "ok" if segredo() else "FALTANDO"), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
