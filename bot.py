"""
Bot de alertas de vermelhos (futebol) para um grupo do Telegram — fonte: FlashScore.

Abre a página "AO VIVO" da FlashScore num browser headless (Playwright/Chromium),
mantém-na aberta (a FlashScore atualiza os resultados sozinha) e lê a página a cada
CHECK_SECONDS. Quando aparece um novo ícone de cartão vermelho num jogo, envia um
alerta para o grupo com o resultado e a situação numérica (ex.: 10 vs 11).

Variáveis de ambiente:
  TELEGRAM_TOKEN     token do bot (@BotFather)                        (obrigatória)
  TELEGRAM_CHAT_ID   id do grupo (negativo, ex.: -1001234567890)       (obrigatória)
  CHECK_SECONDS      intervalo entre leituras da página (omissão 20)
  RELOAD_MINUTES     recarregar a página de X em X minutos (omissão 20)
  LEAGUE_FILTER      palavras separadas por vírgula; só alerta ligas cujo nome
                     contenha uma delas (ex.: "portugal,inglaterra,espanha"). Vazio = todas
  DEBUG_DUMP         "1" para escrever nos logs o HTML de alguns jogos (afinar seletores)
  FLASHSCORE_URL     omissão https://www.flashscore.pt/
"""

import asyncio
import html
import logging
import os
import time

import httpx
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vermelhos")
# não registar cada pedido HTTP (o endereço do Telegram contém o token)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
URL = os.getenv("FLASHSCORE_URL", "https://www.flashscore.pt/")
CHECK_SECONDS = int(os.getenv("CHECK_SECONDS", "15"))
RELOAD_MINUTES = int(os.getenv("RELOAD_MINUTES", "20"))
LEAGUE_FILTER = [x.strip().lower() for x in os.getenv("LEAGUE_FILTER", "").split(",") if x.strip()]
DEBUG_DUMP = os.getenv("DEBUG_DUMP") == "1"
FORGET_AFTER = 45 * 60  # esquece jogos que não aparecem há 45 min

TG = f"https://api.telegram.org/bot{TG_TOKEN}"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Lê todos os jogos visíveis. Os seletores têm alternativas porque a FlashScore
# muda de classes de tempos a tempos; se deixar de funcionar, é aqui que se mexe.
EXTRACT_JS = r"""
(debug) => {
  // Ícone de vermelho: várias formas possíveis (classe, data-testid, título), sem distinguir maiúsculas
  const RED = ['class', 'data-testid', 'title', 'aria-label', 'href', 'xlink:href']
    .flatMap(a => ['redcard', 'red-card', 'red_card', 'cartão vermelho', 'cartao vermelho']
      .map(k => `[${a.replace(':', '\\:')}*="${k}" i]`)).join(', ');
  const txt = el => el ? el.innerText.replace(/\s+/g, ' ').trim() : '';
  const q = (root, sels) => { for (const s of sels) { const e = root.querySelector(s); if (e) return e; } return null; };

  const countReds = c => {
    if (!c) return 0;
    let n = 0;
    c.querySelectorAll(RED).forEach(e => {
      const anc = e.parentElement && e.parentElement.closest(RED);
      if (anc && c.contains(anc)) return;           // evita contar o mesmo ícone duas vezes
      const m = ['class', 'data-testid', 'href', 'xlink:href'].map(a => e.getAttribute(a) || '').join(' ').match(/red-?_?card-?_?(\d)/i);
      n += m ? parseInt(m[1]) : 1;                   // alguns ícones trazem o número (2 vermelhos)
    });
    return n;
  };

  const out = [], samples = [];
  let league = '';
  const nodes = document.querySelectorAll(
    '.event__header, [class*="wclLeagueHeader"], [class*="headerLeague"], .event__match'
  );
  nodes.forEach(el => {
    if (!el.classList.contains('event__match')) {
      if (el.closest('.event__match')) return;
      // ignora elementos internos de um cabeçalho já tratado
      if (el.parentElement && el.parentElement.closest('.event__header, [class*="wclLeagueHeader"], [class*="headerLeague"]')) return;
      const cat = q(el, ['.event__title--type', '[class*="headerLeague__category"]']);
      const name = q(el, ['.event__title--name', '[class*="headerLeague__title"]']);
      let t;
      if (cat || name) {
        const c = txt(cat).replace(/:+$/, '').trim();
        let n = txt(name);
        if (c) n = n.split(c).join('').replace(/^[:\s]+|[:\s]+$/g, '').trim();  // tira o país repetido
        t = [c, n].filter(Boolean).join(': ');
      } else {
        t = txt(el).slice(0, 80);
      }
      if (t) league = t;
      return;
    }
    const home = q(el, ['.event__homeParticipant', '.event__participant--home', '[class*="participant--home"]']);
    const away = q(el, ['.event__awayParticipant', '.event__participant--away', '[class*="participant--away"]']);
    const link = el.querySelector('a[href*="/jogo/"], a.eventRowLink');
    // nome da equipa sem textos temporários como "GOLO"
    const nameOf = c => c ? txt(q(c, ['[class*="wcl-name"]', '[data-testid="wcl-simple-text-01"]', '[class*="participant__participantName"]']) || c) : '';
    out.push({
      id: el.id || (link ? link.getAttribute('href') : txt(home) + '-' + txt(away)),
      league,
      home: nameOf(home),
      away: nameOf(away),
      hs: txt(q(el, ['.event__score--home', '[class*="score--home"]'])),
      as: txt(q(el, ['.event__score--away', '[class*="score--away"]'])),
      minute: txt(q(el, ['.event__stage--block', '.event__stage', '[class*="event__stage"]'])),
      hr: countReds(home),
      ar: countReds(away),
      href: link ? link.href : '',
    });
    // no diagnóstico, mostra primeiro jogos que tenham algo com "card" (para confirmar o ícone do vermelho)
    if (debug) {
      const h = el.outerHTML;
      if (/card/i.test(h)) samples.unshift(h.slice(0, 4000));
      else if (samples.length < 2) samples.push(h.slice(0, 1500));
    }
  });
  return { matches: out, samples: samples.slice(0, 3) };
}
"""


# Abre ligas recolhidas na lista (só botões dentro de cabeçalhos de liga marcados como fechados)
EXPAND_JS = r"""
() => {
  const sel = [
    '.event__header [aria-expanded="false"]',
    '[class*="wclLeagueHeader"] [aria-expanded="false"]',
    '[class*="headerLeague"] [aria-expanded="false"]',
    '.event__expander--close',
    '[class*="wclLeagueHeader"][aria-expanded="false"]',
  ].join(', ');
  let n = 0;
  document.querySelectorAll(sel).forEach(b => { try { b.click(); n++; } catch (e) {} });
  return n;
}
"""


async def open_live(page) -> None:
    await page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    # Banner de cookies: recusa os não essenciais
    try:
        await page.locator("#onetrust-reject-all-handler").click(timeout=5_000)
    except Exception:
        pass
    # Separador "AO VIVO"
    for sel in ['.filters__tab:has-text("AO VIVO")', '[class*="filters"] >> text=AO VIVO', "text=AO VIVO"]:
        try:
            await page.locator(sel).first.click(timeout=5_000)
            break
        except Exception:
            continue
    else:
        log.warning("Não encontrei o separador AO VIVO — vou ler a página principal")
    try:
        await page.wait_for_selector(".event__match", timeout=15_000)
    except Exception:
        log.info("Sem jogos visíveis neste momento")


def build_message(m: dict, team_side: str, hr: int, ar: int, cancelled: bool = False) -> str:
    e = html.escape
    team = m["home"] if team_side == "home" else m["away"]
    hp, ap = 11 - hr, 11 - ar
    if hp < ap:
        situ = f"⚠️ <b>{e(m['home'])}</b> joga com {hp} contra {ap}"
    elif ap < hp:
        situ = f"⚠️ <b>{e(m['away'])}</b> joga com {ap} contra {hp}"
    else:
        situ = f"⚖️ Igualdade numérica ({hp} vs {ap})"
    minute = f" ({e(m['minute'])})" if m["minute"] else ""
    link = f'\n🔗 <a href="{e(m["href"])}">Ver jogo</a>' if m["href"] else ""
    title = (
        f"↩️ <b>VERMELHO ANULADO</b> (VAR) a {e(team)}{minute}"
        if cancelled
        else f"🟥 <b>VERMELHO</b> para {e(team)}{minute}"
    )
    return (
        f"{title}\n"
        f"🏆 {e(m['league'])}\n"
        f"⚽ {e(m['home'])} {e(m['hs'] or '?')}-{e(m['as'] or '?')} {e(m['away'])}\n"
        f"{situ}{link}"
    )


async def send(client: httpx.AsyncClient, text: str, chat_id=None) -> None:
    try:
        r = await client.post(
            f"{TG}/sendMessage",
            json={"chat_id": chat_id or CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        )
        if r.status_code != 200:
            log.error("Falha no envio para o Telegram: %s", r.text)
    except Exception:
        log.exception("Erro ao enviar para o Telegram")


async def block_heavy(route) -> None:
    # Não descarrega imagens/vídeo/fontes: poupa memória e tráfego (os ícones são SVG no HTML)
    if route.request.resource_type in ("image", "media", "font"):
        await route.abort()
    else:
        await route.continue_()


def process_match(m: dict, state: dict, lower: dict, first_cycle: bool) -> list[str]:
    """Compara a leitura atual de um jogo com o que já se sabia e devolve as mensagens a enviar."""
    mid = m["id"]
    seen_now = [m["hr"], m["ar"]]
    if first_cycle or mid not in state:
        # Jogo novo (ou arranque do bot): regista sem alertar,
        # exceto se o jogo surgiu depois do arranque já com vermelhos
        state[mid] = list(seen_now)
        lower[mid] = [0, 0]
        if first_cycle or seen_now == [0, 0]:
            return []
        counts = [0, 0]
    else:
        counts = state[mid]

    msgs = []
    for i, side in enumerate(("home", "away")):
        # Subiu: um alerta por cada vermelho novo (mesmo que saiam 2 na mesma leitura),
        # com a contagem passo a passo (ex.: 10v10 -> 9v10)
        while seen_now[i] > counts[i]:
            counts[i] += 1
            msgs.append(build_message(m, side, counts[0], counts[1]))
        # Desceu (VAR anulou): só confirma após 2 leituras seguidas,
        # para não confundir com um ícone que "pisca" na página
        if seen_now[i] < counts[i]:
            lower[mid][i] += 1
            if lower[mid][i] >= 2:
                counts[i] = seen_now[i]
                msgs.append(build_message(m, side, counts[0], counts[1], cancelled=True))
                lower[mid][i] = 0
        else:
            lower[mid][i] = 0
    state[mid] = counts
    return msgs


async def health_server() -> None:
    """Responde 'ok' na porta 8080 para os health checks do Fly.io (o bot não tem site)."""
    async def handle(reader, writer):
        try:
            await reader.read(1024)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", int(os.getenv("PORT", "8080")))
    log.info("Health check a responder na porta %s", os.getenv("PORT", "8080"))
    async with server:
        await server.serve_forever()


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s_ = divmod(rem, 60)
    if d:
        return f"{d} d {h} h"
    if h:
        return f"{h} h {m} min"
    if m:
        return f"{m} min"
    return f"{s_} s"


def status_text(st: dict) -> str:
    now = time.time()
    last_ok = st.get("last_ok")
    if last_ok and now - last_ok < max(120, CHECK_SECONDS * 6):
        head = "🟢 <b>Bot online</b>"
        read = f"🔄 Última leitura: há {fmt_duration(now - last_ok)}"
    elif last_ok:
        head = "🟠 <b>Bot ligado, mas com problemas a ler a FlashScore</b>"
        read = f"🔄 Última leitura bem-sucedida: há {fmt_duration(now - last_ok)}"
    else:
        head = "🟡 <b>Bot a arrancar…</b>"
        read = "🔄 Ainda sem leituras"
    jogos = (
        f"⚽ A acompanhar {st['matches']} jogos ao vivo em {st['leagues']} ligas"
        if st["matches"] else "⚽ Sem jogos ao vivo neste momento"
    )
    return (
        f"{head}\n"
        f"⏱️ Ligado há {fmt_duration(now - st['start'])}\n"
        f"{jogos}\n"
        f"{read}\n"
        f"🟥 Alertas enviados desde que arrancou: {st['alerts']}"
    )


async def commands_loop(client: httpx.AsyncClient, st: dict) -> None:
    """Responde a /estado (e /status, /start) no Telegram."""
    try:
        await client.post(f"{TG}/setMyCommands", json={"commands": [
            {"command": "estado", "description": "Ver se o bot está online e quantos jogos acompanha"},
        ]})
        # ignora mensagens antigas (enviadas antes de o bot arrancar)
        r = await client.get(f"{TG}/getUpdates", params={"offset": -1, "timeout": 0})
        res = r.json().get("result", [])
        offset = res[-1]["update_id"] + 1 if res else None
    except Exception:
        log.exception("Falha a preparar os comandos")
        offset = None

    while True:
        try:
            params = {"timeout": 25, "allowed_updates": '["message"]'}
            if offset:
                params["offset"] = offset
            r = await client.get(f"{TG}/getUpdates", params=params, timeout=35)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                cmd = (msg.get("text") or "").strip().split()[0:1]
                cmd = cmd[0].split("@")[0].lower() if cmd else ""
                if cmd in ("/estado", "/status", "/start"):
                    await send(client, status_text(st), chat_id=msg["chat"]["id"])
        except Exception:
            log.exception("Erro a ler comandos do Telegram")
            await asyncio.sleep(5)


async def main() -> None:
    asyncio.create_task(health_server())
    st = {"start": time.time(), "matches": 0, "leagues": 0, "last_ok": None, "alerts": 0}
    errors_in_row = 0
    warned = False
    announced = False
    state: dict[str, list[int]] = {}  # id do jogo -> [vermelhos casa, vermelhos fora]
    lower: dict[str, list[int]] = {}  # leituras seguidas com menos vermelhos (VAR)
    last_seen: dict[str, float] = {}
    first_cycle = True  # no arranque não alerta vermelhos que já existiam

    async with async_playwright() as p, httpx.AsyncClient(timeout=20) as client:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(user_agent=UA, locale="pt-PT", viewport={"width": 1280, "height": 2000})
        await ctx.route("**/*", block_heavy)
        page = await ctx.new_page()
        await open_live(page)
        last_reload = time.monotonic()
        last_count_log = 0.0
        log.info("Bot iniciado (leitura a cada %ss, filtro: %s)", CHECK_SECONDS, LEAGUE_FILTER or "todas")
        asyncio.create_task(commands_loop(client, st))

        while True:
            try:
                if time.monotonic() - last_reload > RELOAD_MINUTES * 60:
                    await open_live(page)
                    last_reload = time.monotonic()

                opened = await page.evaluate(EXPAND_JS)
                if opened:
                    log.info("Abri %d liga(s) recolhida(s)", opened)
                    await asyncio.sleep(2)

                data = await page.evaluate(EXTRACT_JS, DEBUG_DUMP)
                matches = data["matches"]
                st["matches"] = len(matches)
                st["leagues"] = len({m["league"] for m in matches})
                st["last_ok"] = time.time()
                if not announced:
                    await send(client, "🟢 <b>Bot online</b> — vou avisar aqui sempre que houver um vermelho.\n"
                               + (f"⚽ A acompanhar {st['matches']} jogos ao vivo em {st['leagues']} ligas"
                                  if st["matches"] else "⚽ Sem jogos ao vivo neste momento")
                               + "\nEscreve /estado para confirmar a qualquer momento.")
                    announced = True
                if warned:
                    await send(client, "✅ Voltei a ler a FlashScore normalmente.")
                    warned = False
                errors_in_row = 0
                if time.monotonic() - last_count_log > 600 or first_cycle:
                    leagues = len({m["league"] for m in matches})
                    log.info("A acompanhar %d jogos ao vivo em %d ligas", len(matches), leagues)
                    last_count_log = time.monotonic()
                if DEBUG_DUMP:
                    log.info("Jogos lidos: %d | exemplo: %s", len(matches), matches[:3])
                    for s in data["samples"]:
                        log.info("HTML de exemplo: %s", s)

                now = time.monotonic()
                for m in matches:
                    if LEAGUE_FILTER and not any(f in m["league"].lower() for f in LEAGUE_FILTER):
                        continue
                    last_seen[m["id"]] = now
                    for msg in process_match(m, state, lower, first_cycle):
                        log.info("Alerta: %s", msg.replace("\n", " | "))
                        await send(client, msg)
                        st["alerts"] += 1

                for mid in [k for k, t in last_seen.items() if now - t > FORGET_AFTER]:
                    last_seen.pop(mid, None)
                    state.pop(mid, None)
                    lower.pop(mid, None)
                first_cycle = False
            except Exception:
                log.exception("Erro no ciclo — a reabrir a página")
                errors_in_row += 1
                # ~5 falhas seguidas (≈1–2 min): avisa o grupo uma vez
                if errors_in_row >= 5 and not warned:
                    await send(client, "⚠️ Estou com problemas a ler a FlashScore. "
                                       "Os alertas podem falhar até eu recuperar — aviso quando voltar ao normal.")
                    warned = True
                try:
                    await page.close()
                except Exception:
                    pass
                try:
                    page = await ctx.new_page()
                    await open_live(page)
                    last_reload = time.monotonic()
                except Exception:
                    log.exception("Falhou a reabertura; tento no próximo ciclo")
            await asyncio.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
