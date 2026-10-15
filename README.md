# AI Software Factory — MVP

Supervisor de agentes de IA para desarrollo de software. Toma tareas de una **cola persistente**, invoca a un **proveedor de IA** (hoy Claude Code; mañana GPT, Gemini, etc.), **verifica el resultado ejecutando pruebas**, registra cada intento y controla **reintentos y consumo**.

Runtime: solo la stdlib de Python 3.11+ (sqlite3, subprocess, logging). Dependencias de desarrollo: `pytest`.

## Arquitectura

```
          ┌──────────────┐  enqueue   ┌───────────────────────┐
 CLI/API ─┤  asf enqueue ├──────────▶ │ TaskQueue (Protocol)  │ ◀── SQLite hoy, Azure mañana
          └──────────────┘            │  claim / lease / retry│
                                      └──────────┬────────────┘
                                                 │ claim (lease)
                                      ┌──────────▼────────────┐
                                      │      Supervisor       │
                                      │ budget → provider →   │
                                      │ tests → record →      │
                                      │ complete/retry/fail   │
                                      └──┬─────────┬─────────┬┘
                         ProviderRegistry│         │         │BudgetGuard
                     ┌───────────────────▼┐  ┌─────▼──────┐  └─ diario / por tarea / tokens
                     │ AIProvider (ABC)   │  │TestCommand │
                     │ ├ ClaudeCodeProvider│  │  Runner    │
                     │ ├ MockProvider      │  └────────────┘
                     │ └ (GPT, Gemini...)  │
                     └────────────────────┘
```

| Módulo | Responsabilidad |
|---|---|
| `ai_factory/models.py` | `TaskSpec`, `Task`, `TaskStatus`, `Usage`, `AttemptRecord` |
| `ai_factory/queue/base.py` | Contrato `TaskQueue` (semántica de lease, at-least-once) |
| `ai_factory/queue/sqlite_queue.py` | Implementación persistente con SQLite (WAL, `BEGIN IMMEDIATE`) |
| `ai_factory/providers/base.py` | Contrato `AIProvider`, `ProviderRequest/Result`, `ProviderError(retryable, usage)` |
| `ai_factory/providers/claude_code.py` | Claude Code headless (`claude -p --output-format json`) con política de seguridad |
| `ai_factory/providers/mock.py` | Proveedor guionado para tests y dry-runs |
| `ai_factory/providers/registry.py` | Nombre de proveedor → instancia (lazy) |
| `ai_factory/supervisor.py` | Ciclo de vida de cada intento, reintentos con backoff, feedback al agente |
| `ai_factory/budget.py` | Tope diario global, tope por tarea (USD) y tope de tokens |
| `ai_factory/test_runner.py` | Ejecuta el comando de verificación sin shell, con timeout |
| `ai_factory/logging_config.py` | Logs JSON estructurados (task_id, attempt, worker_id, costo...) |
| `ai_factory/config.py` | Settings desde variables de entorno |
| `ai_factory/cli.py` | `asf enqueue / run / status / list / usage` |

### Ciclo de una tarea

1. `claim` atómico → la tarea pasa a `running` con un **lease** y `attempts += 1`.
2. Si la tarea agotó su presupuesto → `failed`.
3. Se resuelven el proveedor (desconocido → `failed`) y el workspace (fuera de `ASF_WORKSPACE_ROOT` o inexistente → `failed`).
4. `provider.run()` con timeout. A partir del 2.º intento, el prompt incluye el error y la salida de tests del intento anterior (**loop de feedback**).
5. Si hay `test_command`, se ejecuta en el workspace. La tarea solo se acepta si las pruebas pasan.
6. Se persiste un `AttemptRecord` con salida, salida de tests, tokens y costo (también en caso de error).
7. Resultado:
   - éxito → `succeeded`;
   - error reintentable con intentos disponibles → `pending`, con backoff exponencial (`base * 2^(n-1)`, con tope);
   - en otro caso → `failed`.

Si un worker muere, su lease expira y otro worker retoma la tarea. Si ya era el último intento, la tarea queda `failed`.

### Control de consumo

- **Diario global** (`ASF_DAILY_BUDGET_USD`): si se alcanza, el supervisor deja de reclamar tareas (quedan `pending`).
- **Por tarea** (`--budget-usd` o `ASF_TASK_BUDGET_USD` por defecto): al superarlo no se reintenta más.
- **Tokens por tarea** (`ASF_MAX_TOKENS_PER_TASK`).
- El consumo se toma de lo que reporta el proveedor (`total_cost_usd` y `usage` de Claude Code) y se registra incluso cuando el intento falla.

## Seguridad (por diseño)

- `ClaudeCodeProvider` **rechaza** `--dangerously-skip-permissions`, `bypassPermissions` y `Bash` sin restricciones. Las herramientas se definen por allow-list (default: `Read, Edit, Write, Glob, Grep`). Para habilitar shell hay que acotarlo, por ejemplo `Bash(pytest:*)`.
- El prompt viaja por **stdin**, nunca por argv. Los comandos se ejecutan sin `shell=True`.
- Los workspaces deben estar dentro de `ASF_WORKSPACE_ROOT`; se bloquea el path traversal.
- El proveedor se niega a ejecutarse dentro de una sesión de Claude Code (`CLAUDECODE` presente). Así se evita la ejecución anidada.
- Los tests nunca ejecutan el binario real: un fixture `autouse` hace fallar cualquier intento de hacerlo.

## Uso

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# Dry-run sin IA (proveedor mock)
mkdir workspaces\demo
.\.venv\Scripts\asf enqueue --prompt "hola" --workspace demo --provider mock --test-cmd "python -c print(1)"
.\.venv\Scripts\asf run --once
.\.venv\Scripts\asf list
.\.venv\Scripts\asf status <task_id>
.\.venv\Scripts\asf usage

# Con Claude Code real (desde una terminal normal, NO dentro de Claude Code)
.\.venv\Scripts\asf enqueue --prompt-file spec.md --workspace mi-repo --test-cmd "python -m pytest -q" --budget-usd 1.5
.\.venv\Scripts\asf run            # loop continuo; Ctrl+C para detener
```

`--test-cmd` acepta un string (se divide sin invocar un shell) o un array JSON con el argv exacto. En PowerShell 5.1, conviene usar la forma string, porque PowerShell elimina las comillas internas del JSON al llamar ejecutables nativos.

### Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `ASF_DB_PATH` | `data/factory.db` | Base SQLite de la cola |
| `ASF_WORKSPACE_ROOT` | `workspaces` | Raíz permitida para workspaces |
| `ASF_CLAUDE_BIN` | `claude` | Binario de Claude Code |
| `ASF_CLAUDE_ALLOWED_TOOLS` | `Read,Edit,Write,Glob,Grep` | Allow-list de herramientas |
| `ASF_CLAUDE_MAX_TURNS` | `20` | `--max-turns` |
| `ASF_CLAUDE_MODEL` | — | `--model` opcional |
| `ASF_PROVIDER_TIMEOUT_S` | `900` | Timeout por invocación del proveedor |
| `ASF_TEST_TIMEOUT_S` | `600` | Timeout de las pruebas |
| `ASF_MAX_ATTEMPTS` | `3` | Intentos por defecto |
| `ASF_BACKOFF_BASE_S` / `ASF_BACKOFF_MAX_S` | `30` / `900` | Backoff exponencial |
| `ASF_POLL_INTERVAL_S` | `5` | Espera cuando la cola está vacía |
| `ASF_DAILY_BUDGET_USD` | `20` | Tope diario (`none` = sin tope) |
| `ASF_TASK_BUDGET_USD` | `2` | Tope por tarea por defecto (`none` = sin tope) |
| `ASF_MAX_TOKENS_PER_TASK` | — | Tope de tokens por tarea |
| `ASF_LOG_LEVEL` / `ASF_LOG_FORMAT` | `INFO` / `json` | `json` o `text` |

El lease se calcula como `provider_timeout + test_timeout + 120s`, para que una tarea en curso nunca sea retomada por otro worker.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Cubren la cola (prioridad, exclusividad, concurrencia con hilos, persistencia, backoff, recuperación de leases), el supervisor (éxito, feedback y reintento, backoff con tope, errores reintentables y no reintentables, presupuestos, workspace inseguro, ruteo multi-proveedor, e2e con un comando de test real), el proveedor Claude Code (con un runner falso), el runner de pruebas, la configuración, los logs y la CLI.

## Agregar un proveedor (GPT, Gemini, ...)

1. Crear `ai_factory/providers/openai_provider.py` con una subclase de `AIProvider` y `name = "openai"`.
2. En `run()`, traducir la respuesta a `ProviderResult(output, usage)`. Ante fallas, lanzar `ProviderError(msg, retryable=..., usage=...)`. Siempre hay que reportar `Usage`, para que el control de consumo funcione.
3. Registrar el proveedor en `build_default_registry()` (`ai_factory/cli.py`).
4. Agregar tests con un cliente HTTP falso, igual que en `tests/test_claude_code_provider.py`.

El supervisor no requiere cambios.

## Migración a Azure

| MVP | Azure |
|---|---|
| `SqliteTaskQueue` | **Azure Service Bus** (peek-lock = lease, `DeliveryCount` = attempts, dead-letter = `failed`, `ScheduledEnqueueTime` = backoff) + **Azure SQL / Cosmos DB** para el estado y la tabla `attempts`. Se implementa el mismo `TaskQueue` Protocol. |
| `asf run` (proceso) | **Azure Container Apps Jobs** (event-driven) o AKS, con **KEDA** escalando por longitud de la cola |
| Variables `ASF_*` | App Settings + **Key Vault** (API keys de Anthropic, OpenAI y Google) con Managed Identity |
| Logs JSON a stderr | **Azure Monitor / Log Analytics / Application Insights** (ingesta directa o vía OpenTelemetry) |
| `BudgetGuard` sobre SQLite | La misma lógica sobre Azure SQL, más alertas de Azure Monitor sobre `cost_usd` |
| Workspaces locales | Volumen efímero por job (clonado del repo), con PRs vía Azure DevOps/GitHub; nunca push directo a ramas protegidas |

## Limitaciones conocidas del MVP

- No hay heartbeat de lease: el lease cubre el peor caso de un intento (`provider_timeout + test_timeout`).
- El backoff no usa jitter (es determinístico para facilitar los tests). En Azure conviene agregarlo.
- El tope diario se evalúa antes de reclamar cada tarea, no durante una invocación en curso. Una tarea puede excederlo como máximo en su propio tope por tarea.
- Sin API HTTP; la interfaz es solo la CLI.
