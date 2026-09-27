# GitHub connections: workspace installation, а не PAT каждого пользователя

[Индекс](README.md) · [Архитектура](04-architecture.md) · [Безопасность](05-security.md).

**Ревизия 4, 27 сентября 2026.** Обычный пользователь Projects Hub не обязан иметь GitHub-аккаунт и не должен вставлять personal access token в приложение.

## Базовое решение

Projects Hub регистрируется как **GitHub App**.

Владелец или координатор workspace:
1. нажимает «Подключить GitHub»;
2. устанавливает GitHub App на personal account или organization;
3. в GitHub выбирает только нужные repositories;
4. возвращается в Projects Hub;
5. связывает repositories с project roles.

Обычные участники работают через membership/roles Projects Hub. Для обычного голосового добавления идеи, задачи или project document им не требуется собственный PAT и не требуется GitHub authorization.

Один workspace может иметь несколько GitHub installations: например personal account владельца и несколько organizations.

## Почему не PAT

GitHub App имеет явные permissions, может быть ограничена selected repositories, получает installation webhooks и использует короткоживущие installation access tokens.

PAT создаёт лишний onboarding, привязывает группу к аккаунту конкретного человека, сложнее отзывается и не подходит участникам без GitHub.

PAT не входит в пользовательский MVP. Поля «GitHub token» в продукте нет.

## Identity продукта и GitHub — разные вещи

Projects Hub user — самостоятельная identity.

GitHub connection — внешний resource binding workspace.

Поэтому волонтёр или лектор может работать без GitHub account; GitHub membership само по себе не выдаёт доступ к workspace, а Projects Hub membership не выдаёт прямой доступ к GitHub.

## Два режима repository connection

### Product-managed / knowledge repository

Memory, ideas, project docs, voice sources, requirements.

Workspace owner явно подключает repo. Projects Hub ACL определяет прикладные права участников. Backend пишет через GitHub App installation. GitHub видит действие приложения; Projects Hub сохраняет actor/source provenance. Участнику не нужен прямой GitHub access.

### External / owning repository

Существующий development/product repository с собственными правилами.

Default — **read-only**.

Write включается только connection admin и только через узкий typed operation. Для code/product repo предпочтительны service API/MCP owning продукта либо PR/branch flow. Прямой write в protected/default branch допустим только по явно заданной policy.

Workspace membership не превращается в произвольный GitHub write.

## Connection metadata

Projects Hub хранит durable metadata connection: installation id, numeric repository id, account id/type, project binding, connection role/access policy и последнее проверенное состояние.

Короткоживущая GitHub credential выдаётся сервером только на конкретную операцию. Она не передаётся в browser, Live model, transcript или logs.

Repo авторизуется по installation + numeric repository id. Имя или URL из function arguments не является доказательством доступа.

## Optional user-delegated mode

GitHub App умеет также работать от имени конкретного GitHub user. Такой доступ ограничен одновременно app permissions и правами пользователя.

Это полезно, если нужно GitHub-native attribution человеку или дополнительная GitHub permission boundary.

Но это **не default MVP**, иначе каждый участник снова должен иметь GitHub account.

Если режим появится, он явно отличается от app-managed и включается только там, где это действительно необходимо.

## Attribution

При app-managed operation GitHub не притворяется, что commit сделал конкретный GitHub user.

Projects Hub audit сохраняет actor id, source/conversation id, command id, repository id/path, revision before/after и GitHub commit/PR ref.

Человеческое авторство идеи или решения хранится в product metadata независимо от Git commit actor.

## Минимальная RepositoryConnection

Поля: id, workspace id, provider=github, installation id, account id/type, repository id, display full name, optional project id, role, access mode, optional allowed paths, default branch, last verified at, state.

Roles: memory store, project docs, source dataset, external owning repo, generated artifacts.

Access modes: read only, app-managed write, позднее user-delegated write.

Allowed paths — дополнительное ограничение продукта поверх repository access GitHub App.

## UI подключения

Workspace settings → Integrations → GitHub → «Подключить GitHub».

Дальше открывается GitHub installation page: personal/org, Only select repositories, выбор repos, возможный org approval.

После callback Projects Hub показывает только repositories, доступные installation.

Для добавления ещё repo — «Изменить доступ в GitHub».

В разговоре agent сначала использует repository catalogue tool. Если нужный repo не разрешён, показывает integration card «Нужен доступ к repository» + кнопку «Разрешить в GitHub». После возврата conversation продолжается.

## Webhooks и revocation

Projects Hub реагирует на installation и installation_repositories events и нужные repository changes.

Если repo удалён из installation:
- connection становится unavailable/revoked;
- search/index перестаёт выдавать его;
- pending write повторно проверяет доступ и fail-closed;
- Live-agent получает typed error;
- derived cache следует source ACL/revocation.

Не полагаться только на polling.

## Permissions

Запрашивается минимум под реальные typed operations.

Начальный принцип:
- Metadata read;
- Contents read для docs/source;
- Contents write только для app-managed write;
- Pull requests write только для explicit PR flow;
- никаких широких administration/org permissions «на будущее».

Новая feature с новым permission требует отдельного permission upgrade.

## Branch/write semantics

App-managed knowledge repo: current SHA → deterministic path → expected revision/CAS → commit → exact/current readback → no force.

External repo: default read-only; write через owner service contract или PR/branch, protected branch rules не обходить, unknown outcome reconcile before retry.

Live-agent решает **что** делать; adapter гарантирует **куда, с какими правами и на какой revision**.

## Создание repo

Не требуется для первой вертикали. MVP подключает существующие repositories. Позже можно сделать «Создать project repository» как отдельный owner-approved workflow; автоматически создавать repo на каждый проект не нужно.

## Acceptance

Проверить:
- участник без GitHub account работает с app-managed repo;
- один workspace использует installations разных accounts/orgs;
- app видит только selected repos;
- removed repo сразу недоступен;
- repository id/name нельзя подменить function call;
- voice prompt не повышает write capability;
- external read-only не принимает write;
- GitHub credential отсутствует в browser/model/logs;
- uninstall/suspend прекращает доступ;
- direct external changes дают conflict, не force overwrite.

## Официальные источники

GitHub Docs, проверено 27 сентября 2026:
- Installing a GitHub App from a third party
- Best practices for GitHub Apps
- Authenticating as a GitHub App installation
- Generating a user access token for a GitHub App
- Webhook events and payloads
