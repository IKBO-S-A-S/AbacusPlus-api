#!/usr/bin/env python3
"""
run_tests.py — ejecuta tests de todos los servicios.

Modos:
  local   Crea un venv por servicio, instala deps y corre pytest.
  docker  Corre cada servicio en un contenedor python:3.12-slim aislado.

Uso:
  python scripts/run_tests.py local
  python scripts/run_tests.py docker
  python scripts/run_tests.py local  --services xml-processor llm-service
  python scripts/run_tests.py docker --fail-fast
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SERVICES = [
    "xml-processor",
    "rag-service",
    "llm-service",
    "auth-service",
    "integration-config-service",
    "session-proxy",
    "siigo-service",
]

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
GRAY = "\033[90m"
RESET = "\033[0m"

# Windows cmd/PowerShell no soportan ANSI por defecto en Python < 3.12
if sys.platform == "win32":
    os.system("")  # noqa: S605,S607 — habilita ANSI en Windows Terminal / PowerShell


def header(text: str) -> None:
    print(f"\n{GRAY}{'━' * 44}{RESET}")
    print(f"  {CYAN}{text}{RESET}")
    print(f"{GRAY}{'━' * 44}{RESET}")


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> int:
    result = subprocess.run(cmd, cwd=cwd, env=env)  # noqa: S603
    return result.returncode


def venv_bin(venv: Path, binary: str) -> Path:
    """Retorna la ruta al binario dentro del venv (cross-platform)."""
    if sys.platform == "win32":
        return venv / "Scripts" / binary
    return venv / "bin" / binary


def run_local(svc: str, fail_fast: bool, python_exe: str = sys.executable) -> str:
    """Retorna 'pass', 'skip' o 'fail'."""
    svc_path = ROOT / "services" / svc
    tests_path = svc_path / "tests"
    req_path = svc_path / "requirements.txt"
    venv_path = svc_path / ".venv"

    if not tests_path.exists():
        print(f"  {YELLOW}[SKIP]{RESET} Sin directorio tests/")
        return "skip"
    if not req_path.exists():
        print(f"  {YELLOW}[SKIP]{RESET} Sin requirements.txt")
        return "skip"

    # Crear venv si no existe
    if not venv_path.exists():
        print(f"  {GRAY}Creando venv con {python_exe}...{RESET}")
        if run([python_exe, "-m", "venv", str(venv_path)]) != 0:
            print(f"  {RED}[FAIL]{RESET} No se pudo crear el venv (¿Python instalado?)")
            return "fail"

    pip = str(venv_bin(venv_path, "pip"))
    pytest = str(venv_bin(venv_path, "pytest"))

    print(f"  {GRAY}Instalando dependencias...{RESET}")
    pip_code = run(
        [
            pip,
            "install",
            "-r",
            str(req_path),
            "-q",
            "--prefer-binary",  # evita compilar desde fuente (sin MSVC/gcc)
            "--disable-pip-version-check",
        ]
    )
    if pip_code != 0:
        print(f"  {RED}[FAIL]{RESET} pip install falló (ver salida arriba)")
        return "fail"

    if not Path(pytest).exists():
        print(f"  {RED}[FAIL]{RESET} pytest no encontrado en venv tras instalación")
        return "fail"

    env = os.environ.copy()
    env["PYTHONPATH"] = str(svc_path)

    cmd = [pytest, "tests/", "-v"]
    if fail_fast:
        cmd.append("-x")

    code = run(cmd, cwd=svc_path, env=env)
    if code == 5:
        print(f"  {YELLOW}[SKIP]{RESET} Sin tests implementados (0 collected)")
        return "skip"
    return "pass" if code == 0 else "fail"


def run_docker(svc: str, fail_fast: bool) -> str:
    """Retorna 'pass', 'skip' o 'fail'."""
    svc_path = ROOT / "services" / svc
    tests_path = svc_path / "tests"
    req_path = svc_path / "requirements.txt"

    if not tests_path.exists():
        print(f"  {YELLOW}[SKIP]{RESET} Sin directorio tests/")
        return "skip"
    if not req_path.exists():
        print(f"  {YELLOW}[SKIP]{RESET} Sin requirements.txt")
        return "skip"

    # Docker en Windows requiere forward slashes
    docker_path = str(svc_path).replace("\\", "/")
    pytest_cmd = "python -m pytest tests/ -v" + (" -x" if fail_fast else "")

    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{docker_path}:/app",
        "--workdir",
        "/app",
        "python:3.12-slim",
        "bash",
        "-c",
        f"pip install -r requirements.txt -q && {pytest_cmd}",
    ]
    code = run(cmd)
    if code == 5:
        print(f"  {YELLOW}[SKIP]{RESET} Sin tests implementados (0 collected)")
        return "skip"
    return "pass" if code == 0 else "fail"


def summarize(passed: list, skipped: list, failed: list) -> None:
    print(f"\n{GRAY}{'━' * 44}{RESET}")
    print("  RESUMEN")
    print(f"{GRAY}{'━' * 44}{RESET}")
    if passed:
        print(f"  {GREEN}PASS ({len(passed)}): {', '.join(passed)}{RESET}")
    if skipped:
        print(f"  {YELLOW}SKIP ({len(skipped)}): {', '.join(skipped)}{RESET}")
    if failed:
        print(f"  {RED}FAIL ({len(failed)}): {', '.join(failed)}{RESET}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Corre tests de todos los servicios.")
    parser.add_argument("mode", choices=["local", "docker"], help="Modo de ejecución.")
    parser.add_argument(
        "--services",
        nargs="+",
        metavar="SVC",
        help="Servicios a correr (default: todos).",
    )
    parser.add_argument("--fail-fast", action="store_true", help="Detener en el primer fallo.")
    parser.add_argument(
        "--python",
        default=sys.executable,
        metavar="PATH",
        help="Intérprete Python a usar para los venvs (default: el que corre este script). "
        "Ej: --python python3.11",
    )
    args = parser.parse_args()

    services = args.services or SERVICES
    runner = (
        (lambda svc, ff: run_local(svc, ff, args.python)) if args.mode == "local" else run_docker
    )

    passed, failed, skipped = [], [], []

    for svc in services:
        header(svc)
        result = runner(svc, args.fail_fast)
        if result == "pass":
            print(f"  {GREEN}[PASS]{RESET} {svc}")
            passed.append(svc)
        elif result == "skip":
            skipped.append(svc)
        else:
            print(f"  {RED}[FAIL]{RESET} {svc}")
            failed.append(svc)
            if args.fail_fast:
                print(f"\n{RED}--fail-fast activado — deteniendo.{RESET}")
                break

    summarize(passed, skipped, failed)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
