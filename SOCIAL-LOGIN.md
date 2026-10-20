# Início de sessão opcional com Google e Facebook

Instale as dependências de `requirements.txt` antes de iniciar a nova versão do
serviço. A atualização da base de dados acrescenta associações de identidades,
pedidos de autorização de utilização única, válidos durante dez minutos, e contadores
agregados de atividade das contas. Os utilizadores, palavras-passe e projetos
existentes são preservados.

## Configuração do servidor

Defina `BACKMARS_OAUTH_CALLBACK_BASE` com o endereço público base da API, incluindo
`/api`, e `WEBMARS_PUBLIC_URL` com o endereço da aplicação web. Em produção, é
obrigatório usar HTTPS. Os botões dos fornecedores são obtidos através de
`/api/auth/providers` e só aparecem quando o respetivo identificador de cliente e
segredo estão configurados. Guarde as credenciais nas variáveis de ambiente
protegidas do servidor, nunca em `webmars-config.js`.

Na instalação atual, os endereços de retorno são:

- Google: `https://backmars.nfiles.top/api/auth/google/callback`
- Facebook: `https://backmars.nfiles.top/api/auth/facebook/callback`

No Google Cloud, crie um cliente OAuth para aplicações web, configure a identidade
visual e o público da aplicação e registe o endereço de retorno Google exatamente
como indicado. Defina `BACKMARS_GOOGLE_CLIENT_ID` e `BACKMARS_GOOGLE_CLIENT_SECRET`.
As permissões pedidas são `openid email`; não são pedidos fotografia, perfil,
contactos, acesso ao Drive ou um token de renovação para acesso offline.

No Meta for Developers, configure uma aplicação Facebook Login e registe o endereço
de retorno Facebook exatamente como indicado. Configure a informação pública sobre
privacidade e eliminação de dados e cumpra os requisitos de publicação em vigor na
plataforma. Defina `BACKMARS_FACEBOOK_CLIENT_ID`, `BACKMARS_FACEBOOK_CLIENT_SECRET` e
`BACKMARS_FACEBOOK_API_VERSION`, escolhendo uma versão atualmente suportada da Graph
API. A versão é explícita para evitar atualizações silenciosas numa instalação
existente.

A permissão básica de perfil público faz parte do início de sessão Facebook. A
aplicação pede o email e consulta apenas `id,email`; não guarda o nome nem a
fotografia do perfil. O registo e a aprovação nos painéis dos fornecedores exigem
acesso às contas Google/Meta do operador. O código local não cria essas aplicações.

Antes de publicar a aplicação Facebook, disponibilize na página pública de
privacidade um contacto do operador e instruções para eliminar contas e dados. A
eliminação de contas continua a depender do administrador da instalação; esta
funcionalidade não acrescenta um endpoint de eliminação automática. Configure os
registos de acesso aos endereços de retorno do backend com `$uri`, em vez de
`$request_uri` ou `$request`, para não guardar os parâmetros dos endereços que contêm
códigos de autorização.

## Contas e associação de fornecedores

O primeiro início de sessão Google bem-sucedido cria uma conta Cloud local com um
nome de utilizador gerado, um hash de palavra-passe vazio e inutilizável e o email
verificado. Os acessos seguintes são identificados pelo identificador único atribuído
pelo fornecedor, e não pelo email. Mais tarde, a recuperação de palavra-passe permite
definir uma palavra-passe local através do mecanismo existente de códigos enviados
por email.

As novas contas Facebook exigem um email fornecido pelo Facebook. Esse email não é
automaticamente marcado como verificado: aplica-se o mecanismo de verificação
existente, com envio de email quando este estiver configurado. Se o Facebook não
fornecer um email, crie primeiro uma conta local ou entre numa conta existente e
escolha **Cloud → Métodos de entrada associados**.

Se o email já pertencer a uma conta local, o início de sessão com um fornecedor
externo não associa automaticamente as contas nem permite assumir o controlo da conta
existente. Entre pelo método habitual e associe explicitamente o Google ou o Facebook
através do menu Cloud. Uma identidade já associada a outra conta local não pode ser
reatribuída. A associação preserva o identificador local do utilizador e,
consequentemente, a propriedade de todos os projetos e publicações.

A autorização começa num endpoint POST que exige um cabeçalho `Origin` autorizado.
Usa a validação de `state` da Authlib e, no Google, a verificação de `nonce` e PKCE,
além de um pedido de utilização única guardado no servidor e associado à sessão do
navegador que o iniciou. Ao regressar, a associação verifica também a conta de origem
e a versão da respetiva sessão.

Os tokens dos fornecedores são usados apenas durante o processamento do retorno;
nunca são guardados na base de dados, nos cookies da aplicação ou no frontend. A
janela do fornecedor envia apenas um resultado limitado, o nome do fornecedor e um
estado aleatório para a origem configurada da aplicação. O frontend verifica a janela
de origem, a origem e o estado da mensagem e consulta `/api/me` antes de aceitar o
início de sessão como bem-sucedido. Os erros dos fornecedores não são registados com
os respetivos tokens.

## Estatísticas das contas

`GET /api/statistics?window=60m|24h|30d&context=public|internal|test|all` devolve
contagens agregadas, confirmadas pelo servidor, de criação de contas, inícios de
sessão, associações de fornecedores e operações de guardar e carregar projetos Cloud,
bem como o número de contas Cloud ativas distintas. Os períodos são arredondados para
intervalos de um minuto.

Os pedidos incluem a marca geral de contexto `X-WebMARS-Context`. O processo OAuth
preserva o contexto inicial ao passar pelo fornecedor. Esta marca serve para análise
estatística e não concede permissões de acesso.

O cálculo de contas ativas distintas usa os identificadores das contas existentes
numa tabela privada de atividade, agrupada por minuto, durante um máximo de 35 dias.
Esses identificadores, os identificadores dos fornecedores e os emails não são
devolvidos nas estatísticas nem enviados para o coletor de métricas do navegador. Os
tokens das métricas do navegador não são cruzados com a atividade das contas. As
sessões anónimas do navegador e as contas autenticadas distintas representam
populações diferentes.

Os contadores começam quando esta recolha é instalada. Não são preenchidos
retroativamente como se as ações históricas das contas tivessem sido observadas.

## Verificação antes da ativação

Execute `python -m unittest discover -s tests`. Os testes locais abrangem a expiração
do estado, a rejeição de respostas noutro navegador, a reutilização de respostas, os
conflitos de email, a associação de contas e a separação dos agregados.

Depois, use contas de teste dos fornecedores para verificar, através de HTTPS, o ecrã
real de autorização, o cancelamento, o primeiro início de sessão, os acessos
seguintes, a associação e a propriedade dos projetos Cloud. Teste também a navegação
privada e os navegadores que bloqueiam cookies entre sites. A aplicação e o BackMars
devem, preferencialmente, permanecer no mesmo site, como acontece em `nfiles.top`.

Instale primeiro o backend e a respetiva configuração e só depois o frontend. Os
fornecedores que não estejam configurados permanecem ocultos.
