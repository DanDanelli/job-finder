# Job Finder

Assistente pessoal que lê o currículo, busca vagas em várias fontes, ranqueia
por fit e envia um digest diário no Telegram. Roda num Raspberry Pi.

- Design: [docs/plans/2026-11-15-job-finder-design.md](docs/plans/2026-11-15-job-finder-design.md)
- Plano: [docs/plans/2026-11-15-job-finder-implementation.md](docs/plans/2026-11-15-job-finder-implementation.md)

## Desenvolvimento

```bash
uv sync                      # instala dependências
cp .env.example .env         # ajuste as variáveis
uv run job-finder db upgrade # cria/atualiza o banco
uv run job-finder serve      # sobe a aplicação em http://localhost:8000
uv run pytest                # testes
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## Raspberry Pi (Docker)

```bash
cp .env.example .env
docker compose up -d --build
```

O banco fica no volume `./data`. As migrações rodam automaticamente ao subir.
