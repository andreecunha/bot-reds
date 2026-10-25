# Bot de vermelhos — FlashScore (Telegram)

O bot abre a página "AO VIVO" da FlashScore num browser headless e lê-a a cada 15 s. Quando aparece um cartão vermelho, envia para o grupo uma mensagem como esta:

> 🟥 **VERMELHO** para Porto (63')
> 🏆 PORTUGAL: Liga Portugal
> ⚽ Benfica 1-0 Porto
> ⚠️ **Porto** joga com 10 contra 11

## 1. Criar o bot no Telegram
1. Fala com o **@BotFather**, escreve `/newbot` e guarda o token que ele te dá.
2. Adiciona o bot ao grupo.
3. No grupo, envia `/start@NomeDoTeuBot`.
4. Abre `https://api.telegram.org/bot<TOKEN>/getUpdates` e copia o `chat.id` do grupo. É um número negativo, do género `-100...`.

## 2. Testar no PC (Windows), de preferência com jogos a decorrer
```powershell
pip install -r requirements.txt
python -m playwright install chromium
$env:TELEGRAM_TOKEN="..."
$env:TELEGRAM_CHAT_ID="-100..."
$env:DEBUG_DUMP="1"
python bot.py
```
Com `DEBUG_DUMP=1`, os logs mostram quantos jogos o bot leu e os vermelhos de cada equipa (`hr` para a equipa da casa, `ar` para a de fora). Confirma esses valores com um jogo onde já tenha havido um vermelho.

## 3. Alojar no Fly.io
```bash
fly launch --no-deploy --copy-config --name vermelhos-alertas
fly secrets set TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=-100...
fly deploy
fly scale count 1
fly logs
```
A máquina tem 1 GB de RAM, o que dá cerca de 5–6 €/mês.

## Filtrar ligas (opcional)
Define `LEAGUE_FILTER` com palavras do nome da liga tal como aparece na FlashScore, por exemplo `portugal,inglaterra,espanha`. Se ficar vazio, o bot alerta todas as ligas.

## Se deixar de funcionar
De vez em quando a FlashScore muda o HTML da página. Quando isso acontecer:
1. Liga `DEBUG_DUMP=1`, com `fly secrets set DEBUG_DUMP=1` ou no `[env]` do fly.toml.
2. Vê nos logs o "HTML de exemplo".
3. Ajusta os seletores no bloco `EXTRACT_JS` do `bot.py`.

## Notas
- Quando o bot arranca, ignora os vermelhos que já existiam.
- Os vermelhos por 2.º amarelo também contam, porque a FlashScore mostra o mesmo ícone.
- O bot não faz pedidos repetidos à FlashScore: mantém a página aberta, que se atualiza sozinha, e só a recarrega de 20 em 20 minutos.
