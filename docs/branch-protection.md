# Cómo configurar la protección de la rama master en GitHub

Estos pasos se hacen una sola vez en la interfaz de GitHub. No hay forma de automatizarlos desde el código.

## Pasos

1. Ir a **Settings → Branches → Add rule**
2. En "Branch name pattern" escribir: `master`
3. Activar las siguientes opciones:

### Aprobaciones requeridas
- ✅ **Require a pull request before merging**
  - Mínimo 1 aprobación
  - ✅ Dismiss stale pull request approvals when new commits are pushed

### Revisiones de estado obligatorias
- ✅ **Require status checks to pass before merging**
- ✅ **Require branches to be up to date before merging**

Checks que deben pasar (agregarlos uno a uno en el buscador):
```
01 - Lint / ruff (lint + format check)
02 - Security / bandit (SAST)
02 - Security / pip-audit (xml-processor)
02 - Security / pip-audit (rag-service)
02 - Security / pip-audit (llm-service)
02 - Security / pip-audit (session-proxy)
02 - Security / pip-audit (siigo-service)
02 - Security / pip-audit (integration-config-service)
02 - Security / pip-audit (auth-service)
02 - Security / semgrep (OWASP API rules)
04 - Tests / pytest (xml-processor / py3.9)
04 - Tests / pytest (rag-service / py3.9)
04 - Tests / pytest (llm-service / py3.9)
04 - Tests / pytest (session-proxy / py3.9)
03 - PR Checks / coverage-gate
```

### Otras opciones recomendadas
- ✅ **Require conversation resolution before merging**
- ✅ **Do not allow bypassing the above settings** (incluye admins)
- ✅ **Require linear history**

## Notas

- Los checks de `03 - PR Checks` solo corren en PRs, no en push directo.
- El `coverage-gate` necesita que los artifacts de cobertura de `04-tests.yml` existan.
  Si se abre un PR antes de que corra `04-tests.yml`, el gate queda pendiente hasta que terminen.
- `auth-service` no tiene tests todavía — no incluir en los checks requeridos hasta tenerlos.
- Para desarrollo local: `python scripts/run_tests.py local` corre todos los servicios con venv aislado.
