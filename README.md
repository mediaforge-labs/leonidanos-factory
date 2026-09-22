# Leonidanos Factory

Fábrica automatizada de vídeos do Leonidanos, reconstruída a partir da implementação funcional do `lovacademy/portal-leonidanos` e adaptada para execução hospedada no GitHub/MediaForge.

## Arquitetura

Editorial -> roteiro PT -> thumbnail PT -> voz PT -> MediaForge render -> validação -> Supabase -> upload YouTube PT PRIVATE -> versão EN -> thumbnail EN -> voz/render EN -> validação -> upload YouTube EN PRIVATE -> Shorts -> checkpoints finais.

## Princípios desta reconstrução

- Sem renderização dependente do Dell/self-hosted Windows.
- Upload hospedado em GitHub Actions (`ubuntu-latest`).
- Credenciais apenas via GitHub Secrets; nenhum segredo é versionado.
- Produção agendada só fica ativa quando a variável de repositório `FACTORY_ENABLED` for definida como `true`.
- Uploads do YouTube permanecem `PRIVATE` por padrão.

## Status

Repositório criado para substituir a implementação experimental anterior em `mediaforge-labs/media-pipeline-engine` para o fluxo Leonidanos.
