# Radar Steam — monitor pessoal de promoções

Monitor Python que consulta ofertas da Steam no Brasil usando a API oficial do IsThereAnyDeal e envia alertas a um webhook do Discord.

## Filtros iniciais

- Preço máximo: R$ 100
- Desconto mínimo: 50%
- Nota Steam mínima: 80%
- Gêneros: RPG, Open World, Survival, Adventure, Strategy, Souls-like e Racing
- Histórico: preço atual até 10% acima do menor preço de referência
- Loja: Steam (shop ID 61)
- Frequência: uma vez por hora

## 1. Pré-requisitos

1. Crie uma chave em https://isthereanydeal.com/apps/my/
2. Crie um webhook no Discord: Configurações do servidor → Integrações → Webhooks → Novo webhook.
3. Crie um repositório **privado** no GitHub e envie os arquivos deste projeto.

## 2. Configurar secrets no GitHub

Em Settings → Secrets and variables → Actions → New repository secret, crie:

- `ITAD_API_KEY`: sua chave da API IsThereAnyDeal.
- `DISCORD_WEBHOOK_URL`: URL do webhook do canal Discord.

Não coloque essas credenciais em `config.json`, no código ou em commits.

## 3. Ativar

Em Settings → Actions → General, permita que GitHub Actions leia e escreva no repositório (workflow permissions: Read and write permissions).

Depois acesse Actions → Steam Deal Monitor → Run workflow para testar manualmente. O agendamento executa no minuto 0 de cada hora (UTC). O GitHub pode atrasar execuções agendadas em períodos de alta demanda.

## 4. Rodar localmente

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env
```

Defina as duas variáveis de ambiente antes de executar:

```bash
python -m src.monitor
```

## 5. Como o histórico funciona

O projeto mantém `data/monitor.sqlite3`. Uma oferta é notificada na primeira vez e novamente se o preço cair ou o desconto melhorar. O banco é versionado no repositório para persistir entre execuções do GitHub Actions; por isso, mantenha o repositório privado.

O modo `near` considera uma oferta próxima do menor preço quando ela está até 10% acima do `storeLow` informado pelo ITAD (ou do `historyLow`, se `storeLow` não vier). Quando a moeda do histórico não é BRL, a oferta é descartada para evitar comparações incorretas.

## 6. Limitações conhecidas da primeira versão

- Os nomes de tags do IsThereAnyDeal podem não corresponder exatamente aos rótulos de gênero da Steam. `GENRE_TAGS` em `src/monitor.py` concentra esse mapeamento para ajustes após o primeiro teste.
- O endpoint de ofertas não garante que todos os campos de avaliação estejam presentes na resposta; a nota mínima é enviada como filtro à API. Se o serviço não aceitar algum rótulo de tag, ajuste o mapeamento e rode novamente.
- O link de compra usa o link retornado pelo ITAD. O código usa link direto da Steam somente se o ID Steam estiver disponível no objeto de jogo.
- A primeira execução pode gerar várias notificações, pois o banco ainda estará vazio.
- O GitHub Actions não é um relógio de precisão: execuções agendadas podem atrasar.
- A SteamDB não é consultada por scraping. O histórico vem do IsThereAnyDeal, que agrega preços de lojas cobertas.

## Fontes técnicas

- IsThereAnyDeal API: https://docs.isthereanydeal.com/
- Discord Webhooks: https://discord.com/developers/docs/resources/webhook
- GitHub Actions schedules: https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule
