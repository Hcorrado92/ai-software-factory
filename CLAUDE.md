# CLAUDE.md

Guía para agentes (Claude Code u otros) que trabajen en este repositorio.

## Qué es

MVP de una AI Software Factory: cola persistente (SQLite) + supervisor que invoca proveedores de IA, ejecuta pruebas, reintenta y controla el consumo. Ver `README.md` para la arquitectura completa.

## Comandos

```powershell
.\.venv\Scripts\python.exe -m pytest -q                  # suite completa (~10s)
.\.venv\Scripts\python.exe -m pytest tests/test_supervisor.py -q
.\.venv\Scripts\python.exe -m ai_factory --help
```

Si no existe `.venv`: `python -m venv .venv` y luego `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`.

## Reglas obligatorias

- **Nunca ejecutar el binario real de Claude Code** desde tests ni desde esta sesión (no hay ejecución anidada). Usar `MockProvider` o inyectar un `runner` falso en `ClaudeCodeProvider`. El fixture `forbid_real_claude` de `tests/conftest.py` lo hace cumplir; no hay que desactivarlo.
- **Nunca** agregar `--dangerously-skip-permissions`, `bypassPermissions` ni `Bash` sin restricciones. `ClaudeCodeProvider.__init__` los rechaza y hay tests que lo verifican. No hay que debilitar esas validaciones.
- No usar `shell=True`. Los comandos se pasan como listas argv.
- No tocar entornos productivos ni credenciales reales. La configuración llega solo por variables `ASF_*`.
- El runtime no tiene dependencias externas. Hay que justificar cualquier dependencia nueva y agregarla en `pyproject.toml`.

## Arquitectura e invariantes

- `supervisor.py` depende solo de los contratos `TaskQueue` (`queue/base.py`) y `AIProvider` (`providers/base.py`). No debe importar proveedores ni backends concretos.
- Los proveedores **siempre** reportan `Usage`, también en errores (`ProviderError(usage=...)`). De eso depende el control de presupuesto.
- `ProviderError.retryable=False` se reserva para fallas permanentes (binario ausente, violación de política). Timeouts y errores de API son reintentables.
- La cola es at-least-once: `claim` incrementa `attempts`, y `complete/retry/fail` exigen ser dueño del lease (si no, lanzan `LeaseLostError`).
- Los timestamps son epoch en segundos (float, UTC). Para tests deterministas, el reloj se inyecta (`clock=`).
- El workspace de una tarea debe estar dentro de `workspace_root`. No hay que relajar `_resolve_workspace`.

## Convenciones

- Python 3.11+, type hints, `from __future__ import annotations`, dataclasses inmutables para modelos.
- Logs: `logger = logging.getLogger(__name__)`, con contexto en `extra={...}` (`task_id`, `attempt`, `worker_id`, ...). No interpolar datos en el mensaje.
- Código, identificadores y logs en inglés; documentación en español.
- Tests con pytest, usando los fixtures de `tests/conftest.py` (`queue`, `clock`, `make_supervisor`, `FakeTestRunner`). Todo cambio de comportamiento lleva su test.

## Agregar un proveedor

Crear una subclase de `AIProvider` en `ai_factory/providers/`, registrarla en `build_default_registry()` (`cli.py`) y testearla con un cliente falso. El supervisor no se modifica.
