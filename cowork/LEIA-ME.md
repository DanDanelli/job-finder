# Busca de vagas no Claude Cowork

O Claude procura vagas para você, dá uma nota de 0 a 100 para cada uma de acordo com o
seu perfil e aprende com o que você decide. Não precisa instalar nada nem saber
programar: é uma pasta com quatro arquivos de texto e o app do Claude.

## Do que você precisa

- O app do Claude para computador (Mac ou Windows), com um plano pago que inclua o
  Cowork.
- Uma conta no Indeed (gratuita). Se não tiver, dá para criar durante o passo 2.
- O seu currículo em PDF.

## Passo a passo

### 1. Baixe a pasta

1. Baixe o arquivo:
   [job-search-cowork.zip](https://github.com/DanDanelli/job-finder/releases/latest/download/job-search-cowork.zip).
2. Abra o arquivo baixado. Aparece uma pasta chamada `job-search`.
3. Mova essa pasta para a sua pasta Documentos. Se quiser, renomeie para `vagas`.

Os seus dados vão ficar nessa pasta, no seu computador. Não compartilhe a pasta com
ninguém.

### 2. Conecte o Indeed

1. No app do Claude, abra **Customize** e depois **Connectors** (Conectores).
2. Clique no **+**, procure **Indeed** e clique em **Connect**.
3. Entre com a sua conta do Indeed.

O Indeed tem vagas de todas as áreas no Brasil. O Claude também procura em sites como
Gupy, vagas.com.br e InfoJobs pela busca na web, sem você precisar configurar nada.

### 3. Crie o projeto no Cowork

1. No app do Claude, abra o **Cowork** e crie um projeto novo.
2. Escolha **Use an existing folder** (usar uma pasta existente) e selecione a pasta do
   passo 1.

### 4. Crie o seu perfil

Numa conversa nova dentro do projeto, escreva **criar meu perfil** e anexe o seu
currículo (ou coloque o PDF dentro da pasta).

O Claude lê o currículo e pergunta o que faltar: onde você pode trabalhar, se aceita
remoto ou híbrido, CLT ou PJ, salário mínimo, o que você não quer. Leia o perfil que
ele escrever e peça as correções que quiser. Ele só grava quando você aprovar.

### 5. Busque vagas

Escreva **buscar vagas**. O Claude procura, dá as notas e mostra as melhores, cada uma
com um número (J1, J2...), um resumo, os pontos fortes, o que falta e o link.

Depois diga o que achou, do seu jeito:

- "salvar J3"
- "descartar J5, é agência de publicidade"
- "me candidatei à J7"

O motivo é o que mais ensina o Claude. Com o tempo, as notas ficam mais parecidas com
o seu gosto.

### 6. Toda semana

Escreva **revisão semanal** (uma vez por semana, por exemplo às segundas). O Claude:

- mostra onde a nota dele e a sua decisão não bateram;
- propõe até 3 regras novas a partir das suas decisões, e só usa as que você aprovar;
- limpa as vagas antigas, para a pasta não crescer sem parar.

Se quiser que isso aconteça sozinho, peça ao Claude no projeto para agendar uma tarefa:
por exemplo, "buscar vagas todo dia às 8h" e "revisão semanal toda segunda às 9h".

## O que esperar

- **Ele não se candidata por você.** O Claude só procura e avalia; quem se candidata,
  manda mensagem ou entra em sites é você.
- **Ele pode errar.** Os filtros e as notas são julgamento do Claude, não uma regra
  fixa. Quase sempre acerta, mas confira a vaga antes de se candidatar.
- **O LinkedIn não abre para o Claude.** Vagas que só existem lá ganham uma nota
  baseada só no trecho da busca, e o Claude avisa quando isso acontece.
- **Cada busca gasta parte do limite do seu plano.** Se o limite acabar rápido, busque
  menos vezes por semana.

## Se algo der errado

- **O Claude não segue o passo a passo** (não grava as vagas, não usa os números J1,
  J2...): abra o arquivo `CLAUDE.md` da pasta, copie todo o texto e cole nas
  instruções do projeto (em **Customize**, no projeto do Cowork).
- **O Claude diz que não tem o Indeed:** confira se o conector está ligado na conversa
  (botão **+** na caixa de mensagem, depois **Connectors**).
- **Você quer mudar o perfil:** escreva "atualizar meu perfil" e diga o que mudou.

## Os arquivos da pasta

Você não precisa abrir nenhum deles; o Claude cuida de tudo. Mas, se tiver curiosidade:

- `CLAUDE.md`: as instruções que o Claude segue (em inglês; as respostas saem em
  português).
- `profile.md`: o seu perfil. Vem com um exemplo, que o Claude troca pelo seu.
- `jobs.md`: as vagas avaliadas e o que você decidiu.
- `rules.md`: o que o Claude aprendeu com as suas decisões.
