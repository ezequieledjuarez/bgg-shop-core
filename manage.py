#!/usr/bin/env python
import os
import sys

def main():
    """Configura las variables de entorno y ejecuta la CLI de Django/Saleor."""
    # Apunta al archivo de settings de Saleor
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "No se pudo importar Django. Revisa que esté instalado en el entorno."
        ) from exc
    execute_from_command_line(sys.argv)

if __name__ == "__main__":
    main()